CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA supabase_aws;

DO $$ BEGIN
  IF (SELECT extnamespace FROM pg_extension WHERE extname = 'pg_stat_statements') <> 'supabase_aws'::regnamespace THEN
    RAISE EXCEPTION 'pg_stat_statements must be in the private supabase_aws schema';
  END IF;
END $$;

-- Diagnostics may contain sensitive query text; neither views nor functions belong in the API.
REVOKE ALL ON ALL TABLES IN SCHEMA supabase_aws
  FROM PUBLIC, anon, authenticated, service_role, authenticator, supabase_auth_admin, supabase_storage_admin;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA supabase_aws
  FROM PUBLIC, anon, authenticated, service_role, authenticator, supabase_auth_admin, supabase_storage_admin;
