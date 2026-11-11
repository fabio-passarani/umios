# Architecture

## Data flow

```
 Binance public archive ──► market_data (normalise, check) ──► Parquet store
                                                                   │
                         ┌─────────────────────────────────────────┤
                         ▼                                         ▼
                  strategies (signals)                    backtesting.benchmark
                         │  entry / exit / stop distance           (Buy & Hold)
                         ▼
        execution.TradingSession  ── sizing (portfolio.PositionSizer)
                         │ Order (a request, nothing more)
                         ▼
                 risk.RiskEngine ──► journal (every decision)
                         │   entries: every limit, fail closed
                         │   exits:   reduce-only path, never blocked
                         │ RiskApproval (HMAC-signed, single use)   ✗ rejected: stops here
                         ▼
                execution.PaperBroker ──► portfolio.Portfolio (cash, positions, trades)
                         │
                         ▼
     backtesting.metrics / validation ──► reporting ──► PostgreSQL ──► Grafana
```

The same `TradingSession` drives both the backtest and paper trading. A test proves
that paper trading fed candle by candle (with process restarts in between)
reproduces the backtest exactly, and `scripts/e2e_paper_test.sh` proves it on the
running Docker stack.

## Runtime (Docker stack)

```
                 ┌──────────── one-shot, on every `up` ────────────┐
                 │ db-setup: roles, grants     migrate: schema     │  (administrator account)
                 └──────────────────────┬──────────────────────────┘
                                        ▼
 paper-trader ──(qts_app)──►        PostgreSQL        ◄──(grafana_ro)── Grafana
   hourly step: data, signals,   runs · equity · signals · risk_decisions
   risk engine, simulated fills  fills · trades · positions · paper_state
                                 risk_events · service_checks · heartbeats
 monitor ─────(qts_app)──►            ▲      ▲
   every 5 min: health checks,        │      └──(administrator)── backup-scheduler ──► ./backups
   daily reports ──► ./reports/daily  │
 freqtrade (optional) ──(qts_app: risk journal)──┘   own database: (freqtrade_app)

 shared volume qts-state: kill_switch.json, freqtrade_risk_state.json
```

- A paper step is **one database transaction**: equity, signals, orders, fills,
  trades, positions and the resume point are committed together or not at all.
- The **kill switch** is a file on the shared state volume, re-read on every
  decision: one command stops the entries of every running service.
- The **monitor** is a separate process on purpose: it notices a trader that hangs.
- Nothing needs an inbound port; every published port is bound to `127.0.0.1`.

## Reproducibility

A stored backtest carries its *provenance*: period, universe, strategy version,
parameters, costs, a content hash of the candles, the Git commit and a hash of the
equity curve (`qts.core.provenance`). `qts reproduce RUN_ID` re-runs it and
compares. Research configurations pin `data.end` and the out-of-sample boundary;
the paper configuration is open-ended because it trades forward.

## Design decisions

**Own core + Freqtrade as an adapter, not Freqtrade as the core.** Freqtrade is an
excellent crypto execution framework (exchange connectivity, order lifecycle,
dry-run, UI) and is used for exactly that. But the platform must later cover
equities, UCITS ETFs, FX and futures through other brokers (e.g. Interactive
Brokers), which Freqtrade does not. So strategy logic, sizing, risk and validation
live in a broker-agnostic package, and Freqtrade consumes them through a ~200-line
adapter. This also keeps the GPLv3 dependency isolated (see `licensing.md`).

**Risk engine as a separate component with veto power.** Strategies return a table
of signals and hold no broker handle. Brokers accept only a `RiskApproval`, which
only the engine can mint. See `risk.md`.

**Next-open execution.** A signal computed on the close of day *t* is executed at
the open of *t+1*. Indicators are causal; tests check that appending or altering
future data never changes past signals or past equity.

**PostgreSQL for operations, Parquet for history.** Orders, fills, positions and
risk decisions need transactions, concurrent readers (Grafana) and backups →
PostgreSQL. Price history is append-mostly, columnar and large → Parquet files,
readable by any analytics tool. SQLite is supported for zero-setup local use.

**Typed, strict configuration.** pydantic models with `extra="forbid"`: a typo in
a risk limit is an error, not a silent default. The config fingerprint is stored
with every result.

**structlog.** Events are dictionaries, so secrets are redacted *before*
rendering, and output is JSON for machines or text for humans.

**Indicators in pandas, not TA-Lib.** Five small functions; no native dependency
in the core, and causality is explicit and tested.

## Extension points

| To add | Implement |
|---|---|
| A new data source / asset class | `market_data.sources.base.MarketDataSource` |
| A new strategy | `strategies.base.Strategy` + entry in `build_strategy` and `StrategyConfig` |
| A new broker (e.g. IBKR) | `execution.broker.Broker` (it receives approvals, never raw orders) |
| A new risk check | a block in `RiskEngine._entry_checks` + a limit in `RiskLimits` + a test |

Timeframes other than `1d`, short selling and derivatives are deliberately not
modelled yet (see `roadmap.md`).
