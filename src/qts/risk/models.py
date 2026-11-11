"""Value objects produced by the risk engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from qts.core.types import Order


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of one individual risk check."""

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RiskApproval:
    """Proof that the risk engine approved exactly this order.

    ``signature`` is an HMAC over the order's fields computed with a key that
    only the issuing engine instance holds. A broker asks the engine to verify
    and *consume* the approval, so it cannot be forged, altered or replayed.
    """

    order: Order
    issued_at: datetime
    signature: str


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Full, auditable record of one evaluation (approved or rejected)."""

    order: Order
    approved: bool
    checks: tuple[CheckResult, ...]
    timestamp: datetime
    approval: RiskApproval | None = None

    @property
    def failed_checks(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if not c.passed)

    @property
    def reason(self) -> str:
        """Human-readable summary of why the order was rejected ('' if approved)."""
        return "; ".join(f"{c.name}: {c.detail}" for c in self.failed_checks)
