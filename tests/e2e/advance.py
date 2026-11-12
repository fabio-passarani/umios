"""Deliver candles of the synthetic market to the stack and step the paper trader.

Runs *inside* the platform container. Every action goes through the real CLI
commands (`qts data import-csv`, `qts paper step`), each invoked from scratch:
new settings, new database connections, state reloaded from PostgreSQL — the
same as a process restart between two candles.

    advance.py --days 5                 deliver the next 5 candles, one step each
    advance.py --until position         until a position is open (or --max days)
    advance.py --until flat             until no position is open
    advance.py --days 1 --skip ETH/USDT the next candle of that symbol never arrives
    advance.py --refeed                 deliver everything again, step twice
    advance.py --stale 3                no new candle for 3 days: the step must fail
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo-style imports in the container

from qts.cli import app
from qts.core.config import Settings, load_config
from qts.integrations.db.storage import Storage
from tests.e2e.fixture import SYMBOLS, WARMUP, build

# Which candles have been delivered. Kept outside the stack's volumes: it is the
# test's own memory of "the exchange", and must survive the stack being destroyed.
CURSOR = Path("/e2e-work/cursor.json")
runner = CliRunner()


def cli(*args: str, expect: int = 0) -> str:
    result = runner.invoke(app, list(args))
    if result.exit_code != expect:
        raise SystemExit(
            f"`qts {' '.join(args)}` exited {result.exit_code}, expected {expect}:\n{result.output}"
        )
    return result.output


def load_cursor() -> dict[str, object]:
    if CURSOR.is_file():
        return json.loads(CURSOR.read_text())  # type: ignore[no-any-return]
    return {"delivered": 0, "skipped": {s: [] for s in SYMBOLS}}


def deliver(market: dict, cursor: dict, upto: int) -> None:  # type: ignore[type-arg]
    """Import the candles [0, upto) of every symbol, minus the ones that 'never arrived'."""
    with tempfile.TemporaryDirectory() as tmp:
        for symbol in SYMBOLS:
            frame = market[symbol].iloc[:upto]
            frame = frame.drop(frame.index[[i for i in cursor["skipped"][symbol] if i < upto]])
            path = Path(tmp) / "feed.csv"
            frame.reset_index().to_csv(path, index=False)
            cli("data", "import-csv", "--symbol", symbol, "--file", str(path))
    cursor["delivered"] = upto
    CURSOR.write_text(json.dumps(cursor))


def as_of(market: dict, delivered: int, extra_days: int = 0) -> str:  # type: ignore[type-arg]
    last = market[SYMBOLS[0]].index[delivered - 1]
    return (last + timedelta(days=1 + extra_days, hours=1)).isoformat()


def status() -> dict[str, object]:
    storage = Storage(Settings().resolved_database_url())
    run = storage.active_paper_run()
    if run is None:
        return {"run": None}
    fills = storage.fills(run.id)
    return {
        "run": run.id,
        "bars": len(storage.equity_curve(run.id)),
        "positions": {
            p.symbol: {"quantity": p.quantity, "stop": p.stop_price}
            for p in storage.positions(run.id)
        },
        "fills": len(fills),
        "buys": sum(1 for f in fills if f.side == "buy"),
        "sell_reasons": sorted({f.reason for f in fills if f.side == "sell"}),
        "rejected": sum(1 for d in storage.risk_decisions(run.id) if not d.approved),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true", help="deliver the history, open the session")
    parser.add_argument("--days", type=int, default=0)
    parser.add_argument("--until", choices=["position", "flat"])
    parser.add_argument("--max", type=int, default=120)
    parser.add_argument("--skip")
    parser.add_argument("--refeed", action="store_true")
    parser.add_argument("--stale", type=int, default=0)
    args = parser.parse_args()
    load_config(Settings().config_file)  # fail early on a broken configuration
    market, cursor = build(), load_cursor()
    total = len(market[SYMBOLS[0]])

    if args.init:
        deliver(market, cursor, WARMUP)
        cli("paper", "start")
    elif args.refeed:
        for _ in range(2):
            deliver(market, cursor, int(cursor["delivered"]))  # type: ignore[call-overload]
            cli("paper", "step", "--no-refresh", "--as-of", as_of(market, int(cursor["delivered"])))  # type: ignore[call-overload]
    elif args.stale:
        late = as_of(market, int(cursor["delivered"]), args.stale)  # type: ignore[call-overload]
        output = cli("paper", "step", "--no-refresh", "--as-of", late, expect=1)
        if "stale" not in output:
            raise SystemExit(f"expected a stale-data error, got:\n{output}")
    else:
        if args.skip:
            cursor["skipped"][args.skip].append(int(cursor["delivered"]))  # type: ignore[index,call-overload]
        steps = args.days if not args.until else args.max
        for _ in range(steps):
            upto = int(cursor["delivered"]) + 1  # type: ignore[call-overload]
            if upto > total:
                break
            deliver(market, cursor, upto)
            cli("paper", "step", "--no-refresh", "--as-of", as_of(market, upto))
            if args.until:
                open_positions = len(status()["positions"])  # type: ignore[arg-type]
                if (args.until == "position") == bool(open_positions):
                    break
        else:
            if args.until:
                raise SystemExit(f"never reached '{args.until}' within {args.max} days")
    print(json.dumps({"day": cursor["delivered"], "of": total, **status()}))


if __name__ == "__main__":
    main()
