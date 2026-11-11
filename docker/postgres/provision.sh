#!/bin/sh
# Roles, databases and privileges. Idempotent: runs when the PostgreSQL volume is
# first created (as an init script) and again on every `docker compose up` (the
# db-setup service), so an existing installation picks up changes and rotated
# passwords without being re-created.
#
#   <POSTGRES_USER>  administrator: owns the schema, runs migrations and backups.
#                    Never used by a long-running service.
#   qts_app          the platform (paper trader, monitor, CLI, Freqtrade's risk
#                    journal): SELECT/INSERT/UPDATE/DELETE on the tables, nothing
#                    else. No DDL, no TRUNCATE, no other database, not a superuser.
#   freqtrade_app    owns the separate `freqtrade` database (Freqtrade creates and
#                    migrates its own tables); cannot connect to the platform one.
#   grafana_ro       SELECT only, on the platform database only.
set -eu

: "${POSTGRES_USER:?}" "${POSTGRES_DB:?}"
: "${QTS_APP_DB_PASSWORD:?set QTS_APP_DB_PASSWORD}"
: "${FREQTRADE_DB_PASSWORD:?set FREQTRADE_DB_PASSWORD}"
: "${GRAFANA_DB_PASSWORD:?set GRAFANA_DB_PASSWORD}"

psql -v ON_ERROR_STOP=1 --quiet --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v app_password="$QTS_APP_DB_PASSWORD" \
     -v freqtrade_password="$FREQTRADE_DB_PASSWORD" \
     -v grafana_password="$GRAFANA_DB_PASSWORD" <<'EOSQL'
SELECT format('CREATE ROLE %I', r)
  FROM (VALUES ('qts_app'), ('freqtrade_app'), ('grafana_ro')) AS wanted(r)
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) \gexec

ALTER ROLE qts_app       LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'app_password';
ALTER ROLE freqtrade_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'freqtrade_password';
ALTER ROLE grafana_ro    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'grafana_password';

SELECT 'CREATE DATABASE freqtrade OWNER freqtrade_app'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'freqtrade') \gexec
ALTER DATABASE freqtrade OWNER TO freqtrade_app;

-- Nobody connects anywhere by default; each role gets exactly its database.
REVOKE ALL ON DATABASE freqtrade FROM PUBLIC;
REVOKE ALL ON DATABASE postgres  FROM PUBLIC;
REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;
REVOKE ALL ON DATABASE :"DBNAME" FROM qts_app, grafana_ro, freqtrade_app;
GRANT CONNECT ON DATABASE :"DBNAME" TO qts_app, grafana_ro;

-- Platform schema: the administrator creates objects, the others only use them.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM qts_app, grafana_ro;
GRANT USAGE ON SCHEMA public TO qts_app, grafana_ro;

REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM qts_app, grafana_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM qts_app, grafana_ro;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO qts_app;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA public TO qts_app;
GRANT SELECT                         ON ALL TABLES    IN SCHEMA public TO grafana_ro;

-- Tables the administrator creates later (migrations) get the same grants.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO qts_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO qts_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;

-- The schema version is read-only for the application.
DO $$
BEGIN
  IF to_regclass('public.alembic_version') IS NOT NULL THEN
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.alembic_version FROM qts_app;
  END IF;
END
$$;
EOSQL
# Freqtrade's database: on installations that pre-date freqtrade_app its tables
# were created by the administrator. Hand them over, or the bot cannot migrate them.
psql -v ON_ERROR_STOP=1 --quiet --username "$POSTGRES_USER" --dbname freqtrade <<'EOSQL'
DO $$
DECLARE obj record;
BEGIN
  FOR obj IN
    SELECT c.oid::regclass AS name, c.relkind
      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v')  -- owned sequences follow their table
       AND pg_get_userbyid(c.relowner) <> 'freqtrade_app'
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO freqtrade_app',
                   CASE obj.relkind WHEN 'v' THEN 'VIEW' ELSE 'TABLE' END, obj.name);
  END LOOP;
END
$$;
GRANT ALL ON SCHEMA public TO freqtrade_app;
EOSQL
echo "database roles and privileges are in place"
