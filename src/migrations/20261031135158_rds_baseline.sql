-- Adapted from supabase/postgres 17.6.1.136 migrations/db/init-scripts (see NOTICE).
DO $$
DECLARE
  expected record;
  actual record;
BEGIN
  FOR expected IN SELECT * FROM (VALUES
    ('anon', false, false), ('authenticated', false, false), ('service_role', false, true),
    ('authenticator', true, false), ('supabase_auth_admin', true, false),
    ('supabase_storage_admin', true, false)
  ) AS roles(name, login, bypassrls)
  LOOP
    SELECT * INTO actual FROM pg_roles WHERE rolname = expected.name;
    IF NOT FOUND THEN
      EXECUTE format('CREATE ROLE %I NOINHERIT %s %s', expected.name,
        CASE WHEN expected.login THEN 'LOGIN' ELSE 'NOLOGIN' END,
        CASE WHEN expected.bypassrls THEN 'BYPASSRLS' ELSE 'NOBYPASSRLS' END);
    ELSIF actual.rolcanlogin <> expected.login OR actual.rolbypassrls <> expected.bypassrls
      OR actual.rolsuper OR actual.rolcreaterole OR actual.rolcreatedb OR actual.rolreplication THEN
      RAISE EXCEPTION 'Incompatible existing role: %', expected.name;
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname IN ('postgres', 'rds_superuser', 'supabase_admin')
      AND pg_has_role(expected.name, oid, 'MEMBER')) THEN
      RAISE EXCEPTION 'Application role has administrator membership: %', expected.name;
    END IF;
  END LOOP;
END $$;

GRANT supabase_auth_admin, supabase_storage_admin TO postgres WITH ADMIN TRUE, INHERIT FALSE, SET TRUE;
CREATE SCHEMA IF NOT EXISTS extensions AUTHORIZATION postgres;
CREATE SCHEMA IF NOT EXISTS auth AUTHORIZATION supabase_auth_admin;
CREATE SCHEMA IF NOT EXISTS storage AUTHORIZATION supabase_storage_admin;
CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA extensions;

DO $$
DECLARE
  expected record;
BEGIN
  FOR expected IN SELECT * FROM (VALUES
    ('extensions', 'postgres'), ('auth', 'supabase_auth_admin'), ('storage', 'supabase_storage_admin')
  ) AS schemas(name, owner)
  LOOP
    IF (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = expected.name) <> expected.owner THEN
      RAISE EXCEPTION 'Incompatible schema owner: %', expected.name;
    END IF;
  END LOOP;
  IF EXISTS (SELECT FROM pg_extension WHERE extname IN ('pgcrypto', 'uuid-ossp')
    AND extnamespace <> 'extensions'::regnamespace) THEN
    RAISE EXCEPTION 'Required extensions must be in the extensions schema';
  END IF;
END $$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public, extensions, auth, storage TO anon, authenticated, service_role;
GRANT CREATE ON DATABASE postgres TO supabase_storage_admin;
GRANT anon, authenticated, service_role TO authenticator WITH INHERIT FALSE, SET TRUE;
ALTER ROLE supabase_auth_admin SET search_path = auth, public, extensions;
ALTER ROLE supabase_storage_admin SET search_path = storage, public, extensions;
ALTER ROLE authenticator SET search_path = public, extensions;
ALTER ROLE anon SET statement_timeout = '3s';
ALTER ROLE authenticated SET statement_timeout = '8s';

-- Auth migrations may already have upgraded these helpers; preserve their definitions on adoption.
SET ROLE supabase_auth_admin;
DO $$
DECLARE
  helper record;
  existing record;
BEGIN
  FOR helper IN SELECT * FROM (VALUES
    ('uid', 'uuid', $body$SELECT COALESCE(NULLIF(current_setting('request.jwt.claim.sub', true), ''),
      NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')::uuid$body$),
    ('role', 'text', $body$SELECT COALESCE(NULLIF(current_setting('request.jwt.claim.role', true), ''),
      NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')$body$),
    ('email', 'text', $body$SELECT COALESCE(NULLIF(current_setting('request.jwt.claim.email', true), ''),
      NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'email')$body$),
    ('jwt', 'jsonb', $body$SELECT COALESCE(NULLIF(current_setting('request.jwt.claims', true), '')::jsonb, '{}'::jsonb)$body$)
  ) AS helpers(name, returns, body)
  LOOP
    SELECT * INTO existing FROM pg_proc WHERE oid = to_regprocedure(format('auth.%I()', helper.name));
    IF NOT FOUND THEN
      EXECUTE format('CREATE FUNCTION auth.%I() RETURNS %s LANGUAGE sql STABLE AS %L',
        helper.name, helper.returns, helper.body);
    ELSIF existing.proowner <> current_user::regrole OR existing.prorettype <> helper.returns::regtype
      OR existing.prosecdef THEN
      RAISE EXCEPTION 'Incompatible auth helper: %', helper.name;
    END IF;
  END LOOP;
END $$;
RESET ROLE;
