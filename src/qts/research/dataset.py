"""A research dataset with a sealed final test period.

The single most valuable thing in a research process is data nobody has looked
at. This module makes that property mechanical instead of a matter of
discipline:

* a dataset is a *specification* (universe rules, period, split date) plus a
  **manifest** of content hashes, one per instrument and per segment. The
  manifest is committed, so any change to the data — in the development part or
  in the test part — is detected by :meth:`SealedDataset.verify`;
* research code gets its candles from :meth:`SealedDataset.development`, which
  never returns a candle after the split;
* the test period is reachable only through :meth:`SealedDataset.unseal`, which
  demands a reason and appends it to a ledger file kept under version control.
  Every opening is therefore visible in the repository's history, and its count
  is part of every final result.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from qts.core.errors import DataError, QtsError
from qts.core.provenance import code_version, data_version
from qts.market_data.store import ParquetStore
from qts.market_data.universe import UniverseRules


class SealedDataError(QtsError):
    """The sealed test period was touched without an explicit, recorded opening."""


@dataclass(frozen=True)
class DatasetSpec:
    """What a dataset is. Changing any field makes it a different dataset."""

    name: str
    exchange: str = "binance"
    quote: str = "USDT"
    timeframe: str = "1d"
    start: str = "2017-08-01"
    #: Last day research may look at. Everything after it is the sealed test.
    development_end: str = "2023-12-31"
    test_end: str = "2026-11-09"
    rules: UniverseRules = field(default_factory=UniverseRules)

    def __post_init__(self) -> None:
        if not (self.start < self.development_end < self.test_end):
            raise DataError("dataset needs start < development_end < test_end")

    @classmethod
    def from_file(cls, path: Path | str) -> DatasetSpec:
        """Load a dataset specification from YAML (unknown keys are an error)."""
        file = Path(path)
        if not file.is_file():
            raise DataError(f"dataset specification not found: {file}")
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        try:
            rules = UniverseRules(**raw.pop("rules", {}))
            return cls(rules=rules, **raw)
        except TypeError as exc:
            raise DataError(f"invalid dataset specification {file}: {exc}") from exc

    @property
    def split(self) -> pd.Timestamp:
        """First instant of the sealed test period."""
        return pd.Timestamp(self.development_end, tz="UTC") + pd.Timedelta(days=1)

    @property
    def end(self) -> pd.Timestamp:
        return pd.Timestamp(self.test_end, tz="UTC") + pd.Timedelta(days=1)


class SealedDataset:
    def __init__(self, spec: DatasetSpec, store: ParquetStore, directory: Path) -> None:
        self.spec = spec
        self.store = store
        self.directory = directory
        self._opened = False

    # ----------------------------------------------------------------- files
    @property
    def manifest_path(self) -> Path:
        return self.directory / f"{self.spec.name}.manifest.json"

    @property
    def ledger_path(self) -> Path:
        return self.directory / f"{self.spec.name}.unseal.jsonl"

    # ------------------------------------------------------------------ data
    def symbols(self) -> list[str]:
        root = self.store.root / "ohlcv" / self.spec.exchange.lower()
        suffix = f"_{self.spec.quote.upper()}"
        if not root.is_dir():
            raise DataError(f"no stored data under {root}: download the universe first")
        return sorted(
            f"{p.name[: -len(suffix)]}/{self.spec.quote.upper()}"
            for p in root.iterdir()
            if p.name.endswith(suffix) and (p / f"{self.spec.timeframe}.parquet").is_file()
        )

    def _read(self, symbol: str) -> pd.DataFrame:
        frame = self.store.read(self.spec.exchange, symbol, self.spec.timeframe, self.spec.start)
        return frame.loc[frame.index < self.spec.end]

    def development(self, symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
        """Candles research may use: nothing on or after the split, ever."""
        out: dict[str, pd.DataFrame] = {}
        for symbol in symbols or self.symbols():
            frame = self._read(symbol)
            frame = frame.loc[frame.index < self.spec.split]
            if not frame.empty:
                out[symbol] = frame
        return out

    def unseal(self, reason: str, experiment: str) -> None:
        """Open the test period for this object, on the record.

        ``reason`` says why (it must be the final evaluation of a strategy that
        was frozen beforehand) and ``experiment`` names what is evaluated. The
        entry is appended to the ledger before any test candle is returned.
        """
        if len(reason.strip()) < 20:
            raise SealedDataError("opening the test period needs a real justification")
        if not experiment.strip():
            raise SealedDataError("opening the test period needs the experiment it is for")
        self.directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "dataset": self.spec.name,
            "experiment": experiment,
            "reason": reason.strip(),
            "git_commit": code_version()["git_commit"],
            "opening_number": self.openings() + 1,
        }
        with self.ledger_path.open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps(entry, sort_keys=True) + "\n")
        self._opened = True

    def openings(self) -> int:
        """How many times the test period has been opened so far."""
        if not self.ledger_path.is_file():
            return 0
        return sum(
            1 for line in self.ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()
        )

    def full(self, symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
        """Development *and* test candles. Refused unless :meth:`unseal` was called."""
        if not self._opened:
            raise SealedDataError(
                f"the test period of dataset {self.spec.name!r} (from {self.spec.split:%Y-%m-%d}) "
                "is sealed: call unseal(reason, experiment) for a final evaluation"
            )
        return {s: f for s in (symbols or self.symbols()) if not (f := self._read(s)).empty}

    # -------------------------------------------------------------- manifest
    def build_manifest(self) -> dict[str, Any]:
        """Hash every instrument's development and test segment.

        Computing a hash reads the bytes but reveals nothing about performance;
        it is the one sanctioned way to touch the test period without opening it.
        """
        symbols: dict[str, Any] = {}
        development: dict[str, pd.DataFrame] = {}
        test: dict[str, pd.DataFrame] = {}
        for symbol in self.symbols():
            frame = self._read(symbol)
            dev, held = (
                frame.loc[frame.index < self.spec.split],
                frame.loc[frame.index >= self.spec.split],
            )
            if not dev.empty:
                development[symbol] = dev
            if not held.empty:
                test[symbol] = held
            symbols[symbol] = {
                "development": _segment(dev),
                "test": _segment(held),
            }
        spec = asdict(self.spec)
        return {
            "spec": spec,
            "development_id": data_version(development)["id"] if development else None,
            "test_id": data_version(test)["id"] if test else None,
            "symbols": symbols,
        }

    def write_manifest(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(self.build_manifest(), indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
        return self.manifest_path

    def verify(self) -> dict[str, str]:
        """Compare the stored data with the committed manifest.

        Returns the two dataset ids. Raises :class:`SealedDataError` naming what
        differs: the specification, an instrument that appeared or vanished, or
        a segment whose content changed.
        """
        if not self.manifest_path.is_file():
            raise SealedDataError(
                f"dataset {self.spec.name!r} has no manifest: {self.manifest_path}"
            )
        recorded = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        current = json.loads(json.dumps(self.build_manifest(), sort_keys=True))
        if recorded["spec"] != current["spec"]:
            raise SealedDataError("the dataset specification differs from the manifest")
        missing = sorted(set(recorded["symbols"]) - set(current["symbols"]))
        extra = sorted(set(current["symbols"]) - set(recorded["symbols"]))
        changed = sorted(
            f"{symbol} ({segment})"
            for symbol in set(recorded["symbols"]) & set(current["symbols"])
            for segment in ("development", "test")
            if recorded["symbols"][symbol][segment] != current["symbols"][symbol][segment]
        )
        if missing or extra or changed:
            raise SealedDataError(
                "stored data differs from the manifest — "
                f"missing: {missing[:5]}, unexpected: {extra[:5]}, changed: {changed[:5]}"
            )
        return {"development_id": recorded["development_id"], "test_id": recorded["test_id"]}


def _segment(frame: pd.DataFrame) -> dict[str, Any] | None:
    if frame.empty:
        return None
    info = data_version({"x": frame})["symbols"]["x"]
    return {
        "sha256": info["sha256"],
        "rows": info["rows"],
        "first": info["first"],
        "last": info["last"],
    }
