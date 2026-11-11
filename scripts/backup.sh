#!/usr/bin/env bash
# Back up operational data: PostgreSQL dump + platform state (kill switch, etc.).
# Usage: scripts/backup.sh            (run from the repository root)
# Restore: gunzip -c backups/qts-<stamp>.sql.gz | docker compose exec -T postgres psql -U "$POSTGRES_USER" postgres
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p backups
chmod 700 backups

docker compose run --rm backup

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
docker compose run --rm --no-deps --entrypoint tar -v "$(pwd)/backups:/backups" paper-trader \
    czf "/backups/qts-state-${stamp}.tar.gz" -C /app state
echo "Backups in $(pwd)/backups (git-ignored). Copy them off this machine."
