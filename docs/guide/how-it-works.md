# How it works

This explains what llm-head decides and why, so you can predict where a request will go
and read the logs. The design history and measurements are in
[`docs/specs/load-aware-scheduler.md`](../specs/load-aware-scheduler.md).

- [The problem](#the-problem)
- [What llm-head tracks](#what-llm-head-tracks)
- [The request path](#the-request-path)
- [Placement rules](#placement-rules)
- [Context size](#context-size)
- [The queue](#the-queue)
- [Keep-warm](#keep-warm)
- [CPU spill detection](#cpu-spill-detection)
- [Hosts that stop loading models](#hosts-that-stop-loading-models)
- [Retries](#retries)
- [What it learns](#what-it-learns)

## The problem

On a small cluster of consumer GPUs, what a request costs depends mostly on whether its
model is already in GPU memory:

| Situation | Typical cost |
|---|---|
| Model loaded, slot free | Milliseconds before the first token |
| Model must be loaded | Several seconds (about 4–8 s for a 12 GB model) |
| Model partly on the CPU | Generation 2–8× slower for the whole request |
| Waiting in Ollama's own queue | Unbounded, and invisible to the proxy |

A proxy that spreads requests by open connections ignores all of this. It sends requests
to hosts without the model loaded, which evicts models that other requests need, and
piles requests into Ollama's hidden queue.

## What llm-head tracks

For every host, polled continuously:

- **health**: `GET /` every 5 s;
- **loaded models**: `GET /api/ps` every second, which gives each model's size, how much
  of it is in GPU memory, and its context size;
- **installed models**: `GET /api/tags` every 5 minutes;
- **in-flight requests**: counted by llm-head itself, per model.

From those it knows, at any moment, which host can serve a request immediately.

## The request path

1. A client sends a request to `/olla/ollama/<path>`.
2. If it's not an inference request (for example `api/tags`, `api/show` or
   `api/version`), it's either answered by llm-head or forwarded straight to the least
   busy suitable host. It doesn't queue.
3. Inference requests (`api/generate`, `api/chat`, `api/embed`, `api/embeddings`, and the
   OpenAI `v1/` equivalents) join the queue.
4. Placement looks at every host and either picks one, or leaves the request waiting.
5. If placement planned evictions, llm-head unloads those models first (a `keep_alive: 0`
   request to Ollama).
6. The request is forwarded unchanged, and the response is relayed byte for byte,
   streaming included.
7. When the response ends, the slot is released, and the next waiting request is placed.

## Placement rules

In order:

1. **Loaded with a free slot:** send it to the least busy host that has the model
   loaded, at the right context size and fully on the GPU.
2. **Loaded, but every slot busy:** wait for a slot, unless loading another copy on
   another host is expected to be quicker than waiting.
3. **Fits in free GPU memory somewhere:** load it there without evicting anything.
4. **Otherwise:** evict the cheapest set of idle models on the host where that costs
   least. A model with requests in flight is never evicted. Evicting a model below its
   `keep_warm` target is a last resort.
5. **Never place a model where it would spill onto the CPU**, unless its policy has
   `allow_cpu_offload`, or it's too big for any host even when that host is empty.

Ties go to the host with fewer requests in flight, then to the model's `home` hosts,
then rotate so one host doesn't take every tie.

The decision is in the `X-Olla-Routing-Reason` response header and the `placement`
field of the `Request dispatching` log line:

| Reason | Meaning |
|---|---|
| `loaded` | Model was loaded there with a free slot |
| `loading` | Model is being loaded there already; the request waits for that load |
| `replica_cheaper_than_wait` | All copies were busy, and loading another copy was quicker than waiting |
| `cold_load` | Loaded onto a host with room. No eviction |
| `cold_load_evict` | Other idle models were unloaded first to make room |
| `reload_context` | Model was loaded at a different context size, and is reloaded at the size this request needs |
| `reload_spilled` | Model was partly on the CPU, and is reloaded with room to fit fully |
| `cpu_offload_unavoidable` | Model is too big for any host's GPU. Placed on the biggest one once it was idle |

A request that is waiting is shown in `/internal/queue` and logged as `Request queued`.
It waits either because all copies are busy (`slots_busy`) or because nothing can be
evicted yet (`no_capacity`).

## Context size

Ollama loads a model at a fixed context size (`num_ctx`). A request asking for a
different size makes Ollama reload the model, and Ollama only does that once the loaded
copy has no requests running.

So llm-head treats "loaded at 4096" and "loaded at 8192" as different things:

- a request for 8192 doesn't count a copy loaded at 4096 as warm;
- it reloads that copy only while it's idle, and otherwise uses or loads another one;
- a request that doesn't set `num_ctx` gets the host's `default_num_ctx`.

The most common cause of avoidable cold loads is a client that sends `num_ctx` on some
requests but not others, such as a warmup or health probe without it. Have such clients
always send the same `num_ctx`.

## The queue

llm-head never sends a host more requests for a model than `slots_per_model`. Everything
else waits at the head, where it's visible, ordered and bounded:

- **Order:** by arrival time, minus the client class's `boost`. Interactive clients can
  get ahead of batch work without starving it.
- **Head-of-line:** once the oldest waiter for a model has waited `head_of_line_after`
  (10 s), newer requests for that model wait behind it, so they can't keep taking the
  slots it's waiting for.
- **Bound:** after `max_wait` (120 s), the request gets `503` with a `Retry-After`
  header, instead of hanging until the client times out.
- **Client disconnects:** a client that disconnects while waiting is removed from the
  queue. Its slot is released if it had just been given one.

## Keep-warm

Every 15 seconds, for each model with `keep_warm: N` (exact names only, not globs),
llm-head counts the hosts that have it loaded at the right context size and fully on the
GPU. If fewer than `N`, it loads it on one more host, with `keep_alive: 30m`. It only
uses a host that:

- is healthy and not draining;
- has nothing in flight;
- has room for the model beside what's already loaded. Keep-warm never evicts.

It doesn't run while any request is waiting. It loads at `models.<name>.num_ctx` if set,
otherwise at the context size the model is most often requested at, learned from recent
traffic.

Look for `Warming model` lines in the log.

## CPU spill detection

When a model doesn't fit fully in GPU memory, Ollama puts the rest on the CPU, and that
model then generates much more slowly. Measured on a 16 GB GPU, llama3.2 ran at 68 tok/s
when spilled next to gpt-oss:20b, against 168 tok/s fully on the GPU.

llm-head's memory model (`vram_mb - reserve_mb`, minus what's loaded) is only an
estimate. Ollama's real overhead can be higher, so a pair of models that "fits" on paper
can still spill. llm-head handles this as follows:

1. **Detect.** `/api/ps` reports both `size` and `size_vram` for each model. When
   `size_vram < size`, the model is spilled. llm-head logs `Model spilled onto CPU`
   (WARN), and lists it under `spilled` in `/internal/queue`.
2. **Avoid.** A spilled copy doesn't count as loaded. Requests go to a full copy
   elsewhere, or wait for one, or load one. A spilled copy is used only if the policy
   allows CPU offload, or if the model is too big to fit fully anywhere.
3. **Learn.** Once the spill shows on two polls in a row, llm-head takes what was on the
   GPU at that moment as the host's real capacity. It logs `Endpoint GPU capacity
   learned` and uses the lower figure for placement from then on. Two polls are needed
   so a poll that catches a load half-finished doesn't count.
4. **Remember.** The learned capacity is saved in `stats_file`, so it survives
   restarts. It's raised only if a larger set of models is later seen fully on the GPU.
   In practice that doesn't happen, because placement no longer loads more than the
   learned figure.

**The learned figure only goes down.** If something else ever caused a spill, for
example another process using the GPU, the host stays underrated until you reset it:

```bash
curl -X POST localhost:40114/internal/hosts/<name>/reset-capacity
```

If the spill is real, the next spill teaches it again.

## Hosts that stop loading models

An Ollama host can stop loading models while still serving the one it has loaded and
passing every health check. This happened in production for 5 hours; see
[`docs/reports/2026-09-30-xmas-ollama-wedge.md`](../reports/2026-09-30-xmas-ollama-wedge.md).
llm-head guards against it in three ways:

- **Load watchdog.** A request that needs a model loaded watches `/api/ps` on its host.
  If the model hasn't appeared after `proxy.load_timeout` (45 s), or 4 × its learned
  load time if longer, the request is abandoned before any response byte and retried on
  another host.
- **Quarantine.** That model is then avoided on that host for `proxy.model_quarantine`
  (5 minutes), doubling on each repeat up to an hour. A successful request clears the
  quarantine. If every host with the model is quarantined, llm-head tries one anyway
  rather than failing outright.
- **Stalls.** A streaming response that sends nothing for `stall_timeout` (60 s) is
  ended. Since the client already has part of the answer, it isn't retried. The model is
  quarantined on that host.

Logs to look for: `Model quarantined on endpoint` and `Model quarantine cleared`.

## Retries

A request is retried on another host only if nothing has been sent to the client yet:

- the host couldn't be reached (connection error);
- it answered `502`, `503` or `504`;
- it sent no response headers within `response_header_timeout`;
- the model didn't load in time (the load watchdog).

Up to `proxy.max_attempts` attempts, one per host by default. Once the response has
started, a failure ends the request, because resending the prompt could produce a
duplicate or contradictory answer.

## What it learns

| What | From | Used for |
|---|---|---|
| GPU memory per model | `size_vram` of full loads in `/api/ps` (the largest seen) | Whether a model fits, and what to evict |
| Load time per model | `load_duration` in responses to cold loads | Cost of a cold load vs. waiting; the load watchdog's deadline |
| Request duration per model | Measured | Expected wait for a slot; `Retry-After` |
| Usual context size per model | Recent requests' `num_ctx` (the last ~30 count most) | What size keep-warm loads at |
| Real GPU capacity per host | Spills (see above) | Usable memory in placement |

Before a model has been seen, llm-head estimates from its file size: 1.15 × file size for
memory, 1 s + file size at 1.5 GB/s for load time, and 5 s for a request.
