# Configuration reference

llm-head reads one YAML file, given with `-c`. Only `hosts` is required. Everything else
has a default.

Validate any change before restarting:

```bash
llm-head check-config -c /opt/llm-head/config.yaml
```

Validation is strict:
- an unknown key is an error, so a typo is caught rather than ignored;
- cross-checks catch mistakes such as a `home` host that doesn't exist, or a health
  timeout that isn't shorter than its interval.

The systemd unit runs the same check before every start.

**Durations** accept `500ms`, `3s`, `15m`, `1h`, or a bare number of seconds.

A full annotated example is in [`examples/config.yaml`](../../examples/config.yaml).

- [`hosts`](#hosts)
- [`models`](#models)
- [`queue`](#queue)
- [`scheduling`](#scheduling)
- [`proxy`](#proxy)
- [`health`](#health)
- [`discovery`](#discovery)
- [`server`](#server)
- [`logging`](#logging)
- [`stats_file`](#stats_file)

## `hosts`

One entry per Ollama host. At least one is required.

| Key | Default | Meaning |
|---|---|---|
| `name` | required | Short name used in logs, headers and endpoints. Letters, digits, `_ . -` |
| `url` | required | Ollama base URL, e.g. `http://gpu1.lan:11434` |
| `vram_mb` | required | Total GPU memory in MB |
| `reserve_mb` | `512` | Memory kept free for the CUDA context and fragmentation. Usable memory is `vram_mb - reserve_mb` |
| `slots_per_model` | `2` | Requests one loaded model serves at once. **Must equal `OLLAMA_NUM_PARALLEL`** on the host |
| `max_loaded_models` | `2` | Models loaded at once. **Must equal `OLLAMA_MAX_LOADED_MODELS`** |
| `default_num_ctx` | `4096` | Context size Ollama uses when a request doesn't set `num_ctx`. Match `OLLAMA_CONTEXT_LENGTH` if you set it |

**If `slots_per_model` is higher than Ollama's setting,** Ollama queues the extra
requests where llm-head can't see them, and their wait isn't bounded by
`queue.max_wait`. **If it's lower,** GPU capacity goes unused.

**`reserve_mb` is a starting point.** Ollama's real overhead varies by model and context
size. When Ollama has to put part of a model on the CPU because it doesn't fit, llm-head
measures what the GPU actually held and uses the lower figure from then on. See
[how-it-works.md](how-it-works.md#cpu-spill-detection). You don't need to tune
`reserve_mb` by hand to find the limit.

## `models`

Optional per-model policy. Keys are model names or globs (`llama3*`), case-insensitive,
where an untagged name means `:latest`. An exact name beats a glob, and among globs the
longest pattern wins. Models with no entry get the defaults.

```yaml
models:
  qwen2.5vl:7b-q4_k_m:
    keep_warm: 1
    num_ctx: 8192
    home: [gpu1]
  gpt-oss:20b:
    home: [gpu2]
```

| Key | Default | Meaning |
|---|---|---|
| `home` | `[]` | Preferred hosts. For placement, a tie-breaker only: the model still goes wherever it fits. For `keep_warm` models, where the warm copies are kept |
| `keep_warm` | `0` | Keep the model loaded on this many hosts while they're idle. Can't exceed the number of hosts |
| `num_ctx` | learned | Context size keep-warm loads the model at. Default: the size it's mostly requested at, learned from traffic |
| `allow_cpu_offload` | `false` | Allow placing this model where part of it would run on the CPU |

**`keep_warm`** only uses hosts with nothing in flight, never evicts another model to
make room, and pauses while any request is waiting. It ignores glob keys, so name the
model exactly. With `home` set, it also moves the model back home after it has ended up
warm elsewhere; see [how-it-works.md](how-it-works.md#keep-warm).

**Set `num_ctx` when a client always uses one context size.** A model warmed at the
wrong size is worse than none: Ollama must reload it before the first real request.

**`allow_cpu_offload`** is only for models you accept running several times slower.
A model too big for any single GPU is placed anyway, on the biggest host once it is
idle, whatever this setting says.

## `queue`

Every inference request waits at the head until a host has a free slot for its model.

| Key | Default | Meaning |
|---|---|---|
| `max_wait` | `120s` | After this long waiting, answer `503` with `Retry-After` instead of hanging |
| `head_of_line_after` | `10s` | Once the oldest request for a model has waited this long, newer requests for that model queue behind it instead of taking slots as they free up |
| `classes` | see below | Priority classes by client IP |

```yaml
queue:
  classes:
    interactive:
      match: [127.0.0.1/32, "::1/128", 10.0.0.20/32]
      boost: 30s
    batch:
      default: true
```

| Class key | Meaning |
|---|---|
| `match` | Client IPs or CIDRs in this class |
| `boost` | Requests are ordered as if they arrived this much earlier. `30s` puts an interactive request ahead of batch requests that arrived up to 30 seconds before it |
| `default` | The class for clients that match nothing. Exactly one class must be the default |

The client IP is the TCP peer, unless `server.rate_limits.trust_proxy_headers` is on and
the peer is a trusted proxy.

## `scheduling`

| Key | Default | Meaning |
|---|---|---|
| `evict` | `true` | Before a cold load, unload the models llm-head chose. When `false`, Ollama evicts by its own least-recently-used rule |
| `keep_warm` | `true` | Run the keep-warm loop for models with `keep_warm > 0` |

Set both to `false` while another balancer shares the same hosts, for example during a
side-by-side test. Then llm-head never unloads or loads a model the other balancer is
using.

## `proxy`

Timeouts and retries for requests forwarded to a host.

| Key | Default | Meaning |
|---|---|---|
| `connect_timeout` | `10s` | TCP connect to the host |
| `response_header_timeout` | `120s` | Wait for the host's response headers. See the note below |
| `response_timeout` | `900s` | Whole response, start to finish |
| `read_timeout` | `600s` | Socket read timeout |
| `stall_timeout` | `60s` | Abort a streaming response that sends nothing for this long |
| `load_timeout` | `45s` | A request that needs a model loaded is abandoned and retried elsewhere if the model hasn't appeared in the host's `/api/ps` by then. The real limit is the larger of this and 4 × the model's learned load time |
| `model_quarantine` | `300s` | After a host fails to load or serve a model, avoid that model there for this long. Doubles on each repeat, up to 1 hour |
| `max_attempts` | one per host | Attempts for a request whose host can't be reached or fails before sending anything |

**`response_header_timeout` limits non-streaming requests.** Ollama sends the headers of
a non-streaming (`"stream": false`) response only when the whole answer is ready. So a
non-streaming answer that takes longer than this fails, and the model is quarantined on
that host. Raise it if you have legitimately slow non-streaming requests.

## `health`

| Key | Default | Meaning |
|---|---|---|
| `interval` | `5s` | How often each host is checked |
| `timeout` | `3s` | Must be less than `interval` |
| `path` | `/` | Path requested on each host. Any status below 500 counts as healthy |
| `failure_threshold` | `3` | Consecutive failures before a healthy host is marked offline |

A host starts as `unknown`. Its first successful check makes it `healthy`. If its first
check fails, it is marked `offline` straight away.

A passing health check only means Ollama answers HTTP, not that it can load a model.
llm-head catches that separately; see
[how-it-works.md](how-it-works.md#hosts-that-stop-loading-models).

## `discovery`

| Key | Default | Meaning |
|---|---|---|
| `ps_interval` | `1s` | Poll `/api/ps` (loaded models, GPU memory, context sizes, spills) |
| `tags_interval` | `5m` | Poll `/api/tags` (installed models). Also polled when a host recovers |
| `timeout` | `5s` | Timeout for both polls |

After `ollama pull` on a host, llm-head sees the new model within `tags_interval`.

## `server`

The HTTP server, and the same protections Olla has, under the same option names.

| Key | Default | Meaning |
|---|---|---|
| `host` | `0.0.0.0` | Listen address |
| `port` | `40114` | Listen port (Olla's default) |
| `read_header_timeout` | `10s` | Accepted for Olla compatibility. Currently has no effect |
| `shutdown_timeout` | `10s` | On stop, how long in-flight requests get to finish |
| `request_logging` | `true` | Write an `Access log` line per request |
| `cors.*` | allow all origins | `enabled`, `allowed_origins`, `allowed_methods`, `allowed_headers`, `allow_credentials`, `max_age` |
| `request_limits.max_body_size` | 100 MB | Larger request bodies get `413`. Vision requests carry images, so keep this generous |
| `request_limits.max_header_size` | 1 MB | Larger headers get `431` |
| `rate_limits.global_requests_per_minute` | `1000` | Across all clients |
| `rate_limits.per_ip_requests_per_minute` | `100` | Per client IP. `0` turns rate limiting off |
| `rate_limits.burst_size` | `50` | Requests a client can send at once before the per-minute rate applies |
| `rate_limits.trust_proxy_headers` | `false` | Take the client IP from `X-Forwarded-For` or `X-Real-IP`, but only when the peer is in `trusted_proxy_cidrs` |
| `rate_limits.trusted_proxy_cidrs` | private ranges | |

Only proxied model routes are rate limited. `/internal/*`, `/version`, and the model
lists llm-head answers itself are never limited. A limited request gets `429` with
`Retry-After`.

`health_requests_per_minute` and `per_endpoint` are accepted so an Olla config block can
be copied as is. They have no effect, as in Olla. `cleanup_interval` (default `5m`) sets
how often idle rate-limit state is cleared.

## `logging`

| Key | Default | Meaning |
|---|---|---|
| `file` | `/opt/olla/logs/olla.log` | JSON event log. `null` writes to stderr |
| `level` | `info` | `debug`, `info`, `warn` or `error` |
| `max_size_mb` | `1` | Rotate at this size. Old files are gzipped next to it as `<name>-<UTC time>.log.gz` |
| `max_backups` | `7` | Rotated files kept |

The format and message names match Olla's, so tools that read Olla's log keep working.
Every event is listed in [operations.md](operations.md#log-events).

## `stats_file`

Default `/opt/olla/data/llm-head-stats.json`. `null` keeps nothing across restarts.

llm-head learns, per model:
- the GPU memory it uses;
- how long requests take;
- how long a cold load takes;
- which context size it's usually requested at.

It also learns each host's real GPU capacity when a spill shows it. All of this is saved
here every 60 seconds and on shutdown, so a restart doesn't start from guesses. If the
file is unreadable, llm-head ignores it and logs a warning to stderr (journald).
