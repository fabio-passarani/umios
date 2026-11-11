# Trend-following strategy

Long-only, daily candles, crypto spot. Implementation: `qts.strategies.trend_following`.

## Rules

| Step | Rule | Parameter (default) |
|---|---|---|
| Trend filter | fast EMA above slow EMA | `fast_ema` (50), `slow_ema` (200) |
| Entry | close above the highest high of the previous N days | `breakout_window` (55) |
| Volatility filter | ATR/close within a band, otherwise no entry | `min_atr_pct` (0), `max_atr_pct` (0.12) |
| Exit | close below the lowest low of the previous M days, or trend filter off | `exit_window` (20) |
| Stop loss | entry price − k × ATR, checked against each day's low | `atr_window` (20), `stop_atr_multiple` (3) |
| Trailing stop (optional) | stop ratchets up to highest close − k × ATR | `trailing_stop` (false), `trailing_atr_multiple` (3) |
| Position size | lose `risk_per_trade_pct` of equity if the stop is hit, capped | `risk_per_trade_pct` (1%), `max_position_pct` (30%) |

All parameters are in `configs/*.yaml`; nothing is hard-coded.

## Why these rules and these defaults

They are the textbook Donchian-breakout ("Turtle") family: simple, few parameters,
well documented, and based on an effect (trend persistence) with a long published
history. The defaults are conventional round numbers (55/20 breakout, 50/200 EMA,
3 ATR) fixed **before** looking at results. They were not tuned on this data, and
the validation report shows the consequences honestly (see `validation.md`).

The trailing stop is implemented but **off by default**: it must first be shown to
help out of sample (run `qts validate` with it on and compare) before being enabled.

## Execution assumptions

- signal on the close of day *t* → market order at the open of *t+1*;
- every fill pays `fee_rate` (0.10%) and adverse `slippage_bps` (5 bps);
- a stop is filled at the stop price, or at the open if the market gaps through it
  (never at a better price than was actually available);
- one position per symbol, no pyramiding, no shorting, no leverage.

## Known limitations

- Daily resolution: intraday order of high/low is unknown; a position opened at
  the open can be stopped the same day, which is the conservative assumption.
- Liquidity and market impact are not modelled (irrelevant at personal size on
  BTC/ETH, not negligible on small caps).
- Two assets give few trades (~30 in 9 years): trade statistics are noisy.
