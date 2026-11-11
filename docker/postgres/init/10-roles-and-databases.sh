#!/bin/sh
# Runs once, when the PostgreSQL volume is first created.
#  - a separate database for Freqtrade's own trade table (dry-run)
#  - a read-only role for Grafana: the dashboard can never modify trading data
set -eu

psql -v ON_ERROR_STOP=1 -v grafana_password="$GRAFANA_DB_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'
CREATE DATABASE freqtrade;
CREATE ROLE grafana_ro LOGIN PASSWORD :'grafana_password';
GRANT CONNECT ON DATABASE :"DBNAME" TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
EOSQL
