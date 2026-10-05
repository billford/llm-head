"""HTTP layer: Olla-compatible routes on top of the scheduler.

Clients keep using http://<head>:40114/olla/ollama as their Ollama base URL, and
dashboards keep reading /internal/*. See spec §5 for the parity checklist.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import platform
import sys
import time

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from . import fmt
from .cluster import Cluster
from .config import Config
from .middleware import ClientIP, CorsMiddleware, LimitsMiddleware
from .names import normalize
from .obslog import EventLog
from .placement import HostSnapshot
from .relay import UpstreamUnavailable, forward_headers, open_upstream, response_headers
from .scheduler import Lease, Rejected, Scheduler
from .stats import Stats

VERSION = "0.1.0.dev0"
SERVED_BY = f"llm-head/{VERSION}"
COMMIT = os.environ.get("LLM_HEAD_COMMIT", "dev")

# Answered by the head itself with Olla's exact 501 responses: these would act on one
# arbitrary host, which is never what the caller means in a multi-host cluster.
NOT_SUPPORTED = {
    "api/list": "running models list not supported in multi-instance proxy",
    "api/pull": "model management operations not supported by proxy",
    "api/push": "model management operations not supported by proxy",
    "api/create": "model management operations not supported by proxy",
    "api/copy": "model management operations not supported by proxy",
    "api/delete": "model management operations not supported by proxy",
}

# Paths that run a model and therefore go through the scheduler.
INFERENCE_PATHS = {
    "api/generate", "api/chat", "api/embed", "api/embeddings",
    "v1/chat/completions", "v1/completions", "v1/embeddings", "v1/responses",
}

# A keep_warm copy away from the model's home is unloaded once its home copies cover the
# target and it has been idle this long. Long enough that a copy a burst just used stays.
REHOME_IDLE = 300.0


class Head:
    """Everything a request handler needs."""

    def __init__(self, cfg: Config, *, log: EventLog | None = None, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.log = log or EventLog(cfg.logging.file, cfg.logging.level, cfg.logging.max_size_mb, cfg.logging.max_backups)
        self.stats = Stats(cfg.stats_file)
        self.client = client or httpx.AsyncClient(limits=httpx.Limits(max_connections=None, max_keepalive_connections=32))
        self.cluster = Cluster(cfg, self.stats, self.log, client=self.client)
        self.scheduler = Scheduler(
            self.cluster,
            self.log,
            cfg.queue.max_wait,
            {name: c.boost for name, c in cfg.queue.classes.items()},
            head_of_line_after=cfg.queue.head_of_line_after,
        )
        self.client_ip = ClientIP(cfg.server.rate_limits)
        self.started = time.time()
        self.total_requests = 0
        self.total_failures = 0
        self._background: list[asyncio.Task] = []

    # ---- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        self.log.info("llm-head starting", version=VERSION, hosts=[h.name for h in self.cfg.hosts])
        self.cluster.start()
        self._background.append(asyncio.create_task(self._save_stats_loop()))
        if self.cfg.scheduling.keep_warm:
            self._background.append(asyncio.create_task(self._warm_loop()))

    async def stop(self) -> None:
        for t in self._background:
            t.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.cluster.stop()
        self.stats.save()
        await self.client.aclose()
        self.log.info("llm-head stopped")

    async def _save_stats_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self.stats.save()

    def warm_targets(self) -> list[tuple[str, str, int]]:
        """(host, model, num_ctx) loads that would bring keep_warm models up to target,
        using only hosts with nothing in flight. At most one per model per round.

        Warming never evicts: it only uses a host where the model fits fully on the GPU
        beside what's already there. Otherwise Ollama would pick what to unload, or spill
        the model onto the CPU, and keep-warm would undo the placement policy."""
        if self.scheduler.waiting:
            return []
        now = time.monotonic()
        out = []
        for pattern, policy in self.cfg.models.items():
            if policy.keep_warm == 0 or any(ch in pattern for ch in "*?["):
                continue
            model = normalize(pattern)
            hosts = [h for h in self.cluster.hosts.values() if h.routable and model in h.installed]
            if not hosts:
                continue
            ctx = policy.num_ctx or self.stats.usual_ctx(model) or hosts[0].cfg.default_num_ctx
            ctx = self.effective_ctx(model, ctx) or ctx
            warm = [h for h in hosts if h.is_warm(model, ctx)
                    or (model in h.pending_loads and h.pending_loads[model][3] == ctx)]
            if len(warm) >= policy.keep_warm:
                # Enough copies, but maybe not where they belong: after a drain or reboot
                # a copy can stay warm away from home and keep its host's GPU busy.
                # Add one at home; rehome_evictions() then unloads the stray copy.
                if _home_short(policy, hosts, warm) == 0:
                    continue
                hosts = [h for h in hosts if h.name in policy.home]
            need = self.cluster.facts(model).vram_bytes
            idle = [h for h in hosts if h not in warm and sum(h.inflight.values()) == 0
                    and not (model in h.loaded and h.loaded[model].spilled)
                    and _fits_beside_loaded(h.snapshot(now), model, need)]
            if idle:
                # Prefer the model's home hosts, as placement does.
                idle.sort(key=lambda h: h.name not in policy.home)
                out.append((idle[0].name, model, ctx))
        return out

    def rehome_evictions(self) -> list[tuple[str, str]]:
        """(host, model) copies of keep_warm models to unload: copies away from home that
        are surplus once the home hosts hold enough, and idle for REHOME_IDLE.
        At most one per model per round."""
        if self.scheduler.waiting or not self.cfg.scheduling.evict:
            return []
        now = time.monotonic()
        out = []
        for pattern, policy in self.cfg.models.items():
            if policy.keep_warm == 0 or not policy.home or any(ch in pattern for ch in "*?["):
                continue
            model = normalize(pattern)
            hosts = [h for h in self.cluster.hosts.values() if h.routable and model in h.installed]
            if not hosts:
                continue
            ctx = policy.num_ctx or self.stats.usual_ctx(model) or hosts[0].cfg.default_num_ctx
            ctx = self.effective_ctx(model, ctx) or ctx
            warm = [h for h in hosts if h.is_warm(model, ctx)]
            if len(warm) <= policy.keep_warm or _home_short(policy, hosts, warm) > 0:
                continue
            stray = [h for h in warm if h.name not in policy.home and h.inflight[model] == 0
                     and model not in h.pending_evictions
                     and now - h.last_used.get(model, 0.0) >= REHOME_IDLE]
            if stray:
                stray.sort(key=lambda h: h.last_used.get(model, 0.0))
                out.append((stray[0].name, model))
        return out

    async def _warm_loop(self) -> None:
        """Keep `keep_warm` models loaded on enough hosts, at the context size they're used
        at, and on their home hosts when those have room."""
        while True:
            await asyncio.sleep(15)
            await self.keep_warm_round()

    async def keep_warm_round(self) -> None:
        for host, model, ctx in self.warm_targets():
            await self.warm(host, model, ctx)
        for host, model in self.rehome_evictions():
            h = self.cluster.hosts[host]
            # Hide the copy from placement now, before the unload request is sent.
            h.pending_evictions[model] = h.loaded[model].context_length if model in h.loaded else None
            self.log.info("Rehoming model", model=model, endpoint=host, home=self.cfg.policy_for(model).home)
            await self.evict(host, (model,))

    async def warm(self, host: str, model: str, ctx: int) -> None:
        h = self.cluster.hosts[host]
        self.log.info("Warming model", model=model, endpoint=host, num_ctx=ctx)
        # Counted like a request so placement sees the host as busy and the load as pending.
        self.cluster.begin(host, model, cold=True, evict=(), ctx=ctx)
        ok = False
        try:
            r = await self.client.post(
                h.cfg.url + "/api/generate",
                json={"model": self.cluster.installed_spelling(host, model), "keep_alive": "30m",
                      "options": {"num_ctx": ctx}},
                timeout=120,
            )
            ok = r.status_code == 200
        except httpx.HTTPError as exc:
            self.log.warn("Warming failed", model=model, endpoint=host, error=str(exc))
        finally:
            self.cluster.end(host, model, ok=ok, duration_ms=0, nbytes=0)

    # ---- helpers -----------------------------------------------------------------------

    def effective_ctx(self, model: str, ctx: int | None) -> int | None:
        """The context size `model` will really be loaded with for a request asking for
        `ctx` (None: the host default). Ollama caps it at the model's own maximum, so
        asking for more must still match a copy loaded at that maximum."""
        cap = self.stats.max_ctx.get(model)
        if not cap:
            return ctx
        want = ctx or max(h.default_num_ctx for h in self.cfg.hosts)
        return cap if want > cap else ctx

    def classify(self, ip: str) -> str:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return self.cfg.queue.default_class
        for name, c in self.cfg.queue.classes.items():
            if any(addr in ipaddress.ip_network(m, strict=False) for m in c.match):
                return name
        return self.cfg.queue.default_class

    async def evict(self, host: str, models: tuple[str, ...]) -> None:
        url = self.cluster.hosts[host].cfg.url
        for m in models:
            spelling = self.cluster.installed_spelling(host, m)
            self.log.info("Evicting model", endpoint=host, model=m)
            with contextlib.suppress(httpx.HTTPError):
                # Ollama unloads a model on a request with keep_alive 0 and no prompt.
                path = "/api/embed" if "embed" in m else "/api/generate"
                await self.client.post(url + path, json={"model": spelling, "keep_alive": 0}, timeout=30)


# ---- request handling --------------------------------------------------------------------


def _json_body(raw: bytes) -> dict | None:
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _requested_ctx(body: dict | None) -> int | None:
    opts = (body or {}).get("options")
    n = opts.get("num_ctx") if isinstance(opts, dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None


def _requested_model(body: dict | None) -> str | None:
    if not body:
        return None
    name = body.get("model") or body.get("name")
    return name if isinstance(name, str) and name.strip() else None


def _olla_headers(head: Head, rid: str, host: str | None, model: str | None, elapsed_ms: float,
                  lease: Lease | None = None) -> dict[str, str]:
    h = {
        "X-Olla-Request-Id": rid,
        "Via": f"1.1 {SERVED_BY}",
        "X-Served-By": SERVED_BY,
    }
    if host:
        h["X-Olla-Endpoint"] = host
        h["X-Olla-Backend-Type"] = "ollama"
        h["X-Olla-Response-Time"] = f"{int(elapsed_ms)}ms"
    if model:
        h["X-Olla-Model"] = model
    if lease:
        h["X-Olla-Routing-Strategy"] = "llm-head"
        h["X-Olla-Routing-Decision"] = "routed"
        h["X-Olla-Routing-Reason"] = lease.decision.reason
        h["X-Llm-Head-Queue-Ms"] = str(int(lease.queued_ms))
        if lease.decision.cold:
            h["X-Llm-Head-Cold-Load"] = "true"
    return h


async def proxy(request: Request) -> Response:
    head: Head = request.app.state.head
    rid = request.state.request_id
    path = request.path_params["path"].lstrip("/")
    ip = request.state.client_ip
    raw = await request.body()
    body = _json_body(raw) if request.method == "POST" else None
    req_model = _requested_model(body)
    model = normalize(req_model) if req_model else None
    head.log.info(
        "Request received", request_id=rid, client_ip=ip, method=request.method,
        path=request.url.path, user_agent=request.headers.get("user-agent", ""), model=model,
        content_length=len(raw),
    )

    if request.method == "GET" and path == "api/tags":
        return JSONResponse(aggregate_tags(head), headers={"X-Olla-Request-Id": rid})
    if request.method == "GET" and path == "v1/models":
        return JSONResponse(openai_models(head, extended=False), headers={"X-Olla-Request-Id": rid})
    if path in NOT_SUPPORTED:
        return _plain(501, NOT_SUPPORTED[path], rid)

    started = time.monotonic()
    incoming = forward_headers(list(request.headers.items()), ip, SERVED_BY)
    if model:
        incoming["X-Model"] = model
    query = ("?" + request.url.query) if request.url.query else ""

    # Requests that don't run a model go straight to a suitable host, without a slot.
    is_inference = path in INFERENCE_PATHS and model is not None
    unload_only = is_inference and body is not None and body.get("keep_alive") in (0, "0", "0s") and not (
        body.get("prompt") or body.get("messages") or body.get("input")
    )
    if not is_inference or unload_only:
        host = pick_direct(head, model, prefer_loaded=unload_only)
        if host is None:
            return _reject(head, rid, Rejected(404, "no_endpoint", "No ollama endpoints available"))
        try:
            relay = await open_upstream(
                head.client, request.method, head.cluster.hosts[host].cfg.url + "/" + path + query, incoming, raw,
                connect_timeout=head.cfg.proxy.connect_timeout, header_timeout=head.cfg.proxy.response_header_timeout,
                read_timeout=head.cfg.proxy.read_timeout,
            )
        except UpstreamUnavailable as exc:
            if exc.connection:
                head.cluster.connection_failed(host, str(exc))
            return _bad_gateway(head, rid, host, str(exc))
        headers = response_headers(relay.response)
        headers.update(_olla_headers(head, rid, host, model, relay.headers_ms))
        return StreamingResponse(
            relay.body(head.cfg.proxy.stall_timeout, head.cfg.proxy.response_timeout),
            status_code=relay.response.status_code, headers=headers,
        )

    klass = head.classify(ip)
    ctx = head.effective_ctx(model, _requested_ctx(body))
    exclude: frozenset[str] = frozenset()
    last_error = ""
    attempts = head.cfg.proxy.max_attempts or len(head.cluster.hosts)
    for attempt in range(1, attempts + 1):
        try:
            lease = await head.scheduler.acquire(model, klass=klass, request_id=rid, exclude=exclude, ctx=ctx)
        except Rejected as rej:
            if exclude and rej.reason == "no_healthy_endpoints":
                return _bad_gateway(head, rid, None, last_error)
            if rej.reason == "model_not_found":
                head.log.warn("Model routing rejected request", request_id=rid, model=model,
                              strategy="strict", reason="model_not_found", status=404)
            return _reject(head, rid, rej)

        host = lease.host
        target = head.cluster.hosts[host].cfg.url + "/" + path + query
        if lease.decision.evict and head.cfg.scheduling.evict:
            await head.evict(host, lease.decision.evict)
        head.log.info("Request dispatching", request_id=rid, endpoint=host, target=target, model=model,
                      placement=lease.decision.reason, cold=lease.decision.cold, queued_ms=int(lease.queued_ms),
                      num_ctx=ctx, attempt=attempt)
        opening = open_upstream(
            head.client, request.method, target, incoming, raw,
            connect_timeout=head.cfg.proxy.connect_timeout, header_timeout=head.cfg.proxy.response_header_timeout,
            read_timeout=head.cfg.proxy.read_timeout,
        )
        try:
            if lease.decision.cold:
                relay = await _with_load_watchdog(head, opening, host, model, ctx)
            else:
                relay = await opening
        except UpstreamUnavailable as exc:
            last_error = str(exc)
            if exc.connection:
                head.cluster.connection_failed(host, last_error)
            if exc.model_problem:
                head.cluster.quarantine_model(host, model, last_error)
            head.scheduler.release(lease, ok=False, duration_ms=(time.monotonic() - started) * 1000, nbytes=0)
            head.log.warn("Request failed", request_id=rid, endpoint=host, model=model, error=last_error,
                          attempt=attempt, will_retry=attempt < attempts)
            exclude = exclude | {host}
            continue

        headers = response_headers(relay.response)
        headers.update(_olla_headers(head, rid, host, model, relay.headers_ms, lease))
        return StreamingResponse(
            _finish_stream(head, rid, lease, relay, started),
            status_code=relay.response.status_code,
            headers=headers,
        )

    return _bad_gateway(head, rid, None, last_error)


async def _with_load_watchdog(head: Head, opening, host: str, model: str, ctx: int | None):
    """Wait for response headers, but give up if the model never shows up as loaded.

    A healthy Ollama lists a model in /api/ps within seconds of starting to load it. One
    that has stopped loading models (2026-09-30 incident) never does, and never answers,
    while its health check stays green. Aborting before any response byte makes a retry
    on another host safe.
    """
    h = head.cluster.hosts[host]
    want = ctx or h.cfg.default_num_ctx
    size = head.cluster.file_size(model)
    deadline = max(head.cfg.proxy.load_timeout, 4 * head.stats.load_time_estimate(model, size))
    task = asyncio.ensure_future(opening)
    start = time.monotonic()
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=1.0)
            if done:
                return task.result()
            if h.is_loaded(model, want):
                return await task
            if time.monotonic() - start > deadline:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, UpstreamUnavailable):
                    await task
                raise UpstreamUnavailable(
                    f"model {model} (num_ctx {want}) did not load on {host} within {deadline:.0f}s",
                    connection=False, model_problem=True)
    except asyncio.CancelledError:
        task.cancel()
        raise


async def _finish_stream(head: Head, rid: str, lease: Lease, relay, started: float):
    """Relay the body, then release the slot and log the outcome, however it ends."""
    cancelled = False
    try:
        async for chunk in relay.body(head.cfg.proxy.stall_timeout, head.cfg.proxy.response_timeout):
            yield chunk
    except (asyncio.CancelledError, GeneratorExit):
        cancelled = True
        raise
    finally:
        duration_ms = (time.monotonic() - started) * 1000
        ok = not relay.error and relay.response.status_code < 500
        # A client hanging up is not the host's fault.
        head.scheduler.release(lease, ok=ok or cancelled, duration_ms=duration_ms, nbytes=relay.nbytes)
        u = relay.usage()
        if relay.stalled or relay.error == "response_timeout":
            head.cluster.quarantine_model(lease.host, lease.model, relay.error)
        elif ok and not cancelled:
            head.cluster.model_succeeded(lease.host, lease.model)
        if ok and not cancelled:
            head.stats.observe_request(lease.model, duration_ms / 1000,
                                       load_time=u.load_seconds if lease.decision.cold else 0.0,
                                       ctx=lease.ctx or head.cluster.hosts[lease.host].cfg.default_num_ctx)
            head.log.info(
                "Request completed", request_id=rid, endpoint=lease.host, duration_ms=int(duration_ms),
                status="completed", model=lease.model, total_bytes=relay.nbytes, input_tokens=u.input_tokens,
                output_tokens=u.output_tokens, total_tokens=u.input_tokens + u.output_tokens,
                tokens_per_sec=f"{u.tokens_per_sec:.1f}", ttft_ms=u.ttft_ms, queued_ms=int(lease.queued_ms),
                placement=lease.decision.reason,
            )
        else:
            head.total_failures += 1
            head.log.warn(
                "Request failed", request_id=rid, endpoint=lease.host, model=lease.model,
                duration_ms=int(duration_ms), error=relay.error or ("client disconnected" if cancelled else
                                                                     f"HTTP {relay.response.status_code}"),
                stalled=relay.stalled, total_bytes=relay.nbytes,
            )


def _home_short(policy, hosts: list, warm: list) -> int:
    """How many more warm copies the model's home hosts should hold: up to keep_warm, and
    no more than the home hosts available."""
    homes = [h for h in hosts if h.name in policy.home]
    want = min(policy.keep_warm, len(homes))
    return max(0, want - sum(1 for h in warm if h.name in policy.home))


def _fits_beside_loaded(snap: HostSnapshot, model: str, need: int) -> bool:
    """True if `model` fits fully on the GPU next to the other models loaded there."""
    others = {m: lm for m, lm in snap.loaded.items() if m != model}
    used = sum(lm.vram_bytes for lm in others.values())
    return len(others) < snap.max_loaded_models and used + need <= snap.usable_vram_bytes


def pick_direct(head: Head, model: str | None, *, prefer_loaded: bool) -> str | None:
    """Host for a request that doesn't need a slot: least busy, having the model if named."""
    hosts = [h for h in head.cluster.hosts.values() if h.routable]
    if model:
        hosts = [h for h in hosts if model in h.installed]
        if prefer_loaded:
            loaded = [h for h in hosts if model in h.loaded]
            hosts = loaded or hosts
    if not hosts:
        return None
    return min(hosts, key=lambda h: sum(h.inflight.values())).name


def _plain(status: int, text: str, rid: str, extra: dict[str, str] | None = None) -> Response:
    """Olla's own errors are Go http.Error responses: plain text with a trailing newline."""
    headers = {"X-Olla-Request-Id": rid, "X-Content-Type-Options": "nosniff", **(extra or {})}
    return PlainTextResponse(text + "\n", status_code=status, headers=headers)


def _reject(head: Head, rid: str, rej: Rejected) -> Response:
    if rej.reason in ("model_not_found", "no_healthy_endpoints", "no_endpoint"):
        # Olla answers 404 for both an unknown model and no healthy host having it.
        return _plain(404, "No ollama endpoints available", rid)
    head.total_failures += 1
    # New in llm-head: queue timeouts. JSON with "error" so Ollama client libraries show it.
    headers = {"X-Olla-Request-Id": rid}
    if rej.retry_after:
        headers["Retry-After"] = str(rej.retry_after)
    return JSONResponse({"error": rej.message, "reason": rej.reason}, status_code=rej.status, headers=headers)


def _bad_gateway(head: Head, rid: str, host: str | None, error: str) -> Response:
    head.total_failures += 1
    head.log.error("Request failed", request_id=rid, endpoint=host, error=error, status=502)
    return _plain(502, f"Proxy error: all endpoints failed: {error}", rid)


# ---- aggregated model views ------------------------------------------------------------------


def _unique_models(head: Head) -> dict[str, tuple]:
    """normalized name -> (InstalledModel from the first host listing it, [host names])."""
    out: dict[str, tuple] = {}
    for h in head.cluster.hosts.values():
        if h.status != "healthy":
            continue
        for key, m in h.installed.items():
            if key in out:
                out[key][1].append(h.name)
            else:
                out[key] = (m, [h.name])
    return out


def aggregate_tags(head: Head) -> dict:
    return {"models": [m.raw for m, _ in _unique_models(head).values()]}


def openai_models(head: Head, *, extended: bool) -> dict:
    now = int(time.time())
    data = []
    for key, (m, hosts) in _unique_models(head).items():
        entry = {"id": m.name, "object": "model", "created": now, "owned_by": "olla"}
        if extended:
            d = m.details or {}
            entry["olla"] = {
                "aliases": [m.name],
                "availability": [
                    {"endpoint": h, "state": "loaded" if key in head.cluster.hosts[h].loaded else "available"}
                    for h in sorted(hosts)
                ],
                "family": d.get("family", ""),
                "parameter_size": str(d.get("parameter_size", "")).lower(),
                "quantization": str(d.get("quantization_level", "")).lower().replace("_", ""),
                "capabilities": [],
                "variant": "",
            }
        data.append(entry)
    return {"object": "list", "data": data}


# ---- /internal ------------------------------------------------------------------------------


async def internal_health(request: Request) -> Response:
    return JSONResponse({"status": "healthy"})


def _issues(h) -> str:
    """Olla's issue labels, joined with ", "."""
    out = []
    if h.consecutive_failures > 3:
        out.append("consecutive failures")
    if h.requests > 10 and (h.requests - h.failures) / h.requests < 0.9:
        out.append("low success rate")
    ok = h.requests - h.failures
    if ok and h.latency_total_ms / ok > 5000:
        out.append("high latency")
    if h.status == "offline":
        out.append("unavailable")
    return ", ".join(out)


def _sorted_hosts(head: Head) -> list:
    # Olla lists endpoints by priority, then healthy ones first. All priorities are equal here.
    return sorted(head.cluster.hosts.values(), key=lambda h: h.status != "healthy")


def _endpoint_status(head: Head, h) -> dict:
    total = h.requests
    ok = total - h.failures
    return {
        "name": h.name,
        "status": h.status if not h.draining else "draining",
        "success_rate": fmt.pct(ok, total),
        "avg_latency": fmt.ms(h.latency_total_ms / ok) if ok else "0ms",
        "traffic": fmt.size(h.traffic_bytes),
        "last_check": fmt.ago(h.last_check),
        "next_check": fmt.until(h.last_check + head.cfg.health.interval),
        "issues": _issues(h),
        "models": {"count": len(h.installed), "last_updated": _iso(h.models_updated)},
        "priority": 100,
        "connections": sum(h.inflight.values()),
        "requests": total,
        # llm-head additions
        "loaded_models": sorted(h.loaded),
        "vram_used": fmt.size(sum(m.vram_bytes for m in h.loaded.values())),
        "vram_usable": fmt.size(h.usable_vram_bytes),
        "spilled_models": sorted(m for m, lm in h.loaded.items() if lm.spilled),
        "inflight": {m: n for m, n in h.inflight.items() if n},
    }


async def internal_status(request: Request) -> Response:
    head: Head = request.app.state.head
    hosts = _sorted_hosts(head)
    total_req = sum(h.requests for h in hosts)
    total_fail = sum(h.failures for h in hosts)
    lat = sum(h.latency_total_ms for h in hosts)
    up = sum(1 for h in hosts if h.status == "healthy")
    return JSONResponse({
        "timestamp": _iso(time.time()),
        "endpoints": [_endpoint_status(head, h) for h in hosts],
        "proxy": {"balancer": "llm-head", "engine": "llm-head", "profile": "auto"},
        "security": {"blocked_ips": 0, "status": "normal",
                     "violations": {"rate_limits": head.app_state.rate_limited, "size_limits": head.app_state.size_limited}},
        "system": {
            "active_connections": sum(sum(h.inflight.values()) for h in hosts),
            "avg_latency": fmt.ms(lat / (total_req - total_fail)) if total_req > total_fail else "0ms",
            "endpoints_up": f"{up}/{len(hosts)}",
            "security_violations": head.app_state.rate_limited + head.app_state.size_limited,
            "status": "healthy" if up else "unhealthy",
            "success_rate": fmt.pct(total_req - total_fail, total_req),
            # Every request whose client saw a failure: 5xx, queue timeout, failed or
            # abandoned stream. Monitoring alerts on its growth between checks.
            "total_failures": head.total_failures,
            "total_requests": total_req,
            "total_traffic": fmt.size(sum(h.traffic_bytes for h in hosts)),
            "uptime": fmt.uptime(time.time() - head.started),
            "version": VERSION,
            "commit": COMMIT,
        },
        "queue": {"waiting": len(head.scheduler.waiting), "by_model": head.scheduler.depth_by_model()},
    })


async def internal_endpoints(request: Request) -> Response:
    head: Head = request.app.state.head
    hosts = _sorted_hosts(head)
    return JSONResponse({
        "timestamp": _iso(time.time()),
        "endpoints": [
            {
                "name": h.name,
                "type": "ollama",
                "status": h.status if not h.draining else "draining",
                "last_model_sync": fmt.ago(h.models_updated),
                "health_check": fmt.ago(h.last_check),
                "response_time": f"{int(h.check_latency_ms)}ms",
                "success_rate": fmt.pct(h.requests - h.failures, h.requests),
                "priority": 100,
                "model_count": len(h.installed),
                "request_count": h.requests,
            }
            for h in hosts
        ],
        "total_count": len(hosts),
        "healthy_count": sum(1 for h in hosts if h.status == "healthy"),
        "routable_count": sum(1 for h in hosts if h.routable),
    })


async def internal_models(request: Request) -> Response:
    head: Head = request.app.state.head
    models = _unique_models(head)
    by_family: dict[str, list[str]] = {}
    recent = []
    for m, hosts in models.values():
        fam = (m.details or {}).get("family") or "unknown"
        by_family.setdefault(fam, []).append(m.name)
        recent.append({
            "name": m.name,
            "family": fam,
            "endpoints": sorted(hosts),
            "last_seen": fmt.ago(max(head.cluster.hosts[h].models_updated for h in hosts)),
            "params": (m.details or {}).get("parameter_size", ""),
            "quant": (m.details or {}).get("quantization_level", ""),
            "size": fmt.size(m.size),
        })
    return JSONResponse({
        "timestamp": _iso(time.time()),
        "models_by_family": {k: sorted(v) for k, v in sorted(by_family.items())},
        "recent_models": recent,
        "total_endpoints": len(head.cluster.hosts),
        "total_families": len(by_family),
        "total_models": len(models),
    })


async def internal_process(request: Request) -> Response:
    head: Head = request.app.state.head
    import resource

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        rss *= 1024
    return JSONResponse({
        "timestamp": _iso(time.time()),
        "runtime": {"python_version": platform.python_version(), "num_cpu": os.cpu_count(),
                    "uptime": fmt.uptime(time.time() - head.started)},
        "memory": {"max_rss": fmt.size(rss)},
        "tasks": {"count": len(asyncio.all_tasks())},
    })


async def internal_queue(request: Request) -> Response:
    head: Head = request.app.state.head
    now = time.monotonic()
    return JSONResponse({
        "waiting": [
            {"request_id": w.request_id, "model": w.model, "class": w.klass, "waited_ms": int((now - w.enqueued) * 1000)}
            for w in head.scheduler.waiting
        ],
        "hosts": {
            h.name: {
                "status": h.status, "draining": h.draining, "loaded": sorted(h.loaded),
                "spilled": sorted(m for m, lm in h.loaded.items() if lm.spilled),
                "vram_usable_mb": h.usable_vram_bytes // (1024 * 1024),
                "pending_loads": sorted(h.pending_loads), "inflight": {m: n for m, n in h.inflight.items() if n},
            }
            for h in head.cluster.hosts.values()
        },
    })


async def internal_drain(request: Request) -> Response:
    head: Head = request.app.state.head
    if not _is_loopback(request.state.client_ip):
        return JSONResponse({"error": "drain is only allowed from localhost"}, status_code=403)
    name = request.path_params["name"]
    if name not in head.cluster.hosts:
        return JSONResponse({"error": f"unknown host {name}"}, status_code=404)
    draining = request.url.path.endswith("/drain")
    head.cluster.set_draining(name, draining)
    h = head.cluster.hosts[name]
    return JSONResponse({"host": name, "draining": draining, "inflight": sum(h.inflight.values())})


async def internal_reset_capacity(request: Request) -> Response:
    head: Head = request.app.state.head
    if not _is_loopback(request.state.client_ip):
        return JSONResponse({"error": "reset-capacity is only allowed from localhost"}, status_code=403)
    name = request.path_params["name"]
    if name not in head.cluster.hosts:
        return JSONResponse({"error": f"unknown host {name}"}, status_code=404)
    head.cluster.reset_capacity(name)
    return JSONResponse({"host": name, "vram_usable_mb": head.cluster.hosts[name].usable_vram_bytes // (1024 * 1024)})


async def version(request: Request) -> Response:
    return JSONResponse({
        "name": "llm-head",
        "version": VERSION,
        "description": "Load balancer for Ollama clusters that knows which models each host has loaded",
        "edition": "Community",
        "compatible_with": "Olla v0.0.28",
        "api": {"version": "v1", "endpoints": {"health": "/internal/health", "process": "/internal/process",
                                                "status": "/internal/status", "version": "/version"}},
        "links": {"homepage": "https://github.com/billford/llm-head"},
        "capabilities": ["load_balancing", "health_checking", "rate_limiting", "model_unification",
                         "model_aware_placement", "queueing"],
        "supported_backends": ["ollama"],
    })


async def olla_models(request: Request) -> Response:
    return JSONResponse(openai_models(request.app.state.head, extended=True))


async def not_found(request: Request) -> Response:
    return PlainTextResponse("404 page not found\n", status_code=404)


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    if not ts:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class _AppState:
    rate_limited = 0
    size_limited = 0


def build_app(cfg: Config, *, head: Head | None = None) -> Starlette:
    head = head or Head(cfg)
    head.app_state = _AppState()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await head.start()
        try:
            yield
        finally:
            await head.stop()

    methods = ["GET", "POST", "PUT", "DELETE", "HEAD", "PATCH"]
    app = Starlette(
        routes=[
            Route("/internal/health", internal_health),
            Route("/internal/status", internal_status),
            Route("/internal/status/endpoints", internal_endpoints),
            Route("/internal/status/models", internal_models),
            Route("/internal/process", internal_process),
            Route("/internal/queue", internal_queue),
            Route("/internal/hosts/{name}/drain", internal_drain, methods=["POST"]),
            Route("/internal/hosts/{name}/undrain", internal_drain, methods=["POST"]),
            Route("/internal/hosts/{name}/reset-capacity", internal_reset_capacity, methods=["POST"]),
            Route("/version", version),
            Route("/olla/models", olla_models),
            Route("/olla/ollama/{path:path}", proxy, methods=methods),
            Route("/{path:path}", not_found, methods=methods),
        ],
        lifespan=lifespan,
    )
    app.state.head = head
    app.add_middleware(LimitsMiddleware, head=head)
    app.add_middleware(CorsMiddleware, cfg=cfg.server.cors)
    return app
