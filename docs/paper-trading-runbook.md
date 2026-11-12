# Paper trading runbook

How to start, watch, stop and verify a continuous paper-trading run. Everything
here is simulated: no exchange credentials are needed and no real order can be
placed (`mode: live` is refused by every command, and the Freqtrade adapter
refuses to load outside dry-run).

A continuous test counts as **passed only after 7 days of recorded operation**.
`qts report soak` says `IN PROGRESS` until then; do not read that as a pass.

## 0. What runs

| Service | What it does | Restarts |
|---|---|---|
| `postgres` | operational database | always |
| `db-setup` | roles, grants, passwords (runs once per `up`) | — |
| `migrate` | schema migrations (runs once per `up`) | — |
| `paper-trader` | downloads closed daily candles, runs the strategy, risk engine, simulated fills; one step per hour | always |
| `monitor` | health checks every 5 minutes, daily reports | always |
| `backup-scheduler` | a backup 2 minutes after start, then every 24 h | always |
| `grafana` | dashboard on `127.0.0.1:3000` | always |
| `freqtrade` (optional profile) | Freqtrade dry-run of the same strategy | always |

The strategy trades **daily** candles: expect at most one decision per symbol per
day, and many days with no order at all. A candle of day *D* is processed on
*D+1*, a little after the exchange publishes it.

## 1. Start (your computer or a VPS)

Requirements: Docker with the compose plugin, 2 GB RAM, 5 GB disk, outbound HTTPS
to `data.binance.vision`. No inbound port is needed.

```bash
git clone https://github.com/fabio-passarani/umios.git && cd umios
scripts/setup_host.sh              # .env with random secrets, ./backups, ./reports
docker compose up -d --build       # about 5 minutes the first time
docker compose ps                  # db-setup and migrate: "exited (0)"; the rest: "running"
```

The first step downloads the history since 2018 (about 3 minutes). Then:

```bash
docker compose run --rm paper-trader paper status
docker compose run --rm paper-trader health          # exit code 0 = everything healthy
```

A new session trades only candles that close **after** it started: equity stays
at the initial capital until the first new candle.

On a VPS, make Docker start at boot (`sudo systemctl enable docker`): every
service has `restart: unless-stopped`, so the stack comes back by itself after a
reboot. To look at Grafana from your computer without opening a port:
`ssh -L 3000:127.0.0.1:3000 user@vps`, then <http://127.0.0.1:3000>.

Optional Freqtrade dry-run (needs `api.binance.com` reachable from the machine):

```bash
docker compose --profile freqtrade up -d --build
```

## 2. Watch

Every day, one command:

```bash
docker compose run --rm paper-trader report daily     # yesterday; --date YYYY-MM-DD for another day
```

It shows equity and P&L, the strategy's signals (also "nothing to do"), each
order with the risk engine's decision, fills, open positions with their stops,
monitoring availability, and errors. The monitor also writes it to
`reports/daily/<date>.md` and `.json`.

| Question | Command |
|---|---|
| Is everything healthy right now? | `docker compose run --rm paper-trader health` |
| What does the account look like? | `docker compose run --rm paper-trader paper status` |
| Is the kill switch on? | `docker compose run --rm paper-trader risk status` |
| How is the 7-day test going? | `docker compose run --rm paper-trader report soak` |
| What is a service doing? | `docker compose logs --tail 50 paper-trader` (or `monitor`, `backup-scheduler`) |
| Did the backups run? | `ls -lt backups | head` |

What "healthy" means, check by check:

| Check | Fails when |
|---|---|
| `database` | PostgreSQL does not answer |
| `data:<symbol>` | the newest closed candle is older than `risk.max_data_age_hours` (48 h) |
| `kill_switch` | the kill switch is active |
| `service:paper-trader` | no successful step for 3 hours, or the last step failed |
| `service:backup` | no successful backup for 26 hours |
| `service:freqtrade` | the bot has not reported for 30 minutes (only once it has run) |

## 3. Intervene

```bash
# stop new entries immediately (running services see it without a restart)
docker compose run --rm paper-trader risk kill --reason "why"

# close positions at the next open — works with the kill switch on
docker compose run --rm paper-trader paper close --symbol BTC/USDT
docker compose run --rm paper-trader paper close --symbol BTC/USDT --fraction 0.5
docker compose run --rm paper-trader paper close --all --emergency

# resume: clears the kill switch and re-bases the drawdown measure
docker compose run --rm paper-trader risk reset --operator "your name"
```

The kill switch never blocks an exit. It trips by itself on a drawdown of
`risk.max_drawdown_pct`, on three operational errors in a row, or on an invalid
equity figure; it is reset only by a person.

## 4. Stop

```bash
docker compose stop                     # pause: nothing is lost, `docker compose up -d` resumes
docker compose run --rm paper-trader paper stop    # close the session for good (history is kept)
docker compose down                     # remove containers; volumes (data) stay
```

`docker compose down -v` deletes the database and the state. Take a backup first.

Changing `configs/paper.yaml` requires a new session (`paper stop`, then the
trader starts a new one): results must stay attributable to one configuration.

If you stopped Freqtrade on purpose, tell the monitor, or it will report an
outage: `docker compose run --rm paper-trader monitor forget freqtrade`.

## 5. Verify

After 7 days:

```bash
docker compose run --rm paper-trader report soak ; echo "exit code $?"
```

| Exit code | Verdict |
|---|---|
| 0 | `PASSED`: every criterion below held for at least 7 days |
| 1 | `FAILED`: 7 days elapsed and at least one criterion does not hold |
| 2 | `IN PROGRESS`: fewer than 7 days recorded — **not** a pass |

Criteria: dry-run only · 7 days of monitoring · at least 99 % of monitoring cycles
fully healthy · no monitoring silence longer than 30 minutes · a candle processed
for every calendar day · signals recorded for every candle · no duplicated order ·
no operational error · backups being taken.

Then confirm the paper session against a backtest of the same days: the two use
the same code, so on the same candles they must agree.

```bash
docker compose run --rm paper-trader backtest -c configs/paper.yaml --start <session start date>
docker compose run --rm paper-trader paper status      # compare the equity
```

And prove the backups: `scripts/restore_test.sh` (non-destructive).

## 6. Tests you can run any time

| Command | What it proves | Touches your stack? |
|---|---|---|
| `scripts/smoke_test.sh` | schema version, database roles, kill switch, `mode: live` refused, Grafana | reads; toggles the kill switch on and off |
| `scripts/restore_test.sh` | the newest backup restores, table by table | takes a backup |
| `scripts/e2e_paper_test.sh` | the whole chain on a synthetic market, with restarts, bad data, kill switch, disaster recovery | no: separate project `qts-e2e` |

See also: [backup and restore](backup-restore.md) · [troubleshooting](troubleshooting.md) ·
[risk engine](risk.md) · [configuration](configuration.md).
