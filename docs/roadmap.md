# Roadmap — what is not implemented

Declared explicitly so nothing here is mistaken for a finished feature.

## Not implemented in the MVP

- **Live trading.** No live broker, no order placement, no exchange credentials
  are used. `LiveBroker` is a placeholder that raises.
- **Markets other than crypto spot.** Equities, UCITS ETFs, FX, futures and
  Interactive Brokers are extension points only (`MarketDataSource`, `Broker`).
- **Short selling, leverage, pyramiding.** Backtests run on `1h`, `4h` and `1d`
  candles; paper trading on intraday candles needs a live data source (the
  archive publishes a day's candles the day after) and is not implemented.
- **AI in the trading loop.** `qts.ai` trains, evaluates, tunes and explains a
  regime classifier, but no model output feeds the strategy.
- **Alerting.** Errors are visible in logs, Grafana, `qts health` and the daily
  report, and the monitor records every change of state — but nothing pushes a
  notification (Telegram/e-mail) yet. Someone has to look.
- **Freqtrade trades in Grafana.** The bot's risk decisions are in the platform
  database; its trades, orders and wallet stay in Freqtrade's own database and UI.
- **Off-site backups and encryption.** Backups are automatic, verified and
  restorable, but stay on the same machine until the operator copies them.
- **Secrets manager.** Passwords live in `.env`; `QTS_DB_PASSWORD_FILE` allows
  Docker secrets but the compose file does not use them.
- **Survivorship-free universe** and a larger, point-in-time asset list.
- **Tax reporting** (relevant for Italian residents) and exchange reconciliation.

## Done since the MVP

Independent audit and its fixes ([independent-audit.md](independent-audit.md));
one authorisation model for entries and every kind of exit, in the platform and
in Freqtrade; least-privilege database accounts and versioned migrations;
automatic backups with a tested restore; reproducible backtests with stored
provenance; robustness and significance studies; signal journal, monitor
service, daily report and soak-test verdict; an end-to-end test of the whole
chain on the running stack ([phase3-report.md](phase3-report.md)).

## Not verified yet

- **Seven days of continuous paper trading.** Everything needed is in place and
  the chain is tested end to end on a compressed timeline, but the test itself
  takes seven days on a machine that stays on. Until `qts report soak` says
  `PASSED` it has not been passed.
- **A Freqtrade dry-run entry produced by a strategy signal** (see
  [freqtrade.md](freqtrade.md)).

## Suggested next steps

1. Run the continuous paper-trading test on a VPS
   ([runbook](paper-trading-runbook.md)); read the daily report every day.
2. Alerting: push the monitor's state changes and the daily report.
3. Off-site, encrypted backups.
4. Keep paper trading for several months and compare it with the backtest.
5. Widen the universe with point-in-time constituents to get more trades and
   remove survivorship bias; re-run the validation unchanged.
6. Evaluate the regime classifier as a *filter* in a dedicated validation study.
7. Only then: design the live broker, reconciliation and exchange-side safeguards.
