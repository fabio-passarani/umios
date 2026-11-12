# Configuration

Two sources, on purpose:

- **YAML** (`configs/*.yaml`, versioned) — everything that defines an experiment.
  Unknown keys and inconsistent values are rejected at start-up. Each result
  stores the full configuration and its fingerprint.
- **Environment / `.env`** (never versioned) — deployment settings and secrets.

`qts config -c FILE` validates a file and prints it with its fingerprint.

## YAML

| Key | Default | Meaning |
|---|---|---|
| `mode` | `backtest` | `backtest` or `paper`. `live` is refused by every command |
| `market.symbols` | BTC/USDT, ETH/USDT | traded universe (`BASE/QUOTE`) |
| `market.timeframe` | `1d` | `1d`, `4h` or `1h`. Indicator windows are counted in candles, so the same parameters are a different strategy on another timeframe. Paper trading and the Freqtrade adapter are daily only |
| `data.directory` | `data` | where candles are stored (Parquet) |
| `data.start` | 2018-01-01 | first candle used |
| `data.end` | — | last candle used. **Set it in any configuration whose results you publish**: with an open end the period grows every day. `configs/default.yaml` pins it; `configs/paper.yaml` leaves it open because it trades forward |
| `strategy.params.*` | see `configs/default.yaml` | trend-following rules ([strategy](strategy.md)) |
| `portfolio.initial_capital` | 10 000 | simulated starting capital |
| `portfolio.sizing.risk_per_trade_pct` | 0.01 | equity lost if the stop is hit |
| `portfolio.sizing.max_position_pct` | 0.30 | cap on one position |
| `costs.fee_rate` | 0.001 | fee per fill, fraction of notional |
| `costs.slippage_bps` | 5 | adverse slippage per fill |
| `risk.*` | see [risk engine](risk.md) | hard limits; the sizing above may not exceed them |
| `risk.allowed_symbols` | — | whitelist for **entries**. Both shipped files set it; set it in yours |
| `risk.flatten_on_kill_switch` | false | close every position when the kill switch trips |
| `validation.out_of_sample_start` | — | first day of the out-of-sample segment. When set it replaces the fraction below, so the boundary does not move as data arrives |
| `validation.out_of_sample_fraction` | 0.3 | fallback when no date is set |
| `validation.walk_forward_*`, `parameter_grid` | — | walk-forward windows and the pre-declared grid |
| `validation.monte_carlo_simulations`, `seed` | 2000, 42 | bootstrap size and seed |

The fingerprint of a configuration changes whenever any value changes. A paper
session refuses to continue under a different fingerprint.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `QTS_CONFIG_FILE` | `configs/default.yaml` | YAML used when `-c` is not given |
| `QTS_STATE_DIR` | `state` | kill switch and Freqtrade risk state |
| `QTS_LOG_LEVEL`, `QTS_LOG_JSON` | `INFO`, `true` | logging |
| `QTS_DATABASE_URL` | — | full SQLAlchemy URL; wins over the parts below |
| `QTS_DB_HOST`, `QTS_DB_PORT`, `QTS_DB_NAME`, `QTS_DB_USER` | —, 5432, `qts`, `qts_app` | PostgreSQL connection as separate values (no URL-escaping needed) |
| `QTS_DB_PASSWORD` / `QTS_DB_PASSWORD_FILE` | — | password, or a file containing it (Docker secret); the file wins |
| `QTS_GIT_COMMIT` | — | commit recorded with results; set when the image is built |
| `QTS_ENABLE_LIVE_TRADING`, `QTS_LIVE_TRADING_CONFIRMATION` | `false`, empty | exist so that the refusal is explicit; setting them enables nothing |

With none of the database variables set, state goes to a local SQLite file
(`state/qts.sqlite`): nothing to install for backtests.

## Docker stack (`.env`)

| Variable | Used by |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | the database **administrator**: migrations and backups only |
| `QTS_APP_DB_PASSWORD` | role `qts_app` — the platform (data only, no DDL) |
| `FREQTRADE_DB_PASSWORD` | role `freqtrade_app` — Freqtrade's own database. Hex characters only |
| `GRAFANA_DB_PASSWORD` | role `grafana_ro` — read-only |
| `GRAFANA_ADMIN_USER`, `GRAFANA_ADMIN_PASSWORD` | Grafana login |
| `FREQTRADE_API_USERNAME`, `FREQTRADE_API_PASSWORD`, `FREQTRADE_JWT_SECRET` | Freqtrade API/UI (profile `freqtrade`) |
| `BACKUP_INTERVAL_HOURS`, `BACKUP_KEEP` | automatic backups (24, 14) |
| `QTS_BACKUP_DIR` | host directory for backups (`./backups`) |
| `GIT_COMMIT` | build argument: `GIT_COMMIT=$(git rev-parse HEAD) docker compose build` |

`scripts/setup_host.sh` creates `.env` with a random value for every secret.
Changed passwords (except the administrator's) are applied on the next
`docker compose up -d`.
