"""Forward a request to a backend and relay the response, byte for byte.

Retries happen only before the first response byte. Once streaming starts, resending
the prompt could produce a duplicate or contradictory answer, so a failure then ends
the request.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

# Upstream response statuses worth retrying on another host (before the first byte).
RETRYABLE_STATUS = {502, 503, 504}
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}
STREAMING_TYPES = ("application/x-ndjson", "text/event-stream")
TAIL_BYTES = 256 * 1024  # enough to hold the final stats object of any response


class UpstreamUnavailable(Exception):
    """The backend could not be reached or failed before sending a usable response."""

    def __init__(self, message: str, *, connection: bool, model_problem: bool = False):
        super().__init__(message)
        # True for network failures, which count toward marking the host offline.
        self.connection = connection
        # True when the host is up but can't serve this model (no headers in time, or
        # the model never loaded): quarantine the model on that host, not the host.
        self.model_problem = model_problem


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_per_sec: float = 0.0
    ttft_ms: int = 0
    load_seconds: float = 0.0


@dataclass
class Relay:
    response: httpx.Response
    started: float
    headers_ms: float
    first_byte_ms: float | None = None
    nbytes: int = 0
    stalled: bool = False
    error: str = ""
    _tail: bytearray = field(default_factory=bytearray)

    @property
    def streaming(self) -> bool:
        ct = self.response.headers.get("content-type", "")
        return any(ct.startswith(t) for t in STREAMING_TYPES)

    async def body(self, stall_timeout: float, total_timeout: float) -> AsyncIterator[bytes]:
        it = self.response.aiter_raw().__aiter__()
        deadline = self.started + total_timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.error = "response_timeout"
                    return
                # Non-streaming responses legitimately send nothing until they're done.
                wait = min(stall_timeout, remaining) if self.streaming else remaining
                try:
                    chunk = await asyncio.wait_for(it.__anext__(), timeout=wait)
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError:
                    if self.streaming and wait == stall_timeout:
                        self.stalled = True
                        self.error = f"stalled: no data for {stall_timeout:.0f}s"
                    else:
                        self.error = "response_timeout"
                    return
                if self.first_byte_ms is None:
                    self.first_byte_ms = (time.monotonic() - self.started) * 1000
                self.nbytes += len(chunk)
                self._tail += chunk
                if len(self._tail) > TAIL_BYTES:
                    del self._tail[: len(self._tail) - TAIL_BYTES]
                yield chunk
        except httpx.HTTPError as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            await self.response.aclose()

    def usage(self) -> Usage:
        return parse_usage(bytes(self._tail), self.first_byte_ms or 0.0)


async def open_upstream(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    *,
    connect_timeout: float,
    header_timeout: float,
    read_timeout: float,
) -> Relay:
    """Send the request and wait for response headers. Raises UpstreamUnavailable on any
    failure that is safe to retry elsewhere."""
    started = time.monotonic()
    req = client.build_request(
        method,
        url,
        headers=headers,
        content=body,
        timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=read_timeout, pool=connect_timeout),
    )
    try:
        resp = await asyncio.wait_for(client.send(req, stream=True), timeout=header_timeout)
    except asyncio.TimeoutError as exc:
        raise UpstreamUnavailable(f"no response headers within {header_timeout:.0f}s",
                                  connection=False, model_problem=True) from exc
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable(f"network error: {type(exc).__name__}: {exc}", connection=True) from exc
    if resp.status_code in RETRYABLE_STATUS:
        text = (await resp.aread())[:300].decode(errors="replace")
        await resp.aclose()
        raise UpstreamUnavailable(f"HTTP {resp.status_code}: {text}", connection=False)
    return Relay(resp, started, (time.monotonic() - started) * 1000)


# Client credentials never go to a backend, and backends don't set cookies on our clients.
STRIP_UPSTREAM = {"authorization", "proxy-authorization", "x-api-key", "x-auth-token", "cookie"}
STRIP_DOWNSTREAM = {"set-cookie"}


def forward_headers(incoming: list[tuple[str, str]], client_ip: str, served_by: str) -> dict[str, str]:
    out = {k: v for k, v in incoming if k.lower() not in HOP_BY_HOP | STRIP_UPSTREAM}
    prior = next((v for k, v in incoming if k.lower() == "x-forwarded-for"), "")
    out["X-Forwarded-For"] = f"{prior}, {client_ip}" if prior else client_ip
    out["X-Real-IP"] = client_ip
    out["Via"] = f"1.1 {served_by}"
    out["X-Proxied-By"] = served_by
    return out


def response_headers(resp: httpx.Response) -> dict[str, str]:
    return {
        k: v for k, v in resp.headers.items()
        if k.lower() not in HOP_BY_HOP | STRIP_DOWNSTREAM and not k.lower().startswith("x-olla-")
    }


def parse_usage(tail: bytes, first_byte_ms: float) -> Usage:
    """Pull token counts and timings from the last JSON object in a response.

    Handles Ollama native responses (one JSON object or NDJSON, stats on the final line)
    and OpenAI-style responses (a `usage` object, possibly in an SSE `data:` line).
    """
    u = Usage(ttft_ms=int(first_byte_ms))
    obj = _last_json(tail)
    if not obj:
        return u
    if "eval_count" in obj or "prompt_eval_count" in obj:
        u.input_tokens = int(obj.get("prompt_eval_count") or 0)
        u.output_tokens = int(obj.get("eval_count") or 0)
        eval_ns = obj.get("eval_duration") or 0
        if eval_ns and u.output_tokens:
            u.tokens_per_sec = round(u.output_tokens / (eval_ns / 1e9), 1)
        load_ns = obj.get("load_duration") or 0
        prompt_ns = obj.get("prompt_eval_duration") or 0
        u.load_seconds = load_ns / 1e9
        if load_ns or prompt_ns:
            # Time the backend spent before the first token: loading plus prompt processing.
            u.ttft_ms = int((load_ns + prompt_ns) / 1e6)
    elif isinstance(obj.get("usage"), dict):
        usage = obj["usage"]
        u.input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        u.output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    elif "prompt_eval_count" in obj:  # /api/embed
        u.input_tokens = int(obj.get("prompt_eval_count") or 0)
    return u


def _last_json(tail: bytes) -> dict | None:
    text = tail.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        whole = json.loads(text)
        return whole if isinstance(whole, dict) else None
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None
