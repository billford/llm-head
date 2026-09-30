# Phase 2: shadow testing on real GPUs

2026-09-30 · llm-head on lampoon `:40115` next to production Olla on `:40114`

## Setup

- **llm-head** `15f2bd1` ran as `llm-head-shadow.service` in shadow mode
  (`scheduling.evict: false`, `keep_warm: false`), so it never unloaded or pre-loaded a
  model Olla might be using. It wrote to its own log so the dashboard didn't
  double-count.
- **Hosts:** the production xmas and european (RTX 5060 Ti 16 GB, Ollama 0.32 with the
  Phase 0 settings).
- **Where the tools ran:** on lampoon itself, so network paths were identical for both
  balancers.

## Test 1: side-by-side API comparison (`tools/shadow_compare.py`)

This sent 20 identical requests to both balancers and compared status, content type,
`X-Olla-*` headers, JSON fields and error texts.

| Run | Result |
|---|---|
| 1 | 15/20. **Found a real gap:** Olla sets `X-Olla-Request-Id` on every response, and llm-head only set it on proxied ones. Fixed in `8754d41`, with a test. |
| 2 | 19/20. Which check failed wasn't captured, because only the tail of the output was printed. |
| 3–6 | 20/20 on four consecutive runs. |

## Test 2: identical workloads (`tools/loadtest.py`)

- **Workload:** 120 requests per run, seeded so both balancers got the same sequence. The
  mix matches last week's traffic, except gpt-oss is boosted 5× (to about 16%) to provoke
  memory contention. 30% of requests were streaming.
- **Arrival rate:** one request every 0.8 s, with up to 16 in flight.
- **Order:** runs alternated between Olla and llm-head, 20 s apart. Production traffic
  continued throughout.

| Run | Balancer | OK | Cold loads | qwen2.5vl p95 | llama3.2 p95 | gpt-oss p50 | gpt-oss min tok/s |
|---|---|---|---|---|---|---|---|
| 1 (seed 1) | Olla | 120 | 70 | 13.0 s | 10.9 s | 5.7 s | 63 |
| 1 (seed 1) | llm-head `8754d41` | 120 | 10 | 1.5 s | 9.8 s | 1.5 s | 71 |
| 2 (seed 2) | Olla | 120 | 56 | 9.0 s | 8.6 s | 5.5 s | 68 |
| 2 (seed 2) | llm-head `8754d41` | 120 | 3 | 0.8 s | 0.8 s | 1.5 s | 78 |
| 3 (seed 1) | Olla | **114** | 71 | 15.8 s | 10.0 s | 6.1 s | 61 |
| 3 (seed 1) | llm-head `15f2bd1` | 120 | 4 | 0.8 s | 1.1 s | 1.6 s | 71 |

### Findings

1. **Olla reproduced the original "stuck jobs" problem.** In run 3, six requests (five
   llama3.2 and one gpt-oss) hung for exactly 120 s and failed with `502 Proxy error:
   request timeout after 120.0s`. That is Olla's `response_header_timeout`. They were
   waiting in Ollama's internal queue, which Olla can't see.
2. **Cold loads fell from 56–71 per run to 3–10.** llm-head sends each request to a host
   that already has its model loaded, so it avoids most loads.
3. **qwen2.5vl's slowest 5% improved 10–20×**, from 9–16 s to 0.8 s once placement had
   settled.
4. **gpt-oss's median improved about 4×**, from 5.5–6.1 s to 1.5 s, because it usually
   found a host where it was already loaded.
5. **Neither balancer let gpt-oss spill onto the CPU in these runs.** The slowest gpt-oss
   request was 61 tok/s on Olla and 71 tok/s on llm-head. The 12 tok/s case from the
   production logs didn't recur, probably because Phase 0's `MAX_LOADED_MODELS=2` lets
   Ollama evict idle models first. This test doesn't prove llm-head prevents it on real
   hardware. That still rests on the integration test with the fake Ollama.
6. **Found and fixed a placement bug (`15f2bd1`).** In run 1, llama3.2 took 9.6 s to
   load on xmas, and llm-head queued five more requests behind that load while european
   sat idle. Placement now counts the time left on a load in progress when choosing
   between waiting and loading elsewhere. Run 3 used the same seed and had llama3.2's
   p95 at 1.1 s.
7. **The traffic split isn't a useful measure.** llm-head's split ranged from 53/67 to
   80/40. It follows where models are loaded, and latency is what matters.

### Test-harness mistake

The first round sent requests as fast as possible. llm-head finished so quickly that it
exceeded the per-IP rate limit (100 per minute, burst 50), which is the same limit Olla
enforces. About 30% of its requests got 429s. That confirmed rate-limit parity: 35
rejections, where the token-bucket formula predicted about 35. It made the round useless
for comparing balancers, so it was rerun with requests paced below the limit
(`37be63e`).

### Impact on production traffic

Four real client requests arrived during the test window, 13:19 to 14:00 UTC. All
succeeded, but at a median of 2.0 s instead of the usual ~0.6 s, because they shared the
GPUs with the tests.

## Test 3: real-host scenarios (`tools/realhost_checks.py`)

These cover what synthetic load doesn't: real image payloads, agent-style tool calls,
disconnects, drain, long context, and overload.

| Check | llm-head | Olla |
|---|---|---|
| 1920×1080 vision request, streaming, `num_ctx` 8192 (the photo classifier's request shape) | pass. **First attempt: 128 s** (stalled on xmas and retried on european). **After `4aec078`: 10.1 s**, reloaded at 8192 on an idle host | **502 after 120 s**, same stall on xmas |
| gpt-oss tool call, continuing after the tool result, JSON-schema output | pass (3/3) | **tool call and schema both failed: 502 after 120 s** on xmas |
| Client disconnects mid-stream: slot released | pass | not tested |
| Drain a host: no new work goes there | pass | no drain feature |
| 16k-context request (9,821 prompt tokens) | pass | not tested |
| Burst of 12 requests: never more than 2 per host, the rest wait at the head | pass (peak 2 and 2, 8 waiting) | not tested |

### Finding: context size is part of what "loaded" means

Ollama has to reload a model to change `num_ctx`, and it waits until the loaded copy is
idle to do it. The photo classifier runs its analyses at `num_ctx` 8192. Its warmup and
health probe don't set `num_ctx`, so they keep qwen loaded at the default 4096, and every
real analysis then needs a reload. On a host kept busy by other traffic, that reload can
wait indefinitely.

llm-head now treats context size as part of the loaded state:
- a reload only goes to a host where the model is idle;
- once one request has been starved past a threshold, later requests for that model
  wait behind it (`4aec078`).

### Incident during testing

Around the time these tests ran, Ollama on xmas stopped loading models for 5 hours, and
133 real requests failed through production Olla. See
`2026-09-30-xmas-ollama-wedge.md`. llm-head now detects this (a load watchdog) and
quarantines that model on that host.

## Caveats

- Both balancers shared the GPUs with production traffic and with each other's leftover
  state. Alternating the order limits the bias but doesn't remove it.
- In shadow mode llm-head can't unload models itself. Ollama's own LRU eviction runs
  instead, so these numbers slightly understate llm-head in normal mode.
- The load is synthetic: text-only prompts sized like the real ones. Real qwen2.5vl
  requests include images.

## Recommendation

Before cutover, run a **canary**, after upgrading xmas to Ollama 0.32.4: point one real client at `:40115` for 2–3 days. The
Mac's batch job is the best candidate, since it sends 97% of traffic and isn't
interactive. That tests real request bodies (images, tool calls) and real traffic
patterns while Home Assistant and the lampoon services stay on Olla. Cut over once the
canary has run with no unexplained failures.
