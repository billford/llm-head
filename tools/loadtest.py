"""Replay a seeded, realistic workload against a balancer and summarize what clients saw.

    python tools/loadtest.py --base http://127.0.0.1:40115 --requests 120 --concurrency 6 --seed 1

The model mix and output lengths default to one week of real traffic (2026-09-24..29):
qwen2.5vl 51%, llama3.2:3b 45%, gpt-oss:20b 3%, llama3.1:8b 1%. The same seed produces
the same request sequence, so two balancers can be compared on identical work.
Writes one JSON line per request to --out, and prints a summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from collections import Counter, defaultdict

import httpx

# model, weight, typical output tokens, prompt words
MIX = [
    ("qwen2.5vl:7b-q4_K_M", 51, 38, 700),
    ("llama3.2:3b", 45, 40, 120),
    ("gpt-oss:20b", 3, 100, 300),
    ("llama3.1:8b", 1, 60, 200),
]
WORDS = ("the quick brown fox jumps over a lazy dog while an agent reads logs and summarizes "
         "events from sensors in the house").split()


def workload(n: int, seed: int, gptoss_boost: float):
    rng = random.Random(seed)
    weights = [w * (gptoss_boost if m.startswith("gpt-oss") else 1) for m, w, _, _ in MIX]
    out = []
    for i in range(n):
        model, _, out_tokens, words = rng.choices(MIX, weights=weights)[0]
        prompt = " ".join(rng.choice(WORDS) for _ in range(words)) + f"\nRequest {i}: summarize briefly."
        out.append({
            "i": i,
            "model": model,
            "stream": rng.random() < 0.3,
            "body": {"model": model, "prompt": prompt, "keep_alive": "30m",
                     "options": {"num_predict": out_tokens, "temperature": 0}},
        })
    return out


async def one(client: httpx.AsyncClient, base: str, req: dict) -> dict:
    body = dict(req["body"], stream=req["stream"])
    t0 = time.monotonic()
    rec = {"i": req["i"], "model": req["model"], "stream": req["stream"]}
    try:
        async with client.stream("POST", base + "/olla/ollama/api/generate", json=body) as r:
            last = None
            async for line in r.aiter_lines():
                if line.strip():
                    last = line
            rec["status"] = r.status_code
            rec["endpoint"] = r.headers.get("x-olla-endpoint")
            rec["placement"] = r.headers.get("x-olla-routing-reason")
            rec["queued_ms"] = int(r.headers.get("x-llm-head-queue-ms", 0))
        final = json.loads(last) if last else {}
        rec["eval_count"] = final.get("eval_count", 0)
        ed = final.get("eval_duration") or 0
        rec["tok_s"] = round(rec["eval_count"] / (ed / 1e9), 1) if ed else 0.0
        rec["load_s"] = round((final.get("load_duration") or 0) / 1e9, 2)
        rec["error"] = final.get("error")
    except (httpx.HTTPError, ValueError) as exc:
        rec["status"] = 0
        rec["error"] = f"{type(exc).__name__}: {exc}"
    rec["ms"] = int((time.monotonic() - t0) * 1000)
    return rec


async def run(base: str, reqs: list[dict], concurrency: int, gap: float) -> list[dict]:
    sem = asyncio.Semaphore(concurrency)
    results = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as client:
        async def guarded(req):
            async with sem:
                results.append(await one(client, base, req))

        tasks = []
        for req in reqs:
            tasks.append(asyncio.create_task(guarded(req)))
            if gap:
                await asyncio.sleep(gap)
        await asyncio.gather(*tasks)
    return sorted(results, key=lambda r: r["i"])


def pctl(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0


def summarize(results: list[dict], wall: float) -> dict:
    ok = [r for r in results if r["status"] == 200 and not r.get("error")]
    by_model = defaultdict(list)
    for r in ok:
        by_model[r["model"]].append(r)
    summary = {
        "requests": len(results),
        "ok": len(ok),
        "errors": Counter(str(r.get("status")) + " " + str(r.get("error"))[:80] for r in results
                          if r not in ok).most_common(5),
        "wall_s": round(wall, 1),
        "split": dict(Counter(r["endpoint"] for r in ok)),
        "cold_loads": sum(1 for r in ok if r["load_s"] > 0.5),
        "models": {},
    }
    for m, rs in sorted(by_model.items()):
        ms = [r["ms"] for r in rs]
        tps = [r["tok_s"] for r in rs if r["eval_count"] > 10]
        summary["models"][m] = {
            "n": len(rs), "p50_ms": int(statistics.median(ms)), "p95_ms": pctl(ms, 0.95), "max_ms": max(ms),
            "min_tok_s": min(tps) if tps else None, "median_tok_s": statistics.median(tps) if tps else None,
            "cold": sum(1 for r in rs if r["load_s"] > 0.5),
        }
    gpt = [r for r in ok if r["model"].startswith("gpt-oss") and r["eval_count"] > 10]
    summary["gptoss_spilled"] = sum(1 for r in gpt if r["tok_s"] < 50)
    return summary


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--requests", type=int, default=100)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--gap", type=float, default=0.0, help="seconds between request starts")
    p.add_argument("--gptoss-boost", type=float, default=1.0, help="multiply gpt-oss's share of the mix")
    p.add_argument("--out", default=None)
    a = p.parse_args()
    reqs = workload(a.requests, a.seed, a.gptoss_boost)
    t0 = time.monotonic()
    results = asyncio.run(run(a.base, reqs, a.concurrency, a.gap))
    wall = time.monotonic() - t0
    if a.out:
        with open(a.out, "w") as f:
            for r in results:
                f.write(json.dumps(r) + "\n")
    print(json.dumps(summarize(results, wall), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
