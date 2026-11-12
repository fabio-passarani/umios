#!/usr/bin/env bash
# Prepare a machine (your computer or a VPS) for the Docker stack. Safe to run again.
#   - creates .env from the example with a random value for every secret;
#   - creates ./backups (private) and ./reports (writable by the containers).
# It starts nothing and never overwrites an existing .env.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v docker >/dev/null || { echo "docker is not installed" >&2; exit 1; }
docker compose version >/dev/null || { echo "the docker compose plugin is missing" >&2; exit 1; }

if [ -f .env ]; then
    echo ".env already exists: left untouched"
else
    umask 077
    while IFS= read -r line; do
        case "$line" in
            POSTGRES_PASSWORD=*|QTS_APP_DB_PASSWORD=*|FREQTRADE_DB_PASSWORD=*|GRAFANA_DB_PASSWORD=*|\
            GRAFANA_ADMIN_PASSWORD=*|FREQTRADE_API_PASSWORD=*|FREQTRADE_JWT_SECRET=*)
                # hex only: safe inside URLs and shell
                printf '%s=%s\n' "${line%%=*}" "$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')" ;;
            *) printf '%s\n' "$line" ;;
        esac
    done < .env.example > .env
    echo ".env created with random secrets (mode 600). Keep a copy somewhere safe."
fi

mkdir -p backups reports/daily
chmod 700 backups
# The containers run as an unprivileged user that is not your user: they need
# to be able to write reports here. Reports contain no secrets.
chmod a+rwx reports reports/daily
if grep -v "^#" .env | grep -q "change-me"; then
    echo "WARNING: .env still contains 'change-me' placeholders: replace them." >&2
fi
echo "Ready. Next: docker compose up -d --build   (see docs/paper-trading-runbook.md)"
