"""Generate docker/grafana/dashboards/qts-overview.json.

The dashboard is defined here in Python (short, reviewable, diff-friendly) and
the JSON is committed so Grafana can be provisioned without running anything.
Regenerate with:  python scripts/build_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DS = {"type": "grafana-postgresql-datasource", "uid": "qts-postgres"}
OUT = Path(__file__).resolve().parents[1] / "docker/grafana/dashboards/qts-overview.json"
RUN = "'${run}'"
_next_id = 0


def _id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def target(sql: str, fmt: str = "table") -> dict[str, Any]:
    return {"datasource": DS, "format": fmt, "rawQuery": True, "rawSql": sql, "refId": "A"}


def stat(
    title: str,
    sql: str,
    unit: str,
    x: int,
    y: int,
    w: int = 4,
    thresholds: list[dict[str, Any]] | None = None,
    decimals: int = 2,
) -> dict[str, Any]:
    return {
        "id": _id(),
        "type": "stat",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": 4, "w": w, "x": x, "y": y},
        "targets": [target(sql)],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "color": {"mode": "thresholds"},
                "thresholds": {
                    "mode": "absolute",
                    "steps": thresholds or [{"color": "text", "value": None}],
                },
            },
            "overrides": [],
        },
        "options": {
            "colorMode": "value",
            "graphMode": "none",
            "textMode": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    }


def timeseries(
    title: str, sql: str, unit: str, x: int, y: int, w: int, h: int = 9, fill: int = 0
) -> dict[str, Any]:
    return {
        "id": _id(),
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [target(sql, "time_series")],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "lineWidth": 2,
                    "fillOpacity": fill,
                    "showPoints": "never",
                    "spanNulls": True,
                },
            },
            "overrides": [],
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi"},
        },
    }


def table(
    title: str,
    sql: str,
    x: int,
    y: int,
    w: int,
    h: int = 8,
    overrides: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": _id(),
        "type": "table",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [target(sql)],
        "fieldConfig": {"defaults": {"custom": {"align": "auto"}}, "overrides": overrides or []},
        "options": {"showHeader": True, "cellHeight": "sm"},
    }


def row(title: str, y: int) -> dict[str, Any]:
    return {
        "id": _id(),
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "panels": [],
    }


def unit_override(column: str, unit: str) -> dict[str, Any]:
    return {
        "matcher": {"id": "byName", "options": column},
        "properties": [{"id": "unit", "value": unit}],
    }


GOOD_BAD = [{"color": "red", "value": None}, {"color": "green", "value": 0}]
LAST = f"SELECT COL FROM equity_points WHERE run_id = {RUN} ORDER BY ts DESC LIMIT 1"
METRIC = f"SELECT (metrics->>'KEY')::float FROM runs WHERE id = {RUN}"

panels: list[dict[str, Any]] = [
    row("Account", 0),
    stat(
        "Initial capital", f"SELECT initial_capital FROM runs WHERE id = {RUN}", "currencyUSD", 0, 1
    ),
    stat("Equity", LAST.replace("COL", "equity"), "currencyUSD", 4, 1),
    stat(
        "Profit & Loss",
        f"SELECT e.equity - r.initial_capital FROM equity_points e JOIN runs r ON r.id = e.run_id "
        f"WHERE e.run_id = {RUN} ORDER BY e.ts DESC LIMIT 1",
        "currencyUSD",
        8,
        1,
        thresholds=GOOD_BAD,
    ),
    stat(
        "Total return",
        f"SELECT e.equity / r.initial_capital - 1 FROM equity_points e JOIN runs r ON r.id = e.run_id "
        f"WHERE e.run_id = {RUN} ORDER BY e.ts DESC LIMIT 1",
        "percentunit",
        12,
        1,
        thresholds=GOOD_BAD,
    ),
    stat(
        "Current drawdown",
        LAST.replace("COL", "drawdown"),
        "percentunit",
        16,
        1,
        thresholds=[
            {"color": "red", "value": None},
            {"color": "orange", "value": -0.15},
            {"color": "green", "value": -0.05},
        ],
    ),
    stat(
        "Max drawdown",
        f"SELECT min(drawdown) FROM equity_points WHERE run_id = {RUN}",
        "percentunit",
        20,
        1,
    ),
    timeseries(
        "Equity curve vs Buy & Hold",
        f'SELECT ts AS time, equity AS "Strategy", benchmark_equity AS "Buy & Hold" '
        f"FROM equity_points WHERE run_id = {RUN} AND $__timeFilter(ts) ORDER BY ts",
        "currencyUSD",
        0,
        5,
        16,
    ),
    timeseries(
        "Drawdown",
        f'SELECT ts AS time, drawdown AS "Drawdown" FROM equity_points '
        f"WHERE run_id = {RUN} AND $__timeFilter(ts) ORDER BY ts",
        "percentunit",
        16,
        5,
        8,
        fill=25,
    ),
    row("Performance", 14),
    stat("Sharpe", METRIC.replace("KEY", "sharpe"), "none", 0, 15),
    stat("Sortino", METRIC.replace("KEY", "sortino"), "none", 4, 15),
    stat("CAGR", METRIC.replace("KEY", "cagr"), "percentunit", 8, 15, thresholds=GOOD_BAD),
    stat("Win rate", METRIC.replace("KEY", "win_rate"), "percentunit", 12, 15),
    stat("Profit factor", METRIC.replace("KEY", "profit_factor"), "none", 16, 15),
    stat("Closed trades", METRIC.replace("KEY", "trades"), "none", 20, 15, decimals=0),
    timeseries(
        "Cash and exposure",
        f'SELECT ts AS time, cash AS "Cash", exposure AS "Invested" FROM equity_points '
        f"WHERE run_id = {RUN} AND $__timeFilter(ts) ORDER BY ts",
        "currencyUSD",
        0,
        19,
        12,
        h=8,
        fill=15,
    ),
    timeseries(
        "Realised P&L per closed trade (cumulative)",
        f'SELECT exit_time AS time, sum(pnl) OVER (ORDER BY exit_time) AS "Cumulative P&L" '
        f"FROM trades WHERE run_id = {RUN} AND $__timeFilter(exit_time) ORDER BY exit_time",
        "currencyUSD",
        12,
        19,
        12,
        h=8,
    ),
    row("Positions and trades", 27),
    table(
        "Open positions",
        f"SELECT symbol, quantity, avg_price, last_price, stop_price, unrealized_pnl, entry_time "
        f"FROM positions WHERE run_id = {RUN} ORDER BY symbol",
        0,
        28,
        12,
        overrides=[unit_override("unrealized_pnl", "currencyUSD")],
    ),
    table(
        "Trades (most recent first)",
        f"SELECT exit_time, symbol, quantity, entry_price, exit_price, pnl, return_pct, fees, "
        f"exit_reason, entry_time FROM trades WHERE run_id = {RUN} ORDER BY exit_time DESC "
        f"LIMIT 200",
        12,
        28,
        12,
        overrides=[unit_override("pnl", "currencyUSD"), unit_override("return_pct", "percentunit")],
    ),
    row("Risk", 36),
    stat(
        "Orders evaluated",
        f"SELECT count(*) FROM risk_decisions WHERE run_id = {RUN}",
        "none",
        0,
        37,
        decimals=0,
    ),
    stat(
        "Orders rejected",
        f"SELECT count(*) FROM risk_decisions WHERE run_id = {RUN} AND NOT approved",
        "none",
        4,
        37,
        decimals=0,
        thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 1}],
    ),
    stat(
        "Exposure (% of equity)",
        f"SELECT exposure / NULLIF(equity, 0) FROM equity_points WHERE run_id = {RUN} "
        f"ORDER BY ts DESC LIMIT 1",
        "percentunit",
        8,
        37,
    ),
    stat(
        "Kill switch / halt events",
        f"SELECT count(*) FROM risk_events WHERE run_id = {RUN} AND kind LIKE 'kill_switch%'",
        "none",
        12,
        37,
        decimals=0,
        thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
    ),
    table(
        "Risk events (kill switch, daily-loss halts, errors)",
        f"SELECT ts, kind, detail FROM risk_events WHERE run_id = {RUN} ORDER BY ts DESC LIMIT 100",
        16,
        37,
        8,
        h=12,
    ),
    table(
        "Rejected orders",
        f"SELECT ts, symbol, side, quantity, reference_price, failed_checks, detail "
        f"FROM risk_decisions WHERE run_id = {RUN} AND NOT approved ORDER BY ts DESC LIMIT 100",
        0,
        41,
        16,
    ),
    row("System", 49),
    table(
        "Services (heartbeats)",
        "SELECT service, status, ts AS last_seen, "
        "round(extract(epoch FROM now() - ts) / 3600.0, 1) AS hours_ago, detail "
        "FROM heartbeats ORDER BY service",
        0,
        50,
        8,
    ),
    table(
        "Active strategies / sessions",
        "SELECT kind, strategy, mode, status, started_at, config_fingerprint, id "
        "FROM runs WHERE status = 'running' OR kind = 'paper' ORDER BY started_at DESC LIMIT 20",
        8,
        50,
        8,
    ),
    table(
        "Errors and system log",
        "SELECT ts, level, component, message FROM system_events ORDER BY id DESC LIMIT 100",
        16,
        50,
        8,
    ),
]

dashboard = {
    "uid": "qts-overview",
    "title": "QTS — Trading overview",
    "tags": ["qts"],
    "timezone": "utc",
    "schemaVersion": 39,
    "version": 1,
    "editable": False,
    "graphTooltip": 1,
    "refresh": "5m",
    "time": {"from": "now-10y", "to": "now"},
    "templating": {
        "list": [
            {
                "name": "run",
                "label": "Run",
                "type": "query",
                "datasource": DS,
                "query": "SELECT kind || ' | ' || strategy || ' | ' || to_char(started_at, 'YYYY-MM-DD HH24:MI') "
                "|| ' | ' || status AS __text, id AS __value FROM runs "
                "ORDER BY (kind = 'paper' AND status = 'running') DESC, started_at DESC",
                "refresh": 1,
                "sort": 0,
                "multi": False,
                "includeAll": False,
                "current": {},
            }
        ]
    },
    "annotations": {"list": []},
    "panels": panels,
}

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
print(f"wrote {OUT} ({len(panels)} panels)")
