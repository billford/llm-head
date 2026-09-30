from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn

from llm_head.app import Head, build_app
from llm_head.config import Config
from llm_head.obslog import EventLog

from .fake_ollama import FakeOllama


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def serve(app, port: int):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


class MemLog(EventLog):
    """EventLog that keeps records in memory for assertions."""

    def __init__(self):
        super().__init__(None, "debug")
        self.records: list[dict] = []

    def event(self, level, msg, **fields):
        self.records.append({"level": level, "msg": msg, **fields})

    def find(self, msg: str) -> list[dict]:
        return [r for r in self.records if r["msg"] == msg]


@asynccontextmanager
async def cluster(n_hosts: int = 2, *, head_overrides: dict | None = None, fakes: list[FakeOllama] | None = None):
    """Start n fake Ollama hosts and an llm-head in front of them."""
    fakes = fakes or [FakeOllama() for _ in range(n_hosts)]
    names = ["xmas", "european", "host3", "host4"][: len(fakes)]
    async with _serve_all([(f.app, free_port()) for f in fakes]) as urls:
        raw = {
            "hosts": [
                {"name": n, "url": u, "vram_mb": 16311, "slots_per_model": 2, "max_loaded_models": 2}
                for n, u in zip(names, urls, strict=True)
            ],
            "health": {"interval": "200ms", "timeout": "100ms", "failure_threshold": 2},
            "discovery": {"ps_interval": "50ms", "tags_interval": "5s"},
            "queue": {"max_wait": "5s", "classes": {"interactive": {"match": ["10.9.9.9/32"], "boost": "30s"},
                                                     "batch": {"default": True}}},
            "logging": {"file": None},
            "stats_file": None,
        }
        for k, v in (head_overrides or {}).items():
            raw[k] = v if not isinstance(v, dict) else {**raw.get(k, {}), **v}
        cfg = Config.model_validate(raw)
        log = MemLog()
        head = Head(cfg, log=log)
        app = build_app(cfg, head=head)
        async with serve(app, free_port()) as base:
            # Wait until every host is healthy and has reported its models.
            for _ in range(200):
                if all(h.status == "healthy" and h.installed for h in head.cluster.hosts.values()):
                    break
                await asyncio.sleep(0.02)
            async with httpx.AsyncClient(base_url=base, timeout=30) as client:
                yield client, head, fakes, log


@asynccontextmanager
async def _serve_all(apps):
    urls = []
    stack = []
    try:
        for app, port in apps:
            cm = serve(app, port)
            urls.append(await cm.__aenter__())
            stack.append(cm)
        yield urls
    finally:
        for cm in reversed(stack):
            await cm.__aexit__(None, None, None)


@pytest.fixture
def anyio_backend():
    return "asyncio"
