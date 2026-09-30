# llm-head

A load balancer for small, self-hosted [Ollama](https://ollama.com) clusters that knows
which models each host has loaded.

Common LLM proxies spread requests by counting open connections. On consumer GPUs that's
the wrong signal. What a request actually costs depends on:

- whether its model is already loaded on the host, or has to be loaded first,
- whether the model fits in free GPU memory, or will partly spill onto the CPU
  (about 8× slower),
- how many requests are waiting in Ollama's own queue, which the proxy can't see.

llm-head keeps the queue itself. It tracks which models are loaded on each host and how
much GPU memory is free. It sends each request to a host that already has its model
loaded, and never places a model where it would spill onto the CPU. When a request has to
wait too long, it gets a fast `503` with `Retry-After` instead of a 15-minute hang.

It is designed as a drop-in replacement for [Olla](https://github.com/thushan/olla): the
same `/olla/*` and `/internal/*` API and the same JSON log format, so existing clients
and dashboards keep working.

## Status

**Phase 1 (build) is done. Not yet deployed.** Every Olla API field and log field our
dashboard uses is covered by tests against responses captured from a live Olla v0.0.28.
Next is Phase 2: shadow testing against real GPUs, then cutover. Design and measurements
are in [`docs/specs/load-aware-scheduler.md`](docs/specs/load-aware-scheduler.md).

The spec includes measurements from a real two-GPU cluster (5,973 requests). They show
a 2:1 split between identical hosts, 594 model swaps per 1,000 requests on the busier
host, and gpt-oss:20b slowing from 94 to 12 tokens/s when it shared a 16 GB card.

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
| `GET /internal/queue` | Waiting requests, and each host's loaded models and in-flight counts |
| `POST /internal/hosts/{name}/drain` | Stop new work on a host, e.g. before a reboot. Localhost only |
| `POST /internal/hosts/{name}/undrain` | Put it back in rotation |

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
python -m tests.fake_ollama --port 11500   # a fake GPU host for manual testing
```

## License

MIT
