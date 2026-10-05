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
layout is a one-time operational step (below). It is not a code rollback. That step was
carried out at 16:40. It also exposed a second §7 miss: llama3.2 runs partly on the CPU
whenever it shares a GPU with gpt-oss, and this was also happening before the move. See
"Follow-up".

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
| Dispatched with CPU offload | 0 | **Miss** (corrected after the follow-up below). gpt-oss always ran fully on the GPU (93–94 tok/s). But whenever gpt-oss was loaded on xmas, **llama3.2 on xmas ran partly on the CPU**: 169 requests at a median of 63 tok/s, against 168 tok/s otherwise. The first version of this report only checked for runs below 20 tok/s and missed this |
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
   behind again. **Code done 2026-10-05, not yet deployed:** keep-warm now warms a copy
   at home when home has room, then unloads the stray copy once it has been idle for 5
   minutes ("Rehoming model" in the log).
3. **Spec §7:** reword the split criterion as "per model, across hosts that hold it". The
   current 75 / 25 is placement, not skew. **Done 2026-10-05.**
4. **Re-measure cold loads at the 7-day mark.** With 3 events in 1,129 requests, one
   cold load is worth about 0.9 per 1,000, so the < 1 target needs a longer window. The
   llama3.1:8b batch will still cost one cold load per run, about 2 a day. That's
   probably acceptable for batch work, but it means < 1 per 1,000 isn't reachable on two
   GPUs with this model mix. The third host would remove it.

## Follow-up, 16:40–16:44 UTC: qwen moved back to xmas

Action 1 above was carried out with nothing in flight:
- 16:40:51: llama3.1:8b unloaded on xmas, and qwen on european.
- 16:40:53: keep-warm warmed qwen on xmas at 8192. While qwen loaded, Ollama also
  dropped llama3.2 from xmas.
- 16:41:14: keep-warm warmed llama3.2 on xmas again.
- 16:41:41: a gpt-oss test request through llm-head went to european: a cold load, as
  expected (7.5 s to first token, then 95 tok/s, fully on the GPU).

The layout now matches the config:

| Host | Loaded (in GPU memory / total size) |
|---|---|
| xmas | qwen2.5vl at 8192 (5.9 / 5.9 GiB), llama3.2 (2.9 / 2.9 GiB) |
| european | gpt-oss (11.9 / 11.9 GiB), **llama3.2 (2.05 / 2.86 GiB)** |

### New finding: gpt-oss and llama3.2 don't both fit on one 16 GB GPU

On european, Ollama put about 0.8 GiB (28%) of llama3.2 on the CPU next to gpt-oss.
The same request, sent directly to each host:

| | Total time | Generation speed |
|---|---|---|
| llama3.2 on european, next to gpt-oss | 870–990 ms | 68 tok/s |
| llama3.2 on xmas, next to qwen | 490–525 ms | 168 tok/s |

**This isn't new.** The 24-hour logs show the same slowdown on xmas whenever gpt-oss was
loaded there:

| Period (UTC) | Loaded next to llama3.2 on xmas | xmas llama3.2, median | european llama3.2, median |
|---|---|---|---|
| 10-03 16:28–22:00 | gpt-oss | **63 tok/s**, 623 ms | 168 tok/s, 358 ms |
| 10-03 22:00–10-04 03:05 | llama3.1:8b | 168 tok/s, 350 ms | 167 tok/s, 364 ms |
| 10-04 03:05–13:00 | gpt-oss | **63 tok/s**, 596 ms | 168 tok/s, 366 ms |
| 10-04 13:00–16:40 | llama3.1:8b | 167 tok/s, 359 ms | 167 tok/s, 382 ms |

The canary review assumed gpt-oss "can stay loaded on european next to llama3.2". It
can't, at least not fully on the GPU.

llm-head's memory model thinks the pair fits:
- usable memory: 16,311 − 512 MB reserve, about 15.4 GiB;
- needed: 11.9 + 2.9 = 14.7 GiB.

Ollama's real overhead leaves less room than that, so llm-head can't see the spill.
Ollama does report it: `/api/ps` shows `size_vram < size` for the spilled model.

So the move traded one problem for another:
- **Better:** gpt-oss stays warm on european for interactive requests. It no longer swaps
  with the llama3.1:8b batch.
- **No better:** the llama3.2 copy sharing a GPU with gpt-oss still spills. It's now on
  european all the time instead of on xmas about two-thirds of the time.

### Further actions

5. **Code: make spills visible.** llm-head should read `size_vram` against `size` from
   `/api/ps`. It should treat a partly spilled model as not warm on that host and send its
   traffic elsewhere, and record a spill as a failed fit when estimating memory.
   **Done:** `b4a9cad`, deployed 2026-10-05. See the next section.
6. **Until then** (no longer needed once 5 was deployed), the options were:
   - accept about 0.6 s extra on llama3.2 requests that land on european;
   - lower llama3.2 to `keep_warm: 1` with `home: [xmas]`. That doesn't fully stop it:
     llm-head can still place llama3.2 on european when xmas's slots are busy, because it
     believes the pair fits;
   - raise european's `reserve_mb` so llm-head stops pairing the two (about 1.5 GB more).

## Follow-up, 2026-10-05 13:03–13:06 UTC: spill detection deployed

`b4a9cad` was deployed on lampoon at 13:03:05:
- `git pull`, `pip install` into `/opt/llm-head/venv`, then `safe-restart.sh`. The
  restart happened at once because nothing was in flight.
- The stats file was backed up first, to `data/stats.json.pre-b4a9cad`.

At the restart nothing was spilled. The 13:00 llama3.1:8b batch had pushed gpt-oss off
european, leaving llama3.1:8b and llama3.2 there, both fully on the GPU. llm-head's view
matched `/api/ps` on both hosts. So one gpt-oss test request was sent through llm-head,
as on 10-04:

| Time (UTC) | Event |
|---|---|
| 13:04:26 | gpt-oss sent to european, `cold_load_evict`, which unloads llama3.1:8b. 4.3 s load, 94.8 tok/s, fully on the GPU |
| 13:04:37 | `WARN Model spilled onto CPU`: llama3.2 on european, `size_vram` 2.05 GiB of `size` 2.86 GiB. This is the same spill as on 10-04 |
| 13:04:38 | `Endpoint GPU capacity learned`: european can use 14,267 MB, down from the configured 15,799 MB. It was written to `stats.json` by 13:05:05 |
| 13:04:58 | 6 llama3.2 test requests: **all went to xmas**, each `loaded`. European's spilled copy was not used |
| 13:05–13:06 | keep-warm did nothing on european. The spilled copy doesn't count as warm, and there isn't room to reload it fully |

`GET /internal/queue` now shows european with `"spilled": ["llama3.2:3b"]` and
`"vram_usable_mb": 14267`. xmas shows 15,799 MB, since xmas has never spilled.

**What this means for the layout.**
- With 14,267 MB to work with, llm-head won't load gpt-oss (11.9 GiB) and llama3.2
  (2.9 GiB) side by side on one host. So llama3.2 requests now go to xmas while gpt-oss
  is on european, instead of running at 68 tok/s on european.
- keep-warm will keep only one full copy of llama3.2 (on xmas) while gpt-oss is loaded,
  not the configured two. When both of xmas's llama3.2 slots are busy, a request either
  waits for one or, if gpt-oss is idle, evicts it to load a full copy on european. Which
  one happens depends on placement cost. Either way it never uses the slow copy. The next
  review should check how often this happens.

**If the learned capacity is ever wrong.** Something else using the GPU could cause a
spill that understates a host's capacity. Nothing raises it again on its own, because
llm-head no longer loads more than the learned figure. To reset it, run
`curl -X POST localhost:40114/internal/hosts/<name>/reset-capacity` on lampoon.

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
