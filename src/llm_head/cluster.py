"""Live cluster state: what each host has installed and loaded, its health, and the
requests the head currently has in flight there.

Three background loops per host keep it current:
  health  GET <health.path> every health.interval
  ps      GET /api/ps every discovery.ps_interval (loaded models, their GPU memory,
          and whether Ollama spilled part of one onto the CPU)
  tags    GET /api/tags every discovery.tags_interval, and after recovery
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .config import Config, HostConfig
from .names import normalize
from .obslog import EventLog
from .placement import HostSnapshot, LoadedModel, ModelFacts
from .stats import Stats

# A load the head dispatched is assumed in progress until /api/ps shows it, or this long.
PENDING_LOAD_TTL = 180.0


@dataclass
class InstalledModel:
    name: str  # spelling the backend reported
    size: int  # bytes on disk
    digest: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    modified_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class HostState:
    cfg: HostConfig
    status: str = "unknown"  # unknown | healthy | offline
    consecutive_failures: int = 0
    draining: bool = False
    installed: dict[str, InstalledModel] = field(default_factory=dict)
    loaded: dict[str, LoadedModel] = field(default_factory=dict)
    # GPU memory Ollama actually fits here, learned from spills; None until one is seen.
    vram_capacity: int | None = None
    # Loads and evictions the head started that /api/ps doesn't reflect yet.
    # model -> (bytes, give-up deadline, expected ready time, context size); monotonic seconds
    pending_loads: dict[str, tuple[int, float, float, int]] = field(default_factory=dict)
    # model -> context size of the copy being unloaded
    pending_evictions: dict[str, int | None] = field(default_factory=dict)
    # model -> (quarantined until, strikes); monotonic seconds
    quarantine: dict[str, tuple[float, int]] = field(default_factory=dict)
    inflight: Counter = field(default_factory=Counter)
    last_used: dict[str, float] = field(default_factory=dict)
    # Counters for /internal/status.
    requests: int = 0
    failures: int = 0
    traffic_bytes: int = 0
    latency_total_ms: float = 0.0
    last_check: float = 0.0
    check_latency_ms: float = 0.0
    last_error: str = ""
    models_updated: float = 0.0

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def routable(self) -> bool:
        return self.status == "healthy" and not self.draining

    @property
    def usable_vram_bytes(self) -> int:
        cap = self.cfg.usable_vram_bytes
        return cap if self.vram_capacity is None else min(cap, self.vram_capacity)

    def is_loaded(self, model: str, ctx: int) -> bool:
        lm = self.loaded.get(model)
        return lm is not None and (lm.context_length is None or lm.context_length == ctx)

    def is_warm(self, model: str, ctx: int) -> bool:
        """Loaded at `ctx` and fully on the GPU."""
        return self.is_loaded(model, ctx) and not self.loaded[model].spilled

    def quarantined(self, model: str, now: float) -> bool:
        q = self.quarantine.get(model)
        return q is not None and q[0] > now

    def snapshot(self, now: float) -> HostSnapshot:
        loaded = {
            m: lm for m, lm in self.loaded.items()
            if not (m in self.pending_evictions and self.pending_evictions[m] == lm.context_length)
        }
        for m, (size, deadline, ready_at, ctx) in list(self.pending_loads.items()):
            if deadline < now:
                del self.pending_loads[m]
            elif m not in loaded:
                # Past the estimate but not yet in /api/ps: assume it's nearly done.
                loaded[m] = LoadedModel(size, last_used=now, ready_in=max(0.5, ready_at - now),
                                        context_length=ctx)
        for m, lm in loaded.items():
            lm.last_used = self.last_used.get(m, lm.last_used)
        return HostSnapshot(
            name=self.name,
            routable=self.routable,
            usable_vram_bytes=self.usable_vram_bytes,
            slots_per_model=self.cfg.slots_per_model,
            max_loaded_models=self.cfg.max_loaded_models,
            installed=frozenset(self.installed),
            loaded=loaded,
            inflight={m: n for m, n in self.inflight.items() if n > 0},
            default_num_ctx=self.cfg.default_num_ctx,
        )


class Cluster:
    def __init__(self, cfg: Config, stats: Stats, log: EventLog, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.stats = stats
        self.log = log
        self.hosts: dict[str, HostState] = {h.name: HostState(h, vram_capacity=stats.host_vram.get(h.name))
                                            for h in cfg.hosts}
        self.client = client or httpx.AsyncClient(timeout=cfg.discovery.timeout)
        self._tasks: list[asyncio.Task] = []
        # Called whenever state changes in a way that might unblock waiting requests.
        self.on_change: Callable[[], None] = lambda: None

    # ---- views used by the scheduler -------------------------------------------------

    def snapshots(self) -> list[HostSnapshot]:
        now = time.monotonic()
        return [h.snapshot(now) for h in self.hosts.values()]

    def installed_spelling(self, host: str, model: str) -> str:
        m = self.hosts[host].installed.get(model)
        return m.name if m else model

    def file_size(self, model: str) -> int:
        sizes = [h.installed[model].size for h in self.hosts.values() if model in h.installed]
        return max(sizes) if sizes else 0

    def facts(self, model: str) -> ModelFacts:
        policy = self.cfg.policy_for(model)
        size = self.file_size(model)
        return ModelFacts(
            vram_bytes=self.stats.vram_estimate(model, size),
            typical_duration=self.stats.duration_estimate(model),
            load_time=self.stats.load_time_estimate(model, size),
            home=tuple(policy.home),
            keep_warm=policy.keep_warm,
            allow_cpu_offload=policy.allow_cpu_offload,
        )

    def all_facts(self) -> dict[str, ModelFacts]:
        models = {m for h in self.hosts.values() for m in h.loaded}
        return {m: self.facts(m) for m in models}

    # ---- bookkeeping for dispatched requests -----------------------------------------

    def begin(self, host: str, model: str, *, cold: bool, evict: tuple[str, ...], ctx: int | None = None) -> None:
        h = self.hosts[host]
        h.inflight[model] += 1
        h.last_used[model] = time.monotonic()
        for m in evict:
            h.pending_evictions[m] = h.loaded[m].context_length if m in h.loaded else None
        if cold and model not in h.pending_loads:
            size = self.file_size(model)
            vram = self.stats.vram_estimate(model, size)
            now = time.monotonic()
            h.pending_loads[model] = (vram, now + PENDING_LOAD_TTL,
                                      now + self.stats.load_time_estimate(model, size),
                                      ctx or h.cfg.default_num_ctx)

    def quarantine_model(self, host: str, model: str, reason: str) -> None:
        """Stop sending `model` to `host` for a while: it failed to load or respond there."""
        h = self.hosts[host]
        now = time.monotonic()
        _, strikes = h.quarantine.get(model, (0.0, 0))
        strikes += 1
        period = min(3600.0, self.cfg.proxy.model_quarantine * 2 ** (strikes - 1))
        h.quarantine[model] = (now + period, strikes)
        h.pending_loads.pop(model, None)
        self.log.warn("Model quarantined on endpoint", endpoint=host, model=model, reason=reason,
                      seconds=int(period), strikes=strikes)
        self.on_change()

    def model_succeeded(self, host: str, model: str) -> None:
        h = self.hosts[host]
        if model in h.quarantine:
            del h.quarantine[model]
            self.log.info("Model quarantine cleared", endpoint=host, model=model)

    def quarantined_hosts(self, model: str) -> frozenset[str]:
        now = time.monotonic()
        return frozenset(h.name for h in self.hosts.values() if h.quarantined(model, now))

    def end(self, host: str, model: str, *, ok: bool, duration_ms: float, nbytes: int) -> None:
        h = self.hosts[host]
        h.inflight[model] = max(0, h.inflight[model] - 1)
        h.last_used[model] = time.monotonic()
        h.requests += 1
        if ok:
            h.traffic_bytes += nbytes
            h.latency_total_ms += duration_ms
        else:
            h.failures += 1
        self.on_change()

    def connection_failed(self, host: str, error: str) -> None:
        """A dispatch couldn't reach the host: count it toward marking the host offline."""
        self._record_check(self.hosts[host], ok=False, error=error, latency_ms=0.0)

    def set_draining(self, host: str, draining: bool) -> None:
        self.hosts[host].draining = draining
        self.log.warn("Endpoint drain changed", endpoint_name=host, draining=draining)
        self.on_change()

    def reset_capacity(self, host: str) -> None:
        """Forget the GPU capacity learned from spills and go back to the configured one.
        If the host is still spilling, the next polls learn it again."""
        h = self.hosts[host]
        forgot = self.stats.forget_host_vram(host)
        h.vram_capacity = None
        self.log.warn("Endpoint GPU capacity reset", endpoint=host, forgot_bytes=forgot,
                      usable_vram_bytes=h.usable_vram_bytes)
        self.on_change()

    # ---- background loops -------------------------------------------------------------

    def start(self) -> None:
        for h in self.hosts.values():
            self._tasks += [
                asyncio.create_task(self._health_loop(h)),
                asyncio.create_task(self._ps_loop(h)),
                asyncio.create_task(self._tags_loop(h)),
            ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _health_loop(self, h: HostState) -> None:
        while True:
            await self.check_health(h)
            await asyncio.sleep(self.cfg.health.interval)

    async def check_health(self, h: HostState) -> None:
        start = time.monotonic()
        try:
            r = await self.client.get(h.cfg.url + self.cfg.health.path, timeout=self.cfg.health.timeout)
            ok, err = r.status_code < 500, f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            ok, err = False, f"{type(exc).__name__}: {exc}"
        self._record_check(h, ok=ok, error=err, latency_ms=(time.monotonic() - start) * 1000)

    def _record_check(self, h: HostState, *, ok: bool, error: str, latency_ms: float) -> None:
        h.last_check = time.time()
        h.check_latency_ms = latency_ms
        was = h.status
        if ok:
            h.consecutive_failures = 0
            h.last_error = ""
            h.status = "healthy"
        else:
            h.consecutive_failures += 1
            h.last_error = error
            if h.consecutive_failures >= self.cfg.health.failure_threshold or was == "unknown":
                h.status = "offline"
        if h.status != was:
            level = "info" if h.status == "healthy" else "warn"
            self.log.event(
                level,
                f"Endpoint status changed: {h.name}",
                status=h.status,
                was=was,
                consecutive_failures=h.consecutive_failures,
            )
            self.log.event(
                level,
                "Endpoint status changed:",
                endpoint_name=h.name,
                status=h.status,
                was=was,
                consecutive_failures=h.consecutive_failures,
                endpoint_url=h.cfg.url,
                check_error=h.last_error,
            )
            if h.status == "healthy" and was == "offline":
                self.log.info(f"Endpoint recovered: {h.name} is Healthy")
                asyncio.create_task(self.refresh_tags(h))
            self.on_change()

    async def _ps_loop(self, h: HostState) -> None:
        while True:
            if h.status == "healthy":
                await self.refresh_ps(h)
            await asyncio.sleep(self.cfg.discovery.ps_interval)

    async def refresh_ps(self, h: HostState) -> None:
        try:
            r = await self.client.get(h.cfg.url + "/api/ps")
            r.raise_for_status()
            models = r.json().get("models") or []
        except (httpx.HTTPError, ValueError):
            return
        loaded: dict[str, LoadedModel] = {}
        for m in models:
            name = normalize(m.get("name") or m.get("model") or "")
            vram = int(m.get("size_vram") or 0)
            size = int(m.get("size") or 0)
            if not name:
                continue
            ctx = m.get("context_length")
            spilled = vram < size
            loaded[name] = LoadedModel(vram, last_used=h.last_used.get(name, 0.0),
                                       context_length=int(ctx) if ctx else None, spilled=spilled)
            if not spilled:
                # Only learn footprints from fully-on-GPU loads; a spilled load under-reports.
                self.stats.observe_loaded(name, vram)
            was = h.loaded.get(name)
            if spilled and not (was and was.spilled):
                self.log.warn("Model spilled onto CPU", endpoint=h.name, model=name,
                              size_vram=vram, size=size, context_length=ctx)
            elif was and was.spilled and not spilled:
                self.log.info("Model fully on GPU again", endpoint=h.name, model=name)
        # What's on the GPU right now is a measured capacity when something has spilled,
        # and a lower bound on it otherwise. A spill counts once two polls in a row show
        # it, so a poll that catches a load half done can't shrink the capacity for good.
        settled = any(lm.spilled and name in h.loaded and h.loaded[name].spilled for name, lm in loaded.items())
        if loaded and (settled or not any(lm.spilled for lm in loaded.values())):
            on_gpu = sum(lm.vram_bytes for lm in loaded.values())
            cap = self.stats.observe_host_vram(h.name, on_gpu, spilled=settled)
            if cap is not None:
                h.vram_capacity = cap
                self.log.info("Endpoint GPU capacity learned", endpoint=h.name, usable_vram_bytes=h.usable_vram_bytes,
                              configured_bytes=h.cfg.usable_vram_bytes)
        changed = {(k, v.context_length, v.spilled) for k, v in loaded.items()} != {
            (k, v.context_length, v.spilled) for k, v in h.loaded.items()}
        h.loaded = loaded
        for name, pending in list(h.pending_loads.items()):
            if h.is_loaded(name, pending[3]):
                del h.pending_loads[name]
        # An eviction is done once the model is gone or reloaded with another context size.
        for name, ctx in list(h.pending_evictions.items()):
            if name not in loaded or loaded[name].context_length != ctx:
                del h.pending_evictions[name]
        if changed:
            self.on_change()

    async def _tags_loop(self, h: HostState) -> None:
        # Wait for the first health check so a down host doesn't delay startup.
        await asyncio.sleep(0.1)
        while True:
            if h.status == "healthy":
                await self.refresh_tags(h)
            await asyncio.sleep(self.cfg.discovery.tags_interval if h.installed else 2.0)

    async def refresh_tags(self, h: HostState) -> None:
        try:
            r = await self.client.get(h.cfg.url + "/api/tags")
            r.raise_for_status()
            models = r.json().get("models") or []
        except (httpx.HTTPError, ValueError) as exc:
            self.log.warn("Model discovery failed", endpoint=h.name, error=str(exc))
            return
        installed = {}
        for m in models:
            name = m.get("name") or m.get("model")
            if not name:
                continue
            installed[normalize(name)] = InstalledModel(
                name=name,
                size=int(m.get("size") or 0),
                digest=m.get("digest", ""),
                details=m.get("details") or {},
                modified_at=m.get("modified_at", ""),
                raw=m,
            )
        changed = set(installed) != set(h.installed)
        h.installed = installed
        h.models_updated = time.time()
        if changed:
            self.on_change()
