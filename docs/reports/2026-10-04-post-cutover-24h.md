# Post-cutover check: first 24 hours of llm-head in production

2026-10-04 · window 2026-10-03 16:12:51 to 2026-10-04 16:12:51 UTC · runbook +24 h check
(`docs/specs/cutover-runbook.md`) · compared against spec §7

## Verdict

**No rollback.** None of the runbook's rollback triggers occurred:
- no real-client failures;
- the home-automation agent was served every time;
- the dashboard works.

Three of the four +24 h checks pass. **Cold loads miss the target** (2.7 per 1,000 in
steady state, target < 1). Every one has the same cause: qwen is warm on the wrong host,
left there by the xmas drain during cutover. That pushes gpt-oss off european, its home,
onto xmas, where it evicts and is evicted by a twice-daily llama3.1:8b batch. Fixing the
layout is a one-time operational step (below). It is not a code rollback.

## The four +24 h checks

| Check (spec §7) | Target | Result | |
|---|---|---|---|
| Cold loads (first token after more than 3 s) per 1,000 requests | < 1 | **5.2** over all 24 h (6 / 1,154). **2.7** in steady state from 16:30 (3 / 1,129) | **Miss** |
| qwen2.5vl p95 | < 1 s on every host | european **794 ms** (n = 585). xmas 1,964 ms, n = 4, all during the first 2 min of cutover | **Pass** (european). xmas can't be measured, see below |
| 120 s stalls | 0 | **0.** Longest request 16.0 s, longest queue wait 1 ms, no stalled or quarantined models | **Pass** |
| Queue timeouts | 0 | **0.** No request was ever queued. Every access-log entry was HTTP 200 (1,460 / 1,460) | **Pass** |

### The rest of §7, for completeness

| Metric | Target | Result |
|---|---|---|
| gpt-oss:20b max duration | < 25 s | 16.0 s (the cold load at 16:27). Warm: 4.3–8.7 s |
| Dispatched with CPU offload | 0 | 0. gpt-oss ran at 93–94 tok/s every time. Nothing ran below 20 tok/s |
| Client or dashboard changes | 0 | 0 |
| Request split across identical hosts | within 60 / 40 | 75 / 25 (european / xmas) overall. **50 / 50** for llama3.2, the only model served from both hosts. The overall split now reflects which host holds which model, not balancer skew. §7's wording predates model-aware placement and should be reworded |

## Traffic

1,154 requests completed and **0 failed**. Olla's log format held: the dashboard and the
Icinga `llm-errors-head` check read the same file.

| Model | Host | Requests | p50 | p95 | Max |
|---|---|---|---|---|---|
| qwen2.5vl:7b-q4_k_m | european | 585 | 763 ms | 794 ms | 7,501 ms¹ |
| qwen2.5vl:7b-q4_k_m | xmas | 4 | 1,362 ms | 1,964 ms | 1,964 ms |
| llama3.2:3b | european | 273 | 368 ms | 485 ms | 698 ms |
| llama3.2:3b | xmas | 273 | 487 ms | 838 ms | 1,598 ms |
| llama3.1:8b | xmas | 12 | 1,700 ms | 6,294 ms | 8,597 ms |
| gpt-oss:20b | xmas | 5 | 7,292 ms | 14,280 ms | 16,007 ms |
| gpt-oss:20b | european | 2 | 10,520 ms | — | 12,333 ms |

¹ The first request after cutover, which reloaded qwen at its 8192 context.

All qwen traffic in the window was the photo client's warmups and probes. **No photo
analyses ran**, so the canary's paired-analysis latency (about 13.5 s each with
`keep_warm: 1`) hasn't been seen in production yet. The four qwen requests on xmas are two
warmup pairs at 16:13:57 and 16:14:45. They hit a copy warmed at 16:13:06, just before
xmas was drained for its reboot. Since then qwen has lived only on european, so "p95 on
every host" has nothing to measure on xmas.

llm-head ran the full window without restarting (`NRestarts=0`). Its journal has no
errors. `olla.service` is disabled, `llm-head.service` enabled.

## The six slow first tokens

| Time (UTC) | Model → host | First token | Client | Cause |
|---|---|---|---|---|
| 10-03 16:13:02 | qwen → european | 7.4 s | cutover smoke test (curl) | Reload to 8192 context: shadow mode had left qwen at 4096 |
| 10-03 16:15:13 | gpt-oss → european | 8.1 s | home automation | Cold load while xmas was being drained |
| 10-03 16:27:12 | gpt-oss → xmas | 11.8 s | home automation | Cold load. qwen had been warmed on european during the drain, so gpt-oss no longer fit there |
| 10-03 22:00:16 | llama3.1:8b → xmas | 7.2 s | scheduled batch (6 requests) | Evicted gpt-oss |
| 10-04 03:05:28 | gpt-oss → xmas | 4.5 s | home automation | Evicted llama3.1:8b |
| 10-04 13:00:13 | llama3.1:8b → xmas | 3.2 s | scheduled batch (6 requests) | Evicted gpt-oss |

The first three come from the cutover itself and won't recur. The last three are one
pattern repeating: gpt-oss and llama3.1:8b take turns on xmas. Each eviction costs the
next request a cold load. Only the 03:05 one hit an interactive request. As of
16:36 UTC, xmas holds llama3.1:8b and llama3.2, so **the next home-automation gpt-oss
request will cold-load again.**

## Finding: qwen is warm away from its home, and that costs gpt-oss its host

The production config says:

```yaml
gpt-oss:20b: {home: [european]}
qwen2.5vl:7b-q4_k_m: {keep_warm: 1, num_ctx: 8192, home: [xmas]}
```

What's actually loaded is the reverse of that intent:

| Host | Intended | Actual |
|---|---|---|
| european | gpt-oss + llama3.2 | **qwen** + llama3.2 |
| xmas | qwen + llama3.2 | llama3.2 + whichever of gpt-oss and llama3.1:8b ran last |

The cutover report called this "expected and harmless" because `home` only breaks ties.
**That was wrong.** With qwen on european, gpt-oss (11.9 GB) can't fit there, so it goes
to xmas. There it competes for xmas's second slot with the llama3.1:8b batch.

With the intended layout, gpt-oss would stay loaded on european next to llama3.2. The
llama3.1:8b batch would then go to xmas and could evict xmas's copy of llama3.2 instead,
which is still warm on european. Neither of the steady-state interactive cold loads would
have happened.

Nothing moves qwen back by itself. Keep-warm sees one warm copy and is satisfied.

### Recommended actions

1. **Now (operational, no code):** put qwen back on xmas at a quiet moment, with nothing
   in flight. Keep-warm loads without evicting anything itself (`evict=()`). If xmas still
   has both slots full, Ollama's own LRU picks what to evict, and it could take llama3.2,
   which keep-warm would then fight to bring back. So free xmas's second slot first:
   1. Unload llama3.1:8b on xmas: `curl xmas:11434/api/generate -d '{"model":"llama3.1:8b","keep_alive":0}'`.
   2. Unload qwen on european the same way.
   3. Within about 15 s, keep-warm rewarms qwen. It prefers the model's home host (`40d91dc`)
      and xmas is idle, so it should go there.
   4. Check `/internal/queue`: xmas should show qwen and llama3.2, european llama3.2 only.
   5. Send one gpt-oss request. It should land on european.

   Expect one cold load for qwen and one for gpt-oss.
2. **Code follow-up:** keep-warm should move a model back to its home host when the home
   has room and the model is idle. Otherwise every drain or reboot can leave this layout
   behind again.
3. **Spec §7:** reword the split criterion as "per model, across hosts that hold it". The
   current 75 / 25 is placement, not skew.
4. **Re-measure cold loads at the 7-day mark.** With 3 events in 1,129 requests, one
   cold load is worth about 0.9 per 1,000, so the < 1 target needs a longer window. The
   llama3.1:8b batch will still cost one cold load per run, about 2 a day. That's
   probably acceptable for batch work, but it means < 1 per 1,000 isn't reachable on two
   GPUs with this model mix. The third host would remove it.

## Method

Read from lampoon's `/opt/olla/logs/` (the rotated `olla-2026-10-04T01-36-47` file and
the live `olla.log`), parsed in a local scratch directory. Raw logs are not committed.
- **Requests:** `Request completed` lines carrying a `placement` field (llm-head's). One
  Olla completion at 16:12:51 was excluded.
- **Cold loads:** `ttft_ms > 3000`, the same definition as in Phase 0.
- **Statuses and clients:** from `Access log` lines.
- **Timestamps:** llm-head's access-log lines use ISO `…Z` timestamps, while other lines
  use `YYYY-MM-DD HH:MM:SS`. Both were normalized before filtering on the window.
- **Live state:** `GET /internal/queue` and `systemctl` on lampoon at 16:36 UTC.
