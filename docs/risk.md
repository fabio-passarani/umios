# Risk engine

Implementation: `qts.risk`. Limits: the `risk:` section of the YAML config.

## Principles

1. **Independent.** The package imports nothing from strategies, AI or execution
   (enforced by a test that parses its imports).
2. **Veto, no override.** `evaluate(order, context)` has no "force" argument. A
   rejection is final.
3. **Cannot be bypassed.** `Broker.execute` takes a `RiskApproval`, not an order.
   Approvals are HMAC-signed with a per-engine secret and are single-use; the
   broker asks the engine to verify and consume them. An approval is bound to
   what was evaluated: it expires after one minute, an entry can only be filled
   at the approved price, the kill switch is checked again at execution, and a
   newer entry approval revokes any unused older one (two entries approved on
   the same account snapshot can never both execute).
4. **Fail closed.** Bad numbers, stale data, unknown price, tripped kill switch or
   live mode → reject.
5. **Closing is always possible.** Sells that reduce a position pass even when the
   kill switch is active (they still cannot exceed the position: no shorting).
6. **Everything is journaled.** Each decision records every check; decisions and
   events go to the log and to the database (`risk_decisions`, `risk_events`).

## Checks on every entry

| Check | Limit | Default |
|---|---|---|
| Operating mode is backtest/paper | — | live always rejected |
| Order sanity (finite, positive numbers; no negative or non-finite positions) | — | — |
| Every open position has a trusted price | — | — |
| Symbol whitelist | `allowed_symbols` | BTC/USDT, ETH/USDT |
| Kill switch not active | — | — |
| Daily loss halt | `max_daily_loss_pct` | 5% |
| Drawdown | `max_drawdown_pct` | 25% (trips the kill switch) |
| Market data freshness | `max_data_age_hours` | 48 h |
| Price vs last close | `max_price_deviation_pct` | 25% |
| Stop loss present and below entry | `require_stop_loss` | true |
| Capital at risk on the trade | `max_risk_per_trade_pct` | 1.25% of equity |
| Position size | `max_position_pct` | 35% of equity |
| Total exposure | `max_total_exposure_pct` | 100% (cannot be set above: no leverage) |
| Number of open positions | `max_open_positions` | 4 |
| Cash covers notional + costs | — | — |
| Order rate | `max_orders_per_day` | 10 |

The config loader also refuses a sizing configuration that asks for more than the
risk limits allow.

## Circuit breakers

- **Daily loss halt** — when equity falls by `max_daily_loss_pct` in a day, new
  entries are blocked until a mark-to-market without the breach.
- **Drawdown kill switch** — when drawdown from the equity peak reaches
  `max_drawdown_pct`, the kill switch latches.
- **Repeated errors** — `max_consecutive_errors` operational failures in a row
  (e.g. stale data) latch the kill switch.
- **Invalid equity** (NaN, ≤ 0) latches it immediately.

## Kill switch

Persistent (`state/kill_switch.json`), survives restarts, and a corrupt, malformed
or unreadable state file is treated as *active*. Only a human resets it.

The file is shared by every process and re-read whenever it changes, so
`qts risk kill` stops a paper trader or Freqtrade bot that is **already running**
(in Docker: `docker compose run --rm paper-trader risk kill --reason "…"`), with
no restart. An operator activation blocks every new entry from that moment on,
whatever the timestamp of the candle being processed. Only automatic trips by
the engine carry the simulated time of the breach, so that a replay after a
restart reproduces the same decisions.

```bash
qts risk status
qts risk kill --reason "why"
qts risk reset --operator "your name"   # also re-bases the drawdown measure
```

`flatten_on_kill_switch: true` additionally closes all positions at the next open.

## Tests

`tests/risk/test_engine.py` covers each limit and the kill-switch file (another
process killing or resetting it, malformed content); `tests/risk/test_no_bypass.py`
covers raw orders, forged / altered / replayed / foreign / expired / revoked
approvals, execution at a price other than the approved one, a kill switch
tripping between approval and execution, a strategy that wants to trade while
the engine forbids it, and a kill switch tripping in the middle of a backtest.
The independent audit ([independent-audit.md](independent-audit.md)) lists the
attacks that were tried and their outcome.

## Honest limits

This is application-level enforcement inside one Python process: it prevents
bugs and shortcuts in strategy code, not a malicious developer editing the risk
package. Before any live use, exchange-side protections (API keys without
withdrawal rights, exchange-level position limits) must be added on top.
