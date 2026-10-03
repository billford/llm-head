# Cutover runbook: Olla → llm-head on lampoon

Status: **Ready, waiting for a go time** · prepared 2026-10-03

Expected client-visible downtime: **under 5 seconds**, the time between Olla stopping and
llm-head listening on `:40114`. Rollback: **one command, under 10 seconds**.

## What's staged on lampoon

| Item | Path | State |
|---|---|---|
| Code | `/opt/llm-head/src` (git clone), `/opt/llm-head/venv` | latest `main`, installed |
| Production config | `/opt/llm-head/config.prod.yaml` | passes `check-config` |
| Production unit | `/opt/llm-head/cutover/llm-head.service` | staged, not installed |
| Learned model stats | `/opt/llm-head/data/stats.json` | three days of canary data |
| Health check | `/opt/llm-head/prodcheck.sh <since-UTC>` | real-client errors, plus a model probe on each host |
| Safe restart | `/opt/llm-head/safe-restart.sh` | waits until nothing is in flight |

### How the production config differs from the shadow config

| Setting | Shadow | Production |
|---|---|---|
| Port | `:40115` | `:40114` |
| `scheduling.evict`, `scheduling.keep_warm` | off | on |
| Log file | `/opt/llm-head/logs/llm-head.log` | `/opt/olla/logs/olla.log`, so the Olla dashboard keeps working (parity item P15) |
| Model policy | — | qwen2.5vl `keep_warm: 1`, `num_ctx: 8192`, home xmas · llama3.2:3b `keep_warm: 2` · gpt-oss:20b home european. See the trade-off in `docs/reports/2026-10-03-canary-review.md` |

The unit declares `Conflicts=olla.service`: starting llm-head stops Olla, and starting
Olla stops llm-head. That is what makes the switch and the rollback single commands.

## Go / no-go (check at T−15 min)

- [ ] `prodcheck.sh` shows no real-client failures in the last 24 h, and every model
      probe returns OK.
- [ ] All four Icinga LLM services are green.
- [ ] Both GPU hosts run the same Ollama version and have the Phase 0 settings.
- [ ] `llm-head check-config -c /opt/llm-head/config.prod.yaml` passes.
- [ ] The last 15 minutes of traffic are light: no batch backfill running, and not a
      time the home-automation agent is busy.

## Cutover (all on lampoon unless noted)

1. **Wait until Olla is idle.** This is a gate: the next step only runs when it succeeds.
   ```bash
   until [ "$(curl -s localhost:40114/internal/status | python3 -c 'import json,sys; print(json.load(sys.stdin)["system"]["active_connections"])')" = 0 ]; do sleep 1; done
   ```
2. **Switch.**
   ```bash
   sudo cp /opt/llm-head/cutover/llm-head.service /etc/systemd/system/ \
     && sudo systemctl daemon-reload && sudo systemctl start llm-head
   ```
3. **Verify** within 30 s:
   ```bash
   systemctl is-active llm-head olla      # expect: active / inactive
   curl -s localhost:40114/version | grep -o '"name":"[^"]*"'   # "llm-head"
   curl -s localhost:40114/internal/status/endpoints | python3 -m json.tool | grep -E 'name|status'
   /opt/llm-head/prodcheck.sh $(date -u +%Y-%m-%dT%H:%M:%S)
   ```
   Also send one generate request per hot model through `:40114/olla/ollama`, and load
   the Olla dashboard to confirm its node cards and live log tail.
4. **Move the batch client back to `:40114`** and restart its long-running processes,
   which read their config only at startup.
5. **Retire the shadow and the old service:**
   ```bash
   sudo systemctl disable --now llm-head-shadow
   sudo systemctl disable olla    # keep the binary and config for 30 days
   sudo systemctl enable llm-head
   ```
6. **Monitoring (on the Icinga master):**
   - Point `llm-errors-olla` at the production balancer, and rename it
     `llm-errors-head`.
   - Remove `llm-errors-llm-head`, which watches the `:40115` shadow.
   - `icinga2 daemon -C`, reload, then force the checks and confirm they're green.
7. **Record it.** Tag the deployed commit `cutover-v1` and push the tag.

## After cutover

| When | Check |
|---|---|
| +15 min | `prodcheck.sh`. `Warming model` log lines show qwen at 8192 on xmas and llama3.2 on both hosts |
| +1 h | Icinga green. Home-automation gpt-oss requests are served from european without cold loads |
| +24 h | Compare against spec §7: cold loads per 1,000 requests, qwen p95, zero 120 s stalls, no queue timeouts |

For planned GPU-host reboots from now on, drain the host first:
`curl -X POST localhost:40114/internal/hosts/<name>/drain`, wait until its in-flight count
reaches 0, reboot, then `/undrain`.

## Rollback

Do this if any real-client failure appears that llm-head's logs can't explain, if the
home-automation agent fails, or if the dashboard breaks:

```bash
sudo systemctl start olla          # Conflicts= stops llm-head
sudo systemctl disable llm-head && sudo systemctl enable olla
```

Clients need no change: both balancers serve `:40114`. Afterwards, write up what happened
in `docs/reports/`.

## Decommission (after 30 days with no rollback)

Remove `olla.service`, `/opt/olla/olla` and `/opt/olla/config/`. Keep `/opt/olla/logs`,
since llm-head writes there, and the dashboard.
