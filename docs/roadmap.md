# Roadmap — what is not implemented

Declared explicitly so nothing here is mistaken for a finished feature.

## Not implemented in the MVP

- **Live trading.** No live broker, no order placement, no exchange credentials
  are used. `LiveBroker` is a placeholder that raises.
- **Markets other than crypto spot.** Equities, UCITS ETFs, FX, futures and
  Interactive Brokers are extension points only (`MarketDataSource`, `Broker`).
- **Timeframes other than `1d`**, short selling, leverage, pyramiding.
- **AI in the trading loop.** `qts.ai` trains, evaluates, tunes and explains a
  regime classifier, but no model output feeds the strategy.
- **Database migrations.** The schema is created with `create_all`; Alembic is
  needed before the schema changes in a deployed system.
- **Alerting.** Errors are visible in logs, Grafana and `qts health`, but nothing
  pushes notifications (Telegram/e-mail) yet.
- **Freqtrade data in Grafana**, and risk-engine gating of Freqtrade exits.
- **Survivorship-free universe** and a larger, point-in-time asset list.
- **Tax reporting** (relevant for Italian residents) and exchange reconciliation.

## Suggested next steps

1. Run paper trading for several months and compare it with the backtest.
2. Widen the universe with point-in-time constituents to get more trades and
   remove survivorship bias; re-run validation.
3. Alembic migrations and alerting.
4. Evaluate the regime classifier as a *filter* in a dedicated validation study.
5. Only then: design the live broker, reconciliation and exchange-side safeguards.
