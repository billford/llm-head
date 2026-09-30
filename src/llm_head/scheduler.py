"""The head's queue. Every request waits here until placement gives it a host.

A request never goes to a host unless that host has a free slot for its model, so
Ollama's own hidden queue stays empty and every wait is visible, ordered and bounded.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field

from .cluster import Cluster
from .obslog import EventLog
from .placement import Decision, Kind, choose


class Rejected(Exception):
    def __init__(self, status: int, reason: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.message = message
        self.retry_after = retry_after


@dataclass
class Lease:
    host: str
    model: str
    decision: Decision
    queued_ms: float
    ctx: int | None = None


@dataclass(order=True)
class _Waiter:
    sort_key: float
    seq: int
    model: str = field(compare=False)
    klass: str = field(compare=False)
    request_id: str = field(compare=False)
    exclude: frozenset[str] = field(compare=False)
    enqueued: float = field(compare=False)
    ctx: int | None = field(compare=False)
    future: asyncio.Future = field(compare=False)


class Scheduler:
    def __init__(self, cluster: Cluster, log: EventLog, max_wait: float, boosts: dict[str, float],
                 head_of_line_after: float = 10.0):
        self.cluster = cluster
        self.log = log
        self.max_wait = max_wait
        self.head_of_line_after = head_of_line_after
        self.boosts = boosts
        self.waiting: list[_Waiter] = []
        self._seq = itertools.count()
        self._rotation = 0
        cluster.on_change = self.pump

    async def acquire(
        self, model: str, *, klass: str, request_id: str, exclude: frozenset[str] = frozenset(),
        ctx: int | None = None,
    ) -> Lease:
        loop = asyncio.get_running_loop()
        now = time.monotonic()
        w = _Waiter(
            sort_key=now - self.boosts.get(klass, 0.0),
            seq=next(self._seq),
            model=model,
            klass=klass,
            request_id=request_id,
            exclude=exclude,
            enqueued=now,
            future=loop.create_future(),
            ctx=ctx,
        )
        self.waiting.append(w)
        self.waiting.sort()
        self.pump()
        if not w.future.done():
            self.log.info(
                "Request queued",
                request_id=request_id,
                model=model,
                priority_class=klass,
                queue_depth=sum(1 for x in self.waiting if x.model == model),
            )
        try:
            return await asyncio.wait_for(asyncio.shield(w.future), timeout=self.max_wait)
        except asyncio.TimeoutError:
            self._remove(w)
            if w.future.done() and not w.future.cancelled() and w.future.exception() is None:
                # Placed at the last instant: honor it rather than leak a slot.
                return w.future.result()
            raise Rejected(
                503,
                "queue_timeout",
                f"No capacity for {model} after waiting {self.max_wait:.0f}s",
                retry_after=max(1, int(self.cluster.stats.duration_estimate(model))),
            ) from None
        except asyncio.CancelledError:
            # Client went away while waiting.
            self._remove(w)
            if w.future.done() and not w.future.cancelled() and w.future.exception() is None:
                lease = w.future.result()
                self.release(lease, ok=False, duration_ms=0, nbytes=0)
            raise

    def release(self, lease: Lease, *, ok: bool, duration_ms: float, nbytes: int) -> None:
        self.cluster.end(lease.host, lease.model, ok=ok, duration_ms=duration_ms, nbytes=nbytes)
        # cluster.end triggers pump via on_change.

    def pump(self) -> None:
        """Place as many waiting requests as possible, in priority order."""
        if not self.waiting:
            return
        warm = self.cluster.all_facts()
        now = time.monotonic()
        ahead: dict[str, int] = {}
        # Models whose oldest waiter has waited too long: later waiters queue behind it,
        # so the copies it needs drain instead of being kept busy by newer requests.
        hol_blocked: set[str] = set()
        placed: list[_Waiter] = []
        for w in self.waiting:
            if w.future.done():
                placed.append(w)
                continue
            if w.model in hol_blocked:
                ahead[w.model] = ahead.get(w.model, 0) + 1
                continue
            snaps = self.cluster.snapshots()
            exclude = w.exclude | self.cluster.quarantined_hosts(w.model)
            if all(s.name in exclude for s in snaps if w.model in s.installed and s.routable):
                # Everything that has it is quarantined: trying one beats failing outright.
                exclude = w.exclude
            d = choose(
                w.model,
                snaps,
                self.cluster.facts(w.model),
                queued_ahead=ahead.get(w.model, 0),
                rotation=self._rotation,
                exclude=exclude,
                warm_facts=warm,
                ctx=w.ctx,
            )
            if d.kind == Kind.DISPATCH:
                self._rotation += 1
                self.cluster.begin(d.host, w.model, cold=d.cold, evict=d.evict, ctx=w.ctx)
                lease = Lease(d.host, w.model, d, (time.monotonic() - w.enqueued) * 1000, ctx=w.ctx)
                w.future.set_result(lease)
                placed.append(w)
            elif d.kind == Kind.REJECT:
                msg = {
                    "model_not_found": "No ollama endpoints available",
                    "no_healthy_endpoints": "No healthy ollama endpoints available",
                }.get(d.reason, d.reason)
                w.future.set_exception(Rejected(d.status, d.reason, msg))
                placed.append(w)
            else:
                ahead[w.model] = ahead.get(w.model, 0) + 1
                if now - w.enqueued > self.head_of_line_after:
                    hol_blocked.add(w.model)
        for w in placed:
            self._remove(w)

    def _remove(self, w: _Waiter) -> None:
        try:
            self.waiting.remove(w)
        except ValueError:
            pass

    def depth_by_model(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for w in self.waiting:
            out[w.model] = out.get(w.model, 0) + 1
        return out
