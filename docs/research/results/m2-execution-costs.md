# Execution-cost study `m2-execution-costs`

Development segment only, 2018-01-01 -> 2023-12-31, BTC/USDT, ETH/USDT, capital 10,000. Config fingerprint `ee34f279a758c6cf`, data version `c2f0ed4359355e0b`.

Every figure is net of the costs charged in that scenario (fees + spread + impact). Expect. $ is the mean profit per closed trade, Expect. % its mean return; turnover is traded value per year over average equity.

## Scenarios

| Scenario | CAGR | Sharpe | Sortino | Calmar | Max DD | PF | Win | Expect. $ | Expect. % | Turnover/y | Trades | Costs $ | Cost drag/y | Avg exp. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| no_costs | 9.2% | 0.76 | 1.10 | 0.42 | 21.9% | 7.46 | 35.3% | 371 | 41.6% | 0.6 | 17 | 0 | 0.0% | 6.9% |
| legacy | 9.1% | 0.76 | 1.09 | 0.41 | 22.1% | 7.16 | 35.3% | 367 | 41.2% | 0.6 | 17 | 71 | 0.1% | 6.9% |
| base | 9.1% | 0.76 | 1.10 | 0.41 | 22.1% | 7.25 | 35.3% | 368 | 41.3% | 0.6 | 17 | 50 | 0.1% | 6.9% |
| high | 9.1% | 0.76 | 1.09 | 0.41 | 22.1% | 7.22 | 35.3% | 368 | 41.3% | 0.6 | 17 | 57 | 0.1% | 6.9% |
| stressed_slippage | 9.1% | 0.76 | 1.09 | 0.41 | 22.2% | 7.14 | 35.3% | 366 | 41.2% | 0.6 | 17 | 78 | 0.1% | 6.9% |
| liquidity_10x_thinner | 9.1% | 0.76 | 1.09 | 0.41 | 22.1% | 7.23 | 35.3% | 368 | 41.3% | 0.6 | 17 | 55 | 0.1% | 6.9% |
| liquidity_100x_thinner | 9.1% | 0.76 | 1.09 | 0.41 | 22.1% | 7.18 | 35.3% | 367 | 41.2% | 0.6 | 17 | 68 | 0.1% | 6.9% |
| liquidity_1000x_thinner | 9.0% | 0.75 | 1.09 | 0.41 | 22.2% | 7.04 | 35.3% | 365 | 41.0% | 0.6 | 17 | 105 | 0.1% | 6.9% |
| delay_1h | 8.7% | 0.73 | 1.06 | 0.37 | 23.6% | 6.89 | 41.2% | 352 | 38.5% | 0.6 | 17 | 50 | 0.1% | 6.9% |
| delay_4h | 9.1% | 0.76 | 1.10 | 0.42 | 21.7% | 7.02 | 41.2% | 374 | 42.0% | 0.6 | 17 | 51 | 0.1% | 6.9% |
| delay_1_candle | 10.4% | 0.85 | 1.24 | 0.52 | 20.1% | 11.49 | 56.2% | 462 | 52.8% | 0.6 | 16 | 49 | 0.1% | 7.0% |
| rejections_10pct | 9.1% | 0.76 | 1.10 | 0.41 | 22.2% | 7.13 | 35.3% | 371 | 41.7% | 0.6 | 17 | 50 | 0.1% | 6.9% |
| combined_worst | 9.2% | 0.77 | 1.11 | 0.42 | 21.8% | 6.88 | 41.2% | 376 | 42.1% | 0.6 | 17 | 74 | 0.1% | 6.9% |

| Scenario | Basis | Rejected | Partial | Sharpe 95% CI | P(Sharpe>0) | Trade p-value | Draws (CAGR min/median/max) |
|---|---|---|---|---|---|---|---|
| no_costs | theoretical: what the signals are worth before anybody is paid | 0 | 0 | [-0.05, 1.58] | 96.6% | 0.080 | - |
| legacy | fee observed, slippage assumed | 0 | 0 | [-0.06, 1.58] | 96.5% | 0.081 | - |
| base | fee observed; spread and impact estimated (snapshot fit); participation assumed 1% | 0 | 0 | [-0.06, 1.58] | 96.5% | 0.080 | - |
| high | 90th percentiles of the observed dispersion | 0 | 0 | [-0.06, 1.58] | 96.5% | 0.080 | - |
| stressed_slippage | assumed stress beyond the observed 90th percentile | 0 | 0 | [-0.06, 1.58] | 96.5% | 0.081 | - |
| liquidity_10x_thinner | assumed scenario | 0 | 0 | [-0.06, 1.58] | 96.5% | 0.080 | - |
| liquidity_100x_thinner | assumed scenario | 0 | 0 | [-0.06, 1.58] | 96.5% | 0.081 | - |
| liquidity_1000x_thinner | assumed scenario | 0 | 0 | [-0.06, 1.57] | 96.5% | 0.081 | - |
| delay_1h | fill price observed: open of the hourly candle | 0 | 0 | [-0.09, 1.55] | 96.0% | 0.079 | - |
| delay_4h | fill price observed: open of the hourly candle | 0 | 0 | [-0.06, 1.58] | 96.6% | 0.081 | - |
| delay_1_candle | fill price observed: open of the next candle | 0 | 0 | [0.03, 1.67] | 97.9% | 0.077 | - |
| rejections_10pct | assumed rate; entries are not chased, exits are retried next candle | 3 | 0 | [-0.06, 1.58] | 96.6% | 0.080 | 8.8% / 9.1% / 9.4% |
| combined_worst | all the above at once | 3 | 0 | [-0.05, 1.59] | 96.7% | 0.080 | 8.7% / 9.2% / 9.4% |

Concentration (closed trades): share of profit from the best trade and the best three, and what the other trades made together.

| Scenario | Best trade | Top 3 | P&L without top 3 | Avg hold (days) | Payoff |
|---|---|---|---|---|---|
| no_costs | 53.4% | 109.7% | -613 | 65 | 13.69 |
| legacy | 53.8% | 110.6% | -658 | 65 | 13.13 |
| base | 53.7% | 110.3% | -645 | 65 | 13.29 |
| high | 53.7% | 110.4% | -649 | 65 | 13.24 |
| stressed_slippage | 53.8% | 110.6% | -661 | 65 | 13.09 |
| liquidity_10x_thinner | 53.7% | 110.4% | -648 | 65 | 13.26 |
| liquidity_100x_thinner | 53.8% | 110.5% | -655 | 65 | 13.16 |
| liquidity_1000x_thinner | 54.0% | 110.9% | -677 | 65 | 12.90 |
| delay_1h | 50.2% | 110.7% | -642 | 64 | 9.84 |
| delay_4h | 53.6% | 110.9% | -690 | 64 | 10.03 |
| delay_1_candle | 53.9% | 101.8% | -131 | 68 | 8.94 |
| rejections_10pct | 53.5% | 109.9% | -624 | 65 | 13.07 |
| combined_worst | 53.4% | 111.3% | -724 | 65 | 9.83 |

## Benchmarks

| Scenario | CAGR | Sharpe | Sortino | Calmar | Max DD | PF | Win | Expect. $ | Expect. % | Turnover/y | Trades | Costs $ | Cost drag/y | Avg exp. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cash | 0.0% | 0.00 | 0.00 | 0.00 | 0.0% | 0.00 | 0.0% | 0 | 0.0% | 0.0 | 0 | 0 | 0.0% | 0.0% |
| buy_and_hold_equal_weight | 20.7% | 0.64 | 0.89 | 0.23 | 88.1% | 0.00 | 0.0% | 0 | 0.0% | 0.0 | 0 | 0 | 0.0% | 100.0% |
| buy_and_hold_btc | 20.6% | 0.63 | 0.89 | 0.25 | 81.2% | 0.00 | 0.0% | 0 | 0.0% | 0.0 | 0 | 0 | 0.0% | 100.0% |
| exposure_matched_buy_and_hold | 2.3% | 0.29 | 0.39 | 0.10 | 23.0% | 0.00 | 0.0% | 0 | 0.0% | 0.0 | 0 | 0 | 0.0% | 6.9% |

## Per asset

### BTC/USDT

| Scenario | CAGR | Sharpe | Sortino | Calmar | Max DD | PF | Win | Expect. $ | Expect. % | Turnover/y | Trades | Costs $ | Cost drag/y | Avg exp. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| no_costs | 5.3% | 0.69 | 1.05 | 0.39 | 13.6% | 10.31 | 37.5% | 402 | 34.0% | 0.3 | 8 | 0 | 0.0% | 4.5% |
| legacy | 5.2% | 0.68 | 1.04 | 0.38 | 13.7% | 9.86 | 37.5% | 398 | 33.6% | 0.3 | 8 | 31 | 0.0% | 4.5% |
| base | 5.3% | 0.68 | 1.04 | 0.38 | 13.7% | 10.00 | 37.5% | 399 | 33.8% | 0.3 | 8 | 22 | 0.0% | 4.5% |
| high | 5.3% | 0.68 | 1.04 | 0.38 | 13.7% | 9.97 | 37.5% | 399 | 33.7% | 0.3 | 8 | 24 | 0.0% | 4.5% |
| stressed_slippage | 5.2% | 0.68 | 1.04 | 0.38 | 13.7% | 9.88 | 37.5% | 398 | 33.6% | 0.3 | 8 | 32 | 0.0% | 4.5% |
| liquidity_100x_thinner | 5.3% | 0.68 | 1.04 | 0.38 | 13.7% | 9.92 | 37.5% | 398 | 33.7% | 0.3 | 8 | 28 | 0.0% | 4.5% |
| delay_4h | 5.3% | 0.69 | 1.06 | 0.41 | 13.1% | 10.05 | 50.0% | 411 | 34.9% | 0.3 | 8 | 22 | 0.0% | 4.5% |
| buy_and_hold | 20.6% | 0.63 | 0.89 | 0.25 | 81.2% | 0.00 | 0.0% | 0 | 0.0% | 0.0 | 0 | 0 | 0.0% | 100.0% |

### ETH/USDT

| Scenario | CAGR | Sharpe | Sortino | Calmar | Max DD | PF | Win | Expect. $ | Expect. % | Turnover/y | Trades | Costs $ | Cost drag/y | Avg exp. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| no_costs | 4.5% | 0.56 | 0.77 | 0.24 | 18.7% | 7.25 | 33.3% | 327 | 48.4% | 0.3 | 9 | 0 | 0.0% | 3.0% |
| legacy | 4.4% | 0.55 | 0.76 | 0.24 | 18.8% | 6.97 | 33.3% | 324 | 48.0% | 0.3 | 9 | 31 | 0.0% | 3.0% |
| base | 4.5% | 0.55 | 0.76 | 0.24 | 18.8% | 7.05 | 33.3% | 325 | 48.1% | 0.3 | 9 | 22 | 0.0% | 3.0% |
| high | 4.5% | 0.55 | 0.76 | 0.24 | 18.8% | 7.02 | 33.3% | 325 | 48.0% | 0.3 | 9 | 25 | 0.0% | 3.0% |
| stressed_slippage | 4.4% | 0.55 | 0.76 | 0.24 | 18.9% | 6.93 | 33.3% | 323 | 47.9% | 0.3 | 9 | 35 | 0.1% | 3.0% |
| liquidity_100x_thinner | 4.4% | 0.55 | 0.76 | 0.24 | 18.8% | 6.97 | 33.3% | 324 | 47.9% | 0.3 | 9 | 30 | 0.0% | 3.0% |
| delay_4h | 4.5% | 0.56 | 0.77 | 0.24 | 18.5% | 6.66 | 33.3% | 326 | 48.3% | 0.3 | 9 | 22 | 0.0% | 3.0% |
| buy_and_hold | 20.8% | 0.67 | 0.97 | 0.22 | 94.0% | 0.00 | 0.0% | 0 | 0.0% | 0.0 | 0 | 0 | 0.0% | 100.0% |

## Per year (return; costs as % of equity at the start of the year)

| Scenario | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 |
|---|---|---|---|---|---|---|
| no_costs | 0.0% (0.00%) | 6.0% (0.00%) | 19.2% (0.00%) | 33.0% (0.00%) | 0.0% (0.00%) | 0.7% (0.00%) |
| legacy | 0.0% (0.00%) | 5.9% (0.06%) | 19.0% (0.12%) | 32.8% (0.20%) | 0.0% (0.00%) | 0.5% (0.17%) |
| base | 0.0% (0.00%) | 5.9% (0.04%) | 19.1% (0.08%) | 32.9% (0.14%) | 0.0% (0.00%) | 0.6% (0.12%) |
| high | 0.0% (0.00%) | 5.9% (0.05%) | 19.0% (0.10%) | 32.9% (0.15%) | 0.0% (0.00%) | 0.6% (0.13%) |
| stressed_slippage | 0.0% (0.00%) | 5.9% (0.08%) | 19.0% (0.15%) | 32.8% (0.20%) | 0.0% (0.00%) | 0.5% (0.17%) |
| liquidity_100x_thinner | 0.0% (0.00%) | 5.9% (0.07%) | 19.0% (0.12%) | 32.9% (0.18%) | 0.0% (0.00%) | 0.6% (0.15%) |
| delay_4h | 0.0% (0.00%) | 5.5% (0.04%) | 19.2% (0.09%) | 34.3% (0.14%) | 0.0% (0.00%) | 0.1% (0.12%) |
| buy_and_hold_equal_weight | -77.6% | 56.0% | 344.3% | 169.3% | -66.2% | 118.3% |

## Break-even: uniform cost per fill (fee included)

| Scenario | CAGR | Sharpe | Sortino | Calmar | Max DD | PF | Win | Expect. $ | Expect. % | Turnover/y | Trades | Costs $ | Cost drag/y | Avg exp. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 bps | 9.2% | 0.76 | 1.10 | 0.42 | 21.9% | 7.46 | 35.3% | 371 | 41.6% | 0.6 | 17 | 0 | 0.0% | 6.9% |
| 10 bps | 9.1% | 0.76 | 1.10 | 0.41 | 22.1% | 7.26 | 35.3% | 368 | 41.3% | 0.6 | 17 | 48 | 0.1% | 6.9% |
| 15 bps | 9.1% | 0.76 | 1.09 | 0.41 | 22.1% | 7.16 | 35.3% | 367 | 41.2% | 0.6 | 17 | 71 | 0.1% | 6.9% |
| 25 bps | 9.0% | 0.75 | 1.09 | 0.40 | 22.3% | 6.98 | 35.3% | 364 | 40.9% | 0.6 | 17 | 119 | 0.1% | 6.9% |
| 50 bps | 8.9% | 0.74 | 1.07 | 0.39 | 22.7% | 6.54 | 35.3% | 357 | 40.2% | 0.6 | 17 | 237 | 0.3% | 6.9% |
| 75 bps | 8.7% | 0.73 | 1.05 | 0.38 | 23.1% | 6.16 | 35.3% | 350 | 39.5% | 0.6 | 17 | 354 | 0.4% | 6.9% |
| 100 bps | 8.6% | 0.72 | 1.04 | 0.36 | 23.5% | 5.81 | 35.3% | 342 | 38.8% | 0.6 | 17 | 470 | 0.6% | 6.9% |
| 150 bps | 8.3% | 0.70 | 1.01 | 0.34 | 24.3% | 5.21 | 29.4% | 328 | 37.4% | 0.6 | 17 | 699 | 0.9% | 6.9% |
| 200 bps | 7.9% | 0.67 | 0.97 | 0.32 | 25.0% | 4.69 | 29.4% | 315 | 36.0% | 0.6 | 17 | 893 | 1.1% | 6.8% |
| 300 bps | 7.1% | 0.62 | 0.89 | 0.28 | 25.7% | 4.58 | 31.2% | 321 | 36.3% | 0.5 | 16 | 1,172 | 1.5% | 6.4% |
| 400 bps | 7.0% | 0.60 | 0.86 | 0.27 | 25.6% | 4.65 | 33.3% | 332 | 37.0% | 0.5 | 15 | 1,417 | 1.8% | 6.3% |

| Break-even point | bps per fill |
|---|---|
| cagr_zero_bps | not reached up to 400 |
| cagr_matched_benchmark_bps | not reached up to 400 |
| sharpe_buy_and_hold_bps | 262.7 |
| sharpe_matched_benchmark_bps | not reached up to 400 |

## Diagnostic only: same rules on 4h candles

Parameters were never chosen for this timeframe. This is not a validation and says nothing about 5m/15m.

| Scenario | CAGR | Sharpe | Sortino | Calmar | Max DD | PF | Win | Expect. $ | Expect. % | Turnover/y | Trades | Costs $ | Cost drag/y | Avg exp. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| no_costs | 18.3% | 1.28 | 1.86 | 1.25 | 14.7% | 2.52 | 39.3% | 149 | 3.8% | 7.8 | 117 | 0 | 0.0% | 9.7% |
| legacy | 17.0% | 1.20 | 1.74 | 1.05 | 16.3% | 2.33 | 39.3% | 134 | 3.5% | 7.8 | 117 | 1,273 | 1.2% | 9.8% |
| base | 17.4% | 1.23 | 1.77 | 1.10 | 15.8% | 2.39 | 39.3% | 138 | 3.6% | 7.8 | 117 | 924 | 0.8% | 9.8% |
| high | 17.2% | 1.21 | 1.75 | 1.08 | 15.9% | 2.36 | 39.3% | 136 | 3.5% | 7.8 | 117 | 1,081 | 1.0% | 9.8% |
| buy_and_hold | 20.7% | 0.63 | 0.88 | 0.23 | 88.3% | 0.00 | 0.0% | 0 | 0.0% | 0.0 | 0 | 0 | 0.0% | 100.0% |

## Diagnostic only: same rules on 1h candles

Parameters were never chosen for this timeframe. This is not a validation and says nothing about 5m/15m.

| Scenario | CAGR | Sharpe | Sortino | Calmar | Max DD | PF | Win | Expect. $ | Expect. % | Turnover/y | Trades | Costs $ | Cost drag/y | Avg exp. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| no_costs | 21.4% | 1.22 | 1.71 | 1.28 | 16.7% | 1.57 | 35.7% | 48 | 1.0% | 43.7 | 459 | 0 | 0.0% | 12.1% |
| legacy | 13.8% | 0.84 | 1.18 | 0.75 | 18.5% | 1.33 | 34.4% | 26 | 0.7% | 43.6 | 459 | 6,568 | 6.5% | 12.2% |
| base | 15.8% | 0.94 | 1.32 | 0.87 | 18.1% | 1.39 | 34.9% | 31 | 0.8% | 43.6 | 459 | 5,049 | 4.8% | 12.2% |
| high | 14.5% | 0.88 | 1.22 | 0.78 | 18.6% | 1.36 | 34.4% | 27 | 0.7% | 43.6 | 459 | 5,825 | 5.8% | 12.2% |
| buy_and_hold | 20.9% | 0.64 | 0.89 | 0.24 | 88.5% | 0.00 | 0.0% | 0 | 0.0% | 0.0 | 0 | 0 | 0.0% | 100.0% |
