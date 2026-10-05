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

- **Health.** Active checks every 5 s with a **3 s** timeout. A host is marked down after
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

Parity is tested against responses captured from the live Olla v0.0.28 on 2026-09-30
(`tests/fixtures/olla_v0.0.28_contract.json`, hostnames removed), plus a reading of the
v0.0.28 source for behavior that couldn't safely be triggered on a live system, such as
the 429 response. "✅" means an automated test in `tests/test_parity.py` or
`tests/test_integration.py` checks the behavior. "⏳" means it is covered only by Phase 2
shadow testing against real hosts.

| # | Olla feature | Consumed by | Status |
|---|---|---|---|
| P1 | `:40114`, bind `0.0.0.0` | all clients | ✅ config defaults |
| P2 | `/olla/ollama/api/*` forwarded, NDJSON streaming, body passed **byte for byte** (Olla lowercases the model name for routing only) | all clients | ✅ |
| P3 | `/olla/ollama/v1/*` OpenAI-compatible passthrough | no known client | ✅ |
| P4 | `/olla/models` (OpenAI-shaped, per-endpoint availability) | agents | ✅ every field; availability can also read `loaded` |
| P5 | `/internal/health`, `/internal/status`, `/internal/status/endpoints`, `/internal/status/models` | dashboard, monitoring | ✅ every Olla field present, plus queue depth, loaded models, GPU memory and in-flight counts |
| P5b | `/internal/process`, `/version` | nothing known | Different: they describe Olla's Go runtime and build, so llm-head reports its own |
| P6 | Anthropic translation (`/olla/anthropic/*`) | no known client | Out of scope for v1; those paths return 404 |
| P7 | CORS: preflight 204 with the same headers, `Vary`, and exposed `X-Olla-*` headers | browser widgets | ✅ |
| P8 | Rate limits: token bucket per IP and global, burst 50; applies to proxied routes only; `429 Too Many Requests` as text with `Retry-After` and `X-RateLimit-*` | all | ✅. `health_requests_per_minute` and `per_endpoint` are accepted but ignored, as in Olla |
| P9 | Request limits: `413 Request body too large`, `431 Request headers too large` | vision uploads | ✅ |
| P10 | Timeouts | long gpt-oss jobs | ✅ plus the queue's `max_wait` and stall detection. Olla never enforced `response_timeout`; llm-head does |
| P11 | Retry: connection errors, one attempt per host, never after streaming starts | all | ✅ also retries 502/503/504 received before any response body, but those don't count against the host's health |
| P12 | Health checks, recovery triggers model rediscovery | dashboard | ✅ 5 s interval, 3 s timeout, offline after 3 consecutive failures instead of Olla's backoff |
| P13 | Case-insensitive model matching; strict routing to hosts that have the model; `404 No ollama endpoints available` | all | ✅ also matches an untagged name to `:latest`, which Olla 404s |
| P14 | Response headers `X-Olla-*`, `Via`, `X-Served-By`, `X-RateLimit-*`; model and routing headers only when a model was named | debugging | ✅ `X-Olla-Routing-Reason` gives the placement reason; new `X-Llm-Head-Queue-Ms` and `X-Llm-Head-Cold-Load` |
| P15 | JSON log to `/opt/olla/logs/olla.log`, 1 MB rotation, 7 gzipped backups named in UTC; `Access log`, `Request received`/`dispatching`/`completed`/`failed`, `Endpoint status changed` | dashboard `/logs/*` | ✅ every Olla field, plus `Request queued`, `Evicting model`, `Warming model`, and `placement`/`queued_ms` on dispatch and completion |
| P16 | systemd unit hardening | ops | ✅ `examples/llm-head.service`, which also runs `check-config` before starting |
| P17 | `api/pull`, `push`, `create`, `copy`, `delete`, `list` → `501` with Olla's text | none | ✅ |
| P18 | `api/show` → Olla returns `501` | none | Different: forwarded to a host that has the model |
| P19 | Client `X-Request-ID` reused if printable and ≤ 128 characters | tracing | ✅ |

## 6. Rollout plan

**Phase 0: quick wins with no new code (about 1 hour, reversible)**
- On each GPU host, add to `ollama.service.d/override.conf`:
  `OLLAMA_KEEP_ALIVE=30m`, `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_NUM_PARALLEL=2`,
  `OLLAMA_FLASH_ATTENTION=1`.
- In the Olla config: `check_timeout: 3s`.
- Reboot european during a quiet period to clear the NVIDIA driver mismatch.
- Expected result: fewer cold loads. The 2:1 skew and the CPU spill stay.

*Done 2026-09-30. What happened:*
- **Ollama settings applied on both hosts.** Previous overrides were backed up as
  `override.conf.bak-20260930`. Checked on each host by calling Ollama directly:
  - gpt-oss:20b uses 11.9 GB and runs entirely on the GPU at about 94.5 tokens/s, at
    both 4k and 16k context. `NUM_PARALLEL=2` did not push it onto the CPU.
  - qwen2.5vl and llama3.2:3b now stay loaded together, using 10 GB.
- **Olla requires `check_timeout` to be shorter than `check_interval`.** The first restart
  with `check_timeout: 3s` next to `check_interval: 2s` failed config validation, and
  Olla restarted in a loop for about 20 s until the interval was raised to **5 s**. One
  gpt-oss request in flight at the time was lost. Previous config:
  `config.yaml.bak-20260930`.
- **The european reboot wasn't needed.** It had already rebooted about 5 days earlier,
  and its driver and library versions both read 595.91.07.
- **Lessons for llm-head:**
  1. Validate config before applying it. A bad config must be rejected while the running
     process keeps serving, never discovered by a crash on restart.
  2. The restart tooling has to wait for zero requests in flight (`drain`, §4.5), not
     just report the count.

**Phase 1: build llm-head (Python: Starlette, httpx, uvicorn, single process)**
- Python because the existing dashboard uses it, and at about 1,200 requests a day async
  Python has plenty of headroom. Starlette rather than FastAPI because the proxy streams
  raw bytes and needs no request models.
- Modules:
  - `placement.py`: pure decision function (§4.3).
  - `scheduler.py`: the queue.
  - `cluster.py`: health checks and polling, plus in-flight counts.
  - `relay.py`: streaming, retries and stall detection.
  - `app.py` and `middleware.py`: the Olla-compatible API.
  - `obslog.py`: Olla's log format.
- Tests:
  - placement unit tests using real `/api/ps` sizes;
  - scheduler ordering tests;
  - integration tests against `tests/fake_ollama.py`, which simulates GPU memory, load
    time, LRU eviction, `NUM_PARALLEL` and CPU spill;
  - the parity suite from §5.

*Status 2026-09-30:* built, with 72 tests passing on three consecutive runs. The
integration tests reproduce the problems from §2:
- ties alternating between hosts;
- no more than `slots_per_model` requests sent to a host at once;
- gpt-oss not spilling onto the CPU beside a busy model;
- a slow health check not marking a host offline;
- an HTTP 503 from Ollama not counting against the host.

Two bugs were found and fixed while writing the tests:
- **Eviction chose too many models.** It evicted least-recently-used first, which removed
  two models when removing one would have made room. It now searches for the cheapest
  set of idle models to evict.
- **`keep_warm` could block a request forever.** It was treated as "never evict," so a
  request that needed that memory waited indefinitely. It is now a strong preference: a
  cost penalty, not a veto.

**Phase 2: shadow testing, then cutover**
- Run on `:40115` and replay the corpus. Compare logs for a week of synthetic load.
- Cutover: move Olla to `:40116`, start llm-head on `:40114`. Rolling back means swapping
  the ports back with two `systemctl` commands. Keep the Olla binary and config in place
  for 30 days.

*Status 2026-09-30:* shadow testing ran on lampoon `:40115`; results are in
`docs/reports/2026-09-30-phase2-shadow.md`.
- On identical workloads, llm-head completed 120 of 120 requests in every run, with 3–10
  cold loads against Olla's 56–71.
- qwen2.5vl's p95 was 0.8 s against 9–16 s through Olla.
- Olla hung six requests for 120 s and returned 502s, which is the original problem.
- Shadow testing found two llm-head bugs, both fixed: a missing request-ID header, and
  requests queueing behind a slow in-progress load.

Next: run a 2–3 day canary with the batch client on `:40115`, then cut over.

*Canary 2026-09-30 → 10-03:* 1,682/1,682 real requests succeeded. One latency
regression (photo pairs sharing a GPU) was found and fixed. See
`docs/reports/2026-10-03-canary-review.md`. Cutover steps are in
`docs/specs/cutover-runbook.md`.

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
| Request split across identical hosts, **per model, among the hosts that hold it warm** | 68 / 32 (all models together) | within 60 / 40 |
| Cold loads (first token after more than 3 s) per 1,000 requests | 10.8 (2.0 after Phase 0) | < 1 |
| qwen2.5vl p95 | 5.5 s (xmas) | < 1 s on every host |
| gpt-oss:20b max duration | 75.6 s | < 25 s |
| Requests dispatched with CPU offload | many (12 tok/s runs) | 0 unless `allow_cpu_offload` |
| Longest stall before failing | 15 min | 120 s (queue) / 60 s (no tokens) |
| Client or dashboard code changes needed | — | 0 |

*Split criterion reworded 2026-10-05.* It originally measured all requests together.
That was right for Olla, which ignored models, so any imbalance was skew. Under
model-aware placement the overall split mostly shows which host holds which model:
75 / 25 in the first 24 hours, with llama3.2, the only model on both hosts, at 50 / 50.
See `docs/reports/2026-10-04-post-cutover-24h.md`.

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
