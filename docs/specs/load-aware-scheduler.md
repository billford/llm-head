# Load-aware scheduler for the lampoon LLM cluster

Status: **Draft for review** · 2026-09-29

## 1. Problem

Lampoon runs Olla v0.0.28 (`/opt/olla`, port 40114) in front of two Ollama hosts,
`xmas` and `european`. Jobs pile up on one host and stall there while the other host
sits idle. We want to add a third GPU host, and adding one to the current setup would
spread the same problem across three hosts.

## 2. What the logs show

Source: every `Request completed` line in `/opt/olla/logs/` from 2026-09-24 20:47 to
2026-09-29 23:59. That is 5,973 requests, all HTTP 200. Olla's lifetime counters in
`/internal/status` agree with it.

Both hosts have the same hardware: an RTX 5060 Ti with 16 GB, 30 GB of RAM and 16 cores.
Both run stock Ollama 0.32.x with only `OLLAMA_HOST` set. Every model is pulled on both.

### 2.1 Traffic splits 2:1 on identical hardware

| | xmas | european |
|---|---|---|
| Requests this week | 4,091 | 1,882 |
| Requests over Olla's lifetime | 15,235 | 7,342 |

Olla is set to `least-connections`, not round-robin. Our traffic is almost serial: no
more than 2 requests were ever in flight at once. So both hosts are usually at 0
connections, and the tie goes to the first endpoint in the list, which is xmas. The
balancer ends up acting as "xmas unless it's busy."

### 2.2 The balancer ignores which models are loaded, so xmas thrashes

Over 4,091 requests, xmas switched models **2,428 times**, mostly back and forth between
`qwen2.5vl:7b` and `llama3.2:3b`. The same model on the same hardware performs very
differently on the two hosts:

| Model | Host | p50 | p95 | max | TTFT max |
|---|---|---|---|---|---|
| qwen2.5vl:7b | european | 641 ms | 696 ms | 17.5 s | 4.3 s |
| qwen2.5vl:7b | xmas | 641 ms | **5,471 ms** | **35.2 s** | **19.1 s** |
| llama3.2:3b | xmas | 392 ms | 2,677 ms | 12.6 s | 4.2 s |
| llama3.1:8b | xmas | 1,781 ms | 14,604 ms | 19.7 s | 2.5 s |

TTFT (time to first token) is normally 20 ms. When it reaches multiple seconds, the model
was being loaded from cold. 57 requests waited more than 3 seconds for their first token.

### 2.3 Memory contention pushes gpt-oss onto the CPU. This is the "stuck" behavior.

`gpt-oss:20b` is 13.8 GB. When xmas already has qwen or llama loaded, Ollama can't fit
gpt-oss on the 16 GB card. It puts part of the model on the CPU instead:

| gpt-oss:20b | tokens/s | p95 | max |
|---|---|---|---|
| european (whole model on GPU) | ~94 | 17.4 s | 24.9 s |
| xmas, partly on CPU | **~12.3** | **37.3 s** | **75.6 s** |

The seven slowest requests of the week all ran on xmas at 12.3 tokens/s. The load balancer
can't see this happening. All it knows is that the connection is still open.

### 2.4 Other ways the setup degrades

- **Ollama's hidden queue.** Ollama queues requests internally (default queue depth 512).
  Olla sees one open connection per request and can't tell whether that request is
  generating, waiting in Ollama's queue, or waiting for a model to load.
- **Health checks that flap.** `check_timeout: 1s` marked xmas offline 4 times this week
  because it was busy loading a model, not down. A busy host is not a dead host.
- **15-minute hangs.** `response_timeout: 15m` with no queue timeout means a stalled
  request hangs for up to 15 minutes instead of failing fast with a retry hint.
- **Two requests ended with 0 tokens** after about 33 s (gpt-oss on xmas, 09-28 and
  09-29). Neither was logged as an error.

**Root cause:** the load balancer counts TCP connections. The cost of a request here
depends on whether the model is already loaded, whether it fits in free GPU memory, and
how many requests are queued inside Ollama. Olla can't see any of those.

## 3. Options considered

| Option | Verdict |
|---|---|
| **A. Tune Olla and Ollama in place** | Worth doing now as Phase 0. It doesn't fix the root cause, because none of Olla's balancers (priority, round-robin, least-connections) take loaded models or GPU memory into account. |
| **B. LiteLLM router** | Routes by latency, usage or cost, but can't see which models are loaded or how much GPU memory is free. It would also add a second API layer with its own schema. |
| **C. GPUStack or a vLLM stack** | Solves scheduling but replaces Ollama and our model management, including the `writingstyle-lora` models. Too heavy for two or three consumer 16 GB cards. |
| **D. Custom scheduler that exposes Olla's interface** (recommended) | A small service that exposes the same API, logs and status data as Olla, so clients and the dashboard don't change. The routing logic is our own and knows about loaded models. |

Before starting D, check whether Olla has shipped a balancer that knows about loaded
models since v0.0.28. If it has, re-evaluate A.

## 4. Recommended design

### 4.1 Topology

```
clients (workstation batch jobs, home-automation agent, local services)
        │  :40114  (unchanged)
        ▼
┌──────────────────── lampoon: llm-head ─────────────────────┐
│  Olla-compatible API layer (/olla/*, /internal/*, /version) │
│  admission: rate limits, body limits, CORS                  │
│  per-model queues (priority classes, deadlines)             │
│  placement engine  ◄── host state poller (/api/ps, /api/tags│
│  dispatcher / stream relay / retry                          │
│  JSON logs (Olla schema) ─► /opt/olla/logs/olla.log         │
└──────────┬──────────────────┬───────────────────┬───────────┘
           ▼                  ▼                   ▼
        xmas:11434      european:11434       host3:11434
```

### 4.2 Core idea: the head is the only queue

- **Every request goes through the head**, so its own count of in-flight requests per
  host and model is accurate. It doesn't need to poll anything to know current load.
- **Each host gets a slot count per model**, matching that host's `OLLAMA_NUM_PARALLEL`.
  The head never sends a host more requests than it has slots, and `OLLAMA_MAX_QUEUE` is
  set small. Requests that don't have a slot yet wait at the head, where we can see them,
  prioritize them and time them out.
- **A host state poller** (every 1 s) reads `/api/ps` on each host to learn which models
  are loaded, how much GPU memory each uses, and when each will be unloaded. It reads
  `/api/tags` every 5 minutes for the list of installed models. Loaded GPU memory is the
  sum of `size_vram`, compared against each host's `vram_mb` from config. No agent is
  needed on the GPU hosts.

### 4.3 Choosing a host

For a request for model M, pick the first rule that matches:

1. **M is already loaded and a slot is free.** Pick the loaded host with the fewest
   in-flight requests. Break ties with a rotating index, not list order. This fixes the
   2:1 skew.
2. **M is loaded everywhere it runs, but all slots are busy.** Compare the expected wait
   (queue depth × that model's median duration, which the head measures) with the cost of
   loading M cold on an idle host (load time, also measured). Wait if waiting is cheaper,
   otherwise go to step 3.
3. **M fits in free GPU memory on some host** without unloading anything. Load it there.
   Prefer the model's configured `home` host.
4. **Something has to be unloaded.** Choose the host where the models to unload have no
   requests in flight and were used least recently. **Never unload a model that has a
   request in flight.**
5. **Never place M where it would spill onto the CPU** (model size + KV cache >
   `vram_mb`) unless the model sets `allow_cpu_offload: true`. Otherwise M waits in the
   queue.

Measured costs (per-model median duration and cold-load time for each host) are kept in
memory and saved to `/opt/olla/data/stats.json` so they survive restarts.

### 4.4 Placement policy (config, optional)

```yaml
hosts:
  - name: xmas
    url: http://xmas.lan:11434
    vram_mb: 16311
    slots_per_model: 2
  - name: european
    url: http://european.lan:11434
    vram_mb: 16311
    slots_per_model: 2
models:
  gpt-oss:20b:        { home: [european], keep_warm: 1 }   # needs a card nearly to itself
  qwen2.5vl:7b-q4_k_m: { keep_warm: 2 }                    # hottest model: 51% of traffic
  llama3.2:3b:        { keep_warm: 2 }                     # 45% of traffic
  "writingstyle-lora:*": { allow_cpu_offload: false }
queue:
  max_wait: 120s          # then 503 + Retry-After instead of hanging
  classes:                # matched on client IP or X-Priority header
    interactive: { match: ["10.0.0.20", "127.0.0.1"], weight: 4 }  # home-automation host, local services
    batch:       { default: true, weight: 1 }
```

`keep_warm: N` tells the head to keep M loaded on N hosts, reloading it with a
`keep_alive` ping when needed. Everything else is loaded on demand.

With this week's traffic mix, two hosts would settle into one serving qwen and llama3.2
(about 8 GB together) and the other serving gpt-oss plus small models. A third host
removes the remaining conflict, since gpt-oss can then have a card to itself.

### 4.5 Failure handling

- **Health.** Active checks every 2 s with a **3 s** timeout. A host is marked down after
  **3 consecutive** failures. Passive signals count too: a connection refused or a 5xx
  response before the first byte counts as a failure. A host that is busy but responding
  is never marked down.
- **Retry.** If a request fails before its first byte (connection error, 5xx), send it to
  the next-best host, at most 2 attempts in total. **Never retry once streaming has
  started.** A resent prompt would produce a duplicate or contradictory response.
- **Stalls.** If no tokens arrive for 60 s mid-stream, abort the request, log it as
  `Request failed` with the reason, and count it against the host. This catches the
  0-token hangs in §2.4.
- **Drain.** `POST /internal/hosts/{name}/drain` stops new work going to a host and lets
  its in-flight requests finish. That makes reboots safe, for example a pending
  NVIDIA reboot, and it fits with the `safe_to_reboot` field from `check_pending_reboot`.

## 5. Feature parity with Olla

Nothing ships until every row below is marked ✅ by a contract test that sends the same
request to Olla (:40114) and to llm-head (:40115) and compares the results.

| # | Olla feature (as configured today) | Consumed by | llm-head |
|---|---|---|---|
| P1 | `:40114`, bind `0.0.0.0` | all clients | same |
| P2 | `/olla/ollama/api/*` passthrough (generate, chat, embed/embeddings, tags, show, ps, …), NDJSON streaming | batch jobs, home-automation agent, local RAG and chat services | same paths, byte-for-byte relay |
| P3 | `/olla/ollama/v1/*` OpenAI-compatible passthrough | no known client; kept because it's a cheap passthrough (Ollama serves it natively) | same |
| P4 | `/olla/models` (OpenAI-shaped, per-endpoint availability) | agents | same shape |
| P5 | `/internal/health`, `/internal/status`, `/internal/status/endpoints`, `/internal/status/models`, `/internal/process`, `/version` | dashboard, monitoring health checks | same JSON shapes, plus added fields (queue depth, loaded models) |
| P6 | Anthropic translation (`translation_anthropic`, experimental) | no known client | **out of scope for v1**. During shadow testing, log any request to a route llm-head doesn't implement, then decide. |
| P7 | CORS: `*` origins, GET/POST/OPTIONS, all headers, max-age 3600 | browser widgets | same |
| P8 | Rate limits: 1000/min global, 100/min per IP, 1000/min health, burst 50, 200/min per endpoint; trusted proxy CIDRs | all | same values, same 429 behavior |
| P9 | Request limits: 100 MB body, 1 MB headers | vision uploads (qwen2.5vl) | same |
| P10 | Timeouts: 10 s header read, 120 s response header, 15 m response, 10 m read | long gpt-oss jobs | same, plus the new queue `max_wait` and stall detection |
| P11 | Retry on connection failure | all | improved (§4.5) |
| P12 | Health checks (2 s interval), recovery triggers model rediscovery | dashboard | same, with fixed timeouts |
| P13 | Model discovery every 5 m, **model name unification** (e.g. names are lower-cased, as in `qwen2.5vl:7b-q4_k_m`), strict routing only to hosts that have the model | all | same normalization rules |
| P14 | Response headers `X-Olla-Endpoint`, `X-Olla-Model`, `X-Olla-Request-ID`, `X-Olla-Response-Time`, `X-Olla-Backend-Type` | debugging, possibly clients | same headers |
| P15 | JSON log to `/opt/olla/logs/olla.log`, rotated and gzipped; messages `Access log`, `Request dispatching`, `Request completed` (with `ttft_ms`, `tokens_per_sec`, tokens), `Request failed`, `Endpoint status changed` | dashboard `/logs/*`, this analysis | same messages and fields, plus new `Request queued` and `Model placed` |
| P16 | systemd unit with `NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome=true` | ops | same |

Step 1 of the build is capturing a request corpus: sample traffic from each client with
prompts replaced. The contract tests replay that corpus against both services.

## 6. Rollout plan

**Phase 0: quick wins with no new code (about 1 hour, reversible)**
- On each GPU host, add to `ollama.service.d/override.conf`:
  `OLLAMA_KEEP_ALIVE=30m`, `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_NUM_PARALLEL=2`,
  `OLLAMA_FLASH_ATTENTION=1`.
- In the Olla config: `check_timeout: 3s`.
- Reboot european during a quiet period to clear the NVIDIA driver mismatch.
- Expected result: fewer cold loads. The 2:1 skew and the CPU spill stay.

**Phase 1: build llm-head (Python: FastAPI, httpx, uvicorn, single process)**
- Python because the existing dashboard uses it, and at about 1,200 requests a day async
  Python has plenty of headroom.
- Modules: `api/` (Olla-compatible routes), `state/` (poller and in-flight counts),
  `sched/` (queues and placement), `relay/` (streaming and retry), `obslog/` (Olla log
  schema).
- Tests: unit tests for placement against recorded `/api/ps` snapshots, plus the parity
  suite from §5.

**Phase 2: shadow testing, then cutover**
- Run on `:40115` and replay the corpus. Compare logs for a week of synthetic load.
- Cutover: move Olla to `:40116`, start llm-head on `:40114`. Rolling back means swapping
  the ports back with two `systemctl` commands. Keep the Olla binary and config in place
  for 30 days.

**Phase 3: add the third host**
1. Install Ollama with the Phase 0 settings and pull the models.
2. Install the dashboard's SSH public key for its stats collector. Check that the
   `authorized_keys` line has a space between the key and the comment.
3. Add a `hosts:` block to the llm-head config and hot-reload it.
4. Dashboard: move the hardcoded `GPU_HOSTS` into the shared config.
5. Monitoring: add the host to your checks. In our deployment that means an Icinga host
   object plus hand-installed pending-reboot and NVIDIA plugins.

**Phase 4: dashboard additions**
- Per host: loaded models, GPU memory used vs. total, slots in use.
- Cluster-wide: queue depth per model, recent placement decisions, cold-load count.

## 7. Success criteria (vs. this week's baseline)

| Metric | Baseline | Target |
|---|---|---|
| Request split across identical hosts | 68 / 32 | within 60 / 40 |
| Model switches per 1,000 requests (busiest host) | 594 | < 50 |
| qwen2.5vl p95 | 5.5 s (xmas) | < 1 s on every host |
| gpt-oss:20b max duration | 75.6 s | < 25 s |
| Requests dispatched with CPU offload | many (12 tok/s runs) | 0 unless `allow_cpu_offload` |
| Longest stall before failing | 15 min | 120 s (queue) / 60 s (no tokens) |
| Client or dashboard code changes needed | — | 0 |

## 8. Decisions and open questions

Answered 2026-09-29:

1. **Third host: not purchased yet.** The design has to work with hosts that differ,
   which is why `vram_mb` and slots are set per host. On sizing: with a 16 GB card,
   gpt-oss:20b needs the card nearly to itself. With 24 GB or more, gpt-oss fits next to
   a small model like `llama3.2:3b` without unloading anything, so that host could keep
   gpt-oss loaded all the time. This is the strongest argument for a bigger card.
2. **Anthropic translation (P6) and `/v1/*` (P3): probably unused.** P6 is out of scope
   for v1. P3 stays because it costs nothing. Shadow testing will confirm both.
3. **Priority classes: confirmed.** The home-automation agent and lampoon-local services
   are `interactive`. Bulk jobs from the workstation are `batch`.
4. **Repo:** public on GitHub as `billford/llm-head`, MIT licensed.

Still open:

- **Config reload:** hot-reload on SIGHUP, or a restart that drains requests first?
- **Name:** `llm-head` is a working title.
