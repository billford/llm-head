# llm-head

A load balancer for small, self-hosted [Ollama](https://ollama.com) clusters that knows
which models each host has loaded.

Common LLM proxies spread requests by counting open connections. On consumer GPUs that's
the wrong signal. What a request actually costs depends on:

- whether its model is already loaded on the host, or has to be loaded first,
- whether the model fits in free GPU memory, or will partly spill onto the CPU
  (about 8× slower),
- how many requests are waiting in Ollama's own queue, which the proxy can't see.

llm-head keeps the queue itself. It tracks which models are loaded on each host, **at what
context size**, and how much GPU memory is free. It:

- sends each request to a host that already has its model loaded at the context size the
  request needs (Ollama has to reload a model to change `num_ctx`);
- never places a model where it would spill onto the CPU. If Ollama spills one anyway,
  llm-head notices (`/api/ps` reports less in GPU memory than the model needs), sends
  its requests to a full copy, and learns how much that host's GPU really holds;
- detects a host that has stopped loading models even though its health check passes,
  retries the request elsewhere, and avoids that model on that host for a while;
- keeps chosen models warm at the context size they're actually requested at;
- answers a request that waits too long with a fast `503` and `Retry-After` instead of a
  15-minute hang;
- drains a host before a reboot, so no request is cut off.

It is a drop-in replacement for [Olla](https://github.com/thushan/olla): the same
`/olla/*` and `/internal/*` API and the same JSON log format, so existing clients and
dashboards keep working.

## Status

**In production since 2026-10-03,** replacing Olla v0.0.28 in front of a two-GPU cluster
(RTX 5060 Ti 16 GB × 2). Results on that cluster:

| | Olla | llm-head |
|---|---|---|
| Cold model loads, same 120-request workload | 56–71 | 3–10 |
| qwen2.5vl vision p95, same workload | 9–16 s | 0.8 s |
| Requests stuck until a 120 s timeout | 6 of 120 in one run | 0 |
| 3-day canary, real traffic | — | 1,682 / 1,682 OK |
| Planned GPU-host reboot | requests fail mid-flight | drained first, 0 failures |

How we got there, with measurements and the mistakes along the way:

| Document | What it covers |
|---|---|
| [`docs/specs/load-aware-scheduler.md`](docs/specs/load-aware-scheduler.md) | Problem analysis, design, Olla parity checklist, rollout plan |
| [`docs/reports/2026-09-30-phase2-shadow.md`](docs/reports/2026-09-30-phase2-shadow.md) | Head-to-head against Olla on real GPUs |
| [`docs/reports/2026-09-30-xmas-ollama-wedge.md`](docs/reports/2026-09-30-xmas-ollama-wedge.md) | Incident: an Ollama host stopped loading models for 5 hours while healthy |
| [`docs/reports/2026-10-03-canary-review.md`](docs/reports/2026-10-03-canary-review.md) | Three days of real traffic before cutover |
| [`docs/specs/cutover-runbook.md`](docs/specs/cutover-runbook.md), [`docs/reports/2026-10-03-cutover.md`](docs/reports/2026-10-03-cutover.md) | How the switch was made and rolled back if needed |
| [`docs/reports/2026-10-04-post-cutover-24h.md`](docs/reports/2026-10-04-post-cutover-24h.md) | First 24 hours in production, and the CPU-spill finding that led to spill detection |

## Quick start

```bash
python3 -m venv venv && venv/bin/pip install .
cp examples/config.yaml config.yaml     # edit hosts, vram_mb, slots_per_model
venv/bin/llm-head check-config -c config.yaml
venv/bin/llm-head serve -c config.yaml
```

Point clients at `http://<head>:40114/olla/ollama` exactly as with Olla.

On each Ollama host, set `OLLAMA_NUM_PARALLEL` and `OLLAMA_MAX_LOADED_MODELS` to match
that host's `slots_per_model` and `max_loaded_models`. The head never sends more
requests than that, so waiting happens at the head, where it's visible and bounded.

No GPU handy? [Getting started](docs/guide/getting-started.md#try-it-locally-first) shows
how to run a two-host cluster of fake Ollama servers on a laptop.

## Documentation

| Guide | Read it when |
|---|---|
| [Getting started](docs/guide/getting-started.md) | Installing: preparing Ollama hosts, the config, systemd, pointing clients at it, replacing Olla |
| [Configuration](docs/guide/configuration.md) | You need to know what an option does, or its default |
| [How it works](docs/guide/how-it-works.md) | You want to know why a request went where it did: placement, context size, the queue, keep-warm, CPU spill detection, retries |
| [HTTP API](docs/guide/api.md) | Writing a client or dashboard: routes, response headers, errors, `/internal/*` |
| [Operations](docs/guide/operations.md) | Restarting, upgrading, rebooting a GPU host, monitoring, reading the logs |
| [Troubleshooting](docs/guide/troubleshooting.md) | Something's wrong: by symptom, with the commands to check |

Quick reference for operators, run on the head:

```bash
curl -s localhost:40114/internal/queue                           # what's loaded, waiting, spilled
curl -X POST localhost:40114/internal/hosts/<name>/drain         # before rebooting a GPU host
curl -X POST localhost:40114/internal/hosts/<name>/undrain
contrib/safe-restart.sh llm-head /opt/llm-head/config.yaml       # restart once idle
```

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
python -m tests.fake_ollama --port 11500   # a fake GPU host for manual testing
```

## License

MIT
