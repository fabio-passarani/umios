"""Universe without hindsight, data-quality audit, and the sealed test period."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from qts.market_data.quality import audit_symbol, audit_universe, summarise
from qts.market_data.store import ParquetStore
from qts.market_data.universe import UniverseRules, classify, membership
from qts.research.dataset import DatasetSpec, SealedDataError, SealedDataset
from tests.conftest import random_walk


def asset(days: int, start: str, volume_usd: float, seed: int = 1) -> pd.DataFrame:
    frame = random_walk(days, seed=seed)
    frame.index = pd.date_range(start, periods=days, freq="D", tz="UTC", name="timestamp")
    frame["quote_volume"] = volume_usd
    return frame


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize(
    ("symbol", "excluded"),
    [
        ("BTC/USDT", False), ("LUNA/USDT", False), ("FTT/USDT", False), ("JUP/USDT", False),
        ("SYRUP/USDT", False), ("USDC/USDT", True), ("EUR/USDT", True), ("BTCUP/USDT", True),
        ("ETHDOWN/USDT", True), ("XRPBULL/USDT", True), ("BEAR/USDT", True), ("WBTC/USDT", True),
    ],
)  # fmt: skip
def test_exclusions_are_about_what_an_instrument_is(symbol: str, excluded: bool) -> None:
    assert (classify(symbol) is not None) is excluded


# ---------------------------------------------------------------------- membership
RULES = UniverseRules(min_history_days=30, liquidity_window_days=10, min_quote_volume=1e6, top_n=2)


def test_membership_needs_history_liquidity_and_rank() -> None:
    data = {
        "OLD/USDT": asset(200, "2021-01-01", 9e6, 1),
        "BIG/USDT": asset(200, "2021-01-01", 5e7, 2),
        "MID/USDT": asset(200, "2021-01-01", 2e7, 3),
        "THIN/USDT": asset(200, "2021-01-01", 2e5, 4),  # below the liquidity floor
        "NEW/USDT": asset(20, "2021-06-25", 9e8, 5),  # huge volume, listed three weeks ago
    }
    table = membership(data, RULES)
    july = table.loc["2021-07-05"]
    assert set(july[july].index) == {"BIG/USDT", "MID/USDT"}  # top 2 by traded value
    assert not table["THIN/USDT"].any()
    assert not table["NEW/USDT"].any()  # never old enough inside this sample
    assert not table.loc["2021-01-15"].any()  # nobody has 30 days of history yet
    assert table.sum(axis=1).max() <= RULES.top_n


def test_membership_uses_only_the_past() -> None:
    """Changing or removing anything from a day onwards cannot change earlier rows."""
    data = {f"A{i}/USDT": asset(300, "2021-01-01", 1e6 * (i + 2), i) for i in range(5)}
    full = membership(data, RULES)
    cut = pd.Timestamp("2021-06-10", tz="UTC")
    truncated = membership({s: f.loc[f.index < cut] for s, f in data.items()}, RULES)
    pd.testing.assert_frame_equal(full.loc[full.index < cut], truncated.loc[truncated.index < cut])
    tampered = {s: f.copy() for s, f in data.items()}
    for frame in tampered.values():
        frame.loc[frame.index >= cut, "quote_volume"] *= rng_scale(frame)
    changed = membership(tampered, RULES)
    pd.testing.assert_frame_equal(full.loc[full.index <= cut], changed.loc[changed.index <= cut])


def rng_scale(frame: pd.DataFrame) -> float:
    return 1000.0 if frame["quote_volume"].iloc[0] < 4e6 else 0.001


def test_delisted_asset_leaves_the_day_after_its_last_candle() -> None:
    data = {
        "DEAD/USDT": asset(150, "2021-01-01", 9e7, 1),  # last candle 2021-05-30
        "LIVE/USDT": asset(300, "2021-01-01", 5e7, 2),
    }
    table = membership(data, RULES)
    assert table.loc["2021-05-30", "DEAD/USDT"]
    assert table.loc["2021-05-31", "DEAD/USDT"]  # known to have traded on the 30th
    assert not table.loc["2021-06-01":, "DEAD/USDT"].any()
    assert table.loc["2021-09-15", "LIVE/USDT"]


def test_a_reused_ticker_starts_its_history_again() -> None:
    """LUNA in 2022: the old chain's token stops, weeks later the same ticker is a
    different asset. Its 'history' must not carry across the hole."""
    first = asset(120, "2021-01-01", 9e7, 1)
    second = asset(120, "2021-06-01", 9e7, 2)  # 31 days of nothing, then a new asset
    table = membership(
        {"LUNA/USDT": pd.concat([first, second]), "REF/USDT": asset(300, "2021-01-01", 5e7, 3)},
        RULES,
    )
    column = table["LUNA/USDT"]
    assert column.loc["2021-04-15"]
    assert not column.loc["2021-06-01":"2021-06-30"].any()  # no history yet as the new asset
    assert column.loc["2021-07-01"]  # exactly 30 known days on the review day
    assert column.loc["2021-08-15"]  # 30 days later it may qualify again
    short_hole = pd.concat([first, asset(120, "2021-05-03", 9e7, 2)])  # two missing days
    assert membership({"X/USDT": short_hole, "REF/USDT": asset(300, "2021-01-01", 5e7, 3)}, RULES)[
        "X/USDT"
    ].loc["2021-06-15"]


# -------------------------------------------------------------------------- quality
def test_audit_finds_holes_flat_candles_wicks_and_extreme_moves() -> None:
    frame = asset(100, "2021-01-01", 1e6)
    frame = frame.drop(frame.index[[10, 11, 12, 40]])
    frame.iloc[20, frame.columns.get_loc("volume")] = 0.0
    frame.iloc[25, [1, 2]] = frame.iloc[25, 3]  # high = low
    frame.iloc[30, frame.columns.get_loc("high")] = frame["close"].iloc[30] * 9
    frame.iloc[50:, :4] *= 5.0  # a +400% candle
    end = pd.Timestamp("2021-12-31", tz="UTC")
    audit = audit_symbol(frame, "X/USDT", "1d", end)
    assert audit.missing == 4
    assert [g.missing for g in audit.gaps] == [3, 1]
    assert audit.longest_gap == 3
    assert (audit.zero_volume, audit.wicks, audit.extreme_up) == (1, 1, 1)
    assert audit.flat >= 1
    assert audit.delisted is True  # stops months before the dataset ends
    alive = audit_symbol(asset(100, "2021-09-23", 1e6), "Y/USDT", "1d", end)
    assert alive.delisted is False
    assert alive.gaps == []
    totals = summarise(
        audit_universe({"X/USDT": frame, "Y/USDT": asset(100, "2021-09-23", 1e6)}, "1d", end)
    )
    assert (
        totals["symbols"],
        totals["delisted"],
        totals["with_gaps"],
        totals["missing_candles"],
    ) == (2, 1, 1, 4)
    assert totals["listed_by_year"] == {2021: 2}


# --------------------------------------------------------------------------- sealing
@pytest.fixture
def dataset(tmp_path: Path) -> SealedDataset:
    store = ParquetStore(tmp_path / "data")
    for i, symbol in enumerate(("AAA/USDT", "BBB/USDT")):
        store.write("binance", symbol, "1d", asset(400, "2022-06-01", 5e6, i))
    store.write("binance", "GONE/USDT", "1d", asset(100, "2022-06-01", 5e6, 9))  # delisted in 2022
    spec = DatasetSpec(
        name="t1", start="2022-06-01", development_end="2022-12-31", test_end="2023-06-30"
    )
    return SealedDataset(spec, store, tmp_path / "datasets")


def test_development_data_never_contains_the_test_period(dataset: SealedDataset) -> None:
    frames = dataset.development()
    assert set(frames) == {"AAA/USDT", "BBB/USDT", "GONE/USDT"}
    assert all(f.index.max() <= pd.Timestamp("2022-12-31", tz="UTC") for f in frames.values())
    assert frames["AAA/USDT"].index.max() == pd.Timestamp("2022-12-31", tz="UTC")
    with pytest.raises(SealedDataError, match="is sealed"):
        dataset.full()
    assert dataset.openings() == 0


def test_opening_the_test_period_is_explicit_and_recorded(dataset: SealedDataset) -> None:
    with pytest.raises(SealedDataError, match="justification"):
        dataset.unseal("look", "exp-1")
    with pytest.raises(SealedDataError, match="experiment"):
        dataset.unseal("final evaluation of the frozen candidate", " ")
    dataset.unseal("final evaluation of the frozen candidate", "exp-1")
    full = dataset.full()
    assert full["AAA/USDT"].index.max() == pd.Timestamp("2023-06-30", tz="UTC")
    assert "GONE/USDT" in full
    entries = [json.loads(line) for line in dataset.ledger_path.read_text().splitlines()]
    assert [(e["experiment"], e["opening_number"]) for e in entries] == [("exp-1", 1)]

    again = SealedDataset(dataset.spec, dataset.store, dataset.directory)
    with pytest.raises(SealedDataError):  # a new object is sealed again ...
        again.full()
    assert again.openings() == 1  # ... but the opening stays on the record
    again.unseal("second look, which must show in the count", "exp-2")
    assert again.openings() == 2


def test_manifest_detects_any_change_to_either_segment(dataset: SealedDataset) -> None:
    with pytest.raises(SealedDataError, match="no manifest"):
        dataset.verify()
    dataset.write_manifest()
    ids = dataset.verify()
    assert ids["development_id"] != ids["test_id"]
    assert dataset.openings() == 0  # hashing the test period does not open it
    manifest = json.loads(dataset.manifest_path.read_text())
    assert manifest["symbols"]["GONE/USDT"]["test"] is None

    original = dataset.store.read("binance", "AAA/USDT", "1d")
    for row, segment in ((50, "development"), (350, "test")):
        tampered = original.copy()
        tampered.iloc[row, :4] *= 1.0001
        dataset.store.write("binance", "AAA/USDT", "1d", tampered)
        with pytest.raises(SealedDataError, match=rf"AAA/USDT \({segment}\)"):
            dataset.verify()
        dataset.store.write("binance", "AAA/USDT", "1d", original)
    dataset.verify()

    dataset.store.write("binance", "NEW/USDT", "1d", asset(50, "2022-06-01", 1e6, 4))
    with pytest.raises(SealedDataError, match="unexpected"):
        dataset.verify()
    other = SealedDataset(
        DatasetSpec(
            name="t1", start="2022-06-01", development_end="2023-01-31", test_end="2023-06-30"
        ),
        dataset.store,
        dataset.directory,
    )
    with pytest.raises(SealedDataError, match="specification"):
        other.verify()


def test_dataset_spec_is_validated() -> None:
    with pytest.raises(Exception, match="start < development_end < test_end"):
        DatasetSpec(name="bad", start="2024-01-01", development_end="2023-01-01")
    spec = DatasetSpec(name="ok")
    assert spec.split == pd.Timestamp("2024-01-01", tz="UTC")
    assert np.isfinite(spec.rules.min_quote_volume)


def test_dataset_commands_build_once_verify_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml
    from typer.testing import CliRunner

    from qts.cli import app
    from qts.core.config import AppConfig

    store = ParquetStore(tmp_path / "data")
    for i, symbol in enumerate(("AAA/USDT", "BBB/USDT", "CCC/USDT")):
        store.write("binance", symbol, "1d", asset(500, "2022-01-01", 5e6 * (i + 1), i))
    cfg = AppConfig.model_validate({"data": {"directory": str(tmp_path / "data")}})
    (tmp_path / "cfg.yaml").write_text(yaml.safe_dump(cfg.model_dump(mode="json")))
    spec = {
        "name": "cli-test",
        "start": "2022-01-01",
        "development_end": "2022-12-31",
        "test_end": "2023-05-15",
        "rules": {"min_history_days": 30, "top_n": 2},
    }
    (tmp_path / "sets").mkdir()
    (tmp_path / "sets" / "cli-test.yaml").write_text(yaml.safe_dump(spec))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QTS_DATABASE_URL", f"sqlite:///{tmp_path / 'state' / 'qts.sqlite'}")
    monkeypatch.setenv("QTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("QTS_LOG_LEVEL", "CRITICAL")
    runner = CliRunner()
    args = ["-d", "sets/cli-test.yaml", "-c", "cfg.yaml"]

    built = runner.invoke(app, ["dataset", "build", *args])
    assert built.exit_code == 0, built.output
    assert (tmp_path / "sets" / "cli-test.manifest.json").is_file()
    again = runner.invoke(app, ["dataset", "build", *args])
    assert again.exit_code == 1  # a frozen dataset is never rebuilt
    assert "already exists" in again.output

    verified = runner.invoke(app, ["dataset", "verify", *args])
    assert verified.exit_code == 0, verified.output
    assert "opened 0 time(s)" in verified.output

    report = runner.invoke(app, ["dataset", "report", *args, "--output", "out/report.md"])
    assert report.exit_code == 0, report.output
    text = (tmp_path / "out" / "report.md").read_text()
    for part in ("Sealed test", "Quality findings", "Who was tradable when", "structure only"):
        assert part in text

    revised = store.read("binance", "BBB/USDT", "1d")
    revised.iloc[450, 3] *= 1.01  # one test-period close is changed
    store.write("binance", "BBB/USDT", "1d", revised)
    broken = runner.invoke(app, ["dataset", "verify", *args])
    assert broken.exit_code == 1
    assert "BBB/USDT (test)" in broken.output
