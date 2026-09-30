"""Live cluster state: what each host has installed and loaded, its health, and the
requests the head currently has in flight there.

Three background loops per host keep it current:
  health  GET <health.path> every health.interval
  ps      GET /api/ps every discovery.ps_interval (loaded models and their GPU memory)
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
    # Loads and evictions the head started that /api/ps doesn't reflect yet.
    pending_loads: dict[str, tuple[int, float]] = field(default_factory=dict)  # model -> (bytes, deadline)
    pending_evictions: set[str] = field(default_factory=set)
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

    def snapshot(self, now: float) -> HostSnapshot:
        loaded = {m: lm for m, lm in self.loaded.items() if m not in self.pending_evictions}
        for m, (size, deadline) in list(self.pending_loads.items()):
            if deadline < now:
                del self.pending_loads[m]
            elif m not in loaded:
                loaded[m] = LoadedModel(size, last_used=now)
        for m, lm in loaded.items():
            lm.last_used = self.last_used.get(m, lm.last_used)
        return HostSnapshot(
            name=self.name,
            routable=self.routable,
            usable_vram_bytes=self.cfg.usable_vram_bytes,
            slots_per_model=self.cfg.slots_per_model,
            max_loaded_models=self.cfg.max_loaded_models,
            installed=frozenset(self.installed),
            loaded=loaded,
            inflight={m: n for m, n in self.inflight.items() if n > 0},
        )


class Cluster:
    def __init__(self, cfg: Config, stats: Stats, log: EventLog, client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self.stats = stats
        self.log = log
        self.hosts: dict[str, HostState] = {h.name: HostState(h) for h in cfg.hosts}
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

    def begin(self, host: str, model: str, *, cold: bool, evict: tuple[str, ...]) -> None:
        h = self.hosts[host]
        h.inflight[model] += 1
        h.last_used[model] = time.monotonic()
        if cold:
            vram = self.stats.vram_estimate(model, self.file_size(model))
            h.pending_loads[model] = (vram, time.monotonic() + PENDING_LOAD_TTL)
        h.pending_evictions.update(evict)

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
            if not name:
                continue
            loaded[name] = LoadedModel(vram, last_used=h.last_used.get(name, 0.0))
            if vram and vram >= int(m.get("size") or 0):
                # Only learn footprints from fully-on-GPU loads; a spilled load under-reports.
                self.stats.observe_loaded(name, vram)
        changed = set(loaded) != set(h.loaded)
        h.loaded = loaded
        for name in list(h.pending_loads):
            if name in loaded:
                del h.pending_loads[name]
        h.pending_evictions &= set(loaded)
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
