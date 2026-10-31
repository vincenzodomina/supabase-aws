-- Adapted from supabase/postgres 17.6.1.136 migrations/db/migrations (see NOTICE).
-- 20230529180330_alter_api_roles_for_inherit; 20231013070755_grant_authenticator_to_supabase_storage_admin.
ALTER ROLE anon INHERIT;
ALTER ROLE authenticated INHERIT;
ALTER ROLE service_role INHERIT;
ALTER ROLE authenticator NOINHERIT;
ALTER ROLE supabase_auth_admin NOINHERIT;
ALTER ROLE supabase_storage_admin NOINHERIT;
GRANT authenticator TO supabase_storage_admin WITH INHERIT FALSE, SET TRUE;
REVOKE anon, authenticated, service_role FROM supabase_storage_admin;

-- 20220322085208, 20221028101028, 20231130133139: service transaction and API query limits.
ALTER ROLE supabase_auth_admin SET idle_in_transaction_session_timeout = '60s';
ALTER ROLE authenticator SET statement_timeout = '8s';
ALTER ROLE authenticator SET lock_timeout = '8s';
-- 20250205060043_disable_log_statement_on_internal_roles.
ALTER ROLE supabase_auth_admin SET log_statement = 'none';
ALTER ROLE supabase_storage_admin SET log_statement = 'none';

-- 20250605172253: PostgreSQL 16+ role administration; no distribution-only predefined roles.
GRANT anon, authenticated, service_role, authenticator TO postgres WITH ADMIN TRUE, INHERIT FALSE, SET TRUE;

-- 20250421084701: service-owned schemas and histories. Password synchronization still needs ADMIN.
SET ROLE supabase_auth_admin;
REVOKE CREATE ON SCHEMA auth FROM postgres;
DO $$ BEGIN
  IF to_regclass('auth.schema_migrations') IS NOT NULL THEN
    REVOKE ALL ON auth.schema_migrations FROM postgres;
  END IF;
END $$;
RESET ROLE;

SET ROLE supabase_storage_admin;
REVOKE CREATE ON SCHEMA storage FROM postgres;
-- Storage's own migrations grant object access; broad defaults would expose its migration history.
ALTER DEFAULT PRIVILEGES IN SCHEMA storage REVOKE ALL ON TABLES FROM postgres, anon, authenticated, service_role;
DO $$ BEGIN
  IF to_regclass('storage.migrations') IS NOT NULL THEN
    REVOKE ALL ON storage.migrations FROM postgres, anon, authenticated, service_role;
  END IF;
END $$;
-- 20250623125453 / 20250709135250: Studio access without service-schema ownership.
GRANT USAGE ON SCHEMA storage TO postgres WITH GRANT OPTION;
DO $$ BEGIN
  IF to_regclass('storage.buckets') IS NOT NULL THEN
    GRANT ALL ON storage.buckets TO postgres WITH GRANT OPTION;
  END IF;
  IF to_regclass('storage.objects') IS NOT NULL THEN
    GRANT ALL ON storage.objects TO postgres WITH GRANT OPTION;
  END IF;
END $$;
RESET ROLE;
GRANT supabase_auth_admin, supabase_storage_admin TO postgres WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;

-- 20220321174452_fix-postgrest-alter-type-event-trigger (supersedes 20220126121436).
CREATE OR REPLACE FUNCTION extensions.pgrst_ddl_watch() RETURNS event_trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
  cmd record;
BEGIN
  FOR cmd IN SELECT * FROM pg_event_trigger_ddl_commands()
  LOOP
    IF cmd.command_tag IN (
      'CREATE SCHEMA', 'ALTER SCHEMA', 'CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO', 'ALTER TABLE',
      'CREATE FOREIGN TABLE', 'ALTER FOREIGN TABLE', 'CREATE VIEW', 'ALTER VIEW',
      'CREATE MATERIALIZED VIEW', 'ALTER MATERIALIZED VIEW', 'CREATE FUNCTION', 'ALTER FUNCTION',
      'CREATE TRIGGER', 'CREATE TYPE', 'ALTER TYPE', 'CREATE RULE', 'COMMENT'
    ) AND cmd.schema_name IS DISTINCT FROM 'pg_temp' THEN
      NOTIFY pgrst, 'reload schema';
    END IF;
  END LOOP;
END $$;

CREATE OR REPLACE FUNCTION extensions.pgrst_drop_watch() RETURNS event_trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE
  obj record;
BEGIN
  FOR obj IN SELECT * FROM pg_event_trigger_dropped_objects()
  LOOP
    IF obj.object_type IN ('schema', 'table', 'foreign table', 'view', 'materialized view',
      'function', 'trigger', 'type', 'rule') AND obj.is_temporary IS FALSE THEN
      NOTIFY pgrst, 'reload schema';
    END IF;
  END LOOP;
END $$;

DROP EVENT TRIGGER IF EXISTS pgrst_ddl_watch;
CREATE EVENT TRIGGER pgrst_ddl_watch ON ddl_command_end EXECUTE FUNCTION extensions.pgrst_ddl_watch();
DROP EVENT TRIGGER IF EXISTS pgrst_drop_watch;
CREATE EVENT TRIGGER pgrst_drop_watch ON sql_drop EXECUTE FUNCTION extensions.pgrst_drop_watch();
