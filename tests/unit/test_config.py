from pathlib import Path

import pytest

from qts.core.config import (
    LIVE_CONFIRMATION_PHRASE,
    AppConfig,
    Settings,
    enforce_mode,
    load_config,
)
from qts.core.errors import ConfigError, LiveTradingDisabledError
from qts.core.types import TradingMode

ROOT = Path(__file__).resolve().parents[2]


def test_default_config_file_is_valid() -> None:
    config = load_config(ROOT / "configs" / "default.yaml")
    assert config.mode is TradingMode.BACKTEST
    assert config.market.symbols == ("BTC/USDT", "ETH/USDT")
    assert config.risk.max_total_exposure_pct <= 1.0  # spot: never leveraged


def test_paper_config_file_is_valid() -> None:
    assert load_config(ROOT / "configs" / "paper.yaml").mode is TradingMode.PAPER


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    file = tmp_path / "c.yaml"
    file.write_text("risk:\n  max_drawdown_pctt: 0.9\n")
    with pytest.raises(ConfigError, match="max_drawdown_pctt"):
        load_config(file)


def test_missing_file_and_bad_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [1, 2")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(bad)


def test_sizing_cannot_exceed_risk_limits() -> None:
    with pytest.raises(ValueError, match="risk_per_trade_pct"):
        AppConfig.model_validate({"portfolio": {"sizing": {"risk_per_trade_pct": 0.04}}})


def test_leverage_is_not_configurable() -> None:
    with pytest.raises(ValueError, match="max_total_exposure_pct"):
        AppConfig.model_validate({"risk": {"max_total_exposure_pct": 1.5}})


@pytest.mark.parametrize("symbol", ["BTCUSDT", "BTC/EUR", "/USDT"])
def test_malformed_symbols_rejected(symbol: str) -> None:
    with pytest.raises(ValueError, match="symbol"):
        AppConfig.model_validate({"market": {"symbols": [symbol]}})


def test_fingerprint_is_stable_and_sensitive() -> None:
    a, b = AppConfig(), AppConfig()
    c = AppConfig.model_validate({"costs": {"fee_rate": 0.002}})
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()


def test_backtest_and_paper_modes_start() -> None:
    settings = Settings(_env_file=None)
    assert enforce_mode(AppConfig(), settings) is TradingMode.BACKTEST
    paper = AppConfig.model_validate({"mode": "paper"})
    assert enforce_mode(paper, settings) is TradingMode.PAPER


def test_live_is_disabled_by_default() -> None:
    assert Settings(_env_file=None).enable_live_trading is False
    live = AppConfig.model_validate({"mode": "live"})
    with pytest.raises(LiveTradingDisabledError, match="QTS_ENABLE_LIVE_TRADING"):
        enforce_mode(live, Settings(_env_file=None))


def test_live_is_refused_even_with_full_opt_in() -> None:
    live = AppConfig.model_validate({"mode": "live"})
    half = Settings(_env_file=None, enable_live_trading=True)
    with pytest.raises(LiveTradingDisabledError, match="confirmation"):
        enforce_mode(live, half)
    full = Settings(
        _env_file=None,
        enable_live_trading=True,
        live_trading_confirmation=LIVE_CONFIRMATION_PHRASE,
    )
    with pytest.raises(LiveTradingDisabledError, match="not implemented"):
        enforce_mode(live, full)


def test_secrets_are_masked_in_repr() -> None:
    settings = Settings(_env_file=None, exchange_api_key="super-secret-key")  # type: ignore[arg-type]
    assert "super-secret-key" not in repr(settings)
    assert "super-secret-key" not in str(settings.model_dump())
