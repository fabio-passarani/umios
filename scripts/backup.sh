#!/usr/bin/env bash
# One backup, now: every database and role (pg_dumpall) plus the state volume
# (kill switch, Freqtrade risk state) into ./backups. The stack also does this by
# itself every BACKUP_INTERVAL_HOURS (service backup-scheduler).
#
#   scripts/backup.sh            take a backup
#   scripts/restore_test.sh      prove the newest backup can be restored
#   scripts/restore.sh --help    restore a backup into the running stack
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p backups
chmod 700 backups

docker compose run --rm backup
echo "Backups in $(pwd)/backups (git-ignored). Copy them off this machine."
