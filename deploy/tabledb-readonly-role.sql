-- A SELECT-only role for the MCP server (kg_mcp.py --tabledb-dsn postgresql://cogent_ro:...@host/cogent).
-- The writer (kg_tabledb.py / kg_agent.py --tables) keeps using the owner role. No password is stored in this file: pass it as a psql variable
--   psql -U cogent -d cogent -v ro_password="$COGENT_RO_PASSWORD" -f deploy/tabledb-readonly-role.sql
-- (the compose setup does this through deploy/tabledb-init.sh). Safe to re-run: it also resets the role's password.
SELECT format('CREATE ROLE cogent_ro LOGIN PASSWORD %L', :'ro_password')
 WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cogent_ro') \gexec
SELECT format('ALTER ROLE cogent_ro PASSWORD %L', :'ro_password') \gexec
CREATE SCHEMA IF NOT EXISTS kgtables;
GRANT USAGE ON SCHEMA kgtables TO cogent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA kgtables TO cogent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA kgtables GRANT SELECT ON TABLES TO cogent_ro;   -- applies to tables created later by the owner
ALTER ROLE cogent_ro SET statement_timeout = '10s';
ALTER ROLE cogent_ro SET default_transaction_read_only = on;
