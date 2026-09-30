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


@dataclass(order=True)
class _Waiter:
    sort_key: float
    seq: int
    model: str = field(compare=False)
    klass: str = field(compare=False)
    request_id: str = field(compare=False)
    exclude: frozenset[str] = field(compare=False)
    enqueued: float = field(compare=False)
    future: asyncio.Future = field(compare=False)


class Scheduler:
    def __init__(self, cluster: Cluster, log: EventLog, max_wait: float, boosts: dict[str, float]):
        self.cluster = cluster
        self.log = log
        self.max_wait = max_wait
        self.boosts = boosts
        self.waiting: list[_Waiter] = []
        self._seq = itertools.count()
        self._rotation = 0
        cluster.on_change = self.pump

    async def acquire(
        self, model: str, *, klass: str, request_id: str, exclude: frozenset[str] = frozenset()
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
        ahead: dict[str, int] = {}
        placed: list[_Waiter] = []
        for w in self.waiting:
            if w.future.done():
                placed.append(w)
                continue
            d = choose(
                w.model,
                self.cluster.snapshots(),
                self.cluster.facts(w.model),
                queued_ahead=ahead.get(w.model, 0),
                rotation=self._rotation,
                exclude=w.exclude,
                warm_facts=warm,
            )
            if d.kind == Kind.DISPATCH:
                self._rotation += 1
                self.cluster.begin(d.host, w.model, cold=d.cold, evict=d.evict)
                lease = Lease(d.host, w.model, d, (time.monotonic() - w.enqueued) * 1000)
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
