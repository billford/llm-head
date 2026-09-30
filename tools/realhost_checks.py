"""Scenario checks against real Ollama hosts, run on the head itself.

    python tools/realhost_checks.py --head http://127.0.0.1:40115 [--olla http://127.0.0.1:40114]

These cover what synthetic load doesn't: real image payloads shaped like a vision client's
requests, tool calls and JSON-schema output (as an agent sends), client disconnects, drain,
long context, and a burst larger than the cluster's slots. With --olla, the vision and tool
checks also run through Olla and the results are compared.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import struct
import sys
import threading
import time
import zlib

import httpx

QWEN = "qwen2.5vl:7b-q4_K_M"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")
    return ok


def png(width: int, height: int, seed: int = 7) -> bytes:
    """A photo-sized PNG: smooth color gradients with noise, so it compresses like a
    photo rather than a flat test card."""
    rng = random.Random(seed)
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            n = rng.randint(-24, 24)
            row += bytes((
                max(0, min(255, 40 + x * 180 // width + n)),
                max(0, min(255, 90 + y * 120 // height + n)),
                max(0, min(255, 200 - x * 100 // width + n)),
            ))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 6)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def vision_request(base: str, image_b64: str) -> dict:
    """The same request shape a vision client (photo_classifier) sends."""
    payload = {
        "model": QWEN,
        "prompt": "Describe this image. Respond with ONLY a JSON object with keys "
                  "\"description\" (one sentence) and \"tags\" (array of 3 lowercase strings).",
        "images": [image_b64],
        "options": {"num_ctx": 8192, "temperature": 0},
        "stream": True,
        "keep_alive": "30m",
    }
    t0 = time.monotonic()
    text, final = [], None
    with httpx.stream("POST", base + "/olla/ollama/api/generate", json=payload,
                      headers={"X-Olla-Session-ID": "realhost-check"}, timeout=600) as r:
        status = r.status_code
        endpoint = r.headers.get("x-olla-endpoint")
        for line in r.iter_lines():
            if not line:
                continue
            try:
                c = json.loads(line)
            except ValueError:
                # Balancer errors (e.g. Olla's "Proxy error: ...") are plain text.
                text.append(line)
                continue
            text.append(c.get("response", ""))
            if c.get("done"):
                final = c
    return {"status": status, "endpoint": endpoint, "ms": int((time.monotonic() - t0) * 1000),
            "text": "".join(text), "final": final or {}}


def vision(head: str, olla: str | None) -> None:
    img = png(1920, 1080)
    b64 = base64.b64encode(img).decode()
    print(f"      image: 1920x1080 PNG, {len(b64) / 1e6:.1f} MB base64")
    targets = [("llm-head", head)] + ([("olla", olla)] if olla else [])
    results = {}
    for label, base in targets:
        res = vision_request(base, b64)
        results[label] = res
        f = res["final"]
        ok = res["status"] == 200 and f.get("done_reason") == "stop" and len(res["text"]) > 20
        try:
            parsed = json.loads(res["text"][res["text"].find("{"): res["text"].rfind("}") + 1])
            ok = ok and "description" in parsed
        except ValueError:
            ok = False
        detail = (f"{res['ms']} ms on {res['endpoint']}, prompt_eval_count={f.get('prompt_eval_count')}, "
                  f"load={f.get('load_duration', 0) / 1e9:.1f}s, done_reason={f.get('done_reason')}")
        if res["status"] != 200:
            detail = f"HTTP {res['status']} after {res['ms']} ms: {res['text'][:100]}"
        check(f"vision 1920x1080 stream via {label}", ok, detail)
    if len(results) == 2:
        a, b = results["llm-head"]["final"], results["olla"]["final"]
        check("vision: same image token count through both", a.get("prompt_eval_count") == b.get("prompt_eval_count"),
              f"{a.get('prompt_eval_count')} vs {b.get('prompt_eval_count')}")


TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_sensor",
        "description": "Read a home sensor value",
        "parameters": {"type": "object", "properties": {"entity_id": {"type": "string"}}, "required": ["entity_id"]},
    },
}]


def tools_and_schema(base: str, label: str) -> None:
    msgs = [{"role": "user", "content": "What is the temperature of sensor.living_room_temp? Use the tool."}]
    r = httpx.post(base + "/olla/ollama/api/chat", timeout=300, json={
        "model": "gpt-oss:20b", "messages": msgs, "tools": TOOLS, "stream": False,
        "options": {"num_ctx": 8192}})
    calls = (r.json().get("message") or {}).get("tool_calls") if r.status_code == 200 else None
    check(f"gpt-oss tool call via {label}", bool(calls) and calls[0]["function"]["name"] == "get_sensor",
          f"HTTP {r.status_code}, tool_calls={json.dumps(calls)[:120] if calls else None}")
    if calls:
        msgs += [r.json()["message"], {"role": "tool", "content": "21.5 C"}]
        r2 = httpx.post(base + "/olla/ollama/api/chat", timeout=300, json={
            "model": "gpt-oss:20b", "messages": msgs, "tools": TOOLS, "stream": False, "options": {"num_ctx": 8192}})
        content = (r2.json().get("message") or {}).get("content", "") if r2.status_code == 200 else ""
        check(f"gpt-oss continues after tool result via {label}", "21.5" in content, content[:80].replace("\n", " "))
    schema = {"type": "object", "properties": {"room": {"type": "string", "enum": ["kitchen", "office"]},
                                               "on": {"type": "boolean"}}, "required": ["room", "on"]}
    r = httpx.post(base + "/olla/ollama/api/chat", timeout=300, json={
        "model": "gpt-oss:20b", "format": schema, "stream": False,
        "messages": [{"role": "user", "content": "Turn on the kitchen light. Reply as JSON."}]})
    try:
        obj = json.loads(r.json()["message"]["content"])
        ok = obj.get("room") in ("kitchen", "office") and isinstance(obj.get("on"), bool)
    except (ValueError, KeyError, TypeError):
        ok, obj = False, None
    check(f"gpt-oss JSON-schema output via {label}", ok, json.dumps(obj))


def inflight(head: str) -> dict:
    q = httpx.get(head + "/internal/queue", timeout=10).json()
    return {h: sum(v["inflight"].values()) for h, v in q["hosts"].items()} | {"waiting": len(q["waiting"])}


def disconnect(head: str) -> None:
    body = {"model": "llama3.2:3b", "prompt": "Count slowly from 1 to 500.", "stream": True,
            "options": {"num_predict": 2000}}
    with httpx.stream("POST", head + "/olla/ollama/api/generate", json=body, timeout=60) as r:
        host = r.headers.get("x-olla-endpoint")
        for i, _ in enumerate(r.iter_lines()):
            if i == 5:
                during = inflight(head)
                break
    # Leaving the context closes the connection mid-stream.
    time.sleep(1.5)
    after = inflight(head)
    check("client disconnect frees the slot", during.get(host, 0) >= 1 and after.get(host, 0) == 0,
          f"in flight on {host}: {during.get(host)} during, {after.get(host)} after")


def drain(head: str) -> None:
    hosts = list(httpx.get(head + "/internal/queue", timeout=10).json()["hosts"])
    victim = hosts[0]
    r = httpx.post(f"{head}/internal/hosts/{victim}/drain", timeout=10)
    try:
        seen = set()
        for _ in range(4):
            g = httpx.post(head + "/olla/ollama/api/generate", timeout=120, json={
                "model": "llama3.2:3b", "prompt": "Say ok", "stream": False, "options": {"num_predict": 3}})
            seen.add(g.headers.get("x-olla-endpoint"))
        status = {e["name"]: e["status"] for e in httpx.get(head + "/internal/status", timeout=10).json()["endpoints"]}
        check(f"drain {victim}: no new work there", r.status_code == 200 and victim not in seen,
              f"served by {sorted(seen)}, status shows {status.get(victim)}")
    finally:
        httpx.post(f"{head}/internal/hosts/{victim}/undrain", timeout=10)


def long_context(head: str) -> None:
    words = ("the cluster routes each request to a host that already has its model loaded " * 700).split()
    prompt = " ".join(words) + "\n\nIn one word: what does the cluster route?"
    r = httpx.post(head + "/olla/ollama/api/generate", timeout=600, json={
        "model": "llama3.1:8b", "prompt": prompt, "stream": False,
        "options": {"num_ctx": 16384, "num_predict": 10}})
    j = r.json() if r.status_code == 200 else {}
    check("16k context request", r.status_code == 200 and j.get("prompt_eval_count", 0) > 8000,
          f"HTTP {r.status_code}, prompt_eval_count={j.get('prompt_eval_count')}")


def burst(head: str) -> None:
    """More simultaneous requests than slots: the head must hold the excess."""
    hosts = httpx.get(head + "/internal/queue", timeout=10).json()["hosts"]
    peak = {h: 0 for h in hosts}
    peak_waiting = 0
    stop = threading.Event()

    def sample():
        nonlocal peak_waiting
        while not stop.is_set():
            q = httpx.get(head + "/internal/queue", timeout=10).json()
            for h, v in q["hosts"].items():
                peak[h] = max(peak[h], v["inflight"].get("qwen2.5vl:7b-q4_k_m", 0))
            peak_waiting = max(peak_waiting, len(q["waiting"]))
            time.sleep(0.05)

    sampler = threading.Thread(target=sample)
    sampler.start()
    results = []

    def one(i):
        r = httpx.post(head + "/olla/ollama/api/generate", timeout=300, json={
            "model": QWEN, "prompt": f"Write two sentences about lighthouse number {i}.", "stream": False,
            "options": {"num_predict": 60}})
        results.append(r.status_code)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    sampler.join()
    ok = all(s == 200 for s in results) and all(v <= 2 for v in peak.values()) and peak_waiting > 0
    check("12-request burst: never more than 2 per host, excess waits at the head", ok,
          f"statuses={sorted(set(results))}, peak per host={peak}, peak waiting={peak_waiting}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--head", required=True)
    p.add_argument("--olla")
    p.add_argument("--skip", default="", help="comma-separated: vision,tools,disconnect,drain,context,burst")
    a = p.parse_args()
    skip = set(a.skip.split(","))
    if "vision" not in skip:
        vision(a.head, a.olla)
    if "tools" not in skip:
        tools_and_schema(a.head, "llm-head")
        if a.olla:
            tools_and_schema(a.olla, "olla")
    for name, fn in (("disconnect", disconnect), ("drain", drain), ("context", long_context), ("burst", burst)):
        if name not in skip:
            fn(a.head)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
