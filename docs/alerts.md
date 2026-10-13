# Alerts and weekly report

Alerts go to the owner's phone through [ntfy](https://ntfy.sh) (CLAUDE.md §8.4,
[`guardrail_trader/alerts.py`](https://github.com/creaayi/guardrail-trader/blob/main/guardrail_trader/alerts.py)).

| Level | When | Priority |
|---|---|---|
| Run summary | after every run: status, NAV, drawdown, gap vs IWDA, decisions | default |
| Anomaly | reconciliation failed, stops out of sync, capital guard, kill switch, drawdown tier change, daily loss halt, error, gateway unusable, run not executed | high |
| Scheduler silent | no scheduler heartbeat for more than 2 h while Paris (09:00–17:30) or New York (09:30–16:00) is open | high |
| Weekly report | Fridays after the evening stops slot: headline figures of `reports/YYYY-WW.md` | default |

## Setup

1. Pick a long random topic: anyone who knows it can read the alerts. Put it in `.env` (never in git):
   `NTFY_TOPIC=...`. `NTFY_URL` defaults to `https://ntfy.sh`.
2. Subscribe to the topic in the ntfy app.
3. Without `NTFY_TOPIC`, alerts are off and each run says so in its log.

Sending never blocks a run: 5-second timeout, errors logged without the topic.

## Host watchdog

`scripts/gateway_watchdog.py` runs on the host, not in a container (it restarts the gateway with
`docker compose`). Every 15 minutes it restarts IB Gateway after repeated gateway failures, and raises the
"scheduler silent" alert. User crontab entry:

```
*/15 * * * * cd ~/guardrail-trader && .venv/bin/python scripts/gateway_watchdog.py >> logs/watchdog.cron.log 2>&1
```

## Weekly report

`scripts/weekly_report.py [--date YYYY-MM-DD] [--notify]` writes `reports/YYYY-WW.md` from the journal only
(CLAUDE.md §8.3). Figures without enough data show "n/d"; alpha, beta, Sharpe and Sortino stay "n/d" under
20 daily returns. Success (win rate, calibration) is the single T6 definition: P&L net of commissions and of
the 0.05 % slippage allowance > 0.
