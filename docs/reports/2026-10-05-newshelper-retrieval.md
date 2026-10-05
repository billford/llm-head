# Incident: newshelper's chatbot couldn't reach its news index

2026-10-05 · blameless write-up · status: **mitigated** 21:47 UTC; code fix committed,
not yet deployed

## Summary

At 21:37 UTC a newshelper chat question got the reply "I can't reach NewsHelper's story
index at the moment". The chat proxy on lampoon gives its retrieval step 5 seconds.
Retrieval needs an embedding from `nomic-embed-text` through llm-head, and that took
6.8 s, so the proxy gave up and never called the chat model.

Three llm-head bugs combined. Two were introduced or exposed by the spill-detection
change deployed the same day (`b4a9cad`). The third dates from the context-size change
of 2026-09-30 (`4aec078`):

1. **A cold embedding load evicted gpt-oss instead of a useless spilled copy.**
   european held gpt-oss and a spilled llama3.2. Placement counted the spilled copy
   toward llama3.2's `keep_warm: 2`, so it protected that copy and unloaded gpt-oss.
   That cost 6.8 s.
2. **llm-head then "learned" european's GPU was 2.1 GB.** Once gpt-oss was unloaded,
   the spilled llama3.2 copy stayed partly on the CPU, because Ollama doesn't move it
   back. Two polls in a row showed a spill with only 2.05 GiB on the GPU, and the
   capacity rule took that as european's whole capacity (down from 14,267 MB). From then
   on almost nothing could be placed on european.
3. **Every embedding forced a reload.** nomic-embed-text's maximum context is 2048, so
   Ollama loads it at 2048 whatever is asked. llm-head expected the 4096 default, never
   counted the loaded copy as matching, and unloaded and reloaded it for each request.
   That added 1–7 s to every retrieval.

## Impact

- **newshelper chat:** the one question asked, at 21:37:47, got the "can't reach the
  index" reply. newshelper's chat traffic is very light; this was the only retrieval
  in `rag_serve`'s journal since 2026-10-01.
- **Other clients:** none failed. From 21:37:48 to 21:47, european could take almost no
  new model. A gpt-oss request in that window would have had to go to xmas, evicting qwen
  or llama3.2. None arrived.

## Timeline (UTC)

| Time | Event |
|---|---|
| 13:03 | `b4a9cad` (spill detection) deployed |
| 13:04:37 | A real spill: llama3.2 beside gpt-oss on european. Capacity correctly learned as 14,267 MB |
| 21:37:47 | newshelper retrieval → `api/embed` for nomic-embed-text. Placement on european evicts gpt-oss (`cold_load_evict`) |
| 21:37:48 | `Endpoint GPU capacity learned`: european 2,102 MB, from the lingering spilled llama3.2 |
| 21:37:52 | Chat proxy's 5 s retrieval timeout fires: "This operation was aborted". User gets the "can't reach the index" reply |
| 21:37:54 | Embedding completes (6.8 s); `rag_serve` hits a broken pipe writing to the proxy that had gone |
| 21:38 | Reported |
| 21:47 | Mitigated (below). Test chat through the proxy: HTTP 200 with cited news in 12.3 s |
| 21:48 | Bug 3 confirmed: consecutive embeddings show `reload_context`, then `loading` |

## Mitigation (21:47)

On lampoon, with nothing in flight:
1. Backed up `stats.json` to `stats.json.pre-capfix`.
2. Stopped llm-head.
3. Unloaded the spilled llama3.2 on european.
4. Set `host_vram.european` back to 14960096704 (14,267 MB), the value learned at
   13:04.
5. Started llm-head.

Resetting to the configured figure wouldn't have been enough. llm-head would again
pair gpt-oss with llama3.2, Ollama would spill, and the same lingering-copy bug would
recur the next time gpt-oss was unloaded.

Bug 3 remains live until the fix is deployed. Each nomic embedding after a few minutes
idle reloads, taking about 1–4 s. That's under the proxy's 5 s, but without much margin.

## Fixes (committed, not yet deployed)

| Bug | Fix | Test |
|---|---|---|
| 1 | A spilled copy no longer counts toward `keep_warm`, and is never protected from eviction | `test_spilled_copy_is_evicted_before_a_full_one` |
| 2 | Capacity is learned only from a fresh spill: one that just appeared, with the same models still loaded on the next poll. A spill implying under half the configured memory is never learned; it's logged as `Endpoint GPU capacity not learned` | `test_a_lingering_spill_does_not_shrink_capacity`, `test_a_spill_implying_a_tiny_gpu_is_not_learned` |
| 3 | When a fresh load comes up with less context than asked, that's recorded as the model's maximum (`max_ctx` in the stats file, `Model context capped` in the log). Requests for more are matched to it | `test_a_model_capped_below_the_default_context_is_not_reloaded_every_time` |

Every new test fails on the previous code. The fake Ollama now caps nomic-embed-text at
2048 and handles `keep_alive: 0` on `/api/embed`, as real Ollama does.

## What went wrong in how we worked

1. **The spill-capacity rule was tested only on the spill that prompted it.** The
   lingering-copy case, where a neighbour unloads and the spill stays, follows directly
   from Ollama's behaviour, and the day's own report described it. No test covered it.
2. **A learned value with no bounds.** Nothing stopped a learned capacity from being
   absurdly low. A simple sanity floor would have contained bug 2.
3. **Post-deploy verification covered the case we expected.** The 13:04 check confirmed
   the spill was learned correctly, but didn't watch what happened when the layout
   changed afterwards.

## Follow-up

| # | Action | Status |
|---|---|---|
| 1 | Deploy the fixes, then check `/internal/queue` and an embed pair (`loaded` on the second) | Waiting for approval |
| 2 | newshelper: raise `RETRIEVAL_TIMEOUT_MS` from 5000 to about 15000, so one cold embedding load can't empty the answer | Proposed, newshelper repo |
| 3 | Consider `keep_warm: 1` for nomic-embed-text, so retrieval is always warm | Open. It takes one of the two model slots on a host |
| 4 | Delete `stats.json.pre-capfix` and `stats.json.pre-b4a9cad` on lampoon once the fix is deployed and stable | Open |
