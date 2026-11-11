#!/usr/bin/env bash
# Proves that a backup can actually be restored. Non-destructive: the backup is
# loaded into a throw-away PostgreSQL container and compared with the running
# stack, table by table (row count + checksum of every row), together with the
# roles, the schema version and the state archive (kill switch, risk state).
#
#   scripts/restore_test.sh              take a fresh backup and test it
#   scripts/restore_test.sh --existing   test the newest backup already in ./backups
#                                        (the live data may have moved on since:
#                                         only use it on a quiet stack)
set -euo pipefail
source "$(dirname "$0")/lib.sh"

[ "${1:-}" = "--existing" ] || { mkdir -p "$BACKUPS"; chmod 700 "$BACKUPS"; dc run --rm backup; }
dump="$(newest "$BACKUPS/qts-2*.sql.gz")"
[ -n "$dump" ] || fail "no database dump in $BACKUPS"
stamp="$(basename "$dump" .sql.gz)"; stamp="${stamp#qts-}"
state="$BACKUPS/qts-state-${stamp}.tar.gz"
echo "== testing $dump"

live="$(fingerprint_sql | dc exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F ' ')"
live_revision="$(dc exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'select version_num from alembic_version')"

scratch="qts-restore-test-$$"
work="$(mktemp -d)"
cleanup() { docker rm -f "$scratch" >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT
docker run -d --name "$scratch" -e POSTGRES_USER="$POSTGRES_USER" \
    -e POSTGRES_PASSWORD=restore-test-only -e POSTGRES_DB=postgres postgres:17-alpine >/dev/null
sq() { docker exec -i "$scratch" psql -U "$POSTGRES_USER" "$@"; }
# the image starts PostgreSQL twice (init, then for real): wait for the listening one
for _ in $(seq 1 60); do
    if docker exec "$scratch" pg_isready -q -h 127.0.0.1 -U "$POSTGRES_USER"; then break; fi
    sleep 1
done

echo "== restoring into a scratch PostgreSQL"
# pg_dumpall --clean tries to drop the role it is running as: that one error is expected.
gunzip -c "$dump" | sq -d postgres -q >"$work/restore.log" 2>&1 || true
errors="$(grep -c '^ERROR' "$work/restore.log" || true)"
unexpected="$(grep '^ERROR' "$work/restore.log" | grep -vc 'current user cannot be dropped\|already exists' || true)"
[ "$unexpected" = "0" ] || { grep '^ERROR' "$work/restore.log" | head -5 >&2; fail "$unexpected unexpected errors while restoring"; }

restored="$(fingerprint_sql | sq -d "$POSTGRES_DB" -At -F ' ')"
restored_revision="$(sq -d "$POSTGRES_DB" -Atc 'select version_num from alembic_version')"

echo "== comparing (table rows checksum)"
ok=1
while read -r table count sum; do
    other="$(grep "^$table " <<<"$restored" || true)"
    if [ "$table $count $sum" = "$other" ]; then
        printf '   %-16s %6s rows  identical\n' "$table" "$count"
    else
        printf '   %-16s live: %s %s   restored: %s\n' "$table" "$count" "$sum" "${other#* }"; ok=0
    fi
done <<<"$live"
[ "$ok" = 1 ] || fail "restored data differs from the live database"
[ "$live_revision" = "$restored_revision" ] || fail "schema revision $restored_revision != $live_revision"
echo "   schema revision  $restored_revision  identical"

roles="$(sq -d postgres -Atc "select rolname || ':' || (rolsuper or rolcreaterole or rolcreatedb)::int from pg_roles where rolname in ('qts_app','freqtrade_app','grafana_ro') order by 1")"
[ "$roles" = "$(printf 'freqtrade_app:0\ngrafana_ro:0\nqts_app:0')" ] || fail "roles not restored as unprivileged: $roles"
[ "$(sq -d postgres -Atc "select count(*) from pg_database where datname = 'freqtrade'")" = "1" ] || fail "freqtrade database missing"
echo "   roles            qts_app, freqtrade_app, grafana_ro restored, none superuser"

echo "== what was recovered"
sq -d "$POSTGRES_DB" -At -F ' ' <<'EOSQL' | sed 's/^/   /'
select 'runs:', count(*), 'of which running:', count(*) filter (where status = 'running') from runs;
select 'open positions:', count(*) from positions;
select 'orders evaluated:', count(*), 'rejected:', count(*) filter (where not approved) from risk_decisions;
select 'fills:', count(*), ' trades:', (select count(*) from trades) from fills;
select 'portfolio (run ' || left(run_id, 8) || '): cash', round((state->>'cash')::numeric, 2),
       'pending orders', (select count(*) from json_object_keys(state->'pending')),
       '| risk limits: peak equity', state->'risk'->>'peak_equity',
       'daily halt', state->'risk'->>'daily_halt',
       'errors', state->'risk'->>'consecutive_errors'
  from paper_state;
EOSQL

if [ -f "$state" ]; then
    echo "== state archive $state"
    tar xzf "$state" -C "$work"
    archived="$(cd "$work/state" && find . -type f -exec sha256sum {} + | sort -k2)"
    current="$(dc run --rm -T --no-deps --entrypoint sh backup -c 'cd /state && find . -type f -exec sha256sum {} + | sort -k2')"
    [ "$archived" = "$current" ] || fail "state archive differs from the state volume"
    for file in "$work"/state/*.json; do
        [ -e "$file" ] || continue
        printf '   %-28s %s\n' "$(basename "$file")" "$(head -c 160 "$file")"
    done
    echo "   identical to the state volume"
else
    fail "state archive $state is missing"
fi
echo "RESTORE TEST PASSED ($errors expected notices while restoring)"
