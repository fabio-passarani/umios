"""Persistence API used by the application. Callers never write SQL."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from qts.core.errors import PersistenceError
from qts.core.logging import REDACTED, get_logger
from qts.core.types import Fill, Trade
from qts.integrations.db.models import (
    Base,
    EquityPoint,
    FillRow,
    Heartbeat,
    PaperState,
    PositionRow,
    RiskDecisionRow,
    RiskEventRow,
    Run,
    SystemEvent,
    TradeRow,
)
from qts.portfolio.portfolio import Position
from qts.risk.journal import RiskEvent
from qts.risk.models import RiskDecision

log = get_logger("db")


def _now() -> datetime:
    return datetime.now(tz=UTC)


def normalize_url(url: str) -> str:
    """Make plain ``postgresql://`` URLs use the psycopg 3 driver."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


class Storage:
    """Thin repository over SQLAlchemy sessions."""

    def __init__(self, url: str) -> None:
        normalized = normalize_url(url)
        if normalized.startswith("sqlite:///") and ":memory:" not in normalized:
            Path(normalized.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        try:
            self.engine: Engine = create_engine(normalized, pool_pre_ping=True, future=True)
        except (SQLAlchemyError, ImportError, ValueError) as exc:  # pragma: no cover
            raise PersistenceError(f"cannot configure database ({REDACTED})") from exc
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        self._unit: Session | None = None

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commit on success, roll back and wrap on failure.

        Inside :meth:`transaction` it joins the surrounding unit of work instead
        (flush only), so the whole unit commits or rolls back together.
        """
        if self._unit is not None:
            try:
                yield self._unit
                self._unit.flush()
            except SQLAlchemyError as exc:
                raise PersistenceError(f"database operation failed: {type(exc).__name__}") from exc
            return
        session = self._sessions()
        try:
            yield session
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            # Never echo the exception text: drivers may include the DSN.
            raise PersistenceError(f"database operation failed: {type(exc).__name__}") from exc
        finally:
            session.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run several repository calls as **one** atomic unit of work.

        Everything written inside the block is committed together at the end, or
        not at all if anything raises. Used where a partial write would corrupt
        the record — e.g. a paper-trading step, whose fills, trades and resume
        point must never get out of step with each other. Not re-entrant across
        threads (the platform has a single trading loop); nesting is allowed.
        """
        if self._unit is not None:
            yield
            return
        session = self._sessions()
        self._unit = session
        try:
            yield
            session.commit()
        except SQLAlchemyError as exc:
            session.rollback()
            raise PersistenceError(f"database operation failed: {type(exc).__name__}") from exc
        except BaseException:
            session.rollback()
            raise
        finally:
            self._unit = None
            session.close()

    def init_schema(self) -> None:
        """Create all tables if they do not exist (idempotent)."""
        try:
            Base.metadata.create_all(self.engine)
        except SQLAlchemyError as exc:
            raise PersistenceError(f"cannot create schema: {type(exc).__name__}") from exc

    def ping(self) -> bool:
        """True if the database answers a trivial query."""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    # -------------------------------------------------------------------- runs
    def create_run(
        self,
        kind: str,
        mode: str,
        strategy: str,
        config: dict[str, Any],
        fingerprint: str,
        initial_capital: float,
    ) -> str:
        run_id = uuid4().hex
        with self.session() as s:
            s.add(
                Run(
                    id=run_id,
                    kind=kind,
                    mode=mode,
                    strategy=strategy,
                    status="running",
                    config_fingerprint=fingerprint,
                    config=config,
                    initial_capital=initial_capital,
                    started_at=_now(),
                )
            )
        return run_id

    def finish_run(self, run_id: str, status: str, metrics: dict[str, Any] | None = None) -> None:
        with self.session() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise PersistenceError(f"run {run_id} not found")
            run.status = status
            run.finished_at = _now()
            if metrics is not None:
                run.metrics = _json_safe(metrics)

    def update_metrics(self, run_id: str, metrics: dict[str, Any]) -> None:
        with self.session() as s:
            run = s.get(Run, run_id)
            if run is None:
                raise PersistenceError(f"run {run_id} not found")
            run.metrics = _json_safe(metrics)

    def get_run(self, run_id: str) -> Run | None:
        with self.session() as s:
            return s.get(Run, run_id)

    def active_run(self, kind: str) -> Run | None:
        """The running session of a given kind (``paper``, ``freqtrade``), if any."""
        with self.session() as s:
            return s.scalars(
                select(Run)
                .where(Run.kind == kind, Run.status == "running")
                .order_by(Run.started_at.desc())
            ).first()

    def active_paper_run(self) -> Run | None:
        return self.active_run("paper")

    # ------------------------------------------------------------ time series
    def append_equity(
        self,
        run_id: str,
        equity: pd.Series,
        cash: pd.Series,
        exposure: pd.Series,
        peak_before: float,
        benchmark: pd.Series | None = None,
    ) -> None:
        """Append equity points; ``peak_before`` is the running peak before this batch."""
        peak = peak_before
        index = pd.DatetimeIndex(equity.index)
        bench_map: dict[pd.Timestamp, float] = (
            {pd.Timestamp(k): float(v) for k, v in zip(benchmark.index, benchmark, strict=True)}
            if benchmark is not None
            else {}
        )
        rows = zip(index, equity.to_numpy(), cash.to_numpy(), exposure.to_numpy(), strict=True)
        with self.session() as s:
            for ts, value, cash_value, exposure_value in rows:
                peak = max(peak, float(value))
                s.merge(
                    EquityPoint(
                        run_id=run_id,
                        ts=ts.to_pydatetime(),
                        equity=float(value),
                        cash=float(cash_value),
                        exposure=float(exposure_value),
                        drawdown=float(value) / peak - 1.0,
                        benchmark_equity=bench_map.get(ts),
                    )
                )

    def equity_curve(self, run_id: str) -> pd.DataFrame:
        with self.session() as s:
            rows = s.scalars(
                select(EquityPoint).where(EquityPoint.run_id == run_id).order_by(EquityPoint.ts)
            ).all()
        frame = pd.DataFrame(
            [(r.ts, r.equity, r.cash, r.exposure, r.drawdown) for r in rows],
            columns=["ts", "equity", "cash", "exposure", "drawdown"],
        )
        # SQLite returns naive datetimes (always UTC here); PostgreSQL returns aware ones.
        index = pd.DatetimeIndex(pd.to_datetime(frame["ts"], utc=True), name="ts")
        return frame.drop(columns="ts").set_index(index)

    def add_fills(self, run_id: str, fills: Iterable[Fill]) -> None:
        with self.session() as s:
            s.add_all(
                FillRow(
                    run_id=run_id,
                    order_id=f.order_id,
                    ts=f.timestamp,
                    symbol=f.symbol,
                    side=f.side.value,
                    quantity=f.quantity,
                    price=f.price,
                    fee=f.fee,
                    reason=f.reason.value,
                )
                for f in fills
            )

    def fills(self, run_id: str) -> list[FillRow]:
        with self.session() as s:
            return list(
                s.scalars(
                    select(FillRow).where(FillRow.run_id == run_id).order_by(FillRow.id)
                ).all()
            )

    def add_trades(self, run_id: str, trades: Iterable[Trade]) -> None:
        with self.session() as s:
            s.add_all(
                TradeRow(
                    run_id=run_id,
                    symbol=t.symbol,
                    quantity=t.quantity,
                    entry_time=t.entry_time,
                    exit_time=t.exit_time,
                    entry_price=t.entry_price,
                    exit_price=t.exit_price,
                    fees=t.fees,
                    pnl=t.pnl,
                    return_pct=t.return_pct,
                    exit_reason=t.exit_reason.value,
                )
                for t in trades
            )

    def trades(self, run_id: str) -> list[TradeRow]:
        with self.session() as s:
            return list(
                s.scalars(
                    select(TradeRow).where(TradeRow.run_id == run_id).order_by(TradeRow.exit_time)
                ).all()
            )

    def replace_positions(
        self, run_id: str, positions: Iterable[Position], prices: dict[str, float]
    ) -> None:
        now = _now()
        with self.session() as s:
            s.execute(delete(PositionRow).where(PositionRow.run_id == run_id))
            for p in positions:
                last = prices.get(p.symbol, p.avg_price)
                s.add(
                    PositionRow(
                        run_id=run_id,
                        symbol=p.symbol,
                        quantity=p.quantity,
                        avg_price=p.avg_price,
                        last_price=last,
                        stop_price=p.stop_price,
                        unrealized_pnl=(last - p.avg_price) * p.quantity,
                        entry_time=p.entry_time,
                        updated_at=now,
                    )
                )

    def positions(self, run_id: str) -> list[PositionRow]:
        with self.session() as s:
            return list(s.scalars(select(PositionRow).where(PositionRow.run_id == run_id)).all())

    # --------------------------------------------------------------------- risk
    def add_risk_decisions(self, run_id: str, decisions: Iterable[RiskDecision]) -> None:
        with self.session() as s:
            s.add_all(
                RiskDecisionRow(
                    run_id=run_id,
                    ts=d.timestamp,
                    order_id=d.order.id,
                    symbol=d.order.symbol,
                    side=d.order.side.value,
                    quantity=d.order.quantity,
                    reference_price=d.order.reference_price,
                    order_reason=d.order.reason.value,
                    approved=d.approved,
                    failed_checks=",".join(c.name for c in d.failed_checks),
                    detail=d.reason,
                )
                for d in decisions
            )

    def add_risk_events(self, run_id: str, events: Iterable[RiskEvent]) -> None:
        with self.session() as s:
            s.add_all(
                RiskEventRow(run_id=run_id, ts=e.timestamp, kind=e.kind, detail=e.detail)
                for e in events
            )

    def risk_decisions(self, run_id: str) -> list[RiskDecisionRow]:
        with self.session() as s:
            return list(
                s.scalars(
                    select(RiskDecisionRow)
                    .where(RiskDecisionRow.run_id == run_id)
                    .order_by(RiskDecisionRow.id)
                ).all()
            )

    # --------------------------------------------------------------- operations
    def record_event(self, level: str, component: str, message: str) -> None:
        with self.session() as s:
            s.add(SystemEvent(ts=_now(), level=level, component=component, message=message[:2000]))

    def events(self, limit: int = 50) -> list[SystemEvent]:
        with self.session() as s:
            return list(
                s.scalars(select(SystemEvent).order_by(SystemEvent.id.desc()).limit(limit)).all()
            )

    def heartbeat(self, service: str, status: str = "ok", detail: str = "") -> None:
        with self.session() as s:
            s.merge(Heartbeat(service=service, ts=_now(), status=status, detail=detail[:500]))

    def heartbeats(self) -> list[Heartbeat]:
        with self.session() as s:
            return list(s.scalars(select(Heartbeat)).all())

    # -------------------------------------------------------------- paper state
    def save_paper_state(self, run_id: str, state: dict[str, Any]) -> None:
        with self.session() as s:
            s.merge(PaperState(run_id=run_id, state=state, updated_at=_now()))

    def load_paper_state(self, run_id: str) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(PaperState, run_id)
            return dict(row.state) if row is not None else None


def _json_safe(metrics: dict[str, Any]) -> dict[str, Any]:
    """JSON has no NaN/Infinity: store them as ``None`` / a large sentinel string."""
    safe: dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            safe[key] = None
        else:
            safe[key] = value
    return safe
