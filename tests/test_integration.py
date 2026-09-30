"""End-to-end behavior against fake Ollama hosts (spec §4 and §5)."""

from __future__ import annotations

import asyncio
import json
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
