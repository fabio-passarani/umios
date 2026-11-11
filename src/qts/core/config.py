"""Typed, validated configuration.

Two sources, on purpose:

* **YAML file** (``configs/*.yaml``, versioned): everything that defines an
  experiment — market, strategy parameters, risk limits, costs, validation setup.
  Versioning it is what makes every result reproducible.
* **Environment / ``.env``** (never versioned): secrets and deployment switches
  (database URL, API keys, the live-trading opt-in).

Unknown keys are rejected (``extra="forbid"``) so a typo in a risk limit fails
loudly instead of silently falling back to a default.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from qts.core.errors import ConfigError, LiveTradingDisabledError
from qts.core.types import TradingMode


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketConfig(_Model):
    """What we trade. ``asset_class`` exists so equities/ETF/FX can be added later."""

    asset_class: Literal["crypto_spot"] = "crypto_spot"
    exchange: str = "binance"
    symbols: tuple[str, ...] = ("BTC/USDT", "ETH/USDT")
    timeframe: Literal["1d"] = "1d"
    quote_currency: str = "USDT"

    @model_validator(mode="after")
    def _check_symbols(self) -> MarketConfig:
        if not self.symbols:
            raise ValueError("at least one symbol is required")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be unique")
        for symbol in self.symbols:
            base, sep, quote = symbol.partition("/")
            if not sep or not base or quote != self.quote_currency:
                raise ValueError(f"symbol {symbol!r} must look like BASE/{self.quote_currency}")
        return self


class DataConfig(_Model):
    """Where historical data lives and which period is used."""

    directory: Path = Path("data")
    start: str = "2018-01-01"
    end: str | None = None


class TrendFollowingParams(_Model):
    """Parameters of the daily trend-following strategy (see docs/strategy.md)."""

    breakout_window: int = Field(55, ge=2, description="Donchian high lookback for entries")
    exit_window: int = Field(20, ge=2, description="Donchian low lookback for exits")
    fast_ema: int = Field(50, ge=1)
    slow_ema: int = Field(200, ge=2)
    atr_window: int = Field(20, ge=2)
    stop_atr_multiple: float = Field(3.0, gt=0, description="Initial stop distance in ATRs")
    trailing_stop: bool = Field(False, description="Ratchet the stop up (chandelier exit)")
    trailing_atr_multiple: float = Field(3.0, gt=0)
    min_atr_pct: float = Field(0.0, ge=0, description="Skip entries when ATR/close is below")
    max_atr_pct: float = Field(0.12, gt=0, description="Skip entries when ATR/close is above")

    @model_validator(mode="after")
    def _check(self) -> TrendFollowingParams:
        if self.fast_ema >= self.slow_ema:
            raise ValueError("fast_ema must be smaller than slow_ema")
        if self.min_atr_pct >= self.max_atr_pct:
            raise ValueError("min_atr_pct must be smaller than max_atr_pct")
        return self


class StrategyConfig(_Model):
    name: Literal["trend_following"] = "trend_following"
    params: TrendFollowingParams = TrendFollowingParams()


class SizingConfig(_Model):
    """Position sizing (what the strategy *asks* for; the risk engine re-checks)."""

    risk_per_trade_pct: float = Field(0.01, gt=0, le=0.05)
    max_position_pct: float = Field(0.30, gt=0, le=1.0)


class PortfolioConfig(_Model):
    initial_capital: float = Field(10_000.0, gt=0)
    sizing: SizingConfig = SizingConfig()


class CostConfig(_Model):
    """Trading frictions applied to every simulated fill."""

    fee_rate: float = Field(0.001, ge=0, lt=0.05, description="Taker fee, fraction of notional")
    slippage_bps: float = Field(5.0, ge=0, lt=500, description="Adverse slippage per fill")

    @property
    def slippage_rate(self) -> float:
        return self.slippage_bps / 10_000.0

    @property
    def cost_rate(self) -> float:
        """Worst-case cost of a buy as a fraction of notional (slippage, then fee on top)."""
        return (1.0 + self.slippage_rate) * (1.0 + self.fee_rate) - 1.0


class RiskLimits(_Model):
    """Hard limits enforced by the independent risk engine."""

    max_risk_per_trade_pct: float = Field(0.0125, gt=0, le=0.05)
    max_position_pct: float = Field(0.35, gt=0, le=1.0)
    max_total_exposure_pct: float = Field(1.0, gt=0, le=1.0)
    max_open_positions: int = Field(4, ge=1)
    max_daily_loss_pct: float = Field(0.05, gt=0, lt=1)
    max_drawdown_pct: float = Field(0.25, gt=0, lt=1)
    max_orders_per_day: int = Field(10, ge=1)
    max_price_deviation_pct: float = Field(0.25, gt=0, lt=1)
    max_data_age_hours: float = Field(48.0, gt=0)
    max_consecutive_errors: int = Field(3, ge=1)
    require_stop_loss: bool = True
    flatten_on_kill_switch: bool = False
    allowed_symbols: tuple[str, ...] | None = None


class ValidationConfig(_Model):
    """Out-of-sample, walk-forward and Monte Carlo settings."""

    out_of_sample_fraction: float = Field(0.3, gt=0, lt=1)
    walk_forward_train_days: int = Field(730, ge=30)
    walk_forward_test_days: int = Field(180, ge=7)
    monte_carlo_simulations: int = Field(2000, ge=100)
    seed: int = 42
    # Small, pre-declared grid: walk-forward picks among these on each train window.
    parameter_grid: dict[str, tuple[float, ...]] = Field(
        default_factory=lambda: {
            "breakout_window": (20, 55, 100),
            "stop_atr_multiple": (2.0, 3.0),
        }
    )


class AppConfig(_Model):
    """Root of the YAML configuration."""

    mode: TradingMode = TradingMode.BACKTEST
    market: MarketConfig = MarketConfig()
    data: DataConfig = DataConfig()
    strategy: StrategyConfig = StrategyConfig()
    portfolio: PortfolioConfig = PortfolioConfig()
    costs: CostConfig = CostConfig()
    risk: RiskLimits = RiskLimits()
    validation: ValidationConfig = ValidationConfig()

    @model_validator(mode="after")
    def _consistency(self) -> AppConfig:
        sizing = self.portfolio.sizing
        if sizing.risk_per_trade_pct > self.risk.max_risk_per_trade_pct:
            raise ValueError(
                "portfolio.sizing.risk_per_trade_pct exceeds risk.max_risk_per_trade_pct: "
                "every entry would be rejected by the risk engine"
            )
        if sizing.max_position_pct > self.risk.max_position_pct:
            raise ValueError("portfolio.sizing.max_position_pct exceeds risk.max_position_pct")
        return self

    def fingerprint(self) -> str:
        """Stable hash of the full configuration, stored with every result."""
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class Settings(BaseSettings):
    """Deployment settings and secrets, read from the environment / ``.env``.

    Secrets use :class:`pydantic.SecretStr`, whose ``repr`` is masked, so they
    cannot leak through logging or tracebacks by accident.
    """

    model_config = SettingsConfigDict(
        env_prefix="QTS_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    config_file: Path = Path("configs/default.yaml")
    database_url: SecretStr = SecretStr("sqlite:///state/qts.sqlite")
    state_dir: Path = Path("state")
    log_level: str = "INFO"
    log_json: bool = True
    enable_live_trading: bool = False
    live_trading_confirmation: str = ""
    exchange_api_key: SecretStr | None = None
    exchange_api_secret: SecretStr | None = None


LIVE_CONFIRMATION_PHRASE = "I-ACCEPT-REAL-MONEY-RISK"


def load_config(path: Path | str) -> AppConfig:
    """Load and validate a YAML configuration file."""
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"configuration file not found: {file}")
    try:
        raw: Any = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {file}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{file} must contain a mapping at the top level")
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"invalid configuration in {file}:\n{exc}") from exc


def enforce_mode(config: AppConfig, settings: Settings) -> TradingMode:
    """Gate the operating mode. Called by every entry point before doing anything.

    BACKTEST and PAPER always pass. LIVE needs a double opt-in from the
    environment *and* is still refused, because the MVP ships no live broker:
    there is deliberately no code path that can place a real order.
    """
    if config.mode is not TradingMode.LIVE:
        return config.mode
    if not settings.enable_live_trading:
        raise LiveTradingDisabledError(
            "mode=live requested but QTS_ENABLE_LIVE_TRADING is not set: refusing to start"
        )
    if settings.live_trading_confirmation != LIVE_CONFIRMATION_PHRASE:
        raise LiveTradingDisabledError(
            "mode=live requested without the explicit confirmation phrase: refusing to start"
        )
    raise LiveTradingDisabledError(
        "live trading is not implemented in this version (no live broker exists); "
        "use mode=backtest or mode=paper"
    )
