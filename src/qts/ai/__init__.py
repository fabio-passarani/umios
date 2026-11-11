"""Research-only machine learning (optional ``ai`` extra).

Boundary, enforced by tests: this package produces *analysis and features*. It
imports nothing from ``qts.execution`` or ``qts.risk`` and has no way to create
an order, reach a broker or influence a risk decision.

Status in the MVP: infrastructure only. No model output is wired into the
trading strategy; doing so requires its own validation study first.
"""
