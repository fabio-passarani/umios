# Backtesting and validation

Run everything with `qts validate`. Output: `reports/<UTC stamp>_<config fingerprint>/`
containing `report.md`, `report.json`, `config.yaml` (the exact configuration),
`equity.csv`, `trades.csv`, `walk_forward_oos_returns.csv` and a QuantStats
`tearsheet.html`. Same data + same config ⇒ same numbers (the Monte Carlo is seeded).

## Pipeline

| Study | What it answers | How |
|---|---|---|
| Full-period backtest | What would have happened? | next-open fills, fee + slippage on every fill |
| Buy & Hold benchmark | Was the activity worth it? | equal-weight, same costs, same period |
| In / out of sample | Does it hold on unseen data? | chronological split (last 30% is OOS) |
| Walk-forward | Do parameters chosen in the past work afterwards? | 730-day train → pick best Sharpe in a small grid → trade next 180 days; only stitched test windows count |
| Monte Carlo | How much is luck of ordering? | 2000 block-bootstrap resamples of daily returns (20-day blocks) |

Metrics: total return, CAGR, volatility, Sharpe, Sortino, max drawdown (and
duration), Calmar, profit factor, win rate, expectancy, time in market, fees.
They are implemented in `qts.backtesting.metrics` and unit-tested against
hand-computed values (365 periods per year, risk-free rate 0).

## How each bias is handled

| Bias | Mitigation | Proof |
|---|---|---|
| Look-ahead | causal indicators; signal at close *t* executes at open *t+1* | tests: signals and equity are unchanged when future data is appended or altered |
| Data leakage | chronological splits; walk-forward chooses parameters on train only; ML splits are purged by the label horizon | tests: tampering with test-period data does not change train results or chosen parameters |
| Overfitting | a-priori default parameters; tiny pre-declared grid; OOS + walk-forward efficiency + Monte Carlo reported next to the headline | report warnings |
| Survivorship | **not solved**: BTC and ETH were chosen knowing they survived | the report prints this warning every time |
| Optimistic fills | fees + slippage; gaps through a stop fill at the worse open | engine tests |

## Latest real results

[`results/2026-11-11-trend-following-validation.md`](results/2026-11-11-trend-following-validation.md)
(Binance spot daily candles 2018-01-01 → 2026-11-09, config `0651aa412967b3bb`).

Reading them plainly:

- Over the full period the strategy made **less money than Buy & Hold**
  (+81% vs +320%) with far smaller drawdown (22% vs 88%) and a slightly higher
  Sharpe (0.68 vs 0.59). It is a risk-reduction profile, not a return-maximising one.
- Out of sample (2024-03 → 2026-11) it was roughly flat (+2.6%, Sharpe 0.29)
  while Buy & Hold lost 22%. The OOS Sharpe is less than half the in-sample one.
- Only 29 closed trades: every trade statistic (win rate 41%, profit factor 5.4)
  rests on a handful of large winners in 2020–2021 and is statistically weak.
- Monte Carlo: about 4.5% of resampled paths end with a loss; median max
  drawdown ≈ 21%.
- An independent Freqtrade backtest of the same rules on the same data gives 29
  trades, +79.8% and 21.7% max drawdown — consistent with the internal engine
  (Freqtrade applies fees but no slippage).

**Conclusion: the strategy is not validated for real money.** The evidence is
compatible with a modest edge and equally with luck. The correct next step is
paper trading and a broader, survivorship-free universe — not parameter tuning.
