#!/usr/bin/env bash
# Smoke test of the Docker stack: PostgreSQL + schema + read-only Grafana role +
# provisioned dashboard + kill switch round trip. Needs a populated .env and the
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
docker compose up -d grafana

echo "== schema"
qts db init
tables="$(psql_as "$POSTGRES_USER" "$POSTGRES_PASSWORD" \
    "select count(*) from information_schema.tables where table_schema = 'public'")"
[ "$tables" -ge 10 ] || fail "expected at least 10 tables, found $tables"

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
