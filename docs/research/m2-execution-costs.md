# Milestone 2 — realistic execution and costs

- **Question:** does the existing trend-following strategy keep its result once
  execution is modelled realistically?
- **Data:** development segment of dataset `binance-usdt-1d-v1` only
  (development id `96431e14fa30b4a6`), BTC/USDT and ETH/USDT, trading from
  2018-01-01 to 2023-12-31. The sealed test period (2024-01-01 → 2026-11-09)
  was **not opened** (openings: 0).
- **Strategy:** `trend_following`, parameters of `configs/default.yaml`,
  unchanged. Capital 10 000 USDT, no leverage, long only.
- **Reproduce:** `qts research execution-costs -d research/datasets/binance-usdt-1d-v1.yaml
  --diagnostic 4h --diagnostic 1h` (needs the research data store, see
  [data.md](data.md)). Every number below is in
  [results/m2-execution-costs.md](results/m2-execution-costs.md) and
  `research/experiments/m2-execution-costs.json`; every run is in
  `research/experiments/ledger.jsonl`.

## Verdict

**Fragile.** Not because of costs: realistic costs, high costs, stressed
slippage, a market a thousand times thinner, delays and refused orders all leave
the daily result practically where it was (CAGR 9.2 % without costs; 9.0–9.2 %
under every cost and liquidity scenario; 8.7–10.4 % with delays and refusals).
The fragility is in the evidence itself:

- 17 trades in six years; the best one made 54 % of the profit, the best three
  110 % — **the other 14 trades lost money together**;
- the whole result comes from 2020 and 2021; 2018 and 2022 had no trade, 2019
  and 2023 almost nothing;
- the Sharpe ratio's 95 % interval is [−0.06, 1.58]: zero is inside it; the
  bootstrap test on trade returns gives p = 0.08: not significant at 5 %;
- moving the fill by a few hours or a day changes the CAGR by −0.4 to +1.3
  points — more than any cost scenario does. The sample is too small for its
  timing not to matter.

Against benchmarks it does what a trend filter does: far less return than
holding (9.1 % vs 20.7 % a year) with a quarter of the drawdown (22 % vs 88 %)
and a slightly higher Sharpe ratio (0.76 vs 0.64 — a difference well inside the
statistical noise). It beats a passive portfolio holding the same average
exposure (CAGR 2.3 %, Sharpe 0.29).

So the honest statement is: *the result is robust to costs and not established
as an edge.* The strategy was not modified to change this.

## 1. Before and after costs

Daily candles, both assets, development period. Costs are fee + spread +
impact actually charged; every figure is net.

| Scenario | CAGR | Sharpe | Sortino | Calmar | Max DD | Profit factor | Win rate | Expectancy / trade | Turnover / year | Trades | Total costs | Avg exposure |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| No costs | 9.2 % | 0.76 | 1.10 | 0.42 | 21.9 % | 7.46 | 35.3 % | 371 USDT (41.6 %) | 0.6× | 17 | 0 | 6.9 % |
| Published model (0.10 % fee + 5 bps) | 9.1 % | 0.76 | 1.09 | 0.41 | 22.1 % | 7.16 | 35.3 % | 367 (41.2 %) | 0.6× | 17 | 71 | 6.9 % |
| **Base estimate** | **9.1 %** | **0.76** | **1.10** | **0.41** | **22.1 %** | **7.25** | **35.3 %** | **368 (41.3 %)** | **0.6×** | **17** | **50** | **6.9 %** |
| High costs | 9.1 % | 0.76 | 1.09 | 0.41 | 22.1 % | 7.22 | 35.3 % | 368 (41.3 %) | 0.6× | 17 | 57 | 6.9 % |
| Stressed slippage | 9.1 % | 0.76 | 1.09 | 0.41 | 22.2 % | 7.14 | 35.3 % | 366 (41.2 %) | 0.6× | 17 | 78 | 6.9 % |

Benchmarks over the same window (entry paying the published model's costs):

| Benchmark | CAGR | Sharpe | Sortino | Calmar | Max DD | Avg exposure |
|---|---|---|---|---|---|---|
| Cash | 0.0 % | 0.00 | 0.00 | 0.00 | 0.0 % | 0 % |
| Buy & hold BTC + ETH (equal weight) | 20.7 % | 0.64 | 0.89 | 0.23 | 88.1 % | 100 % |
| Buy & hold BTC | 20.6 % | 0.63 | 0.89 | 0.25 | 81.2 % | 100 % |
| Buy & hold at the strategy's average exposure (6.9 % invested, rest cash) | 2.3 % | 0.29 | 0.39 | 0.10 | 23.0 % | 6.9 % |

Why the last benchmark: the strategy is in cash most of the time (average
exposure 6.9 %), so comparing it only with a fully invested portfolio compares
two different amounts of risk. The exposure-matched portfolio asks whether the
*timing* adds anything over simply holding a small position all the time. It
does here (9.1 % vs 2.3 %), with the caveats of the verdict. Buy & hold starting
on 2018-01-01 starts near a market top: a different start date would change the
benchmark a lot.

Where the cost goes in the base scenario: fees 47.6 USDT, spread 1.5, impact
1.2 — **the fee is 95 % of the cost**. On BTC and ETH, for orders of a few
thousand USDT, the observed spread is a fraction of a basis point; the published
model's 5 bps slippage was the more conservative assumption of the two (15 bps
per fill against 10.6 in the base estimate).

Break-even (a uniform cost per fill, fee included, from 0 to 400 bps): the CAGR
stays positive up to 400 bps per fill (7.0 % there) and stays above the
exposure-matched benchmark; the Sharpe ratio falls below buy & hold's at about
**263 bps per fill** — 17 times the published assumption. With a turnover of
0.6× a year, costs cannot be what decides this strategy.

## 2. Cost impact per asset

| | BTC/USDT alone | ETH/USDT alone |
|---|---|---|
| CAGR without costs → base → stressed | 5.3 % → 5.3 % → 5.2 % | 4.5 % → 4.5 % → 4.4 % |
| Sharpe without costs → stressed | 0.69 → 0.68 | 0.56 → 0.55 |
| Trades | 8 | 9 |
| Total costs, base / stressed (USDT) | 22 / 32 | 22 / 35 |
| 100× thinner market | 5.3 % | 4.4 % |
| Orders 4 hours late | 5.3 % | 4.5 % |
| Buy & hold | 20.6 % (max DD 81 %) | 20.8 % (max DD 94 %) |

Same picture for each: costs remove at most 0.1 point of CAGR. Each asset alone
has eight or nine trades — even less evidence than the pair.

## 3. Cost impact per period

Return of each calendar year; in brackets the costs of that year as a share of
the equity at its start.

| Scenario | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 |
|---|---|---|---|---|---|---|
| No costs | 0.0 % | 6.0 % | 19.2 % | 33.0 % | 0.0 % | 0.7 % |
| Published model | 0.0 % | 5.9 % (0.06 %) | 19.0 % (0.12 %) | 32.8 % (0.20 %) | 0.0 % | 0.5 % (0.17 %) |
| Base estimate | 0.0 % | 5.9 % (0.04 %) | 19.1 % (0.08 %) | 32.9 % (0.14 %) | 0.0 % | 0.6 % (0.12 %) |
| Stressed slippage | 0.0 % | 5.9 % (0.08 %) | 19.0 % (0.15 %) | 32.8 % (0.20 %) | 0.0 % | 0.5 % (0.17 %) |
| 100× thinner | 0.0 % | 5.9 % (0.07 %) | 19.0 % (0.12 %) | 32.9 % (0.18 %) | 0.0 % | 0.6 % (0.15 %) |
| Orders 4 h late | 0.0 % | 5.5 % | 19.2 % | 34.3 % | 0.0 % | 0.1 % |
| Buy & hold | −77.6 % | 56.0 % | 344.3 % | 169.3 % | −66.2 % | 118.3 % |

No year's costs exceed 0.2 % of equity. The distribution of the result over
time is the real finding: two years of strong trends made all of it; in the two
bear years the trend filter kept the strategy out (a loss avoided — buy & hold
lost 78 % and 66 %), and in the 2023 recovery it barely took part.

## 4. Stress scenarios

| Scenario | What changes | Basis | CAGR | Sharpe | Max DD |
|---|---|---|---|---|---|
| Base estimate | spread and impact from liquidity and volatility | fit on observed quotes | 9.1 % | 0.76 | 22.1 % |
| High costs | spread ×3.3, impact coefficient 1.81 | 90th percentiles observed | 9.1 % | 0.76 | 22.1 % |
| Stressed slippage | spread ×10, impact coefficient 6.55 | assumed, beyond observed | 9.1 % | 0.76 | 22.2 % |
| Market 10× thinner | liquidity ÷10 (= account ×10) | assumed | 9.1 % | 0.76 | 22.1 % |
| Market 100× thinner | liquidity ÷100 (= 1 M USDT account) | assumed | 9.1 % | 0.76 | 22.1 % |
| Market 1000× thinner | liquidity ÷1000 (= 10 M USDT account) | assumed | 9.0 % | 0.75 | 22.2 % |
| Orders 1 hour late | fill at the next hour's open | observed hourly prices | 8.7 % | 0.73 | 23.6 % |
| Orders 4 hours late | fill at the open 4 h later | observed hourly prices | 9.1 % | 0.76 | 21.7 % |
| Orders one day late | fill at the next day's open | observed daily prices | 10.4 % | 0.85 | 20.1 % |
| 10 % of orders refused | 20 random draws; entries not chased, exits retried | assumed | 9.1 % (8.8–9.4 %) | 0.76 | 22.2 % |
| All bad at once | high costs + 10× thinner + 4 h late + 10 % refused, 20 draws | combined | 9.2 % (8.7–9.4 %) | 0.77 | 21.8 % |

No partial fill happened in any scenario (orders of a few thousand USDT are a
tiny fraction of BTC/ETH daily volume even 1000× thinner) and no position had to
be liquidated for a delisting. Rejections hit 3 orders in the median draw.

A one-day delay *improving* the result is not a discovery: it means that with
17 trades the outcome depends on which day a few of them happened to start. It
is the clearest sign that the sample is small.

### Diagnostic only: the same rules on 4-hour and 1-hour candles

Not a validation of anything. The parameters are counted in candles and were
chosen for daily candles (55 one-hour candles are 2.3 days, not 55 days); these
are different, untested strategies, and the parameters were not adapted. They are
shown for one reason: to see how the weight of costs grows with trading
frequency.

| Timeframe | Turnover / year | Trades | CAGR no costs → base → published model | Sharpe no costs → base | Cost drag / year (base) |
|---|---|---|---|---|---|
| 1 day | 0.6× | 17 | 9.2 % → 9.1 % → 9.1 % | 0.76 → 0.76 | 0.1 % |
| 4 hours | 7.8× | 117 | 18.3 % → 17.4 % → 17.0 % | 1.28 → 1.23 | 0.8 % |
| 1 hour | 43.6× | 459 | 21.4 % → 15.8 % → 13.8 % | 1.22 → 0.94 | 4.8 % |

At 1 hour, costs take a quarter to a third of the gross return, and the mean
trade return falls from 1.0 % to 0.8 %; the fee is 92 % of the cost. The trend
is unambiguous and is what matters for the multi-asset intraday goal: **at
5–15 minutes, with turnover another order of magnitude higher, costs would not be
a detail but the main term**, and the fee level (taker 0.10 % vs maker or
discounted rates) would decide whether anything is left. These numbers say
nothing about whether any 5m/15m strategy works.

## 5. Verdict

See the top of this document: **fragile** — robust to every execution scenario
tested, not established as an edge. Concretely:

- it is **not invalidated by costs**: no cost or liquidity scenario removes
  more than 0.2 point of CAGR (delays move it by −0.4 to +1.3 points from the
  base estimate, in both
  directions); the break-even cost is an order of magnitude above
  any plausible one;
- it is **not validated**: 17 trades, profit concentrated in three trades and two
  years, Sharpe interval including zero, p = 0.08;
- it should not be read as ready for anything beyond what it already is: a
  daily, low-exposure trend filter on two correlated assets.

The 2024–2026 part of the sealed test period is **partially contaminated for
BTC/USDT and ETH/USDT**: the strategy's published results and the Phase 3
studies used those candles before the seal existed (recorded in the ledger as
`pre-m0-btc-eth-published`). A future final test of this strategy on BTC/ETH
would not be a clean out-of-sample test; only other instruments are clean in
that period.

## 6. Problems of the execution model

What each component is (also in `qts/execution/model.py`):

| Component | Kind | Source |
|---|---|---|
| Fee 0.10 % per fill | **observed** | venue's published spot taker fee |
| Minimum order value 5 USDT | **observed** | venue's order filter on 2026-11-16 |
| Fill price after a delay | **observed** | open of the real hourly/daily candle reached |
| Traded value of the candle | **observed** | candle data |
| Half spread as a function of traded value | **estimated** | cross-section of 518 live quotes on one day |
| Price impact (square-root law) | **estimated** | 85 live order books on the same day |
| Participation limit 1 % of the candle's value | **assumed** | |
| Rejection probability | **assumed** | stress parameter, 0 by default |
| Delisting haircut 10 % | **assumed** | |

Known weaknesses, in order of importance:

1. **No historical bid/ask.** Spreads and impact for 2018–2023 come from one
   snapshot of 2026 (`research/calibration/binance-spot-2026-11-16.json`). The
   relation between liquidity and cost is assumed constant over time; in 2018
   BTC/ETH spreads were probably wider than today. The stress scenarios (spread
   ×10) bound this for BTC/ETH, but not in general.
2. **Weak fits.** The spread fit explains 30 % of the cross-sectional variance
   (R² 0.30), the impact fit 21 %; the least-squares impact coefficient (6.55)
   is driven by the thinnest books, the median one is 0.29. Coefficients are
   scenario inputs, not measurements.
3. **Costs do not rise in a crisis.** Spread and impact depend on 30-day trailing
   liquidity and volatility; on the day of a crash — when stops fire — real
   spreads widen at once. Stop exits are therefore probably under-costed in tail
   events.
4. **Stops inside a candle** fill at the stop price (or at the open after a gap)
   plus modelled costs; the path inside the candle is unknown, and a stop-market
   order in a fast market slips more than the average model says.
5. **Refusals are independent coin tosses**; real outages cluster on volatile
   days (e.g. May 2021).
6. **Volume is the candle's total.** An order executed at the daily open meets
   the book at 00:00 UTC, not the day's average; intraday seasonality of
   liquidity is not modelled (the snapshot itself was taken at 07:29 UTC).
7. **Only market orders.** Maker (limit) orders, their lower fees, queue position
   and the risk of not being filled are not modelled; fee discounts and VIP tiers
   are not modelled either.
8. **Delisting** liquidation uses an assumed 10 % haircut on the last close; it
   was never triggered for BTC/ETH and matters only for the multi-asset universe.
9. **Not included at all:** withdrawal/transfer fees, taxes, funding (spot without
   leverage has none), exchange counterparty risk.
10. A delayed order whose hour is missing now meets the next traded hour (fixed
    during this milestone; it affected 5–13 days of 2018, none with a trade). If
    no hour traded before the candle closed, it falls back to the candle's open.

## 7. Missing data for intraday validation

Needed before any 5m/15m strategy can be evaluated honestly:

| Need | Available? |
|---|---|
| 5m and 15m spot candles, including delisted pairs | **yes**, in the public archive (verified for listed and delisted pairs); not yet downloaded or audited |
| Historical spot bid/ask (best quotes) | **no**: spot book tickers are not archived (only futures). Would have to be recorded going forward, or bought |
| Historical spot order-book depth | **no** for spot (futures only); same options |
| Individual trades and aggregated trades | **yes** (archive); allow estimating effective spreads (e.g. Roll, or the bid-ask bounce of trade prices) and intraday volume profiles — not yet used |
| Spread and liquidity *by time of day* | can be built from trades or recorded quotes; not yet done |
| Exchange outages and maintenance windows | not collected |
| Fee schedule history, maker/taker | current schedule only |
| Latency between signal and order on the real path | unmeasured |

And a methodological point recorded on purpose: **results at 1h or 4h do not
show that a strategy works at 5m or 15m**. Microstructure noise, the share of
cost in each trade and the number of trades are all different; nothing in this
milestone qualifies an intraday strategy as ready.

## 8. Proposal for Milestone 3

Technical, not implemented here:

1. **Intraday data layer.** Download and audit 5m/15m candles for the
   survivorship-free universe of Milestone 1, with the same sealed split and
   manifest (a new dataset, e.g. `binance-usdt-15m-v1`); aggregated trades for a
   liquidity-stratified sample.
2. **Effective-spread estimation from trades** (bid-ask bounce / Roll and
   Corwin-Schultz estimators on 1m–5m data), per instrument and per hour of day,
   to replace the single 2026 snapshot with a *historical, time-varying* estimate;
   compare it with the snapshot on the overlap.
3. **Start recording quotes now.** A small collector storing best bid/ask and top
   of book for the universe every minute, so that in a few months there is a
   genuinely observed spread history to validate the estimates against.
4. **Execution model extensions:** costs that rise with same-day volatility
   (crisis widening), limit orders with fill probability, time-of-day liquidity,
   fee tiers.
5. **Research protocol for intraday hypotheses:** each hypothesis written in the
   ledger *before* it is tested, with its universe, timeframe, parameters and the
   number of variants allowed; multiple-testing correction (deflated Sharpe) over
   all variants recorded; break-even cost reported for each.
6. **Minimum evidence before any claim:** a number of independent trades large
   enough for the trade-level test to be meaningful, results stable across years
   and asset groups, and net of the *high* cost scenario — all on development
   data, with the sealed test opened once at the end.

The daily strategy stays as it is: it is not tuned, and it is not the candidate
for intraday trading.
