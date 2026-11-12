#!/usr/bin/env bash
# Smoke test of the Docker stack: PostgreSQL + versioned schema + least-privilege
# database roles + provisioned dashboard + kill switch round trip. Needs a populated .env and the
# images already built (`docker compose build`). Used by CI; safe to run locally:
# it only creates the schema and toggles the kill switch on and off.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

fail() { echo "SMOKE TEST FAILED: $*" >&2; exit 1; }
qts() { docker compose run --rm -T -e QTS_LOG_LEVEL=ERROR paper-trader "$@"; }
psql_as() {  # psql_as <user> <password> <sql>
    docker compose exec -T -e PGPASSWORD="$2" postgres \
        psql -h 127.0.0.1 -U "$1" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -Atc "$3"
}

docker compose up -d --wait postgres
docker compose up -d grafana   # also runs db-setup (roles) and migrate (schema) to completion

echo "== schema"
qts db current || fail "the database is not at the schema revision this code needs"
tables="$(psql_as "$POSTGRES_USER" "$POSTGRES_PASSWORD" \
    "select count(*) from information_schema.tables where table_schema = 'public'")"
[ "$tables" -ge 11 ] || fail "expected at least 11 tables, found $tables"

echo "== the application role can use the data and nothing else"
denied() {  # denied <description> <user> <password> <sql>
    if psql_as "$2" "$3" "$4" >/dev/null 2>&1; then fail "$2 was able to $1"; fi
}
psql_as qts_app "$QTS_APP_DB_PASSWORD" \
    "insert into system_events (ts, level, component, message) values (now(), 'info', 'smoke', 'ok')" \
    >/dev/null || fail "qts_app cannot write data"
[ "$(psql_as qts_app "$QTS_APP_DB_PASSWORD" 'select rolsuper from pg_roles where rolname = current_user')" = "f" ] \
    || fail "qts_app is a superuser"
denied "create a table"          qts_app "$QTS_APP_DB_PASSWORD" "create table smoke_x (i int)"
denied "drop a table"            qts_app "$QTS_APP_DB_PASSWORD" "drop table fills"
denied "truncate a table"        qts_app "$QTS_APP_DB_PASSWORD" "truncate fills"
denied "alter a table"           qts_app "$QTS_APP_DB_PASSWORD" "alter table runs add column smoke int"
denied "change the schema version" qts_app "$QTS_APP_DB_PASSWORD" "update alembic_version set version_num = 'x'"
denied "create a role"           qts_app "$QTS_APP_DB_PASSWORD" "create role smoke_evil superuser"
if docker compose exec -T -e PGPASSWORD="$QTS_APP_DB_PASSWORD" postgres \
    psql -h 127.0.0.1 -U qts_app -d freqtrade -c 'select 1' >/dev/null 2>&1; then
    fail "qts_app can connect to the freqtrade database"
fi
if docker compose exec -T -e PGPASSWORD="$FREQTRADE_DB_PASSWORD" postgres \
    psql -h 127.0.0.1 -U freqtrade_app -d "$POSTGRES_DB" -c 'select 1' >/dev/null 2>&1; then
    fail "freqtrade_app can connect to the platform database"
fi
docker compose exec -T -e PGPASSWORD="$FREQTRADE_DB_PASSWORD" postgres \
    psql -h 127.0.0.1 -U freqtrade_app -d freqtrade -qc 'create table smoke_t (i int); drop table smoke_t' \
    || fail "freqtrade_app cannot manage its own database"

echo "== Grafana database role is read-only"
psql_as grafana_ro "$GRAFANA_DB_PASSWORD" "select count(*) from runs" >/dev/null \
    || fail "grafana_ro cannot read"
if psql_as grafana_ro "$GRAFANA_DB_PASSWORD" "delete from runs" >/dev/null 2>&1; then
    fail "grafana_ro was able to delete rows"
fi

echo "== kill switch round trip"
qts risk kill --reason "smoke test"
status="$(qts risk status)"   # captured first: `| grep -q` would break the pipe under pipefail
[[ "$status" == *"Kill switch: ACTIVE"* ]] || fail "kill switch did not latch"
qts risk reset --operator "smoke test"
status="$(qts risk status)"
[[ "$status" == *"Kill switch: not active"* ]] || fail "kill switch did not reset"

echo "== live mode is refused"
if docker compose run --rm -T --no-deps -e QTS_ENABLE_LIVE_TRADING=true \
    -e QTS_LIVE_TRADING_CONFIRMATION=I-ACCEPT-REAL-MONEY-RISK \
    --entrypoint sh paper-trader -c \
    "sed 's/^mode: .*/mode: live/' configs/paper.yaml > /tmp/live.yaml && qts config -c /tmp/live.yaml" \
    >/dev/null 2>&1; then
    fail "a configuration with mode: live was accepted"
fi

echo "== Grafana"
grafana="http://127.0.0.1:3000"
curl -fsS --retry 30 --retry-delay 2 --retry-all-errors "$grafana/api/health" >/dev/null \
    || fail "Grafana is not healthy"
auth="${GRAFANA_ADMIN_USER:-admin}:${GRAFANA_ADMIN_PASSWORD}"
curl -fsS -u "$auth" "$grafana/api/dashboards/uid/qts-overview" >/dev/null \
    || fail "dashboard qts-overview is not provisioned"
health="$(curl -fsS -u "$auth" "$grafana/api/datasources/uid/qts-postgres/health")"
[[ "$health" == *'"status":"OK"'* ]] \
    || fail "Grafana cannot query PostgreSQL through the read-only datasource: $health"
code="$(curl -s -o /dev/null -w '%{http_code}' "$grafana/api/dashboards/uid/qts-overview")"
[ "$code" = "401" ] || fail "anonymous access to Grafana returned HTTP $code"

echo "SMOKE TEST PASSED"
