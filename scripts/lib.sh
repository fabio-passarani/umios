# Shared by the operational scripts. Source it; do not execute it.
# QTS_COMPOSE_ARGS lets a caller address another compose project, e.g.
#   QTS_COMPOSE_ARGS="-p qts-e2e -f docker-compose.yml -f docker-compose.e2e.yml"
cd "$(dirname "${BASH_SOURCE[0]}")/.."
[ -f .env ] || { echo "no .env here: copy .env.example and fill it in" >&2; exit 1; }
set -a; . ./.env; set +a

dc() {  # docker compose for the selected project
    # shellcheck disable=SC2086
    docker compose ${QTS_COMPOSE_ARGS:-} "$@"
}
fail() { echo "FAILED: $*" >&2; exit 1; }

# Where backups live on the host (the compose file reads the same variable).
export QTS_BACKUP_DIR="${QTS_BACKUP_DIR:-./backups}"
BACKUPS="${QTS_BACKUP_DIR#./}"

# Tables whose content must survive a restore byte for byte.
QTS_TABLES="runs equity_points fills trades positions risk_decisions risk_events paper_state"

fingerprint_sql() {  # one line per table: name count md5-of-all-rows
    local table sql=""
    for table in $QTS_TABLES; do
        sql+="SELECT '$table', count(*), coalesce(md5(string_agg(t::text, E'\\n' ORDER BY t::text)), '-') FROM $table t;"
    done
    printf '%s' "$sql"
}
newest() { ls -1t $1 2>/dev/null | head -1; }  # newest file matching a glob
