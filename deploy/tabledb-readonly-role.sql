-- A SELECT-only role for the MCP server (kg_mcp.py --tabledb-dsn postgresql://cogent_ro:...@host/cogent).
-- The writer (kg_tabledb.py / kg_agent.py --tables) keeps using the owner role. Run as the database owner after the first build,
-- or keep it in docker-entrypoint-initdb.d and re-run the GRANTs after the schema exists.
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'cogent_ro') THEN
    CREATE ROLE cogent_ro LOGIN PASSWORD 'change-me-too';
  END IF;
END $$;
CREATE SCHEMA IF NOT EXISTS kgtables;
GRANT USAGE ON SCHEMA kgtables TO cogent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA kgtables TO cogent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA kgtables GRANT SELECT ON TABLES TO cogent_ro;   -- applies to tables created later by the owner
ALTER ROLE cogent_ro SET statement_timeout = '10s';
ALTER ROLE cogent_ro SET default_transaction_read_only = on;
