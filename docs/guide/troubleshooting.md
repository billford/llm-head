# Troubleshooting

Start with these three commands on the head. They answer most questions:

```bash
curl -s localhost:40114/internal/queue | python3 -m json.tool    # live state of every host
grep '"level":"WARN"\|"level":"ERROR"' /opt/olla/logs/olla.log | tail -20
journalctl -u llm-head -n 50 --no-pager                          # startup errors, crashes
```

To follow one request, take its `X-Olla-Request-Id` from the response headers (or from
the client's error) and `grep` the log for it. You'll see it received, queued,
dispatched to which host and why, and completed or failed.

To see what a GPU host really has loaded, ask Ollama directly:

```bash
curl -s http://gpu1.lan:11434/api/ps | python3 -m json.tool
```

- [llm-head won't start](#llm-head-wont-start)
- [Clients get 404 "No ollama endpoints available"](#clients-get-404-no-ollama-endpoints-available)
- [Clients get 503 queue_timeout](#clients-get-503-queue_timeout)
- [Clients get 502](#clients-get-502)
- [Clients get 429](#clients-get-429)
- [Requests are slow](#requests-are-slow)
- [A client with a short timeout fails on the first request](#a-client-with-a-short-timeout-fails-on-the-first-request)
- [Too many cold loads](#too-many-cold-loads)
- [A model is spilled onto the CPU](#a-model-is-spilled-onto-the-cpu)
- [A host shows less usable GPU memory than expected](#a-host-shows-less-usable-gpu-memory-than-expected)
- [A model is quarantined on a host](#a-model-is-quarantined-on-a-host)
- [A host is offline](#a-host-is-offline)
- [A new model isn't available](#a-new-model-isnt-available)
- [Keep-warm isn't keeping a model loaded](#keep-warm-isnt-keeping-a-model-loaded)
- [The control endpoints return 403](#the-control-endpoints-return-403)
- [A code change had no effect after restart](#a-code-change-had-no-effect-after-restart)

## llm-head won't start

`systemctl status llm-head` shows the start failing at `ExecStartPre`, or the service
exits right away.

- **`config error: …`:** run `llm-head check-config -c <config>` and fix what it names.
  Common causes:
  - a misspelled key (unknown keys are errors);
  - `health.timeout` not less than `health.interval`;
  - a `home:` host that isn't in `hosts:`;
  - `keep_warm` higher than the number of hosts;
  - two classes, or none, marked `default: true`.
- **`Permission denied` on the log or stats file:** the service user can't write to
  `logging.file` or `stats_file`. Also check the unit's `ReadWritePaths=`.
- **`address already in use`:** something else, often Olla, is on the port.
  `ss -ltnp | grep 40114`.

## Clients get 404 "No ollama endpoints available"

No routable host has that model installed.

1. Is the name right? `curl -s localhost:40114/olla/ollama/api/tags` lists what llm-head
   sees. Names are case-insensitive, and no tag means `:latest`.
2. Is the host that has it healthy and not draining? Check `/internal/queue`. Requests
   for a model only a drained host has get 404.
3. Was it just pulled? llm-head polls installed models every 5 minutes.

## Clients get 503 queue_timeout

The request waited `queue.max_wait` (120 s) and never got a slot. Demand for that model
was above what the cluster could serve, or the model couldn't be placed at all.

Look at `/internal/queue` while it's happening:

- **Many requests waiting for one model, its copies all busy:** more demand than
  `slots_per_model` × copies. Options:
  - give it `keep_warm: 2` so a second copy stays loaded;
  - raise `OLLAMA_NUM_PARALLEL` together with `slots_per_model`, if the GPU has room;
  - spread the batch client's load over time.
- **Waiting, but hosts look idle:** the model may not fit anywhere without evicting a
  busy model. Check whether its estimated memory plus what's loaded exceeds
  `vram_usable_mb` on each host. A host's `vram_usable_mb` can be lower than configured
  after a spill; see
  [below](#a-host-shows-less-usable-gpu-memory-than-expected).
- **A low-priority client starved by a high-priority one:** check the `class` of the
  waiting requests, and the `boost` values.

The response carries `Retry-After`, set to about one typical request duration for the
model.

## Clients get 502

Every attempt failed before the response started. The error text, and the `Request
failed` log lines for that request ID, say why:

| Error | Cause | Check |
|---|---|---|
| `network error: ConnectError` | Host unreachable | Is Ollama running? `curl http://gpu1.lan:11434/` from the head |
| `HTTP 502/503/504: …` | Ollama or something in front of it returned an error | Ollama's journal on the host: `journalctl -u ollama -n 100` |
| `no response headers within 120s` | Host accepted the request but didn't answer in time. With `"stream": false`, a long answer can legitimately take this long | Raise `proxy.response_header_timeout`, or have the client stream |
| `model … did not load on … within Ns` | The load watchdog fired: the model never appeared in `/api/ps` | See [quarantine](#a-model-is-quarantined-on-a-host) |

If every host fails the same way, it's likely the request itself, such as an image
Ollama can't decode, or something cluster-wide.

A failure after the response has started isn't a 502. The stream just ends early, and
the log shows `Request failed` with `stalled` or an error.

## Clients get 429

The rate limit, per client IP or global, was hit. The response has `Retry-After`.

- If many clients share one IP (behind a reverse proxy), turn on
  `server.rate_limits.trust_proxy_headers` and list the proxy in `trusted_proxy_cidrs`.
- Raise `per_ip_requests_per_minute` or `burst_size` for legitimate bursts, such as load
  tests.

## Requests are slow

Look at the request's `Request completed` line:

| Field | If high | Likely cause |
|---|---|---|
| `queued_ms` | Waited at the head for a slot | Demand above capacity; see [503](#clients-get-503-queue_timeout) |
| `ttft_ms` with `placement` `cold_load*` or `reload_*` | Model had to load | See [cold loads](#too-many-cold-loads) |
| `tokens_per_sec` well below normal for that model | Running partly on the CPU | See [spill](#a-model-is-spilled-onto-the-cpu) |

To get a model's normal speed, send one request directly to an idle host with it fully
loaded, and note the `eval_count` / `eval_duration` it reports.

## A client with a short timeout fails on the first request

Some clients wait only a few seconds. newshelper's chat proxy, for example, gives its
retrieval step 5 s, and retrieval needs an embedding. If that request has to load its
model first, it can take longer, especially when another model must be unloaded to make
room. The client then gives up even though llm-head answers.

- Look at the request's `Request completed` line. A cold `placement` and a `duration_ms`
  near the client's timeout confirm it.
- Give such clients a timeout that allows for a cold load, about 15 s for a small model.
- Or keep the model warm (`keep_warm: 1`). It takes one of `max_loaded_models` on a host,
  but a small embedding model fits beside almost anything.

## Too many cold loads

Count them by reason:

```bash
grep '"Request completed"' /opt/olla/logs/olla.log | grep -o '"placement":"[a-z_]*"' | sort | uniq -c
```

| Mostly | Meaning | Fix |
|---|---|---|
| `reload_context` | Same model requested at different context sizes | Make clients send one `num_ctx` consistently, warmup and health probes included. Set `models.<name>.num_ctx` for keep-warm. If the same model gets `reload_context` again and again, check for a `Model context capped` line for it. Versions before 2026-10-05 didn't learn that cap and reloaded such models on every request |
| `cold_load_evict` swapping the same two models on one host | They don't fit together, and both are in demand | Give each a different `home`, so they settle on different hosts |
| `replica_cheaper_than_wait` | Bursts beyond one copy's slots | Expected under load. `keep_warm: 2` keeps the second copy |
| `cold_load` after quiet periods | Ollama unloaded idle models | Raise `OLLAMA_KEEP_ALIVE`, or use `keep_warm` |

A cold load is normal the first time a model is used after a restart or a quiet period.

## A model is spilled onto the CPU

`/internal/queue` lists it under a host's `spilled`, and the log has `Model spilled onto
CPU` with `size_vram` < `size`. That copy runs much slower, so llm-head stops sending it
requests (see [how-it-works.md](how-it-works.md#cpu-spill-detection)).

Usually this is two models that don't really fit together, because Ollama's overhead is
more than `reserve_mb` allows for. llm-head learns that host's real capacity from the
spill and won't pair them again. The spilled copy stays until Ollama unloads it
(`OLLAMA_KEEP_ALIVE`), or until llm-head reloads it fully once there's room.

To clear it straight away, unload the spilled copy on that host:

```bash
curl http://gpu2.lan:11434/api/generate -d '{"model":"llama3.2:3b","keep_alive":0}'
```

If a model spills even when it's alone on a host, it's too big for that GPU at that
context size. Use a smaller quantization or context, or set `allow_cpu_offload: true` to
accept it.

## A host shows less usable GPU memory than expected

`vram_usable_mb` in `/internal/queue` is below `vram_mb - reserve_mb`. That means
llm-head saw a spill on that host and learned a lower capacity. Look for `Endpoint GPU
capacity learned` in the log, and the `Model spilled onto CPU` line just before it.

If the figure is far too low, for example a few GB on a 16 GB card, almost nothing can be
placed on that host. Requests queue or time out, and the other hosts take all the
evictions. Versions before 2026-10-05 could learn such a figure from a spilled copy left
behind after its neighbour unloaded. Unload the spilled copy first, then reset. Otherwise
the leftover copy is measured again:

```bash
curl http://gpu2.lan:11434/api/generate -d '{"model":"<spilled model>","keep_alive":0}'
curl -X POST localhost:40114/internal/hosts/gpu2/reset-capacity
```

- **If that spill was real** (two models that don't fit together), leave it. It's
  stopping the same spill from happening again.
- **If it had another cause,** such as another process using the GPU, a driver problem,
  or a host that has since had its GPU upgraded, reset it:

  ```bash
  curl -X POST localhost:40114/internal/hosts/gpu2/reset-capacity
  ```

  It goes back to the configured figure. If the spill happens again, it's learned again.
  Check `nvidia-smi` on the host for processes other than Ollama.

## A model is quarantined on a host

The log shows `Model quarantined on endpoint` with a `reason`. The model failed to load
in time, or stalled mid-stream, on that host. llm-head avoids that model there for
`seconds`, which doubles on each repeat up to an hour. Other models on the host are
unaffected.

Check the host directly:

```bash
curl -s -m 60 http://gpu1.lan:11434/api/generate \
  -d '{"model":"llama3.2:3b","prompt":"ok","stream":false,"options":{"num_predict":1}}'
```

- **It answers quickly:** it was probably a one-off. The quarantine clears on the next
  success, or when it expires.
- **It hangs, but the model that's already loaded still answers:** Ollama's loader is
  stuck, as in the
  [2026-09-30 incident](../reports/2026-09-30-xmas-ollama-wedge.md). Save diagnostics
  (`journalctl -u ollama`, `/api/ps`, `nvidia-smi`), then
  `sudo systemctl restart ollama` on that host. Drain it first if it's serving.
- **It errors:** read Ollama's journal on that host. Common causes are a corrupted model
  file (`ollama pull` it again) or running out of GPU memory.

## A host is offline

`/internal/queue` shows `"status": "offline"`. The log has `Endpoint status changed:`
with `check_error`.

- `ConnectError` / `ConnectTimeout`: Ollama isn't running, or isn't listening on the
  network (`OLLAMA_HOST`), or a firewall is in the way.
- `ReadTimeout`: Ollama is up but not answering `GET /` within `health.timeout`. Either
  it's overloaded, or `health.timeout` is too tight.

A host comes back automatically on its first passing check. It's logged as `Endpoint
recovered: <name> is Healthy`, and its model list is refreshed.

## A new model isn't available

llm-head refreshes installed models every `discovery.tags_interval` (5 minutes). Wait,
or safe-restart. Make sure the model was pulled on the host llm-head talks to
(`curl http://gpu1.lan:11434/api/tags`). If the log shows `Model discovery failed`,
`/api/tags` on that host is failing.

## Keep-warm isn't keeping a model loaded

Keep-warm only loads onto a host that is healthy, not draining, has nothing in flight,
and has room beside what's already loaded. It never evicts. It also pauses while any
request is waiting. So on a busy cluster, or with large models, it may not find a host.
Check:

- **Is the policy key an exact model name?** Globs are ignored for keep-warm.
- **Is there room?** Compare the model's size with `vram_usable_mb` minus what's loaded.
  A host with a spilled copy of the model doesn't count, and isn't used to warm it.
- **Is it warm at another context size?** Keep-warm counts only copies at the size it
  warms at: `num_ctx` in the policy, or the learned usual size. Set `num_ctx` explicitly
  if traffic uses mixed sizes.
- **Are there `Warming model` / `Warming failed` lines in the log?**

## The control endpoints return 403

`drain`, `undrain` and `reset-capacity` only accept requests from localhost. Run `curl`
on the head itself, against `localhost` or `127.0.0.1`.

## A code change had no effect after restart

The venv has a copy of the package, not a link to the checkout. Run
`/opt/llm-head/venv/bin/pip install /opt/llm-head/src` after `git pull`, then restart.
Check which commit is running in `system.commit` of `/internal/status` (if
`LLM_HEAD_COMMIT` is set in the unit). Otherwise run:

```bash
/opt/llm-head/venv/bin/python -c "import llm_head, os; print(os.path.dirname(llm_head.__file__))"
```
