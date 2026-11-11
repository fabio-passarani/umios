#!/bin/sh
# One backup: all databases and roles (pg_dumpall) + the state volume (kill
# switch, Freqtrade risk state). Used by the `backup` service (one shot,
# scripts/backup.sh) and by `backup-scheduler` (every BACKUP_INTERVAL_HOURS).
# Verifies what it wrote, keeps the newest BACKUP_KEEP sets and records a
# heartbeat so monitoring notices when backups stop.
set -eu
umask 077

keep="${BACKUP_KEEP:-14}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump="/backups/qts-${stamp}.sql.gz"
state="/backups/qts-state-${stamp}.tar.gz"

beat() {  # beat <status> <detail>
    psql --quiet --dbname "$POSTGRES_DB" -v ON_ERROR_STOP=1 -v status="$1" -v detail="$2" <<'EOSQL' || true
INSERT INTO heartbeats (service, ts, status, detail) VALUES ('backup', now(), :'status', :'detail')
ON CONFLICT (service) DO UPDATE SET ts = excluded.ts, status = excluded.status, detail = excluded.detail;
EOSQL
}
fail() { echo "BACKUP FAILED: $1" >&2; rm -f "$dump.tmp" "$state.tmp"; beat error "$1"; exit 1; }

pg_dumpall --clean --if-exists | gzip > "$dump.tmp" || fail "pg_dumpall failed"
gzip -t "$dump.tmp" || fail "database dump is not a valid gzip file"
gunzip -c "$dump.tmp" | grep -q "PostgreSQL database cluster dump complete" \
    || fail "database dump is truncated"
mv "$dump.tmp" "$dump"

if [ -d /state ]; then
    tar czf "$state.tmp" -C / state || fail "state archive failed"
    gzip -t "$state.tmp" || fail "state archive is not a valid gzip file"
    mv "$state.tmp" "$state"
fi

ls -1t /backups/qts-2*.sql.gz 2>/dev/null | tail -n +"$((keep + 1))" | xargs -r rm --
ls -1t /backups/qts-state-*.tar.gz 2>/dev/null | tail -n +"$((keep + 1))" | xargs -r rm --

size="$(du -h "$dump" | cut -f1)"
beat ok "qts-${stamp} (${size})"
echo "backup written: $dump ($size)"
