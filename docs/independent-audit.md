# Independent technical audit

- **Date:** 2026-11-11
- **Audited commit:** `1808625` (default branch `claude/quant-trading-system-mvp-vpltm6`)
- **Fixes and this report:** branch `claude/ecstatic-euler-c1pu79`
- **Method:** fresh clone from GitHub, new Python environments, images built from
  scratch, real market data downloaded again. Nothing from the development session
  was reused. Every statement below marked *verified* was observed by running it.

## Verdict

The project is real, installs from a clean clone and does what the documentation
says: 171 tests passed on the audited commit, the backtest reproduced the published
numbers to the cent, the Docker stack came up, and no code path can place a real
order. The financial logic held up under independent recomputation.

The audit nevertheless found **3 HIGH** and **7 MEDIUM** issues that the existing
test suite did not catch. The most serious: the kill switch did not stop a service
that was already running, and a database failure during a paper-trading step
duplicated orders in the record. All HIGH defects and five MEDIUM ones are fixed
on this branch; every code fix has a regression test that fails on the old code. No
CRITICAL defect was found: nothing could move real money, leak a secret or
silently corrupt a backtest result.

**The strategy itself is not validated as profitable**, and this audit did not try
to make it look better. See [Financial verification](#5-financial-verification).

| Severity | Found | Fixed here | Open |
|---|---:|---:|---:|
| CRITICAL | 0 | — | 0 |
| HIGH | 3 | 3 | 0 |
| MEDIUM | 7 | 5 | 2 |
| LOW | 11 | 1 | 10 |

## Limits of this audit environment

Three things behaved differently in the audit sandbox than they will on a normal
machine. They are environment constraints, not project defects, but they bound
what "verified" means:

1. **TLS interception.** The sandbox re-signs HTTPS with its own CA. The project's
   Dockerfiles could not reach PyPI until that CA was added to the base images.
   This was done with a build-time context override, **without changing the
   Dockerfiles**. The unmodified build is verified by GitHub Actions instead.
2. **`api.binance.com` returns HTTP 451** from the sandbox (geo-block). Freqtrade
   dry-run was verified with the project's own `config.public-data-mirror.json`
   overlay; the default endpoint was not reachable. Websocket streams failed for
   the same reason; Freqtrade fell back to REST.
3. **`ghcr.io` blobs are blocked**, so gitleaks could not run locally. It ran on
   GitHub Actions (passed); locally the full history was scanned with a regex set.

## 1. Repository

| Check | Result |
|---|---|
| History | 15 commits by the development session, linear, descriptive messages. 4 Dependabot branches / PRs open |
| Missing files | none: all 109 tracked files present; every path referenced by README, compose and CI exists |
| Secrets in history | none. Regex scan of all 19 commits on all branches (AWS, GitHub, Slack, OpenAI, JWT, private keys, `password=`-style assignments): only placeholders (`change-me`, `ci-only-password`) and one test fixture. No `.env`, key, database or data file was ever committed. gitleaks passed on GitHub |
| Dependency licences | `pip-licenses` on the 85 installed packages matches `docs/licensing.md`: permissive except psycopg (LGPL-3.0, used unmodified). Freqtrade (GPL-3.0) is confined to the optional extra and one adapter file; a test enforces that `src/qts` never imports it |
| Docker configuration | non-root user, ports bound to `127.0.0.1`, required secrets fail fast when unset (`${VAR:?}`), configs mounted read-only, Grafana sign-up and anonymous access off |

Findings: **M-3** (no lock file), **L-1** (no `main` branch, repository name),
**L-2** (application connects as PostgreSQL superuser).

## 2. Clean installation

| Step | Result |
|---|---|
| `git clone` + Python 3.12 venv + `pip install -e ".[dev,reporting,ai]"` | OK |
| Second venv with `".[dev,freqtrade]"` (freqtrade 2026.10, ccxt 4.5.92) | OK |
| `qts data download` | 3 236 daily candles each for BTC/USDT and ETH/USDT, 2018-01-01 → 2026-11-10, 0 missing, checksums verified, 3 min 12 s |
| `docker compose config` (all profiles) | valid |
| Core image build | OK (see sandbox limit 1); 1.15 GB |
| Freqtrade image build (`freqtradeorg/freqtrade:2026.10`) | OK |
| `docker compose up -d` | postgres healthy, paper-trader running, grafana running |
| PostgreSQL | databases `qts` and `freqtrade`, 10 tables created, role `grafana_ro` can `SELECT` and is denied `INSERT` / `DELETE` / `CREATE TABLE` |
| Grafana 12.2.1 | `/api/health` ok, datasource provisioned with the read-only role and able to query, dashboard *QTS — Trading overview* loaded, anonymous and wrong-password requests → 401 |
| Paper trader in Docker | downloaded data itself, opened a session, heartbeat `ok`, `qts health` all green |
| `scripts/backup.sh` | database dump OK (32 objects); **state archive failed** → fixed (**M-4**) |

## 3. Software tests

Audited commit, clean environment, PostgreSQL 17 available:

| Gate | Result |
|---|---|
| `ruff check .` | passed |
| `ruff format --check .` | 88 files formatted |
| `mypy` (strict) | no issues in 52 files |
| `pytest` | **171 passed, 1 skipped** (the Freqtrade import test, run separately) |
| Freqtrade adapter tests, real Freqtrade installed | 12 passed |
| Coverage | **93.15 %** (gate 85 %); risk engine 97 %, paper trader 96 %, storage 96 % |

After the fixes: **203 passed, 1 skipped**, coverage 93.18 %, same gates green;
17 Freqtrade adapter tests passed with Freqtrade installed.

### Are the tests trustworthy?

Mostly yes. There is **no mocking of the system under test**: the only test
double is an HTTP transport for the download client. Persistence tests run on
real SQLite *and* real PostgreSQL; the paper-trading test compares a session
stepped through simulated restarts against a full backtest; risk tests build real
engines, brokers and ledgers.

Three weaknesses were found:

- **A false positive in the kill-switch tests.** Every test that activated the
  switch "as an operator" passed an explicit activation time in the past
  (`at=NOW - 1h`). The real command (`qts risk kill`) uses the current time, and
  with the current time the entry was *not* blocked (**H-1**). The tests passed
  because they did not exercise the path the operator uses.
- **No test with two processes.** All kill-switch tests used one object, so the
  missing reload (**H-1**) was invisible.
- **No failure injection.** Nothing interrupted a multi-step write, so the
  duplicate-orders defect (**H-3**) was invisible.

Performance tests use seeded synthetic data, which is correct for exercising code
paths; no test asserts a level of profit.

## 4. Risk engine audit

56 hostile probes were run through the public API (script kept outside the
repository; the cases that matter are now regression tests).

| Attempt | Audited commit | After fixes |
|---|---|---|
| Order of 2× the capital | blocked (position, exposure, cash) | blocked |
| Order above free cash; cash covers notional but not costs | blocked | blocked |
| NaN / infinite / zero / negative quantity, price, cash | blocked | blocked |
| Exceed the daily loss limit, in one day or cumulatively intraday | blocked; halt survives a restart | blocked |
| Exceed the maximum drawdown slowly (never tripping the daily limit) | blocked; kill switch latches and stays latched after equity recovers | blocked |
| Reuse a consumed approval (same object, a copy, after snapshot/restore) | blocked | blocked |
| Alter an order after approval (`dataclasses.replace`, or in-place mutation of the frozen object) for quantity, symbol, side, stop, price, reason | blocked (signature) | blocked |
| Send an order with no approval; a forged approval; one signed by another engine | blocked | blocked |
| Buy without a stop, stop above entry, stale data, data from the future, price 40 % off the last close, short sale, buy disguised as a stop-loss | blocked | blocked |
| Any order in `LIVE` mode | blocked | blocked |
| Three consecutive operational errors | kill switch trips | kill switch trips |
| Corrupt kill-switch file | fails closed on load… | fails closed |
| **Execute an approved entry at another price** (approved 30 % of equity at 100, filled at 300 → 90 % position) | **NOT blocked** | blocked |
| **Use an approval 30 days later, after the kill switch tripped** | **NOT blocked** | blocked |
| **Two entries approved on the same account snapshot, both executed** (60 % position, limit 35 %) | **NOT blocked** | blocked |
| **`qts risk kill` from another process while the service runs** | **NOT blocked** | blocked |
| **Operator kill, next candle stamped earlier the same day** | **NOT blocked** | blocked |
| Open position with no price in the context (hidden exposure) | blocked only by coincidence | blocked explicitly |

Service interruption:

| Scenario | Audited commit | After fixes |
|---|---|---|
| Database unreachable during a step | clean `PersistenceError`, no credentials in the message, resumes when it returns | same |
| Stale market data for three cycles | `DataError` each time, kill switch latched on disk | same |
| **Database fails between writing fills and saving the resume point** | **orders duplicated on retry (21 fills → 32)** | nothing kept; retry books each order once |
| Full `docker compose down` / `up` | session, equity and fills intact; repeated step adds nothing | same |

## 5. Financial verification

All checks were recomputed with separate code on the freshly downloaded data.

| Check | Result |
|---|---|
| Look-ahead bias — signals | identical when the future is cut off at 80 random points (max difference 0.0) |
| Look-ahead bias — equity | identical up to bar 2000 when later data is deleted, and when later prices are multiplied by 3 |
| Order timing | all 58 fills: signal on the close of *t*, fill at the open of *t+1* |
| Slippage | always adverse: buys at open × 1.0005, sells at open × 0.9995, stops at min(open, stop) × 0.9995 (gaps fill at the open) |
| Fees | 0.1 % of notional on every fill; fees in fills = fees in trades = 90.15 |
| Cash ledger | rebuilt from fills alone: 18 101.680166, equal to the engine |
| Leverage | exposure never above equity, cash never negative |
| Costs are real | the same run with zero costs ends at 18 253.94 instead of 18 101.68 |
| Metrics | total return, CAGR, volatility, Sharpe, Sortino, max drawdown, win rate, profit factor recomputed independently: equal to 9 significant digits |
| Buy & Hold | recomputed independently: equal (40 846.18) |
| Missing candles | not forward-filled; no fill is invented on a missing candle; a 5-day hole in all symbols is skipped. A pending entry whose bar is missing is filled at the next available open if that is within 48 h (1-day hole: filled one day late) and rejected as stale data otherwise (4-day hole: `data_freshness: market data is 96.0h old`) |
| Backtest vs paper | paper session on real data, stepped through 26 simulated restarts, on SQLite and PostgreSQL: equity equal to the backtest to 12 digits; same fills, trades, decisions. Same figure (10 363.52) from the Docker replay |
| Cross-check with Freqtrade's own backtester | 29 trades, 21 signal exits, 8 stops, 41.4 % wins, **+79.78 %** — against 29 / 21 / 8 / 41.4 % / **+81.02 %** from the internal engine (the two differ in fill and slippage conventions) |

### Reproduction of the published results

Config fingerprint `0651aa412967b3bb`, data through 2026-11-10 (one day more than
the published report):

| Period | Strategy | Buy & Hold | Published |
|---|---:|---:|---|
| Full, 2018-01-01 → 2026-11-10 | +81.02 % | +308.46 % | +81.02 % / +319.77 % |
| In sample | +86.66 % | +424.24 % | +88.20 % / +438.75 % |
| **Out of sample**, 2024-03-15 → 2026-11-10 | **+2.62 %** | **−21.98 %** | +2.62 % / −21.89 % |
| Walk-forward (14 windows) | +70.89 % | +1 205.68 % | +70.89 % / +1 248.30 % |

The strategy columns match exactly where the period is the same. The differences
come from the extra day of data, which also moves the in/out-of-sample boundary by
one day (**L-4**).

### What the out-of-sample result does and does not show

- The out-of-sample return is **+2.62 % in 2.7 years** (CAGR 0.98 %, Sharpe 0.29)
  from **9 trades**. That is statistically indistinguishable from zero.
- It "beats" Buy & Hold only because Buy & Hold lost 22 % in that window and the
  strategy was in cash 70 % of the time. Avoiding a fall is a real property of a
  trend filter, but nine trades cannot establish it.
- Over the full period and over the walk-forward track the strategy made **far
  less money** than holding (+81 % vs +308 %; +71 % vs +1 206 %), with a much
  smaller drawdown (22 % vs 88 %).
- The default parameters were not picked to flatter the out-of-sample window: of
  the six combinations in the declared grid, the default ranks second out of
  sample and fifth in sample.
- The universe (BTC, ETH) was chosen knowing both survived: survivorship bias, as
  the report itself warns.

Conclusion: the machinery is correct; the evidence that the strategy has an edge
is weak. Nothing here supports risking money on it.

## 6. Freqtrade and paper trading

| Check | Result |
|---|---|
| Really integrated | yes: the adapter strategy is loaded by Freqtrade 2026.10 from `user_data/strategies`; signals, sizing and the entry veto come from the platform package (`bridge_ready mode=paper` in the bot log) |
| Dry-run works | bot reaches `RUNNING`, "Dry run is enabled. All trades are simulated", simulated wallet 10 000 USDT; API reports `dry_run: true`, `runmode: dry_run` |
| API protection | no credentials → 401; default password → 401 |
| Simulated orders recorded | entries appear in the `freqtrade` PostgreSQL database (`trades`, `orders` with `dry_run_buy_…` ids). Stake came from the platform sizer (a 9 000 USDT request was cut to 695 USDT = 1 % risk) and the stop from the ATR rule |
| Restart | open trade still present after restarting the bot container and after restarting the Docker daemon |
| Kill switch shared with the platform | **audited commit: a bot already running opened a new trade after `risk kill`**; blocked only after a restart. After the fix: blocked immediately |
| Real trading cannot be enabled | audited commit: with `dry_run: false` the bot would start and the risk engine would veto every entry. After the fix it does not start at all: `LiveTradingDisabledError`, exit code 1, even with both `QTS_*` opt-in variables set. The platform CLI refuses `mode: live` in every command; `LiveBroker` cannot be instantiated; no code path contains an exchange order call |

Entries were triggered with Freqtrade's force-entry API through a temporary
overlay, because a daily strategy produced no signal during the audit. The
overlay was not committed; `force_entry_enable` stays `false` in the repository.

## 7. GitHub Actions

Verified on GitHub, not locally.

- Run #1 on the audited commit: all 6 jobs green — lint/format/types, tests on
  Python 3.11 and 3.12 with a PostgreSQL service, Freqtrade adapter (12 passed,
  not skipped), Docker build, gitleaks on full history.
- The Docker job only built the core image and validated the compose file. It
  never built the Freqtrade image and never started anything (**M-6**, fixed: both
  images are built and `scripts/smoke_test.sh` runs against the live stack).
- Run #6 on this branch (commit `c952dd5`, all code fixes included): **all 6 jobs
  green** — lint/format/types with the pinned versions; tests on Python 3.12
  (pinned) and 3.11 (unpinned) against PostgreSQL; Freqtrade adapter; both Docker
  images built *unmodified* on GitHub's runner; the stack smoke test (schema,
  read-only role, kill switch round trip, `mode: live` refused, Grafana
  dashboard and datasource); gitleaks on the full history.
  <https://github.com/fabio-passarani/umios/actions/runs/43029388029>

## Findings

### HIGH

**H-1 — The kill switch did not stop a running service.** *Fixed in `30283f6`.*
Two independent causes. (a) The state file was read once at start-up, so
`qts risk kill`, which runs in its own process, was invisible to a paper trader
or Freqtrade bot that was already running. (b) The switch was considered active
only for decisions stamped after its activation time; candles are stamped with
their open time, so the first candle processed after an operator kill was still
allowed to enter. A corrupt state file was affected by (b) as well. Reproduced
with the library, with the paper trader (11 entries after the kill) and live in
Docker (Freqtrade opened trade #3 after the kill).
Fix: the file is re-read whenever it changes; only automatic trips by the engine
keep simulated-time semantics, every other activation blocks unconditionally.

**H-2 — An approval was not bound to what had been approved.** *Fixed in `8b296f3`.*
The signature proved the order was unchanged, but the broker accepted any
execution price, at any later time, and the engine issued any number of approvals
against one account snapshot. Each allowed a position far above the configured
limits (90 % and 60 % against 35 %). The normal path (router: evaluate, then
execute at once at the same price) was not affected, so backtests and paper
sessions were never wrong — but the guarantee "cannot be bypassed" did not hold
for anything else that held an approval.
Fix: `consume()` checks age (1 minute), entry price, and the kill switch again;
a new entry approval revokes unused older ones.

**H-3 — A failure while saving a paper step duplicated orders.** *Fixed in `76c71ee`.*
A step was written in eight separate transactions with the resume point last. A
database error in between left fills stored and the state not advanced; the retry
replayed the same candles and stored every order again (21 fills became 32), and
the trade statistics shown in Grafana were computed on duplicates. The equity
curve itself stayed correct.
Fix: one transaction per step.

### MEDIUM

**M-1 — Freqtrade could be started for real trading.** *Fixed in `66a60bd`.*
Entries were vetoed, but the bot was up. It now refuses to start.

**M-2 — Restarting the Freqtrade bot reset its circuit breakers.** *Fixed in `66a60bd`.*
Equity peak, daily halt and order count were in memory only. Now persisted next
to the kill switch; an unreadable file trips the kill switch.

**M-3 — Installs were not reproducible.** *Fixed in `86c6917`.*
Lower bounds only, no lock file. `requirements-lock.txt` pins the audited set;
the Docker image and the Python 3.12 CI jobs use it.

**M-4 — `scripts/backup.sh` failed on the state archive.** *Fixed in `8c75b2e`.*
The unprivileged container user could not write into `./backups`.

**M-5 — Freqtrade exits and stops bypass the risk engine; its trades are not in
Grafana.** *Open, documented.* Only entries are gated. Acceptable for dry-run;
must be addressed before any other use.

**M-6 — CI did not build the Freqtrade image or start the stack.** *Fixed in `c952dd5`.*

**M-7 — The evidence for the strategy is weak.** *Open; not a software defect.*
29 trades in nine years, 9 out of sample. See section 5.

### LOW

- **L-1** No `main` branch: the default branch is a session branch, and the
  repository is named `umios` ("Universal Machine Interface Operating System")
  while the project is `quant-trading-system`. Confusing for a fresh clone.
- **L-2** The application connects to PostgreSQL as the superuser created by the
  image. A dedicated non-superuser role would limit the blast radius.
- **L-3** `RiskLimits.allowed_symbols` defaults to `None` (any symbol). Both
  shipped configs set a whitelist; a hand-written config that omits it would not.
- **L-4** Published results are not pinned: `data.end: null` means the period,
  and with it the in/out-of-sample boundary, moves every day. Set `data.end` when
  publishing a result.
- **L-5** `grafana_ro` can connect to the `freqtrade` and `postgres` databases
  (it can read nothing there).
- **L-6** Freqtrade API credentials default to empty strings in compose if the
  variables are unset; the port is bound to localhost.
- **L-7** A password containing `@`, `/` or `:` breaks the database URL built in
  compose. The documented `openssl rand -hex` avoids it.
- **L-8** CAGR annualises by bar count / 365 rather than calendar time
  (6.92 % vs 6.93 %); Sharpe uses a zero risk-free rate. Both are stated in the code.
- **L-9** Stops are assumed to fill at the stop price plus 5 bps. Crypto wicks can
  be worse; the gap case (open below the stop) is handled conservatively.
- **L-10** Data freshness inside a trading session is judged on the newest candle
  of *any* symbol, not per symbol (the paper trader does check per symbol before
  each step).
- **L-11** The risk engine rejected an order with an unpriced open position only
  by coincidence. *Fixed in `8b296f3`.*
- CI prints Node 20 deprecation warnings for `actions/*`. Dependabot PRs #2–#4
  address it. PR #1 (Python 3.12 → 3.15 base image) should not be merged without
  checking that every dependency ships wheels for 3.15.

## Not verified

- A Freqtrade entry produced by a **strategy signal** in dry-run (none occurred;
  forced entries were used, which follow the same sizing and veto path).
- The default Binance endpoint and websocket streams (blocked from the sandbox).
- The unmodified Docker build on a developer machine (verified on GitHub only).
- Multi-day unattended operation of the paper trader (one live cycle observed,
  1 045 days replayed).
- Restoring a backup into an empty stack.
- The AI research modules beyond their unit tests; they take no part in trading.
- Behaviour under concurrent paper traders on one database (the design assumes one).

## Priorities

1. Merge this branch; the HIGH fixes change no result and no interface used by the CLI.
2. Give the application its own non-superuser database role (L-2).
3. Gate Freqtrade exits through the platform or record them centrally (M-5).
4. Pin `data.end` in any configuration whose results are published (L-4).
5. Create `main`, make it the default branch, and decide the licence (L-1).
6. Before trusting the strategy: several months of paper trading and a wider,
   survivorship-free universe (M-7). No parameter tuning on the current data.

## Fix commits

| Commit | Change |
|---|---|
| `30283f6` | kill switch re-reads its file; operator activation is unconditional |
| `8b296f3` | approvals bound to price, time and account; revoked by newer ones |
| `76c71ee` | atomic persistence of a paper-trading step |
| `66a60bd` | Freqtrade refuses real trading; risk state survives restarts |
| `8c75b2e` | backup script completes |
| `86c6917` | pinned dependency versions |
| `c952dd5` | CI builds both images and smoke-tests the stack |

Backtest results are bit-identical before and after the fixes (final equity
18 101.680166, 58 fills, 29 trades).
