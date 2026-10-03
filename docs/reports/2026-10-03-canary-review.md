# Canary review: 3 days of real traffic through llm-head

2026-10-03 · canary from 2026-09-30 20:01 UTC to 2026-10-03 16:00 UTC

## Setup

- **Canary client:** the batch photo-classifier client (vision analyses, a warmup every
  5 minutes, and health probes) was switched to llm-head on `:40115`.
- **Everything else stayed on Olla:** the SDR scanner classifier, the home-automation
  agent, and the services running on lampoon.
- **llm-head ran in shadow mode** (`scheduling.evict` and `keep_warm` off), because Olla
  still shared the GPUs.

## Verdict

**Reliability: pass. Latency: one regression, explained and fixed.** Recommend cutover,
with the configuration choice in "Cutover implications" below.

## Reliability

| Measure | Result |
|---|---|
| Real-client requests through llm-head | **1,682 / 1,682 HTTP 200** |
| Failed requests | 0. The 6 in the log are the deliberate alert test on 2026-09-30 |
| Retries, queue waits, model quarantines | 0, 0, 0 |
| Photos ingested by the client | 17, all with a stored description (the week before: 76/76) |
| Client-side warnings or errors | 0 in the pipeline log, 0 warmup failures (the last were during the 2026-09-30 incident, through Olla) |
| llm-head restarts | 0 since 2026-09-30 20:07 |
| Alerts | none, apart from the deliberate test |

### A GPU host rebooted during the canary

european rebooted at 2026-10-02 13:53 UTC, apparently on purpose; there was an SSH login
just before. llm-head marked it offline after three failed checks, about 15 s, and back
online 20 s later. No canary request failed. Olla failed one request ("no route to
host") before marking the host offline.

Neither balancer was told in advance. The cutover runbook adds a drain step before
planned reboots.

## Latency

| Request type | Through Olla, week before | Through llm-head, canary |
|---|---|---|
| Warmups, probes and small requests | p50 640–710 ms | p50 709 ms, p95 784 ms |
| Photo analysis, one at a time | p50 9.7 s | **9.4–10.1 s** (same) |
| Photo analysis, two arriving together | 9.7 s each (one per GPU) | **13.0–15.4 s each** (both on one GPU) |

### Finding: paired photos shared one GPU

The client analyzes two photos at once. Olla's least-connections rule split each pair
across both GPUs. llm-head put both on european, the only host with qwen loaded at the
8192 context the client uses, because european had a free slot. Two analyses sharing a
GPU slow each other: one waits about 4 s longer for its first token, or generation drops
to about 43 tok/s.

There were two causes:

1. **Shadow mode** had `keep_warm` off, so nothing loaded qwen at 8192 on a second host.
2. **A bug that would have survived cutover:** the keep-warm loop loaded models at
   Ollama's default context (4096). A second "warm" copy would have been the wrong size,
   and pairs would still have stacked.

Fixed in `8804764` and `40d91dc`:
- llm-head learns the context size each model is mostly requested at;
- it warms at that size, or at a configured `num_ctx`;
- a host counts as warm only at the right size;
- warming prefers the model's home host.

With two warm copies, pairs split across hosts. A test covers this.

## Cutover implications: the GPU-memory trade-off

Two 16 GB GPUs can't keep everything warm. gpt-oss:20b (11.9 GB) doesn't fit next to
qwen (about 6 GB at 8192 context):

| Option | Photo pairs | Home-automation gpt-oss |
|---|---|---|
| A. qwen `keep_warm: 2` | about 9.5 s each | cold load every time (+4–6 s on an interactive request) |
| **B. qwen `keep_warm: 1` (recommended)** | about 13.5 s each (batch, nobody waits) | can stay loaded on european next to llama3.2 |

The prepared production config uses **B**. A third GPU host removes the trade-off.

## Route audit

Every route any client used on Olla since 2026-09-28 is implemented by llm-head:

- `api/generate`
- `api/chat`
- `api/embed`
- `api/ps`
- `api/version`
- `v1/chat/completions`
- `/internal/*`

No client used Anthropic translation or any other route llm-head doesn't implement.
