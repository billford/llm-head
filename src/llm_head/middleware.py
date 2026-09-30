"""ASGI middleware: CORS, request limits, rate limits, request IDs and the access log.

Written as plain ASGI (not BaseHTTPMiddleware) so streamed responses pass through
without buffering.
"""

from __future__ import annotations

import ipaddress
import time
from datetime import datetime, timezone

from .config import CorsConfig, RateLimits
from .ids import accept_request_id, new_request_id

# Olla answers these itself, so like Olla they are not rate limited.
LOCAL_OLLA_PATHS = {
    "/olla/models", "/olla/ollama/api/tags", "/olla/ollama/v1/models", "/olla/ollama/api/list",
    "/olla/ollama/api/pull", "/olla/ollama/api/push", "/olla/ollama/api/create", "/olla/ollama/api/copy",
    "/olla/ollama/api/delete",
}


def is_rate_limited_path(path: str) -> bool:
    """Only proxied routes are rate limited in Olla; /internal/* and /version never are."""
    return path.startswith("/olla/") and path.rstrip("/") not in LOCAL_OLLA_PATHS and not path.startswith(
        "/olla/models/"
    )


class ClientIP:
    """Resolve the client address, honoring X-Forwarded-For only from trusted proxies."""

    def __init__(self, cfg: RateLimits):
        self.trust = cfg.trust_proxy_headers
        self.nets = [ipaddress.ip_network(c, strict=False) for c in cfg.trusted_proxy_cidrs]

    def __call__(self, scope) -> str:
        peer = (scope.get("client") or ("", 0))[0]
        if not self.trust or not self._trusted(peer):
            return peer
        headers = dict(scope.get("headers") or [])
        xff = headers.get(b"x-forwarded-for", b"").decode()
        if xff:
            return xff.split(",")[0].strip()
        real = headers.get(b"x-real-ip", b"").decode().strip()
        return real or peer

    def _trusted(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in n for n in self.nets)


class TokenBucket:
    """Olla's limiter: a token bucket per key refilling at per_minute/60 tokens a second,
    holding at most `burst` tokens (golang.org/x/time/rate semantics)."""

    def __init__(self, per_minute: int, burst: int):
        self.rate = per_minute / 60.0
        self.burst = max(1, burst)
        self.buckets: dict[str, list[float]] = {}  # key -> [tokens, last_refill, last_seen]

    def take(self, key: str, now: float) -> tuple[bool, float]:
        """Take one token. Returns (allowed, seconds until a token is available)."""
        b = self.buckets.get(key)
        if b is None:
            b = self.buckets[key] = [float(self.burst), now, now]
        b[0] = min(self.burst, b[0] + (now - b[1]) * self.rate)
        b[1] = b[2] = now
        if b[0] >= 1:
            b[0] -= 1
            return True, 0.0
        wait = (1 - b[0]) / self.rate if self.rate > 0 else 60.0
        return False, wait

    def cleanup(self, now: float, idle: float = 600.0) -> None:
        for k in [k for k, b in self.buckets.items() if now - b[2] > idle]:
            del self.buckets[k]


class MinuteCounter:
    """Feeds the X-RateLimit-Remaining header. Informational only, as in Olla."""

    def __init__(self):
        self.windows: dict[str, list[float]] = {}  # key -> [window_start, count]

    def hit(self, key: str, now: float) -> int:
        w = self.windows.get(key)
        if w is None or now - w[0] >= 60:
            w = self.windows[key] = [now, 0]
        w[1] += 1
        return int(w[1])

    def cleanup(self, now: float) -> None:
        for k in [k for k, w in self.windows.items() if now - w[0] >= 60]:
            del self.windows[k]


class CorsMiddleware:
    def __init__(self, app, cfg: CorsConfig):
        self.app = app
        self.cfg = cfg

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.cfg.enabled:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        origin = headers.get(b"origin", b"").decode()
        allowed = self._origin_allowed(origin)
        if scope["method"] == "OPTIONS" and origin and b"access-control-request-method" in headers:
            out = [(b"vary", b"Origin, Access-Control-Request-Method, Access-Control-Request-Headers")]
            if allowed:
                req_method = headers[b"access-control-request-method"].decode()
                req_headers = headers.get(b"access-control-request-headers", b"").decode()
                methods = self.cfg.allowed_methods
                if req_method.upper() in (m.upper() for m in methods):
                    out += [
                        (b"access-control-allow-origin", self._allow_origin_value(origin).encode()),
                        (b"access-control-allow-methods", req_method.upper().encode()),
                        (b"access-control-max-age", str(self.cfg.max_age).encode()),
                    ]
                    if req_headers:
                        out.append((b"access-control-allow-headers", req_headers.encode()))
                    if self.cfg.allow_credentials:
                        out.append((b"access-control-allow-credentials", b"true"))
            await send({"type": "http.response.start", "status": 204, "headers": out})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                h = list(message.get("headers") or [])
                h.append((b"vary", b"Origin"))
                if origin and allowed:
                    h.append((b"access-control-allow-origin", self._allow_origin_value(origin).encode()))
                    h.append((b"access-control-expose-headers",
                              b"X-Olla-Request-Id, X-Olla-Endpoint, X-Olla-Backend-Type, X-Olla-Model, "
                              b"X-Olla-Response-Time, X-Olla-Routing-Strategy, X-Olla-Routing-Decision, "
                              b"X-Olla-Routing-Reason, X-Llm-Head-Queue-Ms, X-Llm-Head-Cold-Load"))
                    if self.cfg.allow_credentials:
                        h.append((b"access-control-allow-credentials", b"true"))
                message = dict(message, headers=h)
            await send(message)

        await self.app(scope, receive, send_with_cors)

    def _origin_allowed(self, origin: str) -> bool:
        return bool(origin) and ("*" in self.cfg.allowed_origins or origin in self.cfg.allowed_origins)

    def _allow_origin_value(self, origin: str) -> str:
        if "*" in self.cfg.allowed_origins and not self.cfg.allow_credentials:
            return "*"
        return origin


class LimitsMiddleware:
    """Request ID, size limits, rate limits and the Olla-format access log."""

    def __init__(self, app, head):
        self.app = app
        self.head = head
        rl = head.cfg.server.rate_limits
        self.rl = rl
        self.limits = head.cfg.server.request_limits
        self.global_rl = TokenBucket(rl.global_requests_per_minute, rl.burst_size)
        self.ip_rl = TokenBucket(rl.per_ip_requests_per_minute, rl.burst_size)
        self.counter = MinuteCounter()
        self._last_cleanup = time.monotonic()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        head = self.head
        started = time.monotonic()
        headers = dict(scope.get("headers") or [])
        rid = accept_request_id(headers.get(b"x-request-id", b"").decode(errors="replace")) or new_request_id()
        ip = head.client_ip(scope)
        scope.setdefault("state", {})
        scope["state"]["request_id"] = rid
        scope["state"]["client_ip"] = ip
        path = scope["path"]
        status_box = {"status": 0, "bytes": 0}
        rl_headers: list[tuple[bytes, bytes]] = []

        async def send_logged(message):
            if message["type"] == "http.response.start":
                status_box["status"] = message["status"]
                message = dict(message, headers=list(message.get("headers") or []) + rl_headers)
            elif message["type"] == "http.response.body":
                status_box["bytes"] += len(message.get("body", b""))
            await send(message)

        clen = headers.get(b"content-length")
        try:
            # Size first for non-proxied routes; for proxied ones Olla rate-limits first,
            # and the order is only observable when both would fail.
            limited = is_rate_limited_path(path)
            if limited and self.rl.per_ip_requests_per_minute > 0:
                now = time.monotonic()
                if now - self._last_cleanup > self.rl.cleanup_interval:
                    self.global_rl.cleanup(now)
                    self.ip_rl.cleanup(now)
                    self.counter.cleanup(now)
                    self._last_cleanup = now
                limit = self.rl.per_ip_requests_per_minute
                used = self.counter.hit(ip, now)
                rl_headers += [
                    (b"x-ratelimit-limit", str(limit).encode()),
                    (b"x-ratelimit-remaining", str(max(0, limit - used)).encode()),
                    (b"x-ratelimit-reset", str(int(time.time()) + 60).encode()),
                ]
                ok_global, _ = self.global_rl.take("*", now)
                if not ok_global:
                    retry_after = 60
                else:
                    ok_ip, wait = self.ip_rl.take(ip, now)
                    retry_after = 0 if ok_ip else int(wait) + 1
                if retry_after:
                    head.app_state.rate_limited += 1
                    rl_headers.append((b"retry-after", str(retry_after).encode()))
                    # Olla's limiter runs before request IDs are assigned, so no ID header.
                    return await _plain(send_logged, 429, "Too Many Requests", None)

            header_size = sum(len(k) + len(v) + 4 for k, v in scope.get("headers") or [])
            header_size += len(scope["method"]) + len(scope.get("raw_path") or path) + len("HTTP/1.1") + 4
            if clen and clen.isdigit() and int(clen) > self.limits.max_body_size:
                head.app_state.size_limited += 1
                return await _plain(send_logged, 413, "Request body too large", rid)
            if header_size > self.limits.max_header_size:
                head.app_state.size_limited += 1
                return await _plain(send_logged, 431, "Request headers too large", rid)

            receive = _limit_body(receive, self.limits.max_body_size)
            await self.app(scope, receive, send_logged)
        except _BodyTooLarge:
            head.app_state.size_limited += 1
            if not status_box["status"]:
                await _plain(send_logged, 413, "Request body too large", rid)
        finally:
            if head.cfg.server.request_logging:
                query = scope.get("query_string", b"").decode()
                head.log.info(
                    "Access log",
                    timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    request_id=rid,
                    remote_addr=_fmt_peer(scope),
                    method=scope["method"],
                    path=path.removeprefix("/olla/ollama") or "/",
                    query=query,
                    status=status_box["status"],
                    request_bytes=int(clen) if clen and clen.isdigit() else 0,
                    response_bytes=status_box["bytes"],
                    duration_ms=int((time.monotonic() - started) * 1000),
                    user_agent=headers.get(b"user-agent", b"").decode(errors="replace"),
                    referer=headers.get(b"referer", b"").decode(errors="replace"),
                    content_type=headers.get(b"content-type", b"").decode(errors="replace"),
                    accept=headers.get(b"accept", b"").decode(errors="replace"),
                )


class _BodyTooLarge(Exception):
    pass


def _limit_body(receive, max_bytes: int):
    seen = 0

    async def limited():
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > max_bytes:
                raise _BodyTooLarge()
        return message

    return limited


async def _plain(send, status: int, text: str, rid: str | None) -> None:
    body = (text + "\n").encode()
    headers = [
        (b"content-type", b"text/plain; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
        (b"x-content-type-options", b"nosniff"),
    ]
    if rid:
        headers.append((b"x-olla-request-id", rid.encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _fmt_peer(scope) -> str:
    client = scope.get("client")
    if not client:
        return ""
    host, port = client
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

