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
5. **Closing is always possible.** Anything that reduces a position goes through a
   separate *reduce-only* path (below) that none of the entry conditions can block.
6. **Everything is journaled.** Each decision records every check; decisions and
   events go to the log and to the database (`risk_decisions`, `risk_events`).

## Authorisation model

Every order, from the platform's own session or from Freqtrade, is one of two kinds:

| | New exposure (entry) | Reduction (any exit) |
|---|---|---|
| Reasons | `entry` | `exit_signal`, `stop_loss`, `trailing_stop`, `take_profit`, `manual`, `emergency`, `kill_switch` |
| Judged on | the order, the whole account, market data, every limit and circuit breaker | the order and the position it closes: is it an exit, are the numbers sane, is it no more than what is held |
| In doubt | **rejected** (fail closed) | **authorised** (fail open) |
| Blocked by kill switch, daily halt, drawdown, stale data, bad cash figure, de-listed symbol | yes | never |
| Can it be refused at all | yes | only if it is not a reduction: more than the position (a short), an entry reason, or nonsense numbers |
| Journaled | yes | yes |

How each case is handled:

| Case | Platform session (backtest / paper) | Freqtrade dry-run |
|---|---|---|
| Stop loss | checked against each candle's low; a gap fills at the open | stop price fixed when the entry fills and stored with the trade; Freqtrade enforces it; the exit is authorised and journaled |
| Trailing stop | ratchets up from the highest close; reported as `trailing_stop` | same rule; highest level stored with the trade |
| Take profit | no rule in the current strategy; the reason exists for partial closes by the operator | Freqtrade ROI exits map to `take_profit` |
| Partial close | `qts paper close --fraction`; the stop stays on the remainder | journaled when the partial fills |
| Emergency close | `qts paper close --all --emergency`, or `flatten_on_kill_switch` | `custom_exit` closes everything when the kill switch is on and `flatten_on_kill_switch` is set |
| Manual close | `qts paper close --symbol` | force-exit in the UI/API, journaled as `manual` |
| Exchange / broker error | the step is abandoned, the last committed state kept, the failure counted; three in a row trip the kill switch; the next step redoes the candles | entries fail closed on any exception; failures are counted the same way; exits are never held back |
| Restart with an open position | state (position, stop, pending orders, risk limits) is reloaded from the database | open trades and their stops come from Freqtrade's database, circuit-breaker state from `freqtrade_risk_state.json` |

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
