# Validation report

- Config fingerprint: `0651aa412967b3bb`
- Strategy: `trend_following` on BTC/USDT, ETH/USDT (1d)
- Costs: fee 0.1000% + slippage 5.0 bps per fill
- Generated: 2026-11-11 07:28 UTC — qts 0.1.0

> Past performance of a simulation is not evidence of future returns. Read the warnings at the end before drawing any conclusion.

## Full (2018-01-01 → 2026-11-09)

| Metric | Strategy | Buy & Hold |
|---|---:|---:|
| Total return | 81.02% | 319.77% |
| CAGR | 6.92% | 17.57% |
| Annual volatility | 10.73% | 69.53% |
| Sharpe ratio | 0.68 | 0.59 |
| Sortino ratio | 0.98 | 0.83 |
| Max drawdown | 22.15% | 88.11% |
| Calmar ratio | 0.31 | 0.20 |
| Closed trades | 29 | 0 |
| Win rate | 41.38% | 0.00% |
| Profit factor | 5.39 | 0.00 |
| Expectancy per trade | 279.37 | 0.00 |
| Time in market | 34.37% | — |
| Fees paid | 90.15 | 0.00 |

## In Sample (2018-01-01 → 2024-03-13)

| Metric | Strategy | Buy & Hold |
|---|---:|---:|
| Total return | 88.20% | 438.75% |
| CAGR | 10.73% | 31.19% |
| Annual volatility | 12.47% | 75.84% |
| Sharpe ratio | 0.88 | 0.75 |
| Sortino ratio | 1.28 | 1.05 |
| Max drawdown | 22.15% | 88.11% |
| Calmar ratio | 0.48 | 0.35 |
| Closed trades | 18 | 0 |
| Win rate | 38.89% | 0.00% |
| Profit factor | 7.52 | 0.00 |
| Expectancy per trade | 366.34 | 0.00 |
| Time in market | 33.97% | — |
| Fees paid | 48.18 | 0.00 |

## Out Of Sample (2024-03-14 → 2026-11-09)

| Metric | Strategy | Buy & Hold |
|---|---:|---:|
| Total return | 2.62% | -21.89% |
| CAGR | 0.98% | -8.87% |
| Annual volatility | 3.58% | 51.81% |
| Sharpe ratio | 0.29 | 0.08 |
| Sortino ratio | 0.42 | 0.11 |
| Max drawdown | 4.38% | 58.45% |
| Calmar ratio | 0.22 | -0.15 |
| Closed trades | 9 | 0 |
| Win rate | 33.33% | 0.00% |
| Profit factor | 1.56 | 0.00 |
| Expectancy per trade | 29.11 | 0.00 |
| Time in market | 30.28% | — |
| Fees paid | 19.28 | 0.00 |

## Walk-forward analysis

Stitched out-of-sample track over 14 windows; parameters re-chosen on each train window among `{"breakout_window": [20.0, 55.0, 100.0], "stop_atr_multiple": [2.0, 3.0]}`. Walk-forward efficiency (OOS Sharpe / mean train Sharpe): **0.85**.

| Metric | Walk-forward OOS | Buy & Hold |
|---|---:|---:|
| Total return | 70.89% | 1248.30% |
| CAGR | 8.12% | 46.09% |
| Annual volatility | 11.13% | 71.41% |
| Sharpe ratio | 0.76 | 0.90 |
| Sortino ratio | 1.07 | 1.29 |
| Max drawdown | 16.93% | 77.81% |
| Calmar ratio | 0.48 | 0.59 |
| Closed trades | 36 | 0 |
| Win rate | 36.11% | 0.00% |
| Profit factor | 1.98 | 0.00 |
| Expectancy per trade | 65.66 | 0.00 |
| Fees paid | 101.56 | 0.00 |

| Test window | Chosen parameters | Train Sharpe | Test Sharpe | Test return | Trades |
|---|---|---:|---:|---:|---:|
| 2020-01-01 → 2020-06-28 | breakout_window=55, stop_atr_multiple=2 | 0.52 | -1.21 | -4.10% | 4 |
| 2020-06-29 → 2020-12-25 | breakout_window=100, stop_atr_multiple=2 | 0.32 | 2.29 | 21.58% | 2 |
| 2020-12-26 → 2021-06-23 | breakout_window=20, stop_atr_multiple=2 | 1.25 | 1.80 | 27.71% | 2 |
| 2021-06-24 → 2021-12-20 | breakout_window=20, stop_atr_multiple=2 | 1.37 | 0.48 | 1.92% | 4 |
| 2021-12-21 → 2022-06-18 | breakout_window=20, stop_atr_multiple=2 | 1.47 | 0.00 | 0.00% | 0 |
| 2022-06-19 → 2022-12-15 | breakout_window=20, stop_atr_multiple=2 | 1.51 | 0.00 | 0.00% | 0 |
| 2022-12-16 → 2023-06-13 | breakout_window=20, stop_atr_multiple=2 | 0.97 | 0.25 | 0.77% | 2 |
| 2023-06-14 → 2023-12-10 | breakout_window=20, stop_atr_multiple=2 | 0.26 | 1.83 | 5.01% | 3 |
| 2023-12-11 → 2024-06-07 | breakout_window=20, stop_atr_multiple=2 | 0.64 | 0.63 | 3.63% | 6 |
| 2024-06-08 → 2024-12-04 | breakout_window=20, stop_atr_multiple=2 | 0.73 | 2.07 | 5.79% | 2 |
| 2024-12-05 → 2025-06-02 | breakout_window=20, stop_atr_multiple=2 | 0.94 | -0.83 | -1.39% | 3 |
| 2025-06-03 → 2025-11-29 | breakout_window=20, stop_atr_multiple=2 | 0.86 | 0.17 | 0.54% | 6 |
| 2025-11-30 → 2026-05-28 | breakout_window=55, stop_atr_multiple=2 | 0.90 | 0.00 | 0.00% | 0 |
| 2026-05-29 → 2026-11-09 | breakout_window=55, stop_atr_multiple=2 | 0.73 | -1.95 | -2.09% | 2 |

## Monte Carlo (block bootstrap of daily returns)

2000 simulations, block size 20, seed 42. Probability of ending with a loss: **4.5%**.

| Statistic | Observed | P5 | P25 | Median | P75 | P95 |
|---|---:|---:|---:|---:|---:|---:|
| Total return | 81.02% | 1.58% | 43.36% | 78.41% | 127.66% | 217.58% |
| Max drawdown | 22.15% | 10.74% | 14.54% | 20.62% | 25.40% | 34.75% |
| Sharpe ratio | 0.68 | 0.07 | 0.45 | 0.68 | 0.93 | 1.26 |

## Warnings

- Universe was chosen with hindsight (assets that still exist today): results carry survivorship bias and overstate what was knowable in advance.
- Only 29 closed trades: too few for statistically meaningful trade metrics.
- Out-of-sample Sharpe is less than half the in-sample Sharpe.
- Strategy total return is below Buy & Hold over the full period.
