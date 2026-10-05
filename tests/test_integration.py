"""End-to-end behavior against fake Ollama hosts (spec §4 and §5)."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter

from .conftest import cluster

QWEN = "qwen2.5vl:7b-q4_K_M"
GEN = "/olla/ollama/api/generate"


def gen(model=QWEN, n=4, stream=False, **extra):
    return {"model": model, "prompt": "hi", "stream": stream, "options": {"num_predict": n}, **extra}


async def warm_everywhere(head, fakes, model=QWEN):
    """Load `model` on every fake host and wait until the head has seen it via /api/ps."""
    from llm_head.names import normalize

    for f in fakes:
        await f._ensure_loaded(model)
    for _ in range(100):
        if all(normalize(model) in h.loaded for h in head.cluster.hosts.values()):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("head never saw the model loaded everywhere")


async def test_generate_nonstream_roundtrip_and_logs():
    async with cluster() as (c, head, fakes, log):
        r = await c.post(GEN, json=gen())
        assert r.status_code == 200
        body = r.json()
        assert body["done"] is True and body["eval_count"] == 4
        assert r.headers["x-olla-endpoint"] in {"xmas", "european"}
        assert r.headers["x-olla-model"] == "qwen2.5vl:7b-q4_k_m"
        assert r.headers["x-olla-request-id"]
        await asyncio.sleep(0.05)
        done = log.find("Request completed")
        assert done and done[-1]["output_tokens"] == 4 and done[-1]["input_tokens"] == 10
        access = [a for a in log.find("Access log") if a["path"] == "/api/generate"]
        assert access and access[-1]["status"] == 200


async def test_streaming_is_relayed_line_by_line():
    async with cluster() as (c, head, fakes, log):
        lines = []
        async with c.stream("POST", GEN, json=gen(n=6, stream=True)) as r:
            assert r.headers["content-type"].startswith("application/x-ndjson")
            async for line in r.aiter_lines():
                if line:
                    lines.append(json.loads(line))
        assert len(lines) == 7 and lines[-1]["done"] is True


async def test_unknown_model_gets_olla_404():
    async with cluster() as (c, head, fakes, log):
        r = await c.post(GEN, json=gen(model="does-not-exist:1b"))
        assert r.status_code == 404
        assert r.text == "No ollama endpoints available\n"
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        assert log.find("Model routing rejected request")


async def test_serial_requests_alternate_when_model_is_warm_on_both_hosts():
    """Olla sent 68% of serial traffic to xmas because 0/0 ties went to the first host."""
    async with cluster() as (c, head, fakes, log):
        await warm_everywhere(head, fakes)
        hosts = Counter()
        for _ in range(10):
            r = await c.post(GEN, json=gen())
            hosts[r.headers["x-olla-endpoint"]] += 1
        assert hosts["xmas"] == hosts["european"] == 5


async def test_never_sends_more_than_slots_to_a_host():
    """Olla left excess requests in Ollama's hidden queue; the head should hold them instead."""
    async with cluster() as (c, head, fakes, log):
        for f in fakes:
            f.token_seconds = 0.02
        rs = await asyncio.gather(*(c.post(GEN, json=gen(n=5)) for _ in range(12)))
        assert all(r.status_code == 200 for r in rs)
        assert all(f.max_concurrent <= 2 for f in fakes), [f.max_concurrent for f in fakes]
        assert log.find("Request queued")


async def test_big_model_does_not_spill_next_to_busy_model():
    """The 12 tok/s gpt-oss runs: it was loaded beside a busy qwen and spilled onto the CPU."""
    async with cluster() as (c, head, fakes, log):
        for f in fakes:
            f.token_seconds = 0.01
        # Keep qwen busy on both hosts.
        busy = [asyncio.create_task(c.post(GEN, json=gen(n=60))) for _ in range(2)]
        await asyncio.sleep(0.15)
        r = await c.post(GEN, json=gen(model="gpt-oss:20b", n=4))
        await asyncio.gather(*busy)
        assert r.status_code == 200
        assert sum(f.spills for f in fakes) == 0


async def test_retries_on_another_host_before_first_byte():
    async with cluster() as (c, head, fakes, log):
        await warm_everywhere(head, fakes)
        fakes[0].fail_next = fakes[1].fail_next = 100
        # Every host fails: one attempt per host, then Olla's plain-text 502.
        r = await c.post(GEN, json=gen())
        assert r.status_code == 502 and r.text.startswith("Proxy error: ")
        fakes[1].fail_next = 0
        for _ in range(3):
            r = await c.post(GEN, json=gen())
            assert (r.status_code, r.headers["x-olla-endpoint"]) == (200, "european")
        assert any(f.get("will_retry") for f in log.find("Request failed"))
        # An HTTP 503 from Ollama is not a network failure and must not take the host offline.
        assert head.cluster.hosts["xmas"].status == "healthy"


async def test_offline_host_is_skipped():
    async with cluster() as (c, head, fakes, log):
        fakes[0].healthy = False
        for _ in range(50):
            if head.cluster.hosts["xmas"].status == "offline":
                break
            await asyncio.sleep(0.05)
        assert head.cluster.hosts["xmas"].status == "offline"
        for _ in range(4):
            r = await c.post(GEN, json=gen())
            assert r.headers["x-olla-endpoint"] == "european"
        assert log.find("Endpoint status changed: xmas")


async def test_slow_health_check_under_threshold_does_not_flap():
    """xmas was marked offline 4 times in a week when a 1s check hit a busy host."""
    async with cluster() as (c, head, fakes, log):
        fakes[0].health_delay = 0.15  # just over the 100ms timeout, once
        await asyncio.sleep(0.25)
        fakes[0].health_delay = 0
        await asyncio.sleep(0.5)
        assert not [r for r in log.find("Endpoint status changed: xmas") if r["status"] == "offline"]


async def test_queue_timeout_returns_503_with_retry_after():
    async with cluster(head_overrides={"queue": {"max_wait": "300ms"}}) as (c, head, fakes, log):
        for f in fakes:
            f.token_seconds = 0.05
        busy = [asyncio.create_task(c.post(GEN, json=gen(n=40))) for _ in range(4)]
        await asyncio.sleep(0.1)
        r = await c.post(GEN, json=gen())
        assert r.status_code == 503
        assert int(r.headers["retry-after"]) >= 1
        assert r.json()["reason"] == "queue_timeout"
        await asyncio.gather(*busy)
        # Monitoring watches this counter; client-visible failures must show up in it.
        assert (await c.get("/internal/status")).json()["system"]["total_failures"] >= 1


async def test_drain_stops_new_work_on_a_host():
    async with cluster() as (c, head, fakes, log):
        await warm_everywhere(head, fakes)
        r = await c.post("/internal/hosts/xmas/drain")
        assert r.status_code == 200 and r.json()["draining"] is True
        for _ in range(4):
            assert (await c.post(GEN, json=gen())).headers["x-olla-endpoint"] == "european"
        await c.post("/internal/hosts/xmas/undrain")
        seen = {(await c.post(GEN, json=gen())).headers["x-olla-endpoint"] for _ in range(4)}
        assert "xmas" in seen


async def test_model_name_case_and_latest_tag_are_normalized():
    async with cluster() as (c, head, fakes, log):
        r1 = await c.post(GEN, json=gen(model="QWEN2.5VL:7B-Q4_K_M"))
        r2 = await c.post("/olla/ollama/api/embed", json={"model": "nomic-embed-text", "input": "x"})
        assert r1.status_code == 200 and r2.status_code == 200


async def test_openai_chat_passthrough_parses_usage():
    async with cluster() as (c, head, fakes, log):
        r = await c.post("/olla/ollama/v1/chat/completions",
                         json={"model": "llama3.2:3b", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 3})
        assert r.status_code == 200 and r.json()["usage"]["completion_tokens"] == 3
        await asyncio.sleep(0.05)
        assert log.find("Request completed")[-1]["output_tokens"] == 3


async def test_non_inference_calls_skip_the_queue():
    async with cluster() as (c, head, fakes, log):
        assert (await c.get("/olla/ollama/api/ps")).status_code == 200
        assert (await c.get("/olla/ollama/api/version")).status_code == 200
        r = await c.post("/olla/ollama/api/show", json={"model": QWEN})
        assert r.status_code == 200
        assert not log.find("Request dispatching")


async def test_cold_load_learns_model_footprint():
    async with cluster() as (c, head, fakes, log):
        await c.post(GEN, json=gen(model="llama3.2:3b"))
        await asyncio.sleep(0.2)
        assert head.stats.get("llama3.2:3b").vram_bytes == 3_097_881_476


async def test_shadow_mode_never_unloads_models_itself():
    over = {"scheduling": {"evict": False, "keep_warm": False}}
    async with cluster(head_overrides=over) as (c, head, fakes, log):
        for f in fakes:
            await f._ensure_loaded(QWEN)
            await f._ensure_loaded("llama3.2:3b")
        await asyncio.sleep(0.2)
        r = await c.post(GEN, json=gen(model="gpt-oss:20b"))
        assert r.status_code == 200
        assert not log.find("Evicting model")
        assert not head._background or all("warm" not in repr(t) for t in head._background)


async def test_wedged_host_is_detected_retried_and_quarantined():
    """2026-09-30: Ollama on xmas stopped loading models for 5 hours while passing health
    checks. A request needing a load there must fail over quickly, and later requests for
    that model must avoid xmas."""
    over = {"proxy": {"load_timeout": "1s", "model_quarantine": "60s"},
            "models": {"llama3.2:3b": {"home": ["xmas"]}}}
    async with cluster(head_overrides=over) as (c, head, fakes, log):
        head.stats.get("llama3.2:3b").load_time = 0.05
        fakes[0].wedged = True
        t0 = asyncio.get_running_loop().time()
        r = await c.post(GEN, json=gen(model="llama3.2:3b"))
        first = asyncio.get_running_loop().time() - t0
        assert r.status_code == 200 and r.headers["x-olla-endpoint"] == "european"
        assert 1.0 <= first < 4.0, first
        q = log.find("Model quarantined on endpoint")
        assert q and q[-1]["endpoint"] == "xmas" and q[-1]["model"] == "llama3.2:3b"
        # xmas stays healthy for everything else, and llama now goes straight to european.
        assert head.cluster.hosts["xmas"].status == "healthy"
        t0 = asyncio.get_running_loop().time()
        r = await c.post(GEN, json=gen(model="llama3.2:3b"))
        assert r.headers["x-olla-endpoint"] == "european"
        assert asyncio.get_running_loop().time() - t0 < 0.8


async def test_context_reload_is_not_starved_by_steady_traffic():
    """The warmup/probe traffic at 4096 kept qwen busy while an 8192 analysis waited for a
    reload that never came. The head must get the 8192 request served."""
    over = {"queue": {"head_of_line_after": "300ms", "max_wait": "10s"}}
    async with cluster(head_overrides=over) as (c, head, fakes, log):
        for f in fakes:
            f.token_seconds = 0.01
        await warm_everywhere(head, fakes)
        stop = asyncio.Event()

        async def steady():
            while not stop.is_set():
                await c.post(GEN, json=gen(n=20))

        pumps = [asyncio.create_task(steady()) for _ in range(6)]
        await asyncio.sleep(0.3)
        t0 = asyncio.get_running_loop().time()
        r = await c.post(GEN, json=gen(n=5, options={"num_predict": 5, "num_ctx": 8192}))
        took = asyncio.get_running_loop().time() - t0
        stop.set()
        await asyncio.gather(*pumps)
        assert r.status_code == 200, r.text
        assert took < 3.0, took
        # It was served by reloading qwen at 8192 on a host where qwen was idle.
        big = [d for d in log.find("Request dispatching") if d.get("num_ctx") == 8192]
        assert big and big[-1]["placement"] == "reload_context", big


async def test_keep_warm_uses_the_context_size_requests_actually_use():
    """Canary review 2026-10-03: photos arrive in pairs at num_ctx 8192. Warming qwen at
    the 4096 default left only one host usable at 8192, so both photos of a pair shared
    one GPU (~13.5s each instead of ~9.5s on separate GPUs)."""
    over = {"models": {QWEN: {"keep_warm": 2}}}
    async with cluster(head_overrides=over) as (c, head, fakes, log):
        big = {"num_predict": 4, "num_ctx": 8192}
        for _ in range(3):
            assert (await c.post(GEN, json=gen(options=big))).status_code == 200
        assert head.stats.usual_ctx("qwen2.5vl:7b-q4_k_m") == 8192
        for host, model, ctx in head.warm_targets():
            await head.warm(host, model, ctx)
        await asyncio.sleep(0.2)
        assert all(f.loaded.get(QWEN) and f.loaded[QWEN].ctx == 8192 for f in fakes)
        assert head.warm_targets() == []
        # Copies at 4096 that were still loaded while 8192 ones replaced them are not
        # mistaken for qwen's maximum context.
        assert head.stats.max_ctx == {}
        # A pair of 8192 requests now lands on both hosts.
        for f in fakes:
            f.token_seconds = 0.02
        rs = await asyncio.gather(*(c.post(GEN, json=gen(n=20, options={"num_predict": 20, "num_ctx": 8192}))
                                    for _ in range(2)))
        assert {r.headers["x-olla-endpoint"] for r in rs} == {"xmas", "european"}


def test_usual_ctx_follows_recent_traffic():
    from llm_head.stats import Stats

    s = Stats(None)
    for _ in range(10):
        s.observe_request("m", 1.0, ctx=4096)
    assert s.usual_ctx("m") == 4096
    for _ in range(25):
        s.observe_request("m", 1.0, ctx=8192)
    assert s.usual_ctx("m") == 8192


async def test_keep_warm_prefers_the_home_host():
    over = {"models": {QWEN: {"keep_warm": 1, "num_ctx": 8192, "home": ["european"]}}}
    async with cluster(head_overrides=over) as (c, head, fakes, log):
        assert head.warm_targets() == [("european", "qwen2.5vl:7b-q4_k_m", 8192)]


async def _wait_loaded(head, host, model, present=True):
    from llm_head.names import normalize

    for _ in range(100):
        if (normalize(model) in head.cluster.hosts[host].loaded) == present:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{model} {'never appeared' if present else 'never left'} on {host}")


async def test_keep_warm_moves_a_stray_copy_back_home():
    """24-hour check 2026-10-04: after a drain, qwen (keep_warm 1, home xmas) stayed warm
    on european. Keep-warm counted one copy and was satisfied, so gpt-oss lost its home."""
    from llm_head import app as app_mod

    q = "qwen2.5vl:7b-q4_k_m"
    over = {"models": {QWEN: {"keep_warm": 1, "num_ctx": 4096, "home": ["xmas"]}}}
    async with cluster(head_overrides=over) as (c, head, fakes, log):
        await fakes[1]._ensure_loaded(QWEN)
        await _wait_loaded(head, "european", QWEN)
        # One copy meets the target, but it's away from home: warm one at home.
        assert head.warm_targets() == [("xmas", q, 4096)]
        assert head.rehome_evictions() == []  # never unload before the home copy is warm
        await head.warm("xmas", q, 4096)
        await _wait_loaded(head, "xmas", QWEN)
        assert head.warm_targets() == []
        # The stray copy was just used: keep it for now, a burst may want it.
        eu = head.cluster.hosts["european"]
        eu.last_used[q] = time.monotonic()
        assert head.rehome_evictions() == []
        # Idle long enough: unload it.
        eu.last_used[q] = time.monotonic() - app_mod.REHOME_IDLE - 1
        assert head.rehome_evictions() == [("european", q)]
        # Busy: never.
        eu.inflight[q] += 1
        assert head.rehome_evictions() == []
        eu.inflight[q] -= 1
        # A full round unloads it on the host, and placement stops using it at once.
        await head.keep_warm_round()
        assert q in eu.pending_evictions
        await _wait_loaded(head, "european", QWEN, present=False)
        assert QWEN not in fakes[1].loaded and QWEN in fakes[0].loaded
        assert log.find("Rehoming model")
        r = await c.post(GEN, json=gen())
        assert r.headers["x-olla-endpoint"] == "xmas" and r.headers["x-olla-routing-reason"] == "loaded"


async def test_keep_warm_rehoming_never_evicts_to_make_room_at_home():
    over = {"models": {QWEN: {"keep_warm": 1, "num_ctx": 4096, "home": ["xmas"]}}}
    async with cluster(head_overrides=over) as (c, head, fakes, log):
        await fakes[1]._ensure_loaded(QWEN)
        await fakes[0]._ensure_loaded("llama3.2:3b")
        await fakes[0]._ensure_loaded("nomic-embed-text:latest")
        await _wait_loaded(head, "xmas", "nomic-embed-text:latest")
        # xmas holds max_loaded_models already: wait for room rather than evict.
        assert head.warm_targets() == []
        assert head.rehome_evictions() == []


async def test_keep_warm_rehoming_is_off_without_eviction():
    from llm_head import app as app_mod

    q = "qwen2.5vl:7b-q4_k_m"
    over = {"models": {QWEN: {"keep_warm": 1, "num_ctx": 4096, "home": ["xmas"]}},
            "scheduling": {"evict": False}}
    async with cluster(head_overrides=over) as (c, head, fakes, log):
        for f in fakes:
            await f._ensure_loaded(QWEN)
        await _wait_loaded(head, "xmas", QWEN)
        await _wait_loaded(head, "european", QWEN)
        head.cluster.hosts["european"].last_used[q] = time.monotonic() - app_mod.REHOME_IDLE - 1
        assert head.rehome_evictions() == []


async def _spill_llama_on_european(head, fakes):
    """Recreate european on 2026-10-04: gpt-oss and llama3.2 loaded together on a GPU that
    really holds about 13.9 GiB of models, though the config says 15.4 GiB."""
    xmas, european = fakes
    await xmas._ensure_loaded("llama3.2:3b")
    await european._ensure_loaded("gpt-oss:20b")
    await european._ensure_loaded("llama3.2:3b")
    assert european.loaded["llama3.2:3b"].spilled
    eu = head.cluster.hosts["european"]
    for _ in range(100):
        if eu.vram_capacity and "llama3.2:3b" in head.cluster.hosts["xmas"].loaded:
            return eu
        await asyncio.sleep(0.02)
    raise AssertionError("head never saw the spilled load")


def _fakes_with_small_european():
    from .fake_ollama import FakeOllama, GiB

    return [FakeOllama(), FakeOllama(vram_bytes=int(13.93 * GiB), evict_to_fit=False)]


async def test_spilled_copy_is_noticed_and_avoided():
    async with cluster(fakes=_fakes_with_small_european()) as (c, head, fakes, log):
        eu = await _spill_llama_on_european(head, fakes)
        assert eu.loaded["llama3.2:3b"].spilled
        (warn,) = log.find("Model spilled onto CPU")
        assert (warn["endpoint"], warn["model"]) == ("european", "llama3.2:3b")
        assert warn["size_vram"] < warn["size"]
        # What fit on the GPU becomes european's capacity.
        assert eu.vram_capacity == sum(x.on_gpu for x in fakes[1].loaded.values())
        assert eu.usable_vram_bytes < eu.cfg.usable_vram_bytes
        assert log.find("Endpoint GPU capacity learned")
        q = (await c.get("/internal/queue")).json()["hosts"]
        assert q["european"]["spilled"] == ["llama3.2:3b"] and q["xmas"]["spilled"] == []
        assert q["european"]["vram_usable_mb"] < q["xmas"]["vram_usable_mb"]
        # Every request goes to xmas's full copy, though european is just as idle.
        rs = [await c.post(GEN, json=gen(model="llama3.2:3b")) for _ in range(6)]
        assert {r.headers["x-olla-endpoint"] for r in rs} == {"xmas"}
        assert fakes[1].requests == 0


async def test_keep_warm_does_not_warm_where_the_model_would_spill():
    over = {"models": {"llama3.2:3b": {"keep_warm": 2}}}
    async with cluster(head_overrides=over, fakes=_fakes_with_small_european()) as (c, head, fakes, log):
        eu = await _spill_llama_on_european(head, fakes)
        # The spilled copy doesn't count as warm, but rewarming it in place can't help.
        assert head.warm_targets() == []
        # Once it expires, the learned capacity still says it won't fit beside gpt-oss.
        del fakes[1].loaded["llama3.2:3b"]
        for _ in range(100):
            if "llama3.2:3b" not in eu.loaded:
                break
            await asyncio.sleep(0.02)
        assert head.warm_targets() == []
        # With gpt-oss gone, there's room again.
        del fakes[1].loaded["gpt-oss:20b"]
        for _ in range(100):
            if not eu.loaded:
                break
            await asyncio.sleep(0.02)
        assert head.warm_targets() == [("european", "llama3.2:3b", 4096)]


async def test_learned_capacity_can_be_reset():
    async with cluster(fakes=_fakes_with_small_european()) as (c, head, fakes, log):
        eu = await _spill_llama_on_european(head, fakes)
        assert eu.usable_vram_bytes < eu.cfg.usable_vram_bytes
        # Clear the spill first, or the next polls would learn the capacity again.
        del fakes[1].loaded["llama3.2:3b"]
        for _ in range(100):
            if "llama3.2:3b" not in eu.loaded:
                break
            await asyncio.sleep(0.02)
        r = await c.post("/internal/hosts/european/reset-capacity")
        assert r.status_code == 200
        assert r.json()["vram_usable_mb"] == eu.cfg.usable_vram_bytes // (1024 * 1024)
        assert eu.vram_capacity is None and "european" not in head.stats.host_vram
        assert log.find("Endpoint GPU capacity reset")
        assert (await c.post("/internal/hosts/nowhere/reset-capacity")).status_code == 404


async def test_a_lingering_spill_does_not_shrink_capacity():
    """2026-10-05 21:37: gpt-oss was unloaded from european, the spilled llama3.2 copy
    stayed partly on the CPU (Ollama doesn't move it back), and the head took its 2.05 GiB
    as european's whole capacity. newshelper's retrieval then timed out."""
    async with cluster(fakes=_fakes_with_small_european()) as (c, head, fakes, log):
        eu = await _spill_llama_on_european(head, fakes)
        learned = eu.vram_capacity
        del fakes[1].loaded["gpt-oss:20b"]
        await _wait_loaded(head, "european", "gpt-oss:20b", present=False)
        await asyncio.sleep(0.3)  # several more polls of the lingering spill
        assert eu.loaded["llama3.2:3b"].spilled
        assert eu.vram_capacity == learned and head.stats.host_vram["european"] == learned
        assert len(log.find("Endpoint GPU capacity learned")) == 1


async def test_a_spill_implying_a_tiny_gpu_is_not_learned():
    """Under half the configured memory is more likely another GPU user than a limit."""
    import httpx

    from llm_head.cluster import Cluster
    from llm_head.config import Config
    from llm_head.stats import Stats

    from .conftest import MemLog

    GiB = 1024**3
    tiny = {"models": [{"name": "llama3.2:3b", "size": 3 * GiB, "size_vram": 2 * GiB}]}
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=tiny)))
    cfg = Config.model_validate({"hosts": [{"name": "eu", "url": "http://eu", "vram_mb": 16311}],
                                 "logging": {"file": None}, "stats_file": None})
    log = MemLog()
    c = Cluster(cfg, Stats(None), log, client=client)
    h = c.hosts["eu"]
    for _ in range(3):
        await c.refresh_ps(h)
    assert h.vram_capacity is None and h.usable_vram_bytes == h.cfg.usable_vram_bytes
    assert len(log.find("Endpoint GPU capacity not learned")) == 1


async def test_a_model_capped_below_the_default_context_is_not_reloaded_every_time():
    """nomic-embed-text tops out at 2048 context. Ollama loads it at 2048 whatever is
    asked, so expecting the 4096 default made every embed request a forced reload."""
    async with cluster() as (c, head, fakes, log):
        body = {"model": "nomic-embed-text", "input": "x"}
        first = await c.post("/olla/ollama/api/embed", json=body)
        assert first.status_code == 200 and first.headers["x-llm-head-cold-load"] == "true"
        for _ in range(100):
            if head.stats.max_ctx.get("nomic-embed-text:latest") == 2048:
                break
            await asyncio.sleep(0.02)
        (capped,) = log.find("Model context capped")
        assert (capped["requested"], capped["context_length"]) == (4096, 2048)
        host = first.headers["x-olla-endpoint"]
        await asyncio.sleep(0.2)
        loads = sum(f.loads for f in fakes)
        for _ in range(3):
            r = await c.post("/olla/ollama/api/embed", json=body)
            assert r.headers["x-olla-endpoint"] == host and r.headers["x-olla-routing-reason"] == "loaded"
        assert sum(f.loads for f in fakes) == loads and sum(f.evictions for f in fakes) == 0


def test_host_capacity_learning_and_persistence(tmp_path):
    from llm_head.stats import Stats

    path = str(tmp_path / "stats.json")
    s = Stats(path)
    assert s.observe_host_vram("eu", 15 * 2**30, spilled=False) is None  # no spill, nothing learned
    assert s.observe_host_vram("eu", 14 * 2**30, spilled=True) == 14 * 2**30
    assert s.observe_host_vram("eu", 14 * 2**30 + 1, spilled=True) is None  # never raised by a spill
    assert s.observe_host_vram("eu", 13 * 2**30, spilled=False) is None
    assert s.observe_host_vram("eu", 14.5 * 2**30, spilled=False) == 14.5 * 2**30  # a bigger full load
    s.save()
    assert Stats(path).host_vram == {"eu": 14.5 * 2**30}


async def test_capacity_is_learned_only_from_a_spill_seen_twice():
    """A poll can catch Ollama mid-load; one odd reading mustn't shrink a host for good."""
    import httpx

    from llm_head.cluster import Cluster
    from llm_head.config import Config
    from llm_head.stats import Stats

    from .conftest import MemLog

    GiB = 1024**3
    spilled = {"models": [{"name": "gpt-oss:20b", "size": 12 * GiB, "size_vram": 12 * GiB},
                          {"name": "llama3.2:3b", "size": 3 * GiB, "size_vram": 2 * GiB}]}
    full = {"models": [{"name": "gpt-oss:20b", "size": 12 * GiB, "size_vram": 12 * GiB}]}
    replies = [spilled, full, spilled, spilled]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=replies.pop(0))))
    cfg = Config.model_validate({"hosts": [{"name": "eu", "url": "http://eu", "vram_mb": 16311}],
                                 "logging": {"file": None}, "stats_file": None})
    log = MemLog()
    c = Cluster(cfg, Stats(None), log, client=client)
    h = c.hosts["eu"]
    await c.refresh_ps(h)
    assert h.loaded["llama3.2:3b"].spilled and h.vram_capacity is None
    assert len(log.find("Model spilled onto CPU")) == 1
    await c.refresh_ps(h)
    assert h.vram_capacity is None and log.find("Model fully on GPU again") == []
    await c.refresh_ps(h)
    assert h.vram_capacity is None
    await c.refresh_ps(h)
    assert h.vram_capacity == 14 * GiB and h.usable_vram_bytes == 14 * GiB
    assert len(log.find("Model spilled onto CPU")) == 2
