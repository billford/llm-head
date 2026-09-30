# Incident: Ollama on xmas stopped loading models for 5 hours

2026-09-30 · blameless write-up · status: **resolved** 18:47 UTC

## Summary

From 13:40 to 18:47 UTC, Ollama 0.32.3 on xmas could not load or reload any model.
Requests for the model it already had loaded, qwen2.5vl at 4096 context, still
succeeded. Every request that needed a load hung silently:

- llama3.2:3b and gpt-oss:20b, which weren't loaded;
- qwen2.5vl at 8192 context, which needs a reload.

xmas kept passing health checks, so production Olla kept sending it about 70% of these
requests. Nothing alerted. The problem was found during Phase 2 testing, and a restart
of Ollama on xmas fixed it.

The most likely trigger is the Phase 2 load test, which ended at 13:35–13:40 and pushed
xmas through a rapid sequence of evictions and loads.

## Impact

Real clients through production Olla, 13:40:56–18:47 UTC:

| Client | Failed | Total | How it failed |
|---|---|---|---|
| SDR scanner classifier (llama3.2:3b, 30 s client timeout) | 123 | 145 | 502 after the client's 30 s timeout |
| Photo classifier (qwen2.5vl vision at 8192 context) | 8 | 142 | 502 after Olla's 120 s header timeout. The client retries once, and failed photos are kept for reanalysis |
| Home-automation agent (gpt-oss:20b tool calls) | 2 | 3 | 502 after 120 s |

In the 7 days before the incident, these clients had zero failures.

## Timeline (UTC)

| Time | Event |
|---|---|
| 13:19–13:40 | Phase 2 load tests: 5 × 120 requests through Olla and llm-head, gpt-oss boosted to about 16% |
| 13:35:35 | xmas evicts models to fit an 8192-context load and loads two runners (normal log output) |
| 13:40:56 | First real-client failure: an SDR classifier request times out at 30 s on xmas |
| 13:41 → 18:43 | Failures continue at about 25 per hour. Load tests have ended, and xmas's health check still returns 200 |
| 18:17–18:31 | Phase 2 real-host checks hit the same stall: a 1920×1080 vision request and gpt-oss tool calls through Olla hang for 120 s on xmas |
| 18:32 | Olla's access log shows the failures began at 13:40, with none in the prior week. A direct llama3.2 request to xmas hangs past 45 s; the same request to european succeeds |
| 18:33 | Diagnostics saved on xmas (`~/ollama-wedge-20260930/`: journal, `/api/ps`, processes, `nvidia-smi`) |
| 18:47 | `systemctl restart ollama` on xmas. Direct llama3.2 and qwen requests return 200 in about 2 s, and 4 of 4 llama3.2 requests through Olla land on xmas and succeed |

## What we know about the cause

- **The load path stopped; the loaded model didn't.** Only requests for the model
  already loaded, with the same context size, were served.
- **Ollama logged nothing for the stuck requests.** No load attempt, no error, no
  eviction. This is consistent with the scheduler waiting on a condition that never
  became true.
- **european wasn't affected.** It runs Ollama 0.32.4 and got the same load test. xmas
  runs 0.32.3.
- **Unconfirmed contributors:**
  - Phase 0 settings: `MAX_LOADED_MODELS=2`, `NUM_PARALLEL=2`.
  - Constant 4096-context traffic from the photo classifier's warmup and health probe,
    which keeps qwen busy while other requests need it reloaded at 8192.
  - llm-head shadow polling `/api/ps` once a second. This seems unlikely, because
    european got the same polling.
- The photo classifier's own notes describe a similar lock-up on 2026-08-30. That one was
  caused by a `keep_alive: -1` model that could never be evicted.

## What went wrong in how we worked

1. **I didn't check production health after every load test.** It was checked once, after
   the first round, when four real requests looked fine. A check after each round would
   have caught this within minutes instead of five hours.
2. **Nothing alerts on real-client error rates.** The dashboard shows a 15-minute error
   rate, but nothing pages on it. A GET `/` health check can't detect a scheduler that
   can't load models.

## Follow-up actions

| # | Action | Status |
|---|---|---|
| 1 | llm-head: per-model health for each host (load watchdog plus quarantine) | **Done** `4aec078` |
| 2 | llm-head: context size as part of what's loaded; no reloads onto busy copies | **Done** `4aec078`. The 1920×1080 vision request went from 128 s to 10 s |
| 3 | Upgrade xmas to Ollama 0.32.4, matching european | **Done** 2026-09-30 19:55 UTC. Phase 0 settings kept; probes pass |
| 4 | Load-test rule: check real-client errors after every round | **Adopted.** `prodcheck.sh` on the head host runs the check |
| 5 | Alert on real-client failures, and probe each model on each host directly | Plugins **done** and installed on the head host (`7f16233`, `contrib/icinga/`). Icinga master config pending |
| 6 | Photo classifier: send `num_ctx` in its warmup and probe requests | **Done** in the client repo |
