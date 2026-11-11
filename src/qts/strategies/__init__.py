"""Trading strategies. Strategies only *describe* intent (signals); they never
touch a broker, size capital on their own authority, or see the risk engine."""

from qts.core.config import StrategyConfig
from qts.strategies.base import SIGNAL_COLUMNS, Strategy
from qts.strategies.trend_following import TrendFollowingStrategy

__all__ = ["SIGNAL_COLUMNS", "Strategy", "TrendFollowingStrategy", "build_strategy"]


def build_strategy(config: StrategyConfig) -> Strategy:
    """Factory: map a validated strategy config to an instance."""
    if config.name == "trend_following":
        return TrendFollowingStrategy(config.params)
    raise ValueError(f"unknown strategy {config.name!r}")  # pragma: no cover
