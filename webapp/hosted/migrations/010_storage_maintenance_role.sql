-- 010_storage_maintenance_role.sql
-- Dedicated Storage maintenance identity for private-object cleanup (Slice 6A).
--
-- Cleanup deletes a private object that a request left behind (a failed or
-- crashed correction upload, or a tombstoned generated output). Until now it
-- borrowed the historical actor's identity and minted an `app_storage` JWT for
-- them, so the delete inherited the membership contract: if that user was
-- deactivated before cleanup ran, every delete was denied and customer content
-- could remain indefinitely even though the cleanup ledger was intact.
--
-- The fix is a separate, narrower identity rather than a broader one:
--
--   * `app_storage_maintenance` is its own Postgres role. It is NOT granted to
--     `app_user`/`app_worker`, and no service-role key is introduced.
--   * Its JWT subject is the **organization id**, not a user, so authorization
--     never depends on any membership row.
--   * Its policies cover the `outputs` bucket only, restricted to the org path
--     segment the token was minted for, and only SELECT + DELETE. It cannot
--     insert or update objects, and it has no access to `transcripts`.
--
-- SELECT is required because the Storage API resolves the object row before
-- deleting it; without it a delete cannot find its target. Nothing in the
-- normal request path changes: reads, uploads, and user-facing deletes continue
-- to use `app_storage` with the active-membership contract.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'app_storage_maintenance') THEN
        CREATE ROLE app_storage_maintenance NOLOGIN NOBYPASSRLS NOINHERIT;
    ELSE
        ALTER ROLE app_storage_maintenance NOLOGIN NOBYPASSRLS NOINHERIT;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO app_storage_maintenance;

DO $$
DECLARE
    storage_exists BOOLEAN := EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'storage');
    auth_exists BOOLEAN := EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'auth');
BEGIN
    IF NOT storage_exists THEN
        RETURN;
    END IF;

    IF NOT auth_exists THEN
        RAISE EXCEPTION 'auth schema is required when storage schema is present';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'authenticator') THEN
        RAISE EXCEPTION 'authenticator role is required when storage schema is present';
    END IF;
    EXECUTE 'GRANT app_storage_maintenance TO authenticator';

    EXECUTE 'GRANT USAGE ON SCHEMA storage TO app_storage_maintenance';
    EXECUTE 'GRANT USAGE ON SCHEMA auth TO app_storage_maintenance';

    -- Delete-only maintenance: no INSERT, no UPDATE. SELECT is the lookup the
    -- Storage API performs before the delete.
    EXECUTE 'REVOKE ALL ON storage.objects FROM app_storage_maintenance';
    EXECUTE 'GRANT SELECT, DELETE ON storage.objects TO app_storage_maintenance';

    IF EXISTS (
        SELECT 1 FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE n.nspname = 'auth' AND p.proname = 'uid'
    ) THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION auth.uid() TO app_storage_maintenance';
    END IF;

    EXECUTE 'DROP POLICY IF EXISTS outputs_maintenance_select ON storage.objects';
    EXECUTE 'DROP POLICY IF EXISTS outputs_maintenance_delete ON storage.objects';

    -- The token subject is the org id, and the first path segment is the org
    -- id, so a maintenance token issued for one organization cannot reach
    -- another organization's objects or any other bucket.
    EXECUTE 'CREATE POLICY outputs_maintenance_select ON storage.objects
        FOR SELECT TO app_storage_maintenance
        USING (
            bucket_id = ''outputs''
            AND (storage.foldername(name))[1]::uuid = auth.uid()
        )';

    EXECUTE 'CREATE POLICY outputs_maintenance_delete ON storage.objects
        FOR DELETE TO app_storage_maintenance
        USING (
            bucket_id = ''outputs''
            AND (storage.foldername(name))[1]::uuid = auth.uid()
        )';
END $$;
