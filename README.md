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

### New endpoints

| Endpoint | Purpose |
|---|---|
| `GET /internal/queue` | Waiting requests, and each host's loaded models (and any spilled onto the CPU), usable GPU memory and in-flight counts |
| `POST /internal/hosts/{name}/drain` | Stop new work on a host, e.g. before a reboot. Localhost only |
| `POST /internal/hosts/{name}/undrain` | Put it back in rotation |
| `POST /internal/hosts/{name}/reset-capacity` | Forget the GPU memory learned from spills, e.g. after one caused by something else on the GPU. Localhost only |

## Operating it

| Tool | Purpose |
|---|---|
| `contrib/safe-restart.sh` | Validate the config, wait until nothing is in flight, restart, and confirm it answers |
| `contrib/icinga/` | Icinga/Nagios plugins: alert on client-visible failures (`check_llm_balancer_errors`), and prove each GPU host can still load and serve a model (`check_ollama_models`). Includes example config |
| `tools/shadow_compare.py` | Send identical requests to two balancers and compare what clients would see |
| `tools/loadtest.py` | Replay a seeded, realistic workload. Pace it below your rate limit |
| `tools/realhost_checks.py` | Scenario checks on real hosts: vision, tool calls, disconnects, drain, long context, bursts |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
python -m tests.fake_ollama --port 11500   # a fake GPU host for manual testing
```

## License

MIT
