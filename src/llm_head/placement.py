"""Placement: decide which host should serve a request for a given model.

This module is pure. It takes a snapshot of every host and returns a Decision, so each
rule in spec §4.3 can be tested without a network or an event loop.

Rules, in order:
  1. The model is loaded somewhere with a free slot: use the least-busy such host.
  2. It is loaded but every slot is busy: wait, unless loading a second copy on an idle
     host is expected to be quicker than waiting.
  3. It fits in free GPU memory somewhere: load it there, no eviction.
  4. Otherwise evict the cheapest set of idle models on the host where that costs
     least. Never evict a model with requests in flight.
  5. Never place a model where it would spill onto the CPU, unless the policy allows
     it or the model cannot fit on any host even when that host is empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


# Seconds added to a plan that evicts a keep_warm model below its target. Large enough
# that any other plan wins, finite so the request is still served.
KEEP_WARM_PENALTY = 60.0


class Kind(str, Enum):
    DISPATCH = "dispatch"
    WAIT = "wait"
    REJECT = "reject"


@dataclass(frozen=True)
class Decision:
    kind: Kind
    reason: str
    host: str | None = None
    # Models to unload on `host` before dispatching, in order.
    evict: tuple[str, ...] = ()
    # True when the model is not yet loaded on `host`.
    cold: bool = False
    # For REJECT: HTTP status the caller should return.
    status: int = 0


@dataclass
class LoadedModel:
    vram_bytes: int
    last_used: float = 0.0
    # Seconds until a load the head started should finish; 0 once /api/ps shows it.
    ready_in: float = 0.0


@dataclass
class HostSnapshot:
    name: str
    routable: bool  # healthy and not draining
    usable_vram_bytes: int
    slots_per_model: int
    max_loaded_models: int
    installed: frozenset[str]
    loaded: dict[str, LoadedModel] = field(default_factory=dict)
    # Requests in flight per model, as counted by the head.
    inflight: dict[str, int] = field(default_factory=dict)

    @property
    def total_inflight(self) -> int:
        return sum(self.inflight.values())

    @property
    def used_vram_bytes(self) -> int:
        return sum(m.vram_bytes for m in self.loaded.values())

    @property
    def free_vram_bytes(self) -> int:
        return self.usable_vram_bytes - self.used_vram_bytes

    def free_slots(self, model: str) -> int:
        return self.slots_per_model - self.inflight.get(model, 0)

    def is_idle(self, model: str) -> bool:
        return self.inflight.get(model, 0) == 0


@dataclass(frozen=True)
class ModelFacts:
    """What the head knows about a model's cost, learned from past requests."""

    # Expected GPU memory once loaded (learned from /api/ps, else estimated from disk size).
    vram_bytes: int
    # Typical request duration and cold-load time, in seconds.
    typical_duration: float = 5.0
    load_time: float = 5.0
    home: tuple[str, ...] = ()
    keep_warm: int = 0
    allow_cpu_offload: bool = False


def choose(
    model: str,
    hosts: list[HostSnapshot],
    facts: ModelFacts,
    *,
    queued_ahead: int = 0,
    rotation: int = 0,
    exclude: frozenset[str] = frozenset(),
    warm_facts: dict[str, ModelFacts] | None = None,
) -> Decision:
    """Decide where a request for `model` should go.

    queued_ahead: requests for the same model already waiting in front of this one.
    rotation:     a counter that changes per decision, used to break ties fairly.
    exclude:      hosts already tried for this request (retry after a failure).
    warm_facts:   facts for other models, used to respect their keep_warm when evicting.
    """
    warm_facts = warm_facts or {}
    having = [h for h in hosts if model in h.installed]
    if not having:
        return Decision(Kind.REJECT, "model_not_found", status=404)
    candidates = [h for h in having if h.routable and h.name not in exclude]
    if not candidates:
        return Decision(Kind.REJECT, "no_healthy_endpoints", status=503)

    def rank(h: HostSnapshot) -> tuple:
        # Fewer requests in flight first; then the model's home hosts; then rotate so
        # ties don't always go to the first host in config order (the Olla 2:1 skew).
        home = 0 if h.name in facts.home else 1
        return (h.total_inflight, home, (hosts.index(h) - rotation) % len(hosts))

    # Rule 1: loaded (not still loading) with a free slot.
    warm = [h for h in candidates if model in h.loaded]
    ready = [h for h in warm if h.free_slots(model) > 0 and h.loaded[model].ready_in <= 0]
    if ready and queued_ahead == 0:
        best = min(ready, key=rank)
        return Decision(Kind.DISPATCH, "loaded", host=best.name)

    # Rules 3-5: where could a new copy be loaded, and at what cost?
    cold_options = _cold_options(model, candidates, facts, warm_facts)

    if warm:
        # Rule 2: compare waiting on a warm host (for a slot, and for a load still in
        # progress there) with loading another copy elsewhere. A load in progress looks
        # like a free slot, but a request sent there waits for the load to finish.
        def wait_on(h: HostSnapshot) -> float:
            share = (h.inflight.get(model, 0) + queued_ahead) / h.slots_per_model
            slot_wait = 0.0 if h.free_slots(model) > 0 and queued_ahead == 0 else share * facts.typical_duration
            return h.loaded[model].ready_in + slot_wait

        best_warm = min(warm, key=lambda h: (wait_on(h), rank(h)))
        expected_wait = wait_on(best_warm)
        if cold_options:
            cost, host, evict = min(cold_options, key=lambda o: (o[0], rank(_by_name(candidates, o[1]))))
            if cost < expected_wait:
                return Decision(Kind.DISPATCH, "replica_cheaper_than_wait", host=host, evict=evict, cold=True)
        if best_warm.free_slots(model) > 0:
            reason = "loaded" if best_warm.loaded[model].ready_in <= 0 else "loading"
            return Decision(Kind.DISPATCH, reason, host=best_warm.name)
        return Decision(Kind.WAIT, "slots_busy")

    if cold_options:
        cost, host, evict = min(cold_options, key=lambda o: (o[0], rank(_by_name(candidates, o[1]))))
        reason = "cold_load_evict" if evict else "cold_load"
        return Decision(Kind.DISPATCH, reason, host=host, evict=evict, cold=True)

    # Nothing fits right now without evicting a busy model.
    never_fits = all(facts.vram_bytes > h.usable_vram_bytes for h in candidates)
    if never_fits and not facts.allow_cpu_offload:
        # Rule 5 exception: spilling is unavoidable, so serve it anyway on the host with
        # the most GPU memory once that host has nothing running.
        biggest = max(candidates, key=lambda h: (h.usable_vram_bytes, -h.total_inflight))
        if biggest.total_inflight == 0:
            evict = tuple(biggest.loaded)
            return Decision(Kind.DISPATCH, "cpu_offload_unavoidable", host=biggest.name, evict=evict, cold=True)
    return Decision(Kind.WAIT, "no_capacity")


def _by_name(hosts: list[HostSnapshot], name: str) -> HostSnapshot:
    return next(h for h in hosts if h.name == name)


def _cold_options(
    model: str,
    candidates: list[HostSnapshot],
    facts: ModelFacts,
    warm_facts: dict[str, ModelFacts],
) -> list[tuple[float, str, tuple[str, ...]]]:
    """Every host where `model` could be loaded now, as (cost, host, evictions)."""
    options = []
    for h in candidates:
        if model in h.loaded:
            continue
        fits_ever = facts.vram_bytes <= h.usable_vram_bytes
        if not fits_ever and not facts.allow_cpu_offload:
            continue
        # Prefer plans that keep keep_warm models loaded; fall back to evicting them
        # rather than leaving this request waiting forever.
        penalty = 0.0
        evict = _plan_eviction(model, h, facts, warm_facts, candidates, spare_warm=True)
        if evict is None:
            evict = _plan_eviction(model, h, facts, warm_facts, candidates, spare_warm=False)
            if evict is None:
                continue
            penalty = KEEP_WARM_PENALTY
        cost = facts.load_time + penalty
        # Evicting a model costs whoever uses it next a reload.
        cost += sum(warm_facts.get(m, ModelFacts(0)).load_time for m in evict)
        # Every host has the same GPU, so the busier host shares its compute more.
        cost += h.total_inflight * facts.typical_duration
        options.append((cost, h.name, evict))
    return options


def _plan_eviction(
    model: str,
    host: HostSnapshot,
    facts: ModelFacts,
    warm_facts: dict[str, ModelFacts],
    all_hosts: list[HostSnapshot],
    *,
    spare_warm: bool,
) -> tuple[str, ...] | None:
    """Which idle models must be unloaded on `host` so `model` fits. None if impossible."""
    need = facts.vram_bytes if facts.vram_bytes <= host.usable_vram_bytes else 0
    free = host.free_vram_bytes
    count = len(host.loaded)
    if free >= need and count < host.max_loaded_models:
        return ()

    def protected(m: str) -> bool:
        # Keep enough warm copies of models that ask for it.
        if not spare_warm:
            return False
        kw = warm_facts.get(m, ModelFacts(0)).keep_warm
        if kw == 0:
            return False
        copies = sum(1 for h in all_hosts if h.routable and m in h.loaded)
        return copies <= kw

    idle = [m for m in host.loaded if host.is_idle(m) and not protected(m)]
    # Hosts hold only a handful of models, so try every subset of idle models and keep
    # the cheapest one that makes room: fewest reloads for others, then fewest models,
    # then the least recently used ones.
    best: tuple | None = None
    for mask in range(1, 1 << len(idle)):
        subset = [m for i, m in enumerate(idle) if mask >> i & 1]
        freed = sum(host.loaded[m].vram_bytes for m in subset)
        if free + freed < need or count - len(subset) >= host.max_loaded_models:
            continue
        reload_cost = sum(warm_facts.get(m, ModelFacts(0)).load_time for m in subset)
        recency = sum(host.loaded[m].last_used for m in subset)
        key = (reload_cost, len(subset), recency)
        if best is None or key < best[0]:
            best = (key, subset)
    if best is None:
        return None
    return tuple(sorted(best[1], key=lambda m: host.loaded[m].last_used))
