# HTTP API

llm-head serves two sets of routes:

- **`/olla/*`**: the model API clients use. It's Ollama's API behind a prefix, the same
  as [Olla](https://github.com/thushan/olla).
- **`/internal/*`**: status and control, for dashboards, monitoring and operators.

The default port is 40114.

- [Model API](#model-api)
- [Response headers](#response-headers)
- [Errors](#errors)
- [Status endpoints](#status-endpoints)
- [Control endpoints](#control-endpoints)

## Model API

Use `http://<head>:40114/olla/ollama` as the Ollama base URL. Anything after the prefix
is Ollama's own path.

| Path (after `/olla/ollama/`) | Handling |
|---|---|
| `api/generate`, `api/chat`, `api/embed`, `api/embeddings` | Queued and placed by the scheduler, then forwarded |
| `v1/chat/completions`, `v1/completions`, `v1/embeddings`, `v1/responses` | Same, OpenAI-compatible |
| `GET api/tags` | Answered by llm-head: every model installed on any healthy host, listed once |
| `GET v1/models` | Answered by llm-head: the same list in OpenAI format |
| `api/pull`, `api/push`, `api/create`, `api/copy`, `api/delete`, `api/list` | `501`. These would act on one arbitrary host. Run them on each host directly |
| Anything else (`api/show`, `api/ps`, `api/version`, ...) | Forwarded to the least busy healthy host that has the model (if one is named), without queueing |

Notes:

- **`api/ps` through llm-head shows one host,** not the cluster. Use
  [`/internal/queue`](#get-internalqueue) to see what's loaded everywhere.
- **Model names are case-insensitive,** and an untagged name means `:latest`.
  `QWEN2.5VL:7B-Q4_K_M` and `qwen2.5vl:7b-q4_k_m` are the same model. Hosts always
  receive the spelling they reported themselves.
- **Unloading:** a request with `"keep_alive": 0` and no prompt goes, without queueing,
  to a host that has the model loaded. It unloads it from that one host only.
- **Client credentials** (`Authorization`, `X-Api-Key`, cookies) are never forwarded to
  hosts.
- **Request IDs:** a client's `X-Request-ID` header is reused if it's printable and at
  most 128 characters. Otherwise llm-head makes one. Either way it's returned as
  `X-Olla-Request-Id` and appears in every log line for the request.

`GET /olla/models` lists every model with the hosts it's installed on, and whether it's
loaded on each, in Olla's extended format.

## Response headers

| Header | Meaning |
|---|---|
| `X-Olla-Request-Id` | Request ID; search the log for it |
| `X-Olla-Endpoint` | Host that served the request |
| `X-Olla-Model` | Model name, normalized |
| `X-Olla-Response-Time` | Time until the host's response headers arrived |
| `X-Olla-Routing-Reason` | Placement decision, e.g. `loaded` or `cold_load_evict`. All reasons are listed in [how-it-works.md](how-it-works.md#placement-rules) |
| `X-Llm-Head-Queue-Ms` | Time the request waited at the head for a slot |
| `X-Llm-Head-Cold-Load` | `true` when the model had to be loaded for this request |
| `X-RateLimit-Limit`, `-Remaining`, `-Reset` | Per-IP rate limit, informational |

Browsers can read these through CORS (`Access-Control-Expose-Headers`).

## Errors

| Status | Body | When | What the client should do |
|---|---|---|---|
| `404` | `No ollama endpoints available` (text) | No host has the model, or no healthy host does | Check the model name with `api/tags` |
| `413` / `431` | text | Body or headers over `server.request_limits` | Send less |
| `429` | `Too Many Requests` (text) | Rate limit. Has `Retry-After` | Wait `Retry-After` seconds |
| `501` | text | Model-management path (`api/pull`, ...) | Run it on the host directly |
| `502` | `Proxy error: all endpoints failed: <last error>` (text) | Every attempt failed before the response started | Retry later; see [troubleshooting](troubleshooting.md#clients-get-502) |
| `503` | `{"error": "No capacity for <model> after waiting 120s", "reason": "queue_timeout"}` | Waited `queue.max_wait` without a slot. Has `Retry-After` | Retry after `Retry-After`; see [troubleshooting](troubleshooting.md#clients-get-503-queue_timeout) |

The text errors match Olla's exactly. The `503` is JSON with an `error` field, so
Ollama client libraries show the message.

A host's own errors (for example Ollama's `400` for a bad request) are passed through
unchanged.

## Status endpoints

All are `GET`, open to any client, and never rate limited.

### `GET /internal/health`

`{"status":"healthy"}` whenever the process is up. It doesn't check the hosts; use
`/internal/status` for that.

### `GET /internal/queue`

The most useful view for operators: what's waiting, and each host's live state.

```json
{
  "waiting": [
    {"request_id": "…", "model": "gpt-oss:20b", "class": "batch", "waited_ms": 3400}
  ],
  "hosts": {
    "gpu1": {
      "status": "healthy", "draining": false,
      "loaded": ["llama3.2:3b", "qwen2.5vl:7b-q4_k_m"],
      "spilled": [],
      "vram_usable_mb": 15799,
      "pending_loads": [],
      "inflight": {"qwen2.5vl:7b-q4_k_m": 1}
    },
    "gpu2": {
      "status": "healthy", "draining": false,
      "loaded": ["gpt-oss:20b", "llama3.2:3b"],
      "spilled": ["llama3.2:3b"],
      "vram_usable_mb": 14267,
      "pending_loads": [],
      "inflight": {}
    }
  }
}
```

| Field | Meaning |
|---|---|
| `status` | `healthy`, `offline` or `unknown` (not checked yet) |
| `draining` | `true` after `/drain`: no new requests are sent there |
| `loaded` | Models in `/api/ps` on that host |
| `spilled` | Loaded models partly on the CPU. Requests avoid these copies |
| `vram_usable_mb` | GPU memory placement works with: `vram_mb - reserve_mb`, or less once a spill has shown the real capacity |
| `pending_loads` | Loads llm-head started that `/api/ps` doesn't show yet |
| `inflight` | Requests running there, per model |

### `GET /internal/status`

Olla's status format, plus llm-head fields. Monitoring uses
`system.total_requests` and `system.total_failures`.
- `total_failures` counts every request whose client saw a failure: a `5xx`, a queue
  timeout, or a stream that failed or was abandoned.
- Each entry in `endpoints` adds `loaded_models`, `spilled_models`, `vram_used`,
  `vram_usable` and `inflight`.
- `queue` shows how many requests are waiting, per model.
- `system.commit` is the `LLM_HEAD_COMMIT` environment variable, if set.

### Others

| Endpoint | Content |
|---|---|
| `GET /internal/status/endpoints` | Per host: status, last health check, response time, success rate |
| `GET /internal/status/models` | Installed models grouped by family, with the hosts that have each |
| `GET /internal/process` | Python version, uptime, memory, task count |
| `GET /version` | Name, version, capabilities |

## Control endpoints

`POST` only, and only from localhost. Run them with `curl` on the head itself. From
anywhere else they return `403`. An unknown host name returns `404`.

| Endpoint | Effect |
|---|---|
| `POST /internal/hosts/{name}/drain` | Stop sending new requests to the host. Requests already running finish. Returns `{"host", "draining": true, "inflight": N}` |
| `POST /internal/hosts/{name}/undrain` | Put it back in rotation |
| `POST /internal/hosts/{name}/reset-capacity` | Forget the GPU capacity learned from a spill and go back to the configured `vram_mb - reserve_mb`. Returns `{"host", "vram_usable_mb"}` |

All three are logged at WARN. Drain state isn't saved: a restart puts every host back
in rotation.
