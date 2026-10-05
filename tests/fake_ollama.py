"""A fake Ollama server that models the behavior llm-head schedules around.

It simulates GPU memory, cold-load time, LRU eviction of idle models, OLLAMA_NUM_PARALLEL
slots with a hidden queue behind them, and CPU spill: a model that doesn't fully fit
generates about 8x slower. Counters let tests assert what happened.

Run standalone for soak tests:
    python -m tests.fake_ollama --port 11500 --vram-gb 15.4
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

GiB = 1024**3

DEFAULT_MODELS = {
    "qwen2.5vl:7b-q4_K_M": 5_864_591_195,
    "llama3.2:3b": 3_097_881_476,
    "gpt-oss:20b": int(11.89 * GiB),
    "nomic-embed-text:latest": 300_000_000,
}


@dataclass
class Loaded:
    size: int
    inflight: int = 0
    last_used: float = 0.0
    spilled: bool = False
    ctx: int = 4096
    on_gpu: int = 0  # bytes on the GPU; less than size when spilled


@dataclass
class FakeOllama:
    vram_bytes: int = int(15.4 * GiB)
    models: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    num_parallel: int = 2
    max_loaded: int = 2
    load_seconds: float = 0.05
    token_seconds: float = 0.002
    spill_factor: float = 8.0
    # False: like Ollama 0.32 on european on 2026-10-04, keep idle models loaded and put
    # part of the new one on the CPU instead of evicting to make room.
    evict_to_fit: bool = True
    healthy: bool = True
    health_delay: float = 0.0
    fail_next: int = 0  # respond 503 to this many inference requests
    # Like Ollama on xmas on 2026-09-30: loads never happen and requests needing one hang
    # silently, while already-loaded models keep working and GET / stays healthy.
    wedged: bool = False
    default_ctx: int = 4096
    # Context Ollama gives a model at most, its trained maximum, whatever is asked for.
    max_ctx: dict[str, int] = field(default_factory=lambda: {"nomic-embed-text:latest": 2048})
    reloads: int = 0
    # Observability for tests.
    loaded: dict[str, Loaded] = field(default_factory=dict)
    loads: int = 0
    evictions: int = 0
    spills: int = 0
    requests: int = 0
    max_concurrent: int = 0
    queued_max: int = 0
    _queued: int = 0
    _concurrent: int = 0

    def __post_init__(self):
        self._lock = asyncio.Lock()
        self._slot_free = asyncio.Condition()
        self.app = Starlette(
            routes=[
                Route("/", self.root),
                Route("/api/version", self.version),
                Route("/api/tags", self.tags),
                Route("/api/ps", self.ps),
                Route("/api/show", self.show, methods=["POST"]),
                Route("/api/generate", self.generate, methods=["POST"]),
                Route("/api/chat", self.generate, methods=["POST"]),
                Route("/api/embed", self.embed, methods=["POST"]),
                Route("/v1/chat/completions", self.openai_chat, methods=["POST"]),
            ]
        )

    # ---- helpers -------------------------------------------------------------------

    def _resolve(self, name: str) -> str | None:
        n = name.lower()
        for m in self.models:
            if m.lower() == n or m.lower() == n + ":latest":
                return m
        return None

    async def _ensure_loaded(self, model: str, ctx: int | None = None) -> tuple[Loaded, float]:
        """Load `model`, evicting idle models LRU-first like Ollama. Returns load seconds.

        A model loaded with a different context size is reloaded, but only once it has no
        requests running, as Ollama does."""
        ctx = min(ctx or self.default_ctx, self.max_ctx.get(model, 1 << 30))
        lm = self.loaded.get(model)
        if lm and lm.ctx != ctx:
            if self.wedged:
                await asyncio.Event().wait()
            async with self._slot_free:
                await self._slot_free.wait_for(lambda: lm.inflight == 0)
            async with self._lock:
                if self.loaded.get(model) is lm and lm.ctx != ctx:
                    del self.loaded[model]
                    self.reloads += 1
        if model not in self.loaded and self.wedged:
            await asyncio.Event().wait()
        async with self._lock:
            lm = self.loaded.get(model)
            if lm and lm.ctx == ctx:
                return lm, 0.0
            size = self.models[model]
            used = lambda: sum(x.size for x in self.loaded.values())  # noqa: E731
            idle = sorted((m for m, x in self.loaded.items() if x.inflight == 0), key=lambda m: self.loaded[m].last_used)
            while idle and ((self.evict_to_fit and used() + size > self.vram_bytes)
                            or len(self.loaded) >= self.max_loaded):
                del self.loaded[idle.pop(0)]
                self.evictions += 1
            on_gpu = sum(x.on_gpu for x in self.loaded.values())
            spilled = on_gpu + size > self.vram_bytes
            if spilled:
                self.spills += 1
            lm = Loaded(size=size, spilled=spilled, ctx=ctx, on_gpu=min(size, max(0, self.vram_bytes - on_gpu)))
            self.loaded[model] = lm
            self.loads += 1
        await asyncio.sleep(self.load_seconds)
        return lm, self.load_seconds

    async def _run(self, model: str, n_tokens: int, ctx: int | None = None):
        lm, load_s = await self._ensure_loaded(model, ctx)
        # Ollama queues requests beyond NUM_PARALLEL internally.
        async with self._slot_free:
            self._queued += 1
            self.queued_max = max(self.queued_max, self._queued)
            await self._slot_free.wait_for(lambda: lm.inflight < self.num_parallel)
            self._queued -= 1
            lm.inflight += 1
        self._concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self._concurrent)
        return lm, load_s

    async def _done(self, model: str, lm: Loaded):
        lm.inflight -= 1
        lm.last_used = time.monotonic()
        self._concurrent -= 1
        async with self._slot_free:
            self._slot_free.notify_all()

    def _tok(self, lm: Loaded) -> float:
        return self.token_seconds * (self.spill_factor if lm.spilled else 1.0)

    # ---- routes --------------------------------------------------------------------

    async def root(self, request: Request):
        if self.health_delay:
            await asyncio.sleep(self.health_delay)
        if not self.healthy:
            return PlainTextResponse("down", status_code=503)
        return PlainTextResponse("Ollama is running")

    async def version(self, request: Request):
        return JSONResponse({"version": "0.32.4-fake"})

    async def tags(self, request: Request):
        return JSONResponse(
            {
                "models": [
                    {
                        "name": m,
                        "model": m,
                        "size": s,
                        "digest": f"{abs(hash(m)):064x}"[:64],
                        "modified_at": "2026-09-01T00:00:00Z",
                        "details": {"family": m.split(":")[0].rstrip("0123456789.-"), "parameter_size": "7B", "quantization_level": "Q4_K_M"},
                    }
                    for m, s in self.models.items()
                ]
            }
        )

    async def ps(self, request: Request):
        return JSONResponse(
            {
                "models": [
                    {"name": m, "model": m, "size": x.size, "size_vram": x.on_gpu,
                     "context_length": x.ctx}
                    for m, x in self.loaded.items()
                ]
            }
        )

    async def show(self, request: Request):
        body = await request.json()
        m = self._resolve(body.get("model") or body.get("name") or "")
        if not m:
            return JSONResponse({"error": "model not found"}, status_code=404)
        return JSONResponse({"details": {"family": "fake"}, "model_info": {}})

    async def generate(self, request: Request):
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        m = self._resolve(body.get("model", ""))
        if not m:
            return JSONResponse({"error": f"model '{body.get('model')}' not found"}, status_code=404)
        if self.fail_next:
            self.fail_next -= 1
            return JSONResponse({"error": "service unavailable"}, status_code=503)
        if body.get("keep_alive") == 0 and not body.get("prompt") and not body.get("messages"):
            if m in self.loaded and self.loaded[m].inflight == 0:
                del self.loaded[m]
                self.evictions += 1
            return JSONResponse({"model": m, "done": True, "done_reason": "unload"})
        self.requests += 1
        n = int((body.get("options") or {}).get("num_predict") or 8)
        ctx = (body.get("options") or {}).get("num_ctx")
        chat = request.url.path.endswith("/chat")
        lm, load_s = await self._run(m, n, ctx)
        started = time.monotonic()

        def final(extra: dict) -> dict:
            eval_s = max(1e-6, time.monotonic() - started)
            return {
                "model": m, "done": True, "done_reason": "length", "eval_count": n, "prompt_eval_count": 10,
                "eval_duration": int(eval_s * 1e9), "load_duration": int(load_s * 1e9),
                "prompt_eval_duration": 1_000_000, "total_duration": int((eval_s + load_s) * 1e9), **extra,
            }

        piece = (lambda i: {"message": {"role": "assistant", "content": f"t{i} "}}) if chat else (lambda i: {"response": f"t{i} "})
        if body.get("stream", True) is False:
            try:
                await asyncio.sleep(self._tok(lm) * n)
                text = "".join(f"t{i} " for i in range(n))
                content = {"message": {"role": "assistant", "content": text}} if chat else {"response": text}
                return JSONResponse(final(content))
            finally:
                await self._done(m, lm)

        async def stream():
            try:
                for i in range(n):
                    await asyncio.sleep(self._tok(lm))
                    yield json.dumps({"model": m, "done": False, **piece(i)}).encode() + b"\n"
                yield json.dumps(final({})).encode() + b"\n"
            finally:
                await self._done(m, lm)

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    async def embed(self, request: Request):
        body = await request.json()
        m = self._resolve(body.get("model", ""))
        if not m:
            return JSONResponse({"error": "model not found"}, status_code=404)
        if body.get("keep_alive") == 0 and not body.get("input"):
            if m in self.loaded and self.loaded[m].inflight == 0:
                del self.loaded[m]
                self.evictions += 1
            return JSONResponse({"model": m, "embeddings": []})
        lm, load_s = await self._run(m, 1)
        try:
            return JSONResponse({"model": m, "embeddings": [[0.1] * 8], "load_duration": int(load_s * 1e9), "prompt_eval_count": 3})
        finally:
            await self._done(m, lm)

    async def openai_chat(self, request: Request):
        body = await request.json()
        m = self._resolve(body.get("model", ""))
        if not m:
            return JSONResponse({"error": {"message": "model not found"}}, status_code=404)
        n = int(body.get("max_tokens") or 8)
        lm, _ = await self._run(m, n)
        try:
            await asyncio.sleep(self._tok(lm) * n)
            return JSONResponse(
                {
                    "id": "chatcmpl-1", "object": "chat.completion", "model": m,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": n, "total_tokens": 10 + n},
                }
            )
        finally:
            await self._done(m, lm)


def main() -> None:
    import argparse

    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=11500)
    p.add_argument("--vram-gb", type=float, default=15.4)
    p.add_argument("--token-ms", type=float, default=10.0)
    p.add_argument("--load-s", type=float, default=3.0)
    a = p.parse_args()
    fake = FakeOllama(vram_bytes=int(a.vram_gb * GiB), token_seconds=a.token_ms / 1000, load_seconds=a.load_s)
    uvicorn.run(fake.app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
