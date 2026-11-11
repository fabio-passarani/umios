"""Strategy contract.

A strategy is a pure function from candles to a signal table. It holds no
positions, no cash and no broker handle, which keeps it trivially testable and
makes it impossible for strategy code to bypass risk management.

Timing convention (the single most important rule against look-ahead bias):
the row at time *t* may only use data up to the close of bar *t*, and the engine
acts on it at the **open of bar t+1**.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

#: Columns every strategy must return, aligned to the input index.
#: ``entry``/``exit`` are booleans; ``stop_distance`` is the initial protective
#: stop distance in price units; ``trail_distance`` is NaN when trailing is off.
SIGNAL_COLUMNS: tuple[str, ...] = ("entry", "exit", "stop_distance", "trail_distance")


class Strategy(ABC):
    """Base class for signal generators."""

    name: str

    @property
    @abstractmethod
    def warmup_bars(self) -> int:
        """Bars needed before the first valid signal."""

    @abstractmethod
    def generate_signals(self, candles: pd.DataFrame) -> pd.DataFrame:
        """Return a frame indexed like ``candles`` containing :data:`SIGNAL_COLUMNS`
        (plus any indicator columns useful for inspection)."""
