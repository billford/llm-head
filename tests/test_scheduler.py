"""Queue ordering and slot accounting, without a network."""

from __future__ import annotations

import asyncio

import pytest

from llm_head.cluster import Cluster, InstalledModel
from llm_head.config import Config
from llm_head.placement import LoadedModel
from llm_head.scheduler import Rejected, Scheduler
from llm_head.stats import Stats

from .conftest import MemLog

M = "llama3.2:3b"


def make(slots=1, max_wait=2.0):
    cfg = Config.model_validate({
        "hosts": [{"name": "a", "url": "http://a:1", "vram_mb": 16000, "slots_per_model": slots}],
        "logging": {"file": None}, "stats_file": None,
    })
    log = MemLog()
    cluster = Cluster(cfg, Stats(None), log)
    h = cluster.hosts["a"]
    h.status = "healthy"
    h.installed = {M: InstalledModel(M, 3_000_000_000)}
    h.loaded = {M: LoadedModel(3_000_000_000)}
    sched = Scheduler(cluster, log, max_wait, {"interactive": 30.0, "batch": 0.0})
    return sched


async def test_interactive_jumps_ahead_of_batch():
    s = make()
    first = await s.acquire(M, klass="batch", request_id="b0")
    order = []

    async def req(rid, klass):
        lease = await s.acquire(M, klass=klass, request_id=rid)
        order.append(rid)
        s.release(lease, ok=True, duration_ms=1, nbytes=0)

    tasks = [asyncio.create_task(req("b1", "batch"))]
    await asyncio.sleep(0.01)
    tasks.append(asyncio.create_task(req("i1", "interactive")))
    await asyncio.sleep(0.01)
    s.release(first, ok=True, duration_ms=1, nbytes=0)
    await asyncio.gather(*tasks)
    assert order == ["i1", "b1"]


async def test_batch_is_not_starved_after_boost_window():
    s = make()
    s.boosts["interactive"] = 0.05
    first = await s.acquire(M, klass="batch", request_id="b0")
    order = []

    async def req(rid, klass):
        lease = await s.acquire(M, klass=klass, request_id=rid)
        order.append(rid)
        s.release(lease, ok=True, duration_ms=1, nbytes=0)

    t_batch = asyncio.create_task(req("b1", "batch"))
    await asyncio.sleep(0.1)  # batch has now waited longer than the interactive boost
    t_int = asyncio.create_task(req("i1", "interactive"))
    await asyncio.sleep(0.01)
    s.release(first, ok=True, duration_ms=1, nbytes=0)
    await asyncio.gather(t_batch, t_int)
    assert order == ["b1", "i1"]


async def test_slots_are_returned_on_release():
    s = make(slots=2)
    a = await s.acquire(M, klass="batch", request_id="1")
    b = await s.acquire(M, klass="batch", request_id="2")
    assert s.cluster.hosts["a"].inflight[M] == 2
    waiter = asyncio.create_task(s.acquire(M, klass="batch", request_id="3"))
    await asyncio.sleep(0.01)
    assert not waiter.done() and len(s.waiting) == 1
    s.release(a, ok=True, duration_ms=1, nbytes=0)
    c = await waiter
    assert c.host == "a" and s.cluster.hosts["a"].inflight[M] == 2
    s.release(b, ok=True, duration_ms=1, nbytes=0)
    s.release(c, ok=True, duration_ms=1, nbytes=0)
    assert s.cluster.hosts["a"].inflight[M] == 0


async def test_cancelled_waiter_leaves_the_queue_and_leaks_no_slot():
    s = make()
    a = await s.acquire(M, klass="batch", request_id="1")
    waiter = asyncio.create_task(s.acquire(M, klass="batch", request_id="2"))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert s.waiting == []
    s.release(a, ok=True, duration_ms=1, nbytes=0)
    assert s.cluster.hosts["a"].inflight[M] == 0


async def test_queue_timeout_raises_503():
    s = make(max_wait=0.05)
    await s.acquire(M, klass="batch", request_id="1")
    with pytest.raises(Rejected) as e:
        await s.acquire(M, klass="batch", request_id="2")
    assert (e.value.status, e.value.reason) == (503, "queue_timeout")
    assert s.waiting == []
