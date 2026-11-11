# Validation report

- Config fingerprint: `6dc19f88b80c0b5b`
- Strategy: `trend_following` on BTC/USDT, ETH/USDT (1d)
- Costs: fee 0.1000% + slippage 5.0 bps per fill
- Generated: 2026-11-11 19:30 UTC — qts 0.1.0

> Past performance of a simulation is not evidence of future returns. Read the warnings at the end before drawing any conclusion.

## Provenance

- Period: 2018-01-01 → 2026-11-09 (3235 bars), pinned
- Out-of-sample starts: 2024-03-14 (fixed date)
- Strategy: `trend_following` version 1.0.0
- Data version: `07b2adce8cab3f4a`
  - BTC/USDT: 3235 candles, sha256 `7d5fbdb68cb5573a…`
  - ETH/USDT: 3235 candles, sha256 `80325e0862732522…`
- Code: commit `44e71cc977d0`, qts 0.1.0, Python 3.12.3, numpy 2.5.4, pandas 3.1.1
- Result hash (full-period equity curve): `7578812595522fcb`
- Reproducible: yes

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

## Cost sensitivity

The same strategy, unchanged, under different frictions (full period). The configured costs are marked.

| Fee per fill | Slippage | Total return | CAGR | Sharpe | Max drawdown | Trades |
|---|---:|---:|---:|---:|---:|---:|
| 0.000% | 5 bps | 82.03% | 6.99% | 0.68 | 21.99% | 29 |
| 0.050% | 5 bps | 81.52% | 6.96% | 0.68 | 22.07% | 29 |
| 0.100% ◀ | 5 bps | 81.02% | 6.92% | 0.68 | 22.15% | 29 |
| 0.200% | 5 bps | 80.01% | 6.86% | 0.67 | 22.31% | 29 |
| 0.300% | 5 bps | 79.01% | 6.79% | 0.67 | 22.46% | 29 |
| 0.500% | 5 bps | 77.01% | 6.66% | 0.65 | 22.78% | 29 |
| 0.100% | 0 bps | 81.52% | 6.96% | 0.68 | 22.07% | 29 |
| 0.100% | 10 bps | 80.51% | 6.89% | 0.68 | 22.23% | 29 |
| 0.100% | 25 bps | 79.01% | 6.79% | 0.67 | 22.46% | 29 |
| 0.100% | 50 bps | 76.52% | 6.62% | 0.65 | 22.86% | 29 |
| 0.100% | 100 bps | 71.65% | 6.29% | 0.62 | 23.63% | 29 |
| 0.200% | 25 bps | 78.01% | 6.72% | 0.66 | 22.62% | 29 |

## Parameter sensitivity

One parameter at a time moved by ±20% (a diagnostic: nothing is selected from this table). 14 variants: 100.00% profitable, Sharpe from 0.52 to 0.80 (median 0.67; default 0.68); 28.57% of the variants beat the default.

| Parameter | Value | Total return | Sharpe | Max drawdown | Trades |
|---|---:|---:|---:|---:|---:|
| (default) | — | 81.02% | 0.68 | 22.15% | 29 |
| breakout_window | 44 | 91.33% | 0.71 | 22.53% | 30 |
| breakout_window | 66 | 76.83% | 0.66 | 22.20% | 28 |
| exit_window | 16 | 79.35% | 0.80 | 11.71% | 31 |
| exit_window | 24 | 56.55% | 0.52 | 25.27% | 12 |
| fast_ema | 40 | 79.68% | 0.67 | 22.95% | 30 |
| fast_ema | 60 | 78.30% | 0.67 | 22.15% | 29 |
| slow_ema | 160 | 80.06% | 0.67 | 22.95% | 31 |
| slow_ema | 240 | 78.66% | 0.67 | 22.22% | 29 |
| atr_window | 16 | 79.98% | 0.68 | 22.06% | 29 |
| atr_window | 24 | 81.38% | 0.68 | 22.15% | 29 |
| stop_atr_multiple | 2.4 | 104.40% | 0.70 | 24.40% | 30 |
| stop_atr_multiple | 3.6 | 67.07% | 0.66 | 20.21% | 28 |
| max_atr_pct | 0.096 | 81.02% | 0.68 | 22.15% | 29 |
| max_atr_pct | 0.144 | 81.02% | 0.68 | 22.15% | 29 |

## Market regimes

Bull / bear / sideways from the trailing 90-day return of the equal-weight universe (above +20%, below -20%, in between) — a rule fixed in advance. Returns are compounded over the days in each regime.

| Regime | Days | Share of time | Strategy | Buy & Hold | Time in market |
|---|---:|---:|---:|---:|---:|
| bull | 1188 | 36.72% | 105.54% | 9827.71% | 75.17% |
| sideways | 1163 | 35.95% | -12.16% | 269.96% | 18.23% |
| bear | 794 | 24.54% | 0.26% | -97.80% | 0.88% |
| warm-up | 90 | 2.78% | 0.00% | -47.97% | 0.00% |

## Calendar years

| Year | Strategy | Buy & Hold | Strategy max drawdown | Trades closed |
|---|---:|---:|---:|---:|
| 2018 | 0.00% | -77.57% | 0.00% | 0 |
| 2019 | 5.91% | 55.98% | 7.25% | 2 |
| 2020 | 19.02% | 344.27% | 5.39% | 4 |
| 2021 | 32.83% | 169.31% | 20.52% | 6 |
| 2022 | 0.00% | -66.15% | 0.00% | 0 |
| 2023 | 0.55% | 118.33% | 4.24% | 5 |
| 2024 | 7.83% | 83.61% | 6.93% | 5 |
| 2025 | 1.81% | -8.19% | 2.65% | 5 |
| 2026 | -2.06% | -19.50% | 2.06% | 2 |

## Exposure and correlation

- Time in market: 34.37%; average exposure 6.67% of equity (19.40% when invested), maximum 58.97%.
- Average number of open positions: 0.55; all assets held at once 19.91% of the time.
- Share of time each asset was held: BTC/USDT 30.66%, ETH/USDT 24.05%.
- Median holding period 35 days; longest stretch with no position 488 days.
- Daily-return correlation BTC/USDT ~ ETH/USDT: 0.82 (rolling 90-day: min 0.55, median 0.85, max 0.96).
- Effective number of independent bets: 1.35.

## Statistical significance

| Sample | Trades | Mean trade return | t-stat | p (mean ≤ 0) | Sharpe | 95% interval | P(Sharpe > 0) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Full period | 29 | 28.30% | 1.66 | 0.061 | 0.68 | [0.00, 1.35] | 97.57% |
| Out of sample | 9 | 4.29% | 0.64 | 0.260 | 0.29 | [-0.91, 1.49] | 68.18% |

p-value: one-sided bootstrap of the mean trade return under the null of zero expectancy. Sharpe interval: standard error adjusted for skewness and kurtosis of daily returns. 6 parameter combinations were declared; no correction for multiple testing is applied to the figures above.

## Warnings and limits

- Universe was chosen with hindsight (assets that still exist today): results carry survivorship bias and overstate what was knowable in advance.
- Only 29 closed trades: too few for statistically meaningful trade metrics.
- Out-of-sample Sharpe is less than half the in-sample Sharpe.
- Strategy total return is below Buy & Hold over the full period.
- Out of sample there are 9 closed trades: far too few for any statistical claim. The out-of-sample result is an observation, not a validation.
- The 95% interval of the out-of-sample Sharpe ratio is [-0.91, 1.49]: it includes zero.
- Full period: the mean trade return is not significantly above zero (bootstrap p = 0.06).
- The 2 assets behave like 1.4 independent bets: positions in both are largely the same position twice.
- 6 parameter combinations were declared for walk-forward selection; any figure chosen as the best of several is biased upwards.
- Regimes are defined on the same two assets that are traded, so 'bull' and 'bear' are descriptions of this sample, not independent market states.
