#!/usr/bin/env bash
# Restore a backup into THIS stack. Destructive: every database is replaced by
# the content of the dump and the state volume by the archive.
#
#   scripts/restore.sh --dump backups/qts-<stamp>.sql.gz --yes
#   scripts/restore.sh --latest --yes
#
# The matching state archive (qts-state-<stamp>.tar.gz) is restored too when it
# exists. Services are stopped first and started again afterwards; database
# passwords are re-applied from the current .env.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

dump="" yes=0
while [ $# -gt 0 ]; do
    case "$1" in
        --dump) dump="$2"; shift 2 ;;
        --latest) dump="$(newest "$BACKUPS/qts-2*.sql.gz")"; shift ;;
        --yes) yes=1; shift ;;
        *) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
    esac
done
[ -n "$dump" ] && [ -f "$dump" ] || fail "give --dump FILE or --latest (no dump found)"
gzip -t "$dump" || fail "$dump is not a valid gzip file"
[ "$yes" = 1 ] || fail "this replaces all data in the stack with $dump: repeat with --yes"
stamp="$(basename "$dump" .sql.gz)"; stamp="${stamp#qts-}"
state="$(dirname "$dump")/qts-state-${stamp}.tar.gz"

echo "== stopping services"
for service in paper-trader monitor freqtrade grafana backup-scheduler; do
    dc --profile freqtrade stop "$service" >/dev/null 2>&1 || true  # some are optional
done
dc up -d --wait postgres

echo "== restoring databases from $dump"
log="$(mktemp)"
gunzip -c "$dump" | dc exec -T postgres psql -U "$POSTGRES_USER" -d postgres -q >"$log" 2>&1 || true
unexpected="$(grep '^ERROR' "$log" | grep -vc 'current user cannot be dropped\|already exists' || true)"
[ "$unexpected" = "0" ] || { grep '^ERROR' "$log" | head -5 >&2; fail "$unexpected unexpected errors (log: $log)"; }
rm -f "$log"

if [ -f "$state" ]; then
    echo "== restoring state volume from $state"
    dc run --rm -T --no-deps --user root --entrypoint sh \
        -v "$(cd "$(dirname "$state")" && pwd):/restore:ro" paper-trader -c \
        "rm -rf /app/state/* && tar xzf /restore/$(basename "$state") -C /app && chown -R qts:qts /app/state"
else
    echo "== no state archive for this dump: state volume left as it is"
fi

echo "== re-applying roles and passwords, starting services"
dc up -d
dc run --rm -T -e QTS_LOG_LEVEL=ERROR paper-trader db current
echo "RESTORE COMPLETE from $dump"
