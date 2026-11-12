"""Same data + same configuration + same code = same result, and the stored
provenance is enough to prove it later."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from qts.backtesting.engine import provenance_of, run_backtest
from qts.backtesting.validation import split_point
from qts.cli import app
from qts.core.config import AppConfig, load_config
from qts.core.errors import DataError
from qts.core.provenance import code_version, data_version, result_hash
from qts.market_data.store import ParquetStore
from tests.conftest import random_walk

ROOT = Path(__file__).resolve().parents[2]
SYMBOL = "TEST/USDT"


def test_two_runs_are_bit_identical(config: AppConfig, walk_data: dict[str, pd.DataFrame]) -> None:
    first, second = run_backtest(config, walk_data), run_backtest(config, walk_data)
    assert first.equity.equals(second.equity)
    assert [f.price for f in first.fills] == [f.price for f in second.fills]
    assert (
        provenance_of(first, walk_data)["result_hash"]
        == provenance_of(second, walk_data)["result_hash"]
    )


def test_provenance_records_everything_needed_to_rerun(
    config: AppConfig, walk_data: dict[str, pd.DataFrame]
) -> None:
    pinned = AppConfig.model_validate(
        {**config.model_dump(mode="json"), "data": {"start": "2018-01-01", "end": "2022-02-08"}}
    )
    result = run_backtest(pinned, walk_data)
    record = provenance_of(result, walk_data)
    assert record["period"] == {
        "start": result.equity.index[0].isoformat(),
        "end": result.equity.index[-1].isoformat(),
        "bars": len(result.equity),
        "pinned": True,
    }
    assert record["symbols"] == [SYMBOL]
    assert record["timeframe"] == "1d"
    assert record["strategy"]["name"] == "trend_following"
    assert record["strategy"]["version"] == "1.0.0"
    assert record["strategy"]["params"]["breakout_window"] == 55
    assert record["costs"] == {"fee_rate": 0.001, "slippage_bps": 5.0}
    assert record["config_fingerprint"] == pinned.fingerprint()
    assert record["data_version"]["symbols"][SYMBOL]["rows"] == len(walk_data[SYMBOL])
    assert len(record["data_version"]["symbols"][SYMBOL]["sha256"]) == 64
    assert set(record["code"]) >= {"git_commit", "git_dirty", "qts", "python", "numpy", "pandas"}
    assert record["result_hash"] == result_hash(result.equity)

    open_ended = provenance_of(run_backtest(config, walk_data), walk_data)
    assert open_ended["period"]["pinned"] is False
    assert open_ended["reproducible"] is False  # an open period is never reproducible


def test_data_version_depends_on_content_only(walk_data: dict[str, pd.DataFrame]) -> None:
    frame = walk_data[SYMBOL]
    same = data_version({SYMBOL: frame.copy()})
    assert same == data_version(walk_data)
    changed = frame.copy()
    changed.iloc[700, changed.columns.get_loc("close")] *= 1.0000001  # one tick in one candle
    assert data_version({SYMBOL: changed})["id"] != same["id"]
    assert data_version({SYMBOL: frame.iloc[:-1]})["id"] != same["id"]  # one candle fewer
    assert data_version({SYMBOL: frame, "B/USDT": frame})["id"] != same["id"]


def test_code_version_prefers_the_build_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QTS_GIT_COMMIT", "abc123")
    assert code_version()["git_commit"] == "abc123"
    monkeypatch.setenv("QTS_GIT_COMMIT", "unknown")
    assert code_version()["git_commit"]  # falls back to the repository (or "unknown")


def test_pinned_out_of_sample_boundary_does_not_move_when_data_grows() -> None:
    history = random_walk(1500, seed=3).index
    longer = random_walk(1800, seed=3).index
    by_fraction = split_point(history, 0.3), split_point(longer, 0.3)
    assert by_fraction[0] != by_fraction[1]  # the defect: the boundary drifts
    pinned = split_point(history, 0.3, "2021-01-01"), split_point(longer, 0.3, "2021-01-01")
    assert pinned[0] == pinned[1] == pd.Timestamp("2021-01-01", tz="UTC")
    with pytest.raises(DataError, match="empty"):
        split_point(history, 0.3, "2031-01-01")


def test_shipped_research_config_is_pinned_and_paper_config_is_not() -> None:
    research = load_config(ROOT / "configs" / "default.yaml")
    assert research.data.end is not None
    assert research.validation.out_of_sample_start is not None
    assert load_config(ROOT / "configs" / "paper.yaml").data.end is None  # runs forward


def test_fingerprints_of_existing_configurations_are_unchanged() -> None:
    """A paper session started before this version must not be invalidated."""
    assert load_config(ROOT / "configs" / "paper.yaml").fingerprint() == "8d72075ecd7fb7c2"
    base = AppConfig()
    assert base.fingerprint() == AppConfig.model_validate(base.model_dump()).fingerprint()
    pinned = AppConfig.model_validate(
        {"data": {"end": "2024-01-01"}, "validation": {"out_of_sample_start": "2023-01-01"}}
    )
    assert pinned.fingerprint() != base.fingerprint()


@pytest.mark.parametrize(
    "bad",
    [
        {"data": {"start": "2020-01-01", "end": "2019-01-01"}},
        {"validation": {"out_of_sample_start": "2010-01-01"}},
        {"data": {"end": "2024-01-01"}, "validation": {"out_of_sample_start": "2025-01-01"}},
        {"validation": {"out_of_sample_start": "not-a-date"}},
    ],
)
def test_inconsistent_periods_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic wraps the message
        AppConfig.model_validate(bad)


# ----------------------------------------------------------------- end to end, CLI
@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    raw = AppConfig.model_validate(
        {
            "market": {"symbols": [SYMBOL]},
            "risk": {"allowed_symbols": [SYMBOL]},
            "data": {
                "directory": str(tmp_path / "data"),
                "start": "2018-01-01",
                "end": "2021-06-30",
            },
            "validation": {"out_of_sample_start": "2020-06-01"},
        }
    ).model_dump(mode="json")
    import yaml

    (tmp_path / "cfg.yaml").write_text(yaml.safe_dump(raw))
    ParquetStore(tmp_path / "data").write("binance", SYMBOL, "1d", random_walk(1500, seed=7))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QTS_DATABASE_URL", f"sqlite:///{tmp_path / 'state' / 'qts.sqlite'}")
    monkeypatch.setenv("QTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("QTS_LOG_LEVEL", "CRITICAL")
    monkeypatch.setenv("QTS_GIT_COMMIT", "0123456789abcdef")
    return tmp_path


def _run_id(output: str) -> str:
    return output.split("Saved as run ")[1].split()[0]


def test_reproduce_confirms_a_stored_run_and_detects_changed_data(workspace: Path) -> None:
    runner = CliRunner()
    saved = runner.invoke(app, ["backtest", "-c", "cfg.yaml"])
    assert saved.exit_code == 0, saved.output
    assert "2018-01-01 -> 2021-06-30" in saved.output  # the pinned end, not the data's end
    run_id = _run_id(saved.output)

    again = runner.invoke(app, ["reproduce", run_id, "-c", "cfg.yaml"])
    assert again.exit_code == 0, again.output
    assert "REPRODUCED" in again.output

    # more candles arrive: a pinned run is not affected by them
    ParquetStore(workspace / "data").write("binance", SYMBOL, "1d", random_walk(1600, seed=7))
    assert runner.invoke(app, ["reproduce", run_id, "-c", "cfg.yaml"]).exit_code == 0

    # one historical candle is revised: same period, different data -> not reproduced
    revised = random_walk(1500, seed=7)
    revised.iloc[900, :4] *= 1.01
    ParquetStore(workspace / "data").write("binance", SYMBOL, "1d", revised)
    broken = runner.invoke(app, ["reproduce", run_id, "-c", "cfg.yaml"])
    assert broken.exit_code == 1
    assert "DIFFERENT" in broken.output
    assert "NOT REPRODUCED" in broken.output


def test_reproduce_rejects_unknown_runs(workspace: Path) -> None:
    result = CliRunner().invoke(app, ["reproduce", "nope", "-c", "cfg.yaml"])
    assert result.exit_code == 1
    assert "not found" in result.output
