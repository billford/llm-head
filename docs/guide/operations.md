# Operations

Day-to-day running of llm-head: restarts, upgrades, host reboots, monitoring and logs.
Commands assume the layout in [getting-started.md](getting-started.md): code in
`/opt/llm-head/src`, venv in `/opt/llm-head/venv`, config in `/opt/llm-head/config.yaml`,
and a systemd unit called `llm-head`.

- [Restarting](#restarting)
- [Upgrading](#upgrading)
- [Changing the config](#changing-the-config)
- [Rebooting a GPU host](#rebooting-a-gpu-host)
- [Adding or removing a host](#adding-or-removing-a-host)
- [Adding a model](#adding-a-model)
- [Monitoring](#monitoring)
- [Logs](#logs)
- [Log events](#log-events)
- [The stats file](#the-stats-file)
- [Tools](#tools)

## Restarting

A restart cuts off any request in flight. Use the safe-restart script, which waits until
nothing is in flight or queued:

```bash
/opt/llm-head/src/contrib/safe-restart.sh llm-head /opt/llm-head/config.yaml \
    http://127.0.0.1:40114 300
```

| Argument | Default |
|---|---|
| 1. systemd unit | `llm-head` |
| 2. config | `/opt/llm-head/config.yaml` |
| 3. base URL | `http://127.0.0.1:40114` |
| 4. longest wait, in seconds | `120` |

It:
1. runs `check-config` and stops if the config is invalid;
2. polls `/internal/queue` once a second until nothing is waiting or in flight;
3. restarts the unit with `sudo systemctl restart`;
4. waits up to 30 s for `/internal/health` to answer.

It exits non-zero, without restarting, if the cluster is still busy after the longest
wait. It needs `sudo` for `systemctl`.

Downtime is a few seconds. A client request arriving during the restart gets a
connection error, so clients should retry once.

Learned stats are saved on a clean stop, and every 60 seconds while running.

## Upgrading

The package is installed into the venv as a copy, so pulling alone changes nothing:

```bash
cp -p /opt/llm-head/data/stats.json /opt/llm-head/data/stats.json.pre-upgrade   # your stats_file path
cd /opt/llm-head/src && git pull --ff-only
/opt/llm-head/venv/bin/pip install /opt/llm-head/src
/opt/llm-head/src/contrib/safe-restart.sh llm-head /opt/llm-head/config.yaml
```

Afterwards:
- check `/internal/status` and look at `system.version` and `system.commit`;
- check `/internal/queue`.

**Rolling back:** check out the previous commit, `pip install` again, restore the stats
backup if the new version changed its format, and safe-restart.

## Changing the config

```bash
$EDITOR /opt/llm-head/config.yaml
/opt/llm-head/venv/bin/llm-head check-config -c /opt/llm-head/config.yaml
/opt/llm-head/src/contrib/safe-restart.sh llm-head /opt/llm-head/config.yaml
```

The config is read only at startup, so a change takes effect after the restart.

## Rebooting a GPU host

Drain the host first, so no request is cut off mid-answer:

```bash
curl -X POST localhost:40114/internal/hosts/gpu1/drain
# wait until it reports nothing in flight
watch -n2 "curl -s localhost:40114/internal/queue | python3 -m json.tool | grep -A8 '\"gpu1\"'"
# reboot gpu1 and wait until Ollama is back up
curl -X POST localhost:40114/internal/hosts/gpu1/undrain
```

While it's drained:
- new requests go to the other hosts;
- requests for a model only that host has get `404`.

Drain only works from localhost, and isn't kept across an llm-head restart.

If you skip draining, the host fails its health checks and is marked offline after
`failure_threshold` checks (15 s by default). Until then, requests sent to it fail and
are retried elsewhere. Requests that were mid-stream fail.

## Adding or removing a host

Edit `hosts:` in the config, then safe-restart. For a new host:

1. Install Ollama with the same version and settings as the other hosts. See
   [getting-started.md](getting-started.md#prepare-each-ollama-host).
2. Pull the models it should serve.
3. Add it to `hosts:` with the right `vram_mb`, `slots_per_model` and
   `max_loaded_models`.

Before removing a host, remove it from any `home:` lists, and make sure no `keep_warm`
is higher than the number of hosts left. `check-config` catches both.

## Adding a model

Pull it on every host that should serve it (`ollama pull <model>` on each). llm-head
picks it up within `discovery.tags_interval` (5 minutes), or on restart. No config
change is needed unless you want a policy for it in `models:`.

The first request for a new model uses estimates. After its first full load, llm-head
knows its real memory use and load time.

## Monitoring

`contrib/icinga/` has two Nagios/Icinga plugins and an example Icinga 2 config. Both
plugins work with any Nagios-compatible system.

**`check_llm_balancer_errors`** alerts on client-visible failures. It reads
`system.total_failures` from `/internal/status` and compares with the previous run. Run
it every 5 minutes on the head:

```bash
check_llm_balancer_errors --url http://127.0.0.1:40114 --name llm-head -w 1 -c 5
```

**`check_ollama_models`** proves a GPU host can actually serve. A health check can't do
that. It sends a 1-token request directly to the host, for a small model you name, and
with `--loaded` for every model already loaded, at its current context size so nothing
is reloaded or evicted:

```bash
check_ollama_models --host http://gpu1.lan:11434 --model llama3.2:3b --loaded
```

Also worth watching:

| Signal | Where | Why |
|---|---|---|
| `spilled` not empty for long | `/internal/queue` | A host is running something slowly; see [troubleshooting](troubleshooting.md#a-model-is-spilled-onto-the-cpu) |
| `waiting` growing | `/internal/queue` or `queue.waiting` in `/internal/status` | Demand is above capacity |
| `Model quarantined on endpoint` | log | A host failed to load or serve a model |
| Cold loads per 1,000 requests | `Request completed` lines with `ttft_ms > 3000` | The main measure of placement quality |

## Logs

The event log is JSON lines, one event per line, in `logging.file`
(`/opt/olla/logs/olla.log` by default). Rotated files are gzipped beside it.

Two things to know when reading it:

- **Timestamps:** event lines use the server's local time (`2026-10-05 13:04:37`), but
  `Access log` lines use UTC in ISO format (`2026-10-05T13:04:37Z`). This matches Olla.
  Run the server in UTC, or convert before comparing.
- **Python warnings** (an unreadable stats file, for example) and crashes go to stderr,
  so they're in `journalctl -u llm-head`, not the event log.

Useful searches:

```bash
LOG=/opt/olla/logs/olla.log
grep '"level":"WARN"\|"level":"ERROR"' $LOG | tail            # anything wrong
grep '<request id>' $LOG                                          # one request, end to end
grep '"Request completed"' $LOG | grep -o '"placement":"[a-z_]*"' | sort | uniq -c
grep '"Model spilled onto CPU"\|"capacity learned"' $LOG
```

## Log events

Olla's events keep Olla's names and fields. The rest are additions.

| Event | Level | Meaning |
|---|---|---|
| `llm-head starting` / `llm-head stopped` | INFO | Process start and clean stop |
| `Access log` | INFO | One per HTTP request: path, status, bytes, duration, client |
| `Request received` | INFO | A model API request arrived |
| `Request queued` | INFO | No host could take it yet; `queue_depth` for that model |
| `Request dispatching` | INFO | Sent to `endpoint`, with `placement`, `cold`, `queued_ms`, `num_ctx`, `attempt` |
| `Request completed` | INFO | Finished: `duration_ms`, tokens, `tokens_per_sec`, `ttft_ms` (time to first token), `placement` |
| `Request failed` | WARN / ERROR | Failed. WARN with `will_retry` when another host will be tried; ERROR (`status: 502`) when every attempt failed |
| `Model routing rejected request` | WARN | No host has the model (`404`) |
| `Evicting model` | INFO | Unloading a model to make room |
| `Warming model` / `Warming failed` | INFO / WARN | Keep-warm load started, or failed |
| `Rehoming model` | INFO | Keep-warm is unloading a copy away from the model's `home`, now that home holds enough |
| `Model spilled onto CPU` | WARN | `/api/ps` shows a model partly on the CPU: `size_vram` < `size` |
| `Model fully on GPU again` | INFO | A spilled copy is now fully on the GPU |
| `Endpoint GPU capacity learned` | INFO | Usable GPU memory for a host changed, from a spill |
| `Endpoint GPU capacity not learned` | WARN | A spill would imply under half the configured memory; probably something else is using the GPU. Check `nvidia-smi` |
| `Endpoint GPU capacity reset` | WARN | Someone called `reset-capacity` |
| `Model context capped` | INFO | Ollama loaded a model with less context than asked; that's its maximum, and is remembered |
| `Model quarantined on endpoint` | WARN | Model failed to load or stalled; avoided there for `seconds` |
| `Model quarantine cleared` | INFO | A request for it succeeded there again |
| `Endpoint status changed: <name>` and `Endpoint status changed:` | INFO / WARN | Health status changed. Two lines, as in Olla |
| `Endpoint recovered: <name> is Healthy` | INFO | Back from offline |
| `Endpoint drain changed` | WARN | Drained or undrained |
| `Model discovery failed` | WARN | `/api/tags` on a host failed |

## The stats file

`stats_file` (JSON) holds what llm-head has learned:
- per-model memory, durations, load times and usual context size;
- `host_vram`: the GPU capacity learned from spills;
- `max_ctx`: models whose maximum context is below what was asked for.

- It's safe to delete when llm-head is stopped. llm-head goes back to estimates and
  relearns from traffic within hours.
- Don't edit it while llm-head is running. The running process overwrites it within a
  minute.
- To undo a learned host capacity, use `reset-capacity` instead of editing the file.
- Back it up before upgrades.

## Tools

In `tools/`, for testing against real hosts. Run them from the head, with the repo's
venv.

| Tool | Use |
|---|---|
| `shadow_compare.py --olla URL --head URL` | Sends the same requests to two balancers and compares statuses, headers and JSON shape. Exit 0 only if all match |
| `loadtest.py --base URL --requests N --concurrency C --seed S` | Replays a realistic, seeded model mix and summarizes cold loads and latency. Keep it under your rate limit (`--gap`). **It loads and evicts real models**: check real clients after each run |
| `realhost_checks.py --head URL` | Scenario checks: vision payloads, tool calls, client disconnects, drain, long context, a burst larger than the cluster |
