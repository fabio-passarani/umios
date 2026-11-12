# Research data

Where the candles used for research come from, what is done about their defects,
and how the final test period is protected. The generated description of the
current dataset is in
[dataset-binance-usdt-1d-v1.md](dataset-binance-usdt-1d-v1.md).

## Source

Binance's public archive (`data.binance.vision`): one file per symbol, timeframe
and month, each with a SHA-256 checksum that is verified on download. No account
and no API key are involved. Three properties make it suitable for research:

- files are immutable, so a dataset can be identified by a hash of its content;
- **it keeps the history of pairs that were later delisted**, and its index can be
  listed — which is what makes a universe without survivorship bias possible;
- every candle carries traded value, number of trades and taker volume, so
  liquidity can be measured instead of assumed.

The same candles are served by the exchange's live market-data API; the dataset
report compares the two (they were identical on every instrument checked), so a
backtest describes the same market a running system would see.

What the source does **not** contain: bid and ask prices. Spreads are therefore
modelled, never measured (see the execution model).

## Commands

```bash
qts universe list                      # every pair ever listed, and why some are excluded
qts universe download --timeframe 1d   # whole history, delisted pairs included (resumable)
qts dataset build  -d research/datasets/<name>.yaml    # freeze: write the manifest (once)
qts dataset verify -d research/datasets/<name>.yaml    # stored candles == manifest?
qts dataset report -d research/datasets/<name>.yaml    # content, quality, who was tradable when
```

Timeframes: `1d`, `4h`, `1h`. Daily candles define the universe; intraday candles
are downloaded for the instruments that were ever members.

## What counts as an instrument

Everything the archive lists for the quote currency, minus what is not an
independent spot asset — decided by what the instrument *is*, never by how it
performed:

| Excluded | Why |
|---|---|
| Stablecoins and fiat currencies | pegged: no return to capture, and a broken peg is a different risk |
| Leveraged tokens (`…UP`, `…DOWN`, `…BULL`, `…BEAR`) | decaying derivatives of another listed asset |
| Wrapped duplicates | the same asset twice |

Assets that collapsed or were delisted (LUNA, FTT, SRM, …) are **kept**.

## Defects and the rule for each

| Defect found in the real data | Rule |
|---|---|
| Missing candles | never filled in. A pending order waits for the next real candle; if that is too far away the risk engine rejects the entry as stale |
| Long holes: a pair suspended, or a ticker retired and **reused for a different asset** (LUNA after May 2022) | a hole longer than `max_gap_days` restarts the instrument's history: it must qualify again from zero |
| Listing-day spikes and wicks nobody could trade | an instrument is not tradable for its first `min_history_days` |
| Pairs with almost no trading | membership needs a minimum median traded value, and only the most liquid names are kept |
| Delisting | the instrument leaves the universe the day after its last candle; a position still open has to be closed (execution model) |
| Renames (ERD → EGLD, LEND → AAVE) | appear as a delisting followed by a new listing. Treated as such: conservative, a forced exit and a waiting period |
| A pair whose files contain impossible values (one case: negative volume) | rejected by the quality checks at download and left out |

## Who was tradable when

`qts.market_data.universe.membership` returns, for every day, the set of
instruments a strategy may hold. The row of day *D* is computed from candles up
to *D−1*. The rules and the reason for each default are in `UniverseRules`; they
are part of the dataset specification, so changing one defines a new dataset.

Tests prove the two properties that matter: removing or altering the data from a
day onwards does not change any earlier row, and an asset that later died is a
member while it was alive.

## The sealed test period

A dataset has a fixed split. Research uses candles up to `development_end`; the
rest is the final test.

- **Mechanically sealed.** `SealedDataset.development()` never returns a candle
  after the split. `SealedDataset.full()` raises unless `unseal(reason,
  experiment)` was called.
- **Every opening is recorded** in `<name>.unseal.jsonl`, next to the manifest and
  under version control: when, for which experiment, why, at which commit, and
  the running count. The count is printed with every final result.
- **Tamper-evident.** The manifest holds a SHA-256 of each instrument's
  development and test segment. `qts dataset verify` fails on any difference —
  a revised candle, a missing or an extra instrument, a changed rule.
- **Never rebuilt.** `qts dataset build` refuses to overwrite a manifest. New data
  means a new dataset name, so the test period cannot drift.

Computing the hashes reads the test candles' bytes but reveals nothing about
performance. Structure (dates, row counts) of the test period is shown in the
report; nothing else is computed on it.

### An honest limit

The period from 2024 onwards **has been seen before** for BTC and ETH with the
existing trend-following strategy, in the audit and in the validation reports.
For that strategy on those two assets the test is not untouched, and sealing it
now cannot undo that. For other assets and other strategy families nothing has
been looked at — but crypto assets move together, so the independence is partial.
Any final result must be read with this in mind, and the only clean evidence
will be paper trading on data that does not exist yet.
