-- Creates or updates the application's database role.
--
-- Run as the master user by platform/db/bootstrap_app_user.py, which first
-- sets the transaction-local setting app.password. The password therefore
-- never appears in SQL text sent by the client or in the client's logs.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mobile_api_app') THEN
    EXECUTE format('ALTER ROLE mobile_api_app WITH LOGIN PASSWORD %L', current_setting('app.password'));
  ELSE
    EXECUTE format('CREATE ROLE mobile_api_app LOGIN PASSWORD %L', current_setting('app.password'));
  END IF;
END
$$;

GRANT CONNECT ON DATABASE mobileapi TO mobile_api_app;

-- Migrations run as the application role, so it must be able to create tables.
GRANT USAGE, CREATE ON SCHEMA public TO mobile_api_app;
