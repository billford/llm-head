# Phase 0 results

2026-09-30 · compares Olla logs before and after the Phase 0 tuning (spec §6)

## What was measured

- **Before:** 5,189 requests over 105 hours, 2026-09-25 14:54 to 2026-09-30 00:10 UTC.
  The oldest rotated log from the original baseline has since aged out, so these numbers
  differ slightly from the spec's §2.
- **After:** 494 requests over 12 hours, 2026-09-30 00:11 to 12:17 UTC. This window is
  mostly overnight traffic.

Both windows come from the `Request completed` lines in Olla's log, analyzed with the same
script.

## Results

| Metric | Before | After |
|---|---|---|
| Requests with first token after more than 3 s (cold load or queued), per 1,000 requests | 10.8 | **2.0** |
| xmas qwen2.5vl p95 | 8,497 ms | **649 ms** |
| xmas qwen2.5vl max | 35.2 s | 10.3 s |
| xmas llama3.2:3b p95 | 2,124 ms | **604 ms** |
| european llama3.2:3b p50 | 1,219 ms | **429 ms** |
| gpt-oss:20b runs below 50 tok/s (partly on CPU) | 2 | 0 (only 2 requests, not conclusive) |
| Requests running more than 10 s with 0 tokens | 5 | 0 |
| Traffic split xmas / european | 69 / 31 | **66 / 34** |

## What this means

- **Cold loads are down about 5×.** `MAX_LOADED_MODELS=2` and `KEEP_ALIVE=30m` keep the
  two busiest models (qwen2.5vl and llama3.2:3b) loaded together on both hosts, so the
  slow tail on xmas has mostly gone.
- **"Model switches" is no longer a useful metric.** xmas still alternates between models
  on 593 of every 1,000 requests, but both models stay loaded, so switching costs
  nothing. The spec's success criterion now counts cold loads (first token after more
  than 3 s) instead.
- **The traffic split hasn't changed, as expected.** Olla's least-connections balancer
  still sends every request to the first host whenever both are idle. Tuning Ollama
  can't fix that. It's Phase 1 work.
- **gpt-oss is the case to watch.** There were too few gpt-oss requests overnight to
  confirm the CPU-spill problem is gone. It can still happen when gpt-oss arrives while
  another model is busy on the same card, because Ollama won't unload a model that is
  serving a request. Only a scheduler that knows which models are loaded prevents it
  (spec §4.3, rule 5).

## Remaining work

Phase 1 (llm-head) remains necessary for:

- the traffic split,
- keeping gpt-oss off the CPU,
- a visible queue with timeouts,
- adding a third host.
