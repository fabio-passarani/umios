# quant-trading-system

Personal Algorithmic Trading Platform — Quantitative Strategies, Risk Management,
Backtesting, Paper Trading and AI Research.

> **Status: MVP (v0.1.0).** Crypto **spot**, long-only, **no leverage**, daily timeframe.
> The system runs in **BACKTEST** or **PAPER** mode only. **Live trading is not
> implemented**: there is no code path that can send a real order, and `mode: live`
> is refused at start-up. Nothing in this repository is financial advice, and a
> good backtest is not evidence of future returns.

## What it does

| Capability | Where |
|---|---|
| Download real history (Binance public archive, checksum-verified), normalise, store as Parquet | `qts.market_data` |
| Daily trend-following strategy, fully configured from YAML | `qts.strategies` |
| **Independent risk engine** with veto power, circuit breakers and kill switch | `qts.risk` |
| Backtesting with fees and slippage, Buy & Hold benchmark, metrics | `qts.backtesting` |
| Validation: in/out-of-sample, walk-forward, Monte Carlo | `qts.backtesting.validation` |
| Paper trading (same engine as the backtest, state in the database) | `qts.execution.paper` |
| Freqtrade integration (backtesting and dry-run) through a thin adapter | `freqtrade/`, `qts.integrations.freqtrade` |
| Operational data in PostgreSQL (versioned schema, least-privilege accounts), Grafana dashboard | `qts.integrations.db`, `docker/` |
| Monitoring service, daily report, soak-test verdict, automatic backups with tested restore | `qts.monitoring`, `scripts/` |
| Reproducible results: stored provenance, `qts reproduce` | `qts.core.provenance` |
| Research-only ML infrastructure (LightGBM, Optuna, SHAP) | `qts.ai` |

## Quick start (local Python)

Requires Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,reporting]"        # add ",ai" and/or ",freqtrade" when needed
# exact audited versions (Python 3.12): append  -c requirements-lock.txt

qts config                               # validate configs/default.yaml, print fingerprint
qts data download                        # real BTC/USDT and ETH/USDT daily candles since 2018
qts backtest                             # strategy vs Buy & Hold, saved to the local database
qts validate                             # IS/OOS, walk-forward, Monte Carlo, robustness -> reports/<run>/
qts reproduce <run id>                   # re-run a stored backtest and prove it gives the same result
pytest                                   # test suite
```

Without any `.env`, state goes to a local SQLite file under `./state` — no services needed.

## Quick start (Docker: PostgreSQL + paper trading + Grafana)

```bash
scripts/setup_host.sh                    # .env with random secrets, ./backups, ./reports
docker compose up -d --build             # postgres, migrations, paper-trader, monitor, backups, grafana
docker compose run --rm paper-trader report daily
```

Step by step, including how to run it for a week on a VPS and how to judge the
result: [paper trading runbook](docs/paper-trading-runbook.md).

Grafana: <http://127.0.0.1:3000> (user/password from `.env`) → dashboard *QTS — Trading
overview*. All ports are bound to `127.0.0.1` only.

```bash
docker compose run --rm paper-trader backtest -c configs/default.yaml   # shows up in Grafana
docker compose run --rm paper-trader paper status
docker compose run --rm paper-trader risk kill --reason "manual stop"   # kill switch
docker compose --profile freqtrade up -d --build                        # Freqtrade dry-run
scripts/backup.sh                                                       # DB + state backup
```

## Operating modes

| Mode | Meaning | Available |
|---|---|---|
| `backtest` | Simulation on stored history | yes (default) |
| `paper` | Forward simulation on new candles, simulated account, no credentials | yes |
| `live` | Real orders | **no — refused at start-up, no live broker exists** |

## Configuration

Everything that defines an experiment lives in versioned YAML
([`configs/default.yaml`](configs/default.yaml), [`configs/paper.yaml`](configs/paper.yaml)):
market, strategy parameters, sizing, costs, risk limits, validation settings. Unknown
keys and inconsistent limits are rejected. Each result stores the full configuration
and its fingerprint, so it can always be reproduced. Secrets come only from the
environment / `.env` (git-ignored) — see [`.env.example`](.env.example).

## Project layout

```
src/qts/
  core/          configuration, operating modes, logging (secret redaction), errors, types
  market_data/   sources, canonical OHLCV schema, quality checks, Parquet store
  strategies/    Strategy contract, indicators, trend following
  portfolio/     ledger (cash/positions/trades), position sizing
  risk/          independent risk engine, kill switch, decision journal
  execution/     brokers (paper; live is a refusing placeholder), router, trading session, paper trader
  backtesting/   engine, metrics, Buy & Hold benchmark, validation pipeline
  reporting/     report bundles, tearsheet, persistence of runs
  monitoring/    health checks
  integrations/  db/ (PostgreSQL/SQLite), freqtrade/ (bridge without Freqtrade imports)
  ai/            research-only features, regimes, LightGBM, Optuna, SHAP
freqtrade/       Freqtrade user_data with the adapter strategy
configs/         YAML configs, Freqtrade JSON configs
docker/          Grafana provisioning + dashboard, PostgreSQL init, Freqtrade image
tests/           unit/ risk/ backtesting/ integration/
docs/            architecture, strategy, risk, validation, operations, security, licensing, roadmap
```

## Documentation

Operating it:

- [Paper trading runbook](docs/paper-trading-runbook.md) — start, watch, intervene, stop, verify
- [Configuration](docs/configuration.md) — every YAML key and environment variable
- [Backup and restore](docs/backup-restore.md) — what is saved, how to prove it, how to restore
- [Troubleshooting](docs/troubleshooting.md) — symptom, cause, fix
- [Operations](docs/operations.md) — commands and services at a glance

Understanding it:

- [Architecture](docs/architecture.md) — modules, data flow, runtime, design decisions and why
- [Strategy](docs/strategy.md) — trend-following rules and parameters
- [Risk engine](docs/risk.md) — the authorisation model, every check, the kill switch
- [Backtesting and validation](docs/validation.md) — methodology, reproducibility, robustness,
  and the [latest real results](docs/results/2026-11-11-validation-pinned.md)
- [Freqtrade](docs/freqtrade.md) — how the integration works and how to run it
- [Security](docs/security.md) — secrets, database accounts, what is and is not protected
- [Licensing](docs/licensing.md) — dependency licences and why there is no `LICENSE` yet

Where it stands:

- [Independent audit](docs/independent-audit.md) — what was verified from a clean clone and fixed
- [Phase 3 report](docs/phase3-report.md) — hardening, what was tested, what is still open
- [Roadmap](docs/roadmap.md) — what is **not** implemented or not verified yet

## Quality gates

`ruff check .` · `ruff format --check .` · `mypy` (strict) · `pytest` (unit, integration,
PostgreSQL) — all run in [GitHub Actions](.github/workflows/ci.yml) together with a build of
both Docker images, a smoke test of the running stack, a backup-and-restore test, an
end-to-end paper trading test and a secret scan.

Branches: `main` is the stable version, `develop` collects finished work, changes arrive
through pull requests from `feature/*` branches.
