# Getting started

This guide takes you from nothing to llm-head serving requests in front of your Ollama
hosts. Allow about 30 minutes.

- [Requirements](#requirements)
- [Try it locally first](#try-it-locally-first)
- [Prepare each Ollama host](#prepare-each-ollama-host)
- [Install llm-head](#install-llm-head)
- [Write the config](#write-the-config)
- [Run it under systemd](#run-it-under-systemd)
- [Point clients at it](#point-clients-at-it)
- [Replacing Olla](#replacing-olla)
- [Check it's working](#check-its-working)

## Requirements

- **Python 3.12 or later** on the machine that runs llm-head (the "head"). It needs
  little CPU or memory; a small VM or the machine already running your proxy is fine.
- **Ollama on each GPU host**, reachable over HTTP from the head. Every host should have
  the models you want it to serve already pulled. llm-head doesn't pull models.
- **Network access from clients to the head** on one port, 40114 by default.

## Try it locally first

The repo has a fake Ollama server that simulates GPU memory, load times and CPU spill.
You can run a two-host "cluster" on a laptop with no GPU:

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'

cat > /tmp/local.yaml <<'EOF'
hosts:
  - {name: gpu1, url: "http://127.0.0.1:11501", vram_mb: 16311}
  - {name: gpu2, url: "http://127.0.0.1:11502", vram_mb: 16311}
logging: {file: null}     # log to stderr
stats_file: null          # don't save learned stats
EOF

.venv/bin/python -m tests.fake_ollama --port 11501 &
.venv/bin/python -m tests.fake_ollama --port 11502 &
.venv/bin/llm-head serve -c /tmp/local.yaml
```

In another terminal:

```bash
curl -s -D- localhost:40114/olla/ollama/api/generate \
  -d '{"model":"llama3.2:3b","prompt":"hi","stream":false}' | grep -i '^x-'
curl -s localhost:40114/internal/queue
```

The response headers show which host served the request and why
(`x-olla-routing-reason: cold_load` the first time, `loaded` after). `/internal/queue`
shows what each host has loaded.

## Prepare each Ollama host

llm-head only works well if it knows each host's limits, and Ollama enforces the same
limits. On every GPU host, set these in Ollama's environment. With systemd, use
`sudo systemctl edit ollama` and add:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_NUM_PARALLEL=2"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_KEEP_ALIVE=30m"
Environment="OLLAMA_FLASH_ATTENTION=1"
```

Then restart it with `sudo systemctl restart ollama`.

| Setting | Why |
|---|---|
| `OLLAMA_HOST` | Listen on the network, not just localhost, so the head can reach it |
| `OLLAMA_NUM_PARALLEL` | Requests each loaded model serves at once. Must equal the host's `slots_per_model` in llm-head's config |
| `OLLAMA_MAX_LOADED_MODELS` | Models held in GPU memory at once. Must equal `max_loaded_models` |
| `OLLAMA_KEEP_ALIVE` | How long an idle model stays loaded. 30 minutes avoids reloading models that are used every few minutes |
| `OLLAMA_FLASH_ATTENTION` | Smaller memory footprint for long contexts, on GPUs that support it |

**Run the same Ollama version on every host.** In one incident, a host on an older
version stopped loading models while still passing health checks. See
[`docs/reports/2026-09-30-xmas-ollama-wedge.md`](../reports/2026-09-30-xmas-ollama-wedge.md).

If you set `OLLAMA_CONTEXT_LENGTH`, put the same value in the host's `default_num_ctx`.

Check each host from the head:

```bash
curl -s http://gpu1.lan:11434/api/tags | python3 -m json.tool | grep '"name"'
```

## Install llm-head

On the head:

```bash
sudo mkdir -p /opt/llm-head && sudo chown "$USER" /opt/llm-head
git clone https://github.com/billford/llm-head /opt/llm-head/src
python3 -m venv /opt/llm-head/venv
/opt/llm-head/venv/bin/pip install /opt/llm-head/src
```

This installs a copy of the code into the venv, not a link to the checkout. After a
`git pull`, run the `pip install` again or the old code keeps running. See
[Upgrading](operations.md#upgrading).

## Write the config

Start from the example:

```bash
cp /opt/llm-head/src/examples/config.yaml /opt/llm-head/config.yaml
```

Edit at least:

- **`hosts`**: one entry per GPU host, with:
  - `url`: the host's Ollama address;
  - `vram_mb`: total GPU memory, from `nvidia-smi --query-gpu=memory.total --format=csv`;
  - `slots_per_model` and `max_loaded_models`, matching the Ollama settings above.
- **`models`**: optional per-model policy. Which models to keep loaded (`keep_warm`), at
  what context size (`num_ctx`), and where they belong (`home`). Delete the example
  entries if you don't know yet. llm-head works without any.
- **`queue.classes`**: which client IPs are interactive and get ahead of batch work.
- **`logging.file`** and **`stats_file`**: paths the service user can write to.

Every option is described in [configuration.md](configuration.md). Validate before
starting:

```bash
/opt/llm-head/venv/bin/llm-head check-config -c /opt/llm-head/config.yaml
# ok: 2 hosts [gpu1 (16311 MB), gpu2 (16311 MB)], listening on 0.0.0.0:40114
```

`check-config` exits non-zero and says what's wrong if the config is invalid. Unknown
keys are errors, so a typo can't be silently ignored.

## Run it under systemd

A unit file is in `examples/llm-head.service`. It runs `check-config` before every start,
so a bad config fails the start cleanly instead of crash-looping.

```bash
sudo cp /opt/llm-head/src/examples/llm-head.service /etc/systemd/system/
sudo systemctl edit llm-head    # override User=, Group=, paths if yours differ
sudo systemctl daemon-reload
sudo systemctl enable --now llm-head
```

The example unit runs as user `olla` and can only write to `/opt/olla/logs` and
`/opt/olla/data` (`ProtectSystem=full`, `ReadWritePaths=`). If your `logging.file` or
`stats_file` is somewhere else, add that directory to `ReadWritePaths`.

Optionally, set `Environment=LLM_HEAD_COMMIT=<git sha>` so `/internal/status` reports
which commit is running.

## Point clients at it

Clients use the head as their Ollama base URL, with the `/olla/ollama` prefix:

| Client type | Base URL |
|---|---|
| Ollama native (`/api/generate`, `/api/chat`, `/api/embed`) | `http://<head>:40114/olla/ollama` |
| OpenAI-compatible (`/v1/chat/completions`, ...) | `http://<head>:40114/olla/ollama/v1` |

Clients need no other change. llm-head forwards each request unchanged to the host it
picks and relays the response unchanged, streaming included.

## Replacing Olla

llm-head serves the same paths, headers and log format as
[Olla](https://github.com/thushan/olla) v0.0.28, on the same default port. To switch:

1. Copy your Olla server settings (rate limits, CORS, request limits) into the `server:`
   block. The option names are the same.
2. Set `logging.file` to Olla's log file if a dashboard reads it.
3. While Olla is still serving, you can run llm-head on another port with
   `scheduling.evict: false` and `scheduling.keep_warm: false` and compare the two with
   `tools/shadow_compare.py`. Those settings stop llm-head loading or unloading models
   that Olla is using.
4. Stop Olla, then start llm-head on its port. Clients don't change.

`docs/specs/cutover-runbook.md` is the runbook used for the production switch, including
a one-command rollback.

## Check it's working

```bash
curl -s localhost:40114/internal/health          # {"status":"healthy"}
curl -s localhost:40114/internal/queue           # each host: status, loaded models
curl -s localhost:40114/olla/ollama/api/tags     # models across all hosts
```

Then send a real request and look at the routing headers:

```bash
curl -s -D- -o /dev/null localhost:40114/olla/ollama/api/generate \
  -d '{"model":"llama3.2:3b","prompt":"hi","stream":false}' | grep -i '^x-'
```

Next: [operations.md](operations.md) for monitoring, upgrades and host reboots.
