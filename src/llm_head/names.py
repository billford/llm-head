"""Model name normalization.

Requests, /api/tags and /api/ps can spell the same model differently: "qwen2.5vl:7b-q4_K_M"
vs "qwen2.5vl:7b-q4_k_m", or "nomic-embed-text" vs "nomic-embed-text:latest". Everything
inside the head is keyed by the normalized name. Backends always receive the spelling
they reported in /api/tags.
"""

from __future__ import annotations


def normalize(name: str) -> str:
    n = name.strip().lower()
    if not n:
        return n
    # An untagged name means ":latest", as in Ollama itself. The tag separator is the
    # last colon after the final slash, so "host:5000/ns/model" stays untagged.
    last = n.rsplit("/", 1)[-1]
    if ":" not in last:
        n += ":latest"
    return n
