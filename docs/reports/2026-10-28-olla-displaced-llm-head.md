# Incident: Olla took :40114 back from llm-head, twice

2026-10-28 · blameless write-up · status: **resolved** 17:30 UTC (llm-head serving,
Olla retired on lampoon and misfit, Icinga check added)

## Summary

From **2026-10-24 06:06 UTC to 2026-10-28 17:30 UTC**, lampoon's port 40114 was served
by Olla, not llm-head. The same happened on **2026-10-07 at 06:06**, until llm-head was
started again on 10-08 03:00. Nothing alerted either time: european and xmas stayed
healthy, Olla proxied every request to them, and `llm-errors-head` only asks whatever
answers on :40114.

It came to light by accident: the yard tracker's care drafts were failing, and while
pointing it at the cluster, `llm-head.service` turned out to be `inactive`.

## Cause

1. After the cutover `olla.service` was **disabled but not removed**, and
   `olla-dashboard.service` (still enabled) has `Wants=olla.service`.
2. At 06:06 UTC `apt-daily-upgrade` restarts services. Restarting the dashboard pulled
   Olla in.
3. llm-head's unit has `Conflicts=olla.service`, so starting Olla **stopped llm-head**.
   Olla then bound :40114, as it did before the cutover.

The cutover runbook's last step ("Remove `olla.service`, `/opt/olla/olla` and
`/opt/olla/config/`") had not been done, and nothing covered the dashboard.

## Impact

- **Every cluster client** was routed by Olla for 4.5 days (and ~21 h on 10-07): no
  model-aware placement, spill detection, wedge retry or keep-warm. No client failures
  were found that trace to it. Olla's endpoint status showed both hosts at 100%.
- **Monitoring** was blind to it. `llm-errors-head` stayed green (Olla answers the same
  paths), and the `olla-dashboard` health check was green too.

## Fix (2026-10-28)

On **lampoon**:
- `olla-dashboard.service` stopped and disabled.
- `/etc/systemd/system/olla.service` moved to `/opt/olla/olla.service.retired`, then
  `olla.service` **masked** (`mask` refuses while a unit file sits in `/etc`).
- `llm-head.service` started; it logged `ok: 2 hosts [xmas, european]` and holds :40114.

On **misfit** (Icinga, `zones.d/misfit/hosts.conf`, backup `hosts.conf.bak.<time>`):
- the `olla-dashboard` http check on lampoon replaced by **`llm-head`**:
  `http://127.0.0.1:40114/internal/queue` must answer 200. Only llm-head serves that
  path; Olla answers 404, so the check goes CRITICAL if Olla ever holds the port again.
  Tested both ways with `check_http_health` on lampoon.

## A second Olla, on misfit

misfit runs no AI, but still ran its own Olla on :40114 (enabled, plus a dashboard),
proxying to **lola (192.168.1.203)** and **wanderlust (192.168.1.100)**. Both are gone:
lola answers ping but no Ollama, wanderlust nothing. It is now **stopped and disabled**,
with its dashboard. In the four days of logs it kept, its only AI request was the yard
tracker's failed draft.

**Correction to 2026-10-05-newshelper-retrieval.md:** it calls this "misfit's own Olla
balancer" that "does route" `qwen2.5:32b`, and says yard_tracker "uses misfit's
balancer, with the correct prefix", as if that were a working second cluster. It was a
leftover whose hosts were already failing; the yard tracker's last successful call was
2026-10-11.

## Clients that were on misfit's Olla

| Client | Before | Now |
|---|---|---|
| yard_tracker (misfit) | misfit's Olla, `qwen2.5:32b` | `http://lampoon:40114/olla/ollama`, `gpt-oss:20b` (yard_tracker `40aa2d6`). qwen2.5:32b (~20 GB) does not fit the 16 GB hosts |
| agenttracker (lampoon) | `OLLAMA_BASE_URL=http://192.168.1.182:40114`, `qwen2.5:32b`, failing since 2026-07-26 (296 failures in the last 2 days); its database shows **no event ever scored** | `http://127.0.0.1:40114/olla/ollama`, `gpt-oss:20b` in `/opt/agenttracker/.env` (backup `.env.bak.<time>`), restarted 17:40:46 UTC. Scoring from the first event after; failed events were already marked processed, so no backlog. Example config fixed in agenttracker's repo |

## Follow-ups

- Finish the runbook's removal step on lampoon (`/opt/olla/olla`, `/opt/olla/config/`;
  keep `/opt/olla/logs`) and decide whether `/opt/olla/dashboard` goes too.
- The runbook's cutover should disable or remove anything that `Wants=` the old unit,
  not only the unit itself.
