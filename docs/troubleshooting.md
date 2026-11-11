# Troubleshooting

Start with `docker compose ps` and
`docker compose run --rm paper-trader health`. Expected failures print one
`ERROR:` line and exit with code 1; details are in the structured log
(`docker compose logs <service>`) and in Grafana → *System*.

| Symptom | Cause | What to do |
|---|---|---|
| `required variable QTS_APP_DB_PASSWORD is missing` on `up` | `.env` pre-dates the separate database accounts | add `QTS_APP_DB_PASSWORD` and `FREQTRADE_DB_PASSWORD` (see `.env.example`), or run `scripts/setup_host.sh` on a fresh checkout |
| `db-setup` exits with an error | the administrator password in `.env` does not match the one the volume was created with | restore the original `POSTGRES_PASSWORD`; other passwords can change freely and are re-applied on `up` |
| `database schema is at revision X, this version needs Y` | code was updated, migrations not applied | `docker compose up -d` (runs `migrate`), or `qts db upgrade` with the administrator account |
| `permission denied for table …` in a service log | a table was created outside the migrations | `docker compose up -d db-setup` re-applies the grants |
| `<symbol>: newest candle closed … ago: data is stale` | the archive has not published yesterday's candle yet, or no internet | usually clears within hours. Three failures in a row trip the kill switch: once data is back, `risk reset` |
| `download failed for https://data.binance.vision/…` | network or DNS problem | check connectivity from the host; the next hourly step retries |
| Kill switch `ACTIVE` | drawdown limit, three operational errors, or an operator | `risk status` shows the reason. Fix the cause, then `risk reset --operator "name"` |
| No trade for days | normal: daily trend following is in the market about a third of the time | `report daily` shows the signals: `flat` / `hold` mean the strategy looked and did nothing |
| `configuration changed since this paper session started` | `configs/paper.yaml` was edited | revert the edit, or `paper stop` and let the trader open a new session |
| `service:paper-trader` unhealthy | the last step failed or the container is not running | `docker compose logs --tail 50 paper-trader` |
| `service:backup` unhealthy | no successful backup for 26 h | `docker compose logs backup-scheduler`; check that `./backups` exists and is writable; `scripts/backup.sh` |
| `service:freqtrade` unhealthy after you stopped it | a service that reported once is expected to keep reporting | `monitor forget freqtrade` |
| Reports missing in `reports/daily` | the containers' user cannot write into `./reports` | `chmod a+rwx reports reports/daily` (or `scripts/setup_host.sh`); `report daily` prints the same report in any case |
| Backups unreadable without `sudo` | `./backups` was created by Docker as root | `sudo chown -R "$USER" backups`; new backups take the owner of the directory |
| Freqtrade: `Could not load markets` | `api.binance.com` unreachable from this machine (blocked in some countries) | add `--config /freqtrade/configs/freqtrade/config.public-data-mirror.json` to the service command |
| Freqtrade exits with `LiveTradingDisabledError` | it was started without `--dry-run` or with `dry_run: false` | intended: real trading is not available |
| `QtsTrendFollowing` trade has no stop in the UI right after entry | the stop is set when the entry fills | it appears within one bot loop (60 s); it is stored with the trade and survives restarts |
| `NOT REPRODUCED` from `qts reproduce` | the candles of that period changed, or the strategy version did | the output says which; re-download the data or check out the commit recorded with the run |
| Grafana shows no data | no run selected, or the session has no candle yet | pick a run at the top; a new session shows data after its first candle |

Still stuck: `scripts/smoke_test.sh` checks the stack end to end and names the
first thing that is wrong.
