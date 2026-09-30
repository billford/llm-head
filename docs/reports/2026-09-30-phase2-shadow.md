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

## Caveats

- Both balancers shared the GPUs with production traffic and with each other's leftover
  state. Alternating the order limits the bias but doesn't remove it.
- In shadow mode llm-head can't unload models itself. Ollama's own LRU eviction runs
  instead, so these numbers slightly understate llm-head in normal mode.
- The load is synthetic: text-only prompts sized like the real ones. Real qwen2.5vl
  requests include images.

## Recommendation

Before cutover, run a **canary**: point one real client at `:40115` for 2–3 days. The
Mac's batch job is the best candidate, since it sends 97% of traffic and isn't
interactive. That tests real request bodies (images, tool calls) and real traffic
patterns while Home Assistant and the lampoon services stay on Olla. Cut over once the
canary has run with no unexplained failures.
