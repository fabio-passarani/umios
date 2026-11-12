# Backtesting and validation

Run everything with `qts validate`. Output: `reports/<UTC stamp>_<config fingerprint>/`
containing `report.md`, `report.json`, `config.yaml` (the exact configuration),
`equity.csv`, `trades.csv`, `walk_forward_oos_returns.csv` and a QuantStats
`tearsheet.html`. Same data + same config + same code ⇒ same numbers (the Monte
Carlo and the bootstrap tests are seeded).

## Reproducibility

- `configs/default.yaml` pins the period (`data.end`) and the out-of-sample
  boundary (`validation.out_of_sample_start`). With an open end the same command
  gives a different result every day, and a boundary defined as a fraction of the
  data drifts with it; the report warns when either is not fixed.
- Every saved backtest records its **provenance**: start, end, bar count, symbols,
  timeframe, strategy name and version, parameters, fee, slippage, a SHA-256 of
  the candles used (per symbol and overall), the Git commit (flagged when the
  working tree had uncommitted changes), library versions, a hash of the equity
  curve, and the metrics.
- `qts reproduce RUN_ID` re-runs a stored backtest from its own saved
  configuration and compares configuration, data, bar count, strategy version and
  result. Exit code 1 if anything that matters differs; a different commit alone
  is reported, not failed — identical results from newer code are the goal.
- To publish a result: pin the period, commit, run, and quote the run's data
  version and commit (they are printed by `qts backtest` and in the report).

## Pipeline

| Study | What it answers | How |
|---|---|---|
| Full-period backtest | What would have happened? | next-open fills, fee + slippage on every fill |
| Buy & Hold benchmark | Was the activity worth it? | equal-weight, same costs, same period |
| In / out of sample | Does it hold on unseen data? | chronological split (last 30% is OOS) |
| Walk-forward | Do parameters chosen in the past work afterwards? | 730-day train → pick best Sharpe in a small grid → trade next 180 days; only stitched test windows count |
| Monte Carlo | How much is luck of ordering? | 2000 block-bootstrap resamples of daily returns (20-day blocks) |
| Cost sensitivity | Does it survive worse frictions? | same rules under fees 0–5× and slippage 0–100 bps |
| Parameter sensitivity | Lonely peak or plateau? | each parameter ±20 %, one at a time; reported as a neighbourhood, nothing is selected |
| Regimes, calendar years | Where does the result come from? | bull / bear / sideways from the trailing 90-day return of the universe (rule fixed in advance, no look-ahead) |
| Correlation, exposure | How many independent bets? | rolling correlation, effective number of bets, time in market, exposure, holding periods |
| Significance | Could this be chance? | bootstrap test of the mean trade return; Sharpe with a standard error adjusted for skew and kurtosis; full period and out of sample |

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

[`results/2026-11-11-validation-pinned.md`](results/2026-11-11-validation-pinned.md)
— Binance spot daily candles 2018-01-01 → 2026-11-09, out of sample from
2024-03-14, config `6dc19f88b80c0b5b`, data version `07b2adce8cab3f4a`, commit
`44e71cc`. The headline figures equal the first report
([`results/2026-11-11-trend-following-validation.md`](results/2026-11-11-trend-following-validation.md),
same rules and parameters, unpinned configuration).

| | Full period | Out of sample |
|---|---:|---:|
| Strategy / Buy & Hold | +81.02 % / +319.77 % | +2.62 % / −21.89 % |
| Max drawdown, strategy / Buy & Hold | 22.15 % / 88.11 % | 4.38 % / 58.45 % |
| Closed trades | 29 | 9 |
| Mean trade return, p (mean ≤ 0) | 28.3 %, p = 0.061 | 4.3 %, p = 0.260 |
| Sharpe, 95 % interval | 0.68, [0.00, 1.35] | 0.29, [−0.91, 1.49] |

Reading them plainly:

- **Not statistically validated.** Out of sample there are 9 trades and the
  interval of the Sharpe ratio includes zero. Even over the full period the mean
  trade return is not significant at the 5 % level.
- It made **less money than Buy & Hold** over the full period, with a far smaller
  drawdown. It is a risk-reduction profile, not a return-maximising one.
- The out-of-sample "outperformance" exists because Buy & Hold fell 22 % while
  the strategy sat in cash 70 % of the time.
- The result is **not sensitive to costs** (+71.65 % even at 100 bps slippage:
  there are few trades) **nor to the parameters** (all 14 ±20 % variants are
  profitable, Sharpe 0.52–0.80, 29 % of them beat the default: the default is not
  a cherry-picked peak).
- The profit comes from **bull regimes** (+105.5 % over 37 % of the days).
  Sideways regimes lost 12.2 %. In bear regimes the strategy was invested 0.9 %
  of the time. 2018 and 2022 have no trade at all.
- BTC and ETH correlate at 0.82 (never below 0.55 over 90 days): the two assets
  are worth **1.35 independent bets**.
- An independent Freqtrade backtest of the same rules gives 29 trades and +79.8 %.

Limits of the data and residual biases:

- **Survivorship**: the universe was chosen knowing both assets survived.
- **One market, one cycle structure**: 2018–2026 contains two large crypto bull
  markets; almost all the profit is in 2020–2021.
- **Regimes are defined on the traded assets themselves**: they describe this
  sample, they are not independent market states.
- **Six parameter combinations were declared** for walk-forward selection and no
  multiple-testing correction is applied.
- **Daily candles**: intraday paths are unknown; a stop is assumed to fill at its
  level (or at the open after a gap) plus slippage.
- **Simulated fills**: no order book, no partial fills, no exchange outages.

**Conclusion: the strategy is not validated for real money.** The evidence is
compatible with a modest edge and equally with luck. More history of the same two
assets cannot change that; a wider, survivorship-free universe and months of
paper trading can. Parameter tuning on this data would only make it worse.
