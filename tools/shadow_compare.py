"""Send identical requests to Olla and llm-head and compare what a client would see.

    python tools/shadow_compare.py --olla http://127.0.0.1:40114 --head http://127.0.0.1:40115

For each request it checks the status code, Content-Type, the X-Olla-* headers, the set
of JSON field paths (llm-head may add fields, never drop them), and exact text for
plain-text errors. Generation requests use num_predict=5 so they're cheap.
Exit code is 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

SMALL = {"num_predict": 5}
CASES = [
    ("GET", "/internal/health", None),
    ("GET", "/internal/status", None),
    ("GET", "/internal/status/endpoints", None),
    ("GET", "/internal/status/models", None),
    ("GET", "/olla/models", None),
    ("GET", "/olla/ollama/api/tags", None),
    ("GET", "/olla/ollama/v1/models", None),
    ("GET", "/olla/ollama/api/ps", None),
    ("GET", "/olla/ollama/api/version", None),
    ("GET", "/nonexistent", None),
    ("POST", "/olla/ollama/api/generate", {"model": "llama3.2:3b", "prompt": "Say ok", "stream": False, "options": SMALL}),
    ("POST", "/olla/ollama/api/generate", {"model": "llama3.2:3b", "prompt": "Say ok", "stream": True, "options": SMALL}),
    ("POST", "/olla/ollama/api/chat", {"model": "llama3.2:3b", "messages": [{"role": "user", "content": "Say ok"}],
                                       "stream": False, "options": SMALL}),
    ("POST", "/olla/ollama/api/chat", {"model": "qwen2.5vl:7b-q4_K_M", "messages": [{"role": "user", "content": "Say ok"}],
                                       "stream": False, "options": SMALL}),
    ("POST", "/olla/ollama/api/embed", {"model": "nomic-embed-text:latest", "input": "hello"}),
    ("POST", "/olla/ollama/v1/chat/completions", {"model": "llama3.2:3b", "messages": [{"role": "user", "content": "Say ok"}],
                                                  "max_tokens": 5}),
    ("POST", "/olla/ollama/api/generate", {"model": "does-not-exist:1b", "prompt": "x", "stream": False}),
    ("POST", "/olla/ollama/api/generate", b"{not json"),
    ("POST", "/olla/ollama/api/pull", {"model": "llama3.2:3b"}),
    ("OPTIONS", "/olla/ollama/api/chat", "preflight"),
]
MODEL_HEADERS = ["X-Olla-Endpoint", "X-Olla-Backend-Type", "X-Olla-Model", "X-Olla-Request-Id",
                 "X-Olla-Response-Time", "X-Olla-Routing-Decision", "X-Ratelimit-Limit"]
PREFLIGHT_HEADERS = ["Access-Control-Allow-Origin", "Access-Control-Allow-Methods",
                     "Access-Control-Allow-Headers", "Access-Control-Max-Age"]
# Fields whose keys are data (model family names) rather than schema.
DATA_KEYED = ("models_by_family.",)


def keyset(obj, prefix=""):
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(prefix + k)
            out |= keyset(v, prefix + k + ".")
    elif isinstance(obj, list) and obj:
        out |= keyset(obj[0], prefix + "[].")
    return out


def body_json(r: httpx.Response):
    text = r.text.strip()
    try:
        return json.loads(text)
    except ValueError:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        try:
            return json.loads(lines[-1]) if lines else None
        except ValueError:
            return None


def send(client: httpx.Client, base: str, method: str, path: str, body):
    headers = {}
    if body == "preflight":
        headers = {"Origin": "http://example.com", "Access-Control-Request-Method": "POST",
                   "Access-Control-Request-Headers": "content-type"}
        return client.request(method, base + path, headers=headers)
    if isinstance(body, bytes):
        return client.request(method, base + path, content=body, headers={"content-type": "application/json"})
    return client.request(method, base + path, json=body)


def compare(a: httpx.Response, b: httpx.Response, method: str, path: str, body) -> list[str]:
    problems = []
    if a.status_code != b.status_code:
        problems.append(f"status {a.status_code} vs {b.status_code}")
    cta = a.headers.get("content-type", "").split(";")[0]
    ctb = b.headers.get("content-type", "").split(";")[0]
    if cta != ctb:
        problems.append(f"content-type {cta!r} vs {ctb!r}")
    if body == "preflight":
        for h in PREFLIGHT_HEADERS:
            if a.headers.get(h) != b.headers.get(h):
                problems.append(f"{h}: {a.headers.get(h)!r} vs {b.headers.get(h)!r}")
        return problems
    for h in MODEL_HEADERS:
        if h in a.headers and h not in b.headers:
            problems.append(f"missing header {h}")
    if "X-Olla-Model" in a.headers and a.headers.get("X-Olla-Model") != b.headers.get("X-Olla-Model"):
        problems.append(f"X-Olla-Model {a.headers.get('X-Olla-Model')!r} vs {b.headers.get('X-Olla-Model')!r}")
    ja, jb = body_json(a), body_json(b)
    if ja is not None:
        if jb is None:
            problems.append("Olla returned JSON, llm-head did not")
        else:
            missing = {k for k in keyset(ja) - keyset(jb) if not k.startswith(DATA_KEYED)}
            if missing:
                problems.append(f"missing JSON fields: {sorted(missing)[:8]}")
    elif cta.startswith("text/plain") and a.status_code >= 400 and a.text != b.text:
        problems.append(f"text {a.text!r} vs {b.text!r}")
    return problems


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--olla", required=True)
    p.add_argument("--head", required=True)
    args = p.parse_args()
    failures = 0
    with httpx.Client(timeout=300) as client:
        for method, path, body in CASES:
            label = f"{method} {path}"
            if isinstance(body, dict) and "model" in body:
                label += f" [{body['model']}{' stream' if body.get('stream') else ''}]"
            elif isinstance(body, bytes):
                label += " [invalid JSON]"
            ra = send(client, args.olla, method, path, body)
            rb = send(client, args.head, method, path, body)
            problems = compare(ra, rb, method, path, body)
            mark = "PASS" if not problems else "FAIL"
            failures += bool(problems)
            print(f"{mark}  {label}  ({ra.status_code})")
            for pr in problems:
                print(f"        {pr}")
    print(f"\n{len(CASES) - failures}/{len(CASES)} match")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
