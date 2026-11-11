#!/usr/bin/env bash
# End-to-end test of paper trading on the real Docker stack:
#
#   market data -> strategy -> signal -> risk engine -> execution -> PostgreSQL -> Grafana
#
# Nothing is forced: every position is opened because the strategy produced a
# signal on a prepared (synthetic, deterministic) market, and closed by a stop,
# an exit signal or an operator command. Along the way the stack is restarted,
# fed duplicated, missing and late candles, halted with the kill switch, and
# finally destroyed and restored from a backup. No real order exists anywhere.
#
# Runs in its own compose project (qts-e2e): it never touches a real stack.
#   scripts/e2e_paper_test.sh            run and clean up
#   KEEP=1 scripts/e2e_paper_test.sh     leave the stack up for inspection
set -euo pipefail

export QTS_COMPOSE_ARGS="-p qts-e2e -f docker-compose.yml -f docker-compose.e2e.yml"
export QTS_BACKUP_DIR="./tests/e2e/.work/backups"
source "$(dirname "$0")/lib.sh"

step() { printf '\n== %s\n' "$*"; }
qts() { dc exec -T cli qts "$@"; }
advance() { dc exec -T cli python /app/tests/e2e/advance.py "$@"; }
check() { dc exec -T cli python /app/tests/e2e/check.py "$@"; }
sql() { dc exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -c "$1"; }
field() {  # field <json> <python expression over d>
    python3 -c "import json,sys; d=json.loads(sys.argv[1]); print($2)" "$1"
}
expect() {  # expect <description> <actual> <wanted>
    [ "$2" = "$3" ] || fail "$1: got '$2', wanted '$3'"
    echo "   ok: $1 ($2)"
}
cleanup() {
    code=$?
    if [ "${KEEP:-0}" != 1 ]; then
        dc --profile disabled down -v --remove-orphans >/dev/null 2>&1 || true
        rm -rf tests/e2e/.work
    fi
    exit $code
}
trap cleanup EXIT

step "fresh stack"
dc --profile disabled down -v --remove-orphans >/dev/null 2>&1 || true
rm -rf tests/e2e/.work && mkdir -p "$BACKUPS" tests/e2e/.work/driver && chmod 700 "$BACKUPS"
chmod 777 tests/e2e/.work/driver   # written by the container's own user; test scratch only
dc up -d --wait postgres
dc up -d grafana cli
expect "application connects as the unprivileged role" \
    "$(dc exec -T cli python -c 'from qts.core.config import Settings; print(Settings().db_user)')" "qts_app"

step "history delivered, session opened (trades only candles that arrive from now on)"
state="$(advance --init)"
expect "nothing traded yet" "$(field "$state" "d['fills']")" "0"

step "the strategy generates the entry signals"
state="$(advance --until position --max 30)"
expect "a position was opened by a strategy signal" "$(field "$state" "len(d['positions']) > 0")" "True"
state="$(advance --days 3)"
positions_before="$(field "$state" "json.dumps(d['positions'], sort_keys=True)")"
echo "   positions: $positions_before"
expect "orders evaluated so far were all approved" "$(field "$state" "d['rejected']")" "0"

step "PostgreSQL restarts while positions are open"
dc restart postgres >/dev/null
dc up -d --wait postgres
state="$(advance --days 2)"
expect "positions and stops survive" "$(field "$state" "json.dumps(d['positions'], sort_keys=True)")" "$positions_before"

step "every container is destroyed and recreated (volumes kept)"
dc down >/dev/null 2>&1
dc up -d --wait postgres
dc up -d grafana cli
state="$(advance --days 2)"
expect "positions and stops survive" "$(field "$state" "json.dumps(d['positions'], sort_keys=True)")" "$positions_before"

step "duplicated candles"
fills_before="$(field "$state" "d['fills']")"; bars_before="$(field "$state" "d['bars']")"
state="$(advance --refeed)"
expect "no new fill" "$(field "$state" "d['fills']")" "$fills_before"
expect "no new candle" "$(field "$state" "d['bars']")" "$bars_before"

step "a candle that never arrives, then data three days late"
advance --days 1 --skip ETH/USDT >/dev/null
advance --days 4 >/dev/null
advance --stale 3
expect "the late feed was recorded as an error" \
    "$(sql "select count(*) > 0 from system_events where level = 'error' and message like '%stale%'")" "t"
state="$(advance --days 3)"
expect "one failed cycle did not trip the kill switch" \
    "$(qts risk status | head -1)" "Kill switch: not active "

step "the market crashes: stop loss and exit signal close the positions"
state="$(advance --until flat --max 80)"
reasons="$(field "$state" "','.join(d['sell_reasons'])")"
expect "exits by stop loss and by exit signal" "$reasons" "exit_signal,stop_loss"
expect "the daily loss circuit breaker fired" \
    "$(sql "select count(*) > 0 from risk_events where kind = 'daily_loss_halt'")" "t"
advance --days 5 >/dev/null

step "the recorded session equals an independent backtest of the delivered data"
check equals-backtest
check invariants

step "Grafana reads the same data through its read-only account"
grafana="http://127.0.0.1:${E2E_GRAFANA_PORT:-53000}"
curl -fsS --retry 30 --retry-delay 2 --retry-all-errors "$grafana/api/health" >/dev/null
run_id="$(sql "select id from runs where kind = 'paper' and status = 'running'")"
query="{\"queries\":[{\"refId\":\"A\",\"datasource\":{\"uid\":\"qts-postgres\"},\"format\":\"table\",\"rawSql\":\"select count(*) from fills where run_id = '$run_id'\"}],\"from\":\"now-1h\",\"to\":\"now\"}"
seen="$(curl -fsS -u "${GRAFANA_ADMIN_USER:-admin}:${GRAFANA_ADMIN_PASSWORD}" -H 'content-type: application/json' \
    "$grafana/api/ds/query" -d "$query" | python3 -c "import json,sys; print(json.load(sys.stdin)['results']['A']['frames'][0]['data']['values'][0][0])")"
expect "fills visible in Grafana" "$seen" "$(sql "select count(*) from fills where run_id = '$run_id'")"

step "disaster: backup, every volume destroyed, restore"
fingerprint_before="$(fingerprint_sql | dc exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F ' ')"
"$(dirname "$0")/restore_test.sh" | sed 's/^/   /'
dc --profile disabled down -v --remove-orphans >/dev/null 2>&1
dc up -d --wait postgres
expect "the database is empty after the disaster" "$(sql "select count(*) from runs" 2>/dev/null || echo gone)" "gone"
"$(dirname "$0")/restore.sh" --latest --yes | sed 's/^/   /'
dc up -d cli
fingerprint_after="$(fingerprint_sql | dc exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F ' ')"
expect "every table identical after the restore" "$(echo "$fingerprint_after" | md5sum)" "$(echo "$fingerprint_before" | md5sum)"
# the candle store is not part of the backup (it is re-downloadable): deliver it again
advance --refeed >/dev/null
advance --days 5 >/dev/null
check equals-backtest

step "kill switch while the strategy keeps signalling"
qts risk kill --reason "e2e: operator stop" >/dev/null
buys_before="$(sql "select count(*) from fills where run_id = '$run_id' and side = 'buy'")"
advance --days 120 >/dev/null   # through the second rally, where the strategy wants to enter
expect "no entry while the kill switch is active" \
    "$(sql "select count(*) from fills where run_id = '$run_id' and side = 'buy'")" "$buys_before"
expect "the strategy did ask, and the risk engine refused" \
    "$(sql "select count(*) > 0 from risk_decisions where run_id = '$run_id' and not approved and failed_checks like '%kill_switch%'")" "t"
qts risk reset --operator "e2e" >/dev/null

step "trading resumes; the operator closes half of a position"
state="$(advance --until position --max 40)"
symbol="$(field "$state" "sorted(d['positions'])[0]")"
quantity="$(field "$state" "d['positions'][sorted(d['positions'])[0]]['quantity']")"
qts paper close --symbol "$symbol" --fraction 0.5 | sed 's/^/   /'
state="$(advance --days 1)"
expect "half of $symbol is left" \
    "$(field "$state" "round(d['positions']['$symbol']['quantity'] / $quantity, 6)")" "0.5"
expect "the partial close is recorded as a manual exit" \
    "$(sql "select count(*) from fills where run_id = '$run_id' and reason = 'manual'")" "1"

step "to the end of the market: the decline produces exit signals"
state="$(advance --until flat --max 120)"
advance --days 400 >/dev/null   # whatever is left of the fixture
check invariants
expect "no real-money mode anywhere" "$(sql "select string_agg(distinct mode, ',') from runs")" "paper"

step "reports"
day="$(sql "select to_char(max(ts), 'YYYY-MM-DD') from fills where run_id = '$run_id'")"
qts report daily --date "$day" | sed -n '1,12p' | sed 's/^/   /'
set +e; qts report soak >/dev/null; soak=$?; set -e
expect "the soak test is not declared passed after a few minutes" "$soak" "2"

echo
echo "E2E PAPER TRADING TEST PASSED"
sql "select 'fills', count(*) from fills where run_id = '$run_id' union all select 'signals', count(*) from signals where run_id = '$run_id' union all select 'orders evaluated', count(*) from risk_decisions where run_id = '$run_id' union all select 'candles', count(*) from equity_points where run_id = '$run_id'" | sed 's/^/   /'
