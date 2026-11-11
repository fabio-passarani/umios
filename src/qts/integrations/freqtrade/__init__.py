"""Bridge between Freqtrade and the platform's own strategy / risk engine.

This package never imports ``freqtrade`` (GPLv3): it exposes plain functions and
objects that the thin strategy class in ``freqtrade/user_data/strategies`` calls.
"""

from qts.integrations.freqtrade.bridge import FreqtradeBridge

__all__ = ["FreqtradeBridge"]
