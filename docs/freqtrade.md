# Freqtrade integration

Freqtrade (GPLv3) is used as an execution framework for crypto; trading rules,
sizing and the risk veto stay in the platform.

```
freqtrade/user_data/strategies/qts_trend_following.py   (thin IStrategy)
        │ populate_*            -> qts.strategies (same signals as the platform)
        │ custom_stake_amount   -> qts.portfolio.PositionSizer
        │ confirm_trade_entry   -> qts.risk.RiskEngine  (False = Freqtrade does not enter)
        │ custom_stoploss       -> ATR stop from the signal candle
        ▼
qts.integrations.freqtrade.FreqtradeBridge   (no Freqtrade import)
```

Freqtrade run modes are mapped to platform modes; anything that is not
backtest/hyperopt/dry-run resolves to `LIVE`. In that mode the adapter **refuses
to load** (`LiveTradingDisabledError`), so a bot started without `--dry-run`, or
with `dry_run: false`, exits immediately instead of starting — whatever the
`QTS_*` opt-in variables say. As a second line of defence the risk engine also
rejects every entry in `LIVE` mode.

The bot shares the platform's kill switch (`state/kill_switch.json`, re-read on
every decision: `qts risk kill` stops a running bot's entries without a restart)
and keeps its own circuit-breaker state (equity peak, daily-loss halt, daily
order count) in `state/freqtrade_risk_state.json`, so restarting the bot does not
reset the drawdown measure.

## Backtest with Freqtrade (cross-check of the internal engine)

```bash
pip install -e ".[freqtrade]"
qts data download && qts data export-freqtrade
cd freqtrade
QTS_CONFIG_FILE=../configs/default.yaml freqtrade backtesting \
  --config ../configs/freqtrade/config.json --userdir user_data --timerange 20180101-
```

## Dry-run (paper trading against the live order book)

```bash
docker compose --profile freqtrade up -d --build     # UI/API on http://127.0.0.1:8080
```

`configs/freqtrade/config.json` has `dry_run: true`, empty API keys and a
simulated 10 000 USDT wallet; the compose command also passes `--dry-run`.
API username, password and JWT secret come only from `.env`.

## `config.public-data-mirror.json`

Optional overlay that points market-data requests to Binance's public mirror
`data-api.binance.vision`. Use it only where `api.binance.com` is unreachable
(it was needed to verify the integration from a US-hosted build machine). It is
not needed from Italy.

## Not done yet

- Freqtrade's own trades are stored in its `freqtrade` database and shown in its
  UI, not in the Grafana dashboard.
- In Freqtrade the risk engine gates **entries**; exits and stops are executed by
  Freqtrade itself.
