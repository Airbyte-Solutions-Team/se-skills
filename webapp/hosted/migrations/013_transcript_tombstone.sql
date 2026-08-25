-- 013_transcript_tombstone.sql
-- Transcript deletion tombstone + durable Storage reconciliation (Slice 6B2A1).
--
-- Before this migration, deleting a transcript deleted the private Storage
-- object first and the metadata row second, in two separate operations with no
-- shared transaction. A failure between them left either a listable transcript
-- whose bytes were gone, or bytes with no deletion record. Postgres and Supabase
-- Storage cannot share a transaction, so deletion becomes:
--
--     authorize -> durably tombstone/hide (one transaction, with its audit event)
--                -> asynchronously reconcile the exact Storage object
--                -> mark cleanup complete
--
-- The database is the single source of truth for both deletion intent
-- (`tombstoned_at`) and physical reconciliation (`cleanup_state`). A tombstoned
-- transcript is immediately invisible to every user-facing path and can never be
-- the input of a new job, while its row survives as the provenance anchor that
-- terminal jobs, outputs, reviews, exports, and audit events already reference.
--
-- Retention/purge of tombstoned metadata is deliberately NOT in this slice; the
-- row is kept, not scheduled for erasure.

-- ---------------------------------------------------------------------------
-- Tombstone and cleanup state
-- ---------------------------------------------------------------------------
-- `tombstoned_at` is both the deletion-request time and the visibility
-- invariant: a user-visible transcript is one with no tombstone. Cleanup columns
-- carry the same lease/attempt shape as the output and correction cleanup
-- ledgers, so a failed or ambiguous Storage delete stays retryable and a
-- restarted worker rediscovers the row.
ALTER TABLE public.transcripts
    ADD COLUMN IF NOT EXISTS tombstoned_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS delete_requested_by UUID REFERENCES public.users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS cleanup_state TEXT NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS cleanup_completed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cleanup_attempts INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cleanup_claimed_by TEXT,
    ADD COLUMN IF NOT EXISTS cleanup_claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cleanup_last_error TEXT;

ALTER TABLE public.transcripts DROP CONSTRAINT IF EXISTS transcripts_cleanup_state_check;
ALTER TABLE public.transcripts ADD CONSTRAINT transcripts_cleanup_state_check
    CHECK (cleanup_state IN ('none', 'pending', 'complete'));

-- A live transcript has no cleanup state, and cleanup state only exists for a
-- tombstoned one, so "hidden" and "being reconciled" cannot drift apart.
ALTER TABLE public.transcripts DROP CONSTRAINT IF EXISTS transcripts_tombstone_cleanup_check;
ALTER TABLE public.transcripts ADD CONSTRAINT transcripts_tombstone_cleanup_check
    CHECK (
        (tombstoned_at IS NULL AND cleanup_state = 'none' AND cleanup_completed_at IS NULL)
        OR (tombstoned_at IS NOT NULL AND cleanup_state IN ('pending', 'complete'))
    );

-- Partial index for the worker's claim scan; live transcripts stay out of it.
CREATE INDEX IF NOT EXISTS idx_transcripts_cleanup_pending
    ON public.transcripts(tombstoned_at)
    WHERE cleanup_state = 'pending';

-- ---------------------------------------------------------------------------
-- Visibility: tombstoned rows are invisible to `app_user`
-- ---------------------------------------------------------------------------
-- Migration 002 granted `app_user` a single `FOR ALL` policy plus SELECT,
-- INSERT, and DELETE. Deletion is now a logical mutation owned by the
-- SECURITY DEFINER function below, so the direct DELETE privilege is withdrawn
-- and the remaining policies exclude tombstones. Ordinary RLS therefore hides a
-- tombstoned transcript from list, load, download, and any ad-hoc SELECT — the
-- application does not have to remember to filter.
DROP POLICY IF EXISTS org_tenant_transcripts ON public.transcripts;

DROP POLICY IF EXISTS org_tenant_transcripts_select ON public.transcripts;
CREATE POLICY org_tenant_transcripts_select ON public.transcripts
    FOR SELECT TO app_user
    USING (public.is_active_org_member(org_id) AND tombstoned_at IS NULL);

DROP POLICY IF EXISTS org_tenant_transcripts_insert ON public.transcripts;
CREATE POLICY org_tenant_transcripts_insert ON public.transcripts
    FOR INSERT TO app_user
    WITH CHECK (public.is_active_org_member(org_id) AND tombstoned_at IS NULL);

REVOKE DELETE, UPDATE ON TABLE public.transcripts FROM app_user;

-- The worker never touches this table directly; it reaches it only through the
-- claim/finalize/release functions below.
REVOKE ALL ON TABLE public.transcripts FROM app_worker;

-- A physical row delete may only remove a tombstone whose Storage object has
-- actually been reconciled, so no privileged path (retention, repair, or a
-- future purge slice) can turn a live transcript into a silent hard delete that
-- skips the tombstone, its audit event, and the Storage cleanup it schedules.
CREATE OR REPLACE FUNCTION app_private.assert_transcript_delete_is_reconciled()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF OLD.tombstoned_at IS NULL OR OLD.cleanup_state <> 'complete' THEN
        RAISE EXCEPTION 'transcript must be tombstoned and reconciled before physical deletion'
            USING ERRCODE = 'SE022';
    END IF;
    RETURN OLD;
END;
$$;

ALTER FUNCTION app_private.assert_transcript_delete_is_reconciled() OWNER TO app_admin;
REVOKE ALL ON FUNCTION app_private.assert_transcript_delete_is_reconciled() FROM PUBLIC;

DROP TRIGGER IF EXISTS transcripts_guard_physical_delete ON public.transcripts;
CREATE TRIGGER transcripts_guard_physical_delete
    BEFORE DELETE ON public.transcripts
    FOR EACH ROW EXECUTE FUNCTION app_private.assert_transcript_delete_is_reconciled();

-- ---------------------------------------------------------------------------
-- Audit semantics
-- ---------------------------------------------------------------------------
-- Migration 012 audited `transcript_delete` from an AFTER DELETE trigger. With
-- logical deletion there is no row delete to hang it on, and a physical delete
-- now happens on a privileged reconciliation/retention path where there is no
-- authenticated user action to attribute. The event is therefore written inside
-- `request_transcript_deletion`, in the transaction that tombstones the row, and
-- its meaning is stated precisely:
--
--     transcript_delete = a user's deletion request was accepted and the
--                         transcript is hidden. It is NOT proof that the
--                         private Storage object has already been purged;
--                         `cleanup_state = 'complete'` is that proof.
--
-- No second event is recorded when cleanup finishes, and dropping the trigger
-- means a later privileged hard delete cannot fabricate a user deletion event.
-- The INSERT trigger (`transcript_upload`) is unchanged.
DROP TRIGGER IF EXISTS audit_transcript_delete ON public.transcripts;

-- ---------------------------------------------------------------------------
-- Logical deletion request
-- ---------------------------------------------------------------------------
-- SQLSTATEs:
--   SE020  transcript not accessible (missing, wrong account/opportunity,
--          another organization, or no active membership — indistinguishable)
--   SE021  a queued or running job still references the transcript
--   SE023  enqueue attempted against a tombstoned transcript
CREATE OR REPLACE FUNCTION public.request_transcript_deletion(
    p_context_token TEXT,
    p_account_id UUID,
    p_opportunity_id UUID,
    p_transcript_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_row public.transcripts;
    v_user_id UUID;
BEGIN
    v_user_id := (SELECT user_id FROM app_private.verify_context_token(p_context_token));
    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'transcript not accessible' USING ERRCODE = 'SE020';
    END IF;

    -- Lock the transcript first and derive the organization from the trusted
    -- row, never from the request. Every authorization failure below raises the
    -- same error, so a cross-organization id is indistinguishable from a
    -- non-existent one. This is also the lock that serializes deletion against
    -- `enqueue_job` (which takes a share lock on the same row) and against a
    -- concurrent duplicate deletion request.
    SELECT t.* INTO v_row
    FROM public.transcripts t
    WHERE t.id = p_transcript_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'transcript not accessible' USING ERRCODE = 'SE020';
    END IF;

    IF v_row.account_id <> p_account_id
        OR (p_opportunity_id IS NULL AND v_row.opportunity_id IS NOT NULL)
        OR (p_opportunity_id IS NOT NULL AND v_row.opportunity_id IS DISTINCT FROM p_opportunity_id) THEN
        RAISE EXCEPTION 'transcript not accessible' USING ERRCODE = 'SE020';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM public.memberships m
        WHERE m.user_id = v_user_id
          AND m.org_id = v_row.org_id
          AND m.active = true
    ) THEN
        RAISE EXCEPTION 'transcript not accessible' USING ERRCODE = 'SE020';
    END IF;

    -- Replay: the tombstone already exists, so the request is idempotent and
    -- records no second event. Cleanup state is reported as-is; a replay never
    -- claims the object is gone.
    IF v_row.tombstoned_at IS NOT NULL THEN
        RETURN jsonb_build_object(
            'transcript_id', v_row.id,
            'status', 'already_deleted',
            'tombstoned_at', v_row.tombstoned_at,
            'cleanup_state', v_row.cleanup_state
        );
    END IF;

    -- Active work blocks logical deletion: deleting the input of a queued or
    -- running job would leave the job pointing at content it may still need.
    -- Deletion never implicitly cancels a job; the user cancels first. Terminal
    -- jobs (success/failure/timeout/cancelled) keep referencing the tombstoned
    -- transcript UUID and do not block deletion.
    IF EXISTS (
        SELECT 1 FROM public.jobs j
        WHERE j.transcript_id = v_row.id
          AND j.org_id = v_row.org_id
          AND j.status IN ('queued', 'running')
    ) THEN
        RAISE EXCEPTION 'transcript has active jobs' USING ERRCODE = 'SE021';
    END IF;

    UPDATE public.transcripts t
    SET tombstoned_at = now(),
        delete_requested_by = v_user_id,
        cleanup_state = 'pending',
        cleanup_attempts = 0,
        cleanup_claimed_by = NULL,
        cleanup_claimed_at = NULL,
        cleanup_last_error = NULL,
        updated_at = now()
    WHERE t.id = v_row.id;

    -- Same metadata contract as migration 012: safe server-side UUIDs only. No
    -- filename, size, Storage path, or transcript content.
    PERFORM app_private.record_audit_event(
        v_row.org_id,
        v_user_id,
        'transcript_delete',
        'transcripts',
        v_row.id,
        NULL,
        jsonb_build_object(
            'transcript_id', v_row.id,
            'account_id', v_row.account_id,
            'opportunity_id', v_row.opportunity_id
        )
    );

    RETURN jsonb_build_object(
        'transcript_id', v_row.id,
        'status', 'accepted',
        'cleanup_state', 'pending'
    );
END;
$$;

ALTER FUNCTION public.request_transcript_deletion(TEXT, UUID, UUID, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.request_transcript_deletion(TEXT, UUID, UUID, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.request_transcript_deletion(TEXT, UUID, UUID, UUID) TO app_user;

-- ---------------------------------------------------------------------------
-- Enqueue rejects tombstoned transcripts at the database boundary
-- ---------------------------------------------------------------------------
-- Same body as migration 012 with one change: the transcript lookup takes a
-- share lock and rejects a tombstoned row. The share lock is what makes the
-- delete/enqueue race have exactly one winner — deletion's `FOR UPDATE` waits
-- for an in-flight enqueue to commit and then sees its queued job (SE021),
-- while an enqueue that starts after the tombstone commits is rejected here.
-- Lock order is the same in both functions (transcript, then job rows), so the
-- pair cannot deadlock.
CREATE OR REPLACE FUNCTION public.enqueue_job(
    p_context_token TEXT,
    p_account_id UUID,
    p_transcript_id UUID,
    p_opportunity_id UUID,
    p_skill TEXT,
    p_skill_version TEXT,
    p_model TEXT,
    p_runtime_version TEXT,
    p_idempotency_key TEXT,
    p_max_attempts INTEGER,
    p_timeout_seconds INTEGER,
    p_payload JSONB,
    p_input_refs JSONB,
    p_source_manifest JSONB
)
RETURNS TABLE(job_id UUID, job_status TEXT, job_created_at TIMESTAMPTZ)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_org_id UUID;
    v_user_id UUID;
    v_existing RECORD;
    v_job_id UUID;
    v_job_status TEXT;
    v_created TIMESTAMPTZ;
    v_tombstoned_at TIMESTAMPTZ;
    v_null_uuid UUID := '00000000-0000-0000-0000-000000000000'::uuid;
BEGIN
    -- Resolve the account's organization first; all relationships must match it.
    SELECT a.org_id INTO v_org_id
    FROM public.accounts a
    WHERE a.id = p_account_id;

    IF v_org_id IS NULL THEN
        RAISE EXCEPTION 'Account not found';
    END IF;

    v_user_id := public.require_active_member_for_org(p_context_token, v_org_id);

    -- Validate transcript belongs to the account and organization, and is not
    -- logically deleted. A tombstoned transcript is rejected here and not only
    -- in the API, so no caller of this function can enqueue work against
    -- content that is being reconciled away.
    SELECT t.tombstoned_at INTO v_tombstoned_at
    FROM public.transcripts t
    WHERE t.id = p_transcript_id
      AND t.account_id = p_account_id
      AND t.org_id = v_org_id
    FOR SHARE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Transcript not found for this account';
    END IF;
    IF v_tombstoned_at IS NOT NULL THEN
        RAISE EXCEPTION 'Transcript has been deleted' USING ERRCODE = 'SE023';
    END IF;

    -- Validate optional opportunity belongs to the account and organization.
    IF p_opportunity_id IS NOT NULL THEN
        PERFORM 1
        FROM public.opportunities o
        WHERE o.id = p_opportunity_id
          AND o.account_id = p_account_id
          AND o.org_id = v_org_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Opportunity not found for this account';
        END IF;
    END IF;

    -- Idempotency: atomic insert-or-select with a full request fingerprint.
    -- The unique index serializes concurrent requests with the same key, so
    -- only the first insert wins; the loser selects and compares the stored row.
    IF p_idempotency_key IS NOT NULL THEN
        INSERT INTO public.jobs (
            org_id, account_id, opportunity_id, transcript_id, requester_id,
            skill, skill_version, model, runtime_version, status,
            payload, input_refs, source_manifest, max_attempts, timeout_at, idempotency_key
        ) VALUES (
            v_org_id, p_account_id, p_opportunity_id, p_transcript_id, v_user_id,
            p_skill, p_skill_version, p_model, p_runtime_version, 'queued',
            p_payload, p_input_refs, p_source_manifest,
            COALESCE(p_max_attempts, 3),
            NULL,
            p_idempotency_key
        )
        ON CONFLICT (org_id, idempotency_key) DO NOTHING
        RETURNING id, jobs.status, jobs.created_at
          INTO v_job_id, v_job_status, v_created;

        IF v_job_id IS NOT NULL THEN
            PERFORM app_private.record_job_action_audit(
                v_org_id, v_user_id, 'job_run_requested', v_job_id, p_account_id,
                p_transcript_id, p_opportunity_id
            );
            job_id := v_job_id;
            job_status := v_job_status;
            job_created_at := v_created;
            RETURN NEXT;
            RETURN;
        END IF;

        SELECT id, status, created_at, account_id, transcript_id, opportunity_id,
               skill, skill_version, model, runtime_version, max_attempts,
               payload, input_refs, source_manifest
          INTO v_existing
        FROM public.jobs
        WHERE org_id = v_org_id AND idempotency_key = p_idempotency_key;

        IF v_existing IS NULL THEN
            RAISE EXCEPTION 'Idempotency race: existing row disappeared';
        END IF;

        IF v_existing.account_id <> p_account_id
            OR v_existing.transcript_id <> p_transcript_id
            OR COALESCE(v_existing.opportunity_id, v_null_uuid) <> COALESCE(p_opportunity_id, v_null_uuid)
            OR v_existing.skill <> p_skill
            OR v_existing.skill_version <> p_skill_version
            OR COALESCE(v_existing.model, '') <> COALESCE(p_model, '')
            OR COALESCE(v_existing.runtime_version, '') <> COALESCE(p_runtime_version, '')
            OR v_existing.max_attempts <> COALESCE(p_max_attempts, 3)
            OR v_existing.payload <> p_payload
            OR v_existing.input_refs <> p_input_refs
            OR v_existing.source_manifest <> p_source_manifest THEN
            RAISE EXCEPTION 'Idempotency key conflict: same key reused with different scope'
                USING ERRCODE = '40901';
        END IF;

        job_id := v_existing.id;
        job_status := v_existing.status;
        job_created_at := v_existing.created_at;
        RETURN NEXT;
        RETURN;
    END IF;

    INSERT INTO public.jobs (
        org_id, account_id, opportunity_id, transcript_id, requester_id,
        skill, skill_version, model, runtime_version, status,
        payload, input_refs, source_manifest, max_attempts, timeout_at, idempotency_key
    ) VALUES (
        v_org_id, p_account_id, p_opportunity_id, p_transcript_id, v_user_id,
        p_skill, p_skill_version, p_model, p_runtime_version, 'queued',
        p_payload, p_input_refs, p_source_manifest,
        COALESCE(p_max_attempts, 3),
        NULL,
        p_idempotency_key
    )
    RETURNING id, jobs.status AS job_status, jobs.created_at AS job_created_at
      INTO v_job_id, v_job_status, v_created;

    PERFORM app_private.record_job_action_audit(
        v_org_id, v_user_id, 'job_run_requested', v_job_id, p_account_id,
        p_transcript_id, p_opportunity_id
    );

    job_id := v_job_id;
    job_status := v_job_status;
    job_created_at := v_created;
    RETURN NEXT;
    RETURN;
END;
$$;

ALTER FUNCTION public.enqueue_job(TEXT, UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, INTEGER, JSONB, JSONB, JSONB) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.enqueue_job(TEXT, UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, INTEGER, JSONB, JSONB, JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.enqueue_job(TEXT, UUID, UUID, UUID, TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, INTEGER, JSONB, JSONB, JSONB) TO app_user;

-- ---------------------------------------------------------------------------
-- Worker reconciliation (claim / finalize / release)
-- ---------------------------------------------------------------------------
-- The worker receives only trusted identifiers — transcript id, organization id,
-- and the Storage path the server generated at upload time — and has no direct
-- privilege on `public.transcripts`, `public.memberships`, `public.audit_events`,
-- or the Storage tables. Authorization for the object delete comes from an
-- exact-target `{org, bucket, path}` maintenance credential minted server-side,
-- so cleanup never depends on the requester still being an active member.
CREATE OR REPLACE FUNCTION public.claim_next_transcript_cleanup(
    p_worker_id TEXT,
    p_lease_seconds INTEGER DEFAULT 60,
    p_max_attempts INTEGER DEFAULT 10
)
RETURNS TABLE(
    transcript_id UUID,
    org_id UUID,
    storage_path TEXT,
    cleanup_attempts INTEGER
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_id UUID;
BEGIN
    IF p_worker_id IS NULL OR length(p_worker_id) = 0 THEN
        RAISE EXCEPTION 'worker id is required' USING ERRCODE = 'SE005';
    END IF;
    IF p_lease_seconds IS NULL OR p_lease_seconds <= 0 OR p_lease_seconds > 3600 THEN
        RAISE EXCEPTION 'lease must be between 1 and 3600 seconds' USING ERRCODE = 'SE005';
    END IF;

    -- A claim is a lease: another worker (or a restart of this one) cannot
    -- delete the same object concurrently, and an expired lease makes the row
    -- claimable again, which is what makes cleanup survive crashes.
    SELECT t.id INTO v_id
    FROM public.transcripts t
    WHERE t.cleanup_state = 'pending'
      AND t.tombstoned_at IS NOT NULL
      AND t.cleanup_attempts < p_max_attempts
      AND (t.cleanup_claimed_at IS NULL
           OR t.cleanup_claimed_at < now() - make_interval(secs => p_lease_seconds))
    ORDER BY t.tombstoned_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1;

    IF v_id IS NULL THEN
        RETURN;
    END IF;

    UPDATE public.transcripts t
    SET cleanup_attempts = t.cleanup_attempts + 1,
        cleanup_claimed_by = p_worker_id,
        cleanup_claimed_at = now()
    WHERE t.id = v_id;

    RETURN QUERY
    SELECT t.id, t.org_id, t.storage_path, t.cleanup_attempts
    FROM public.transcripts t
    WHERE t.id = v_id;
END;
$$;

ALTER FUNCTION public.claim_next_transcript_cleanup(TEXT, INTEGER, INTEGER) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.claim_next_transcript_cleanup(TEXT, INTEGER, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.claim_next_transcript_cleanup(TEXT, INTEGER, INTEGER) TO app_worker;

-- Physical reconciliation is finished: the object is gone (deleted now, or
-- already absent). The row stays as the tombstoned provenance anchor; only
-- `cleanup_state` moves. No audit event is written here — `transcript_delete`
-- already recorded the user's action, and cleanup is not a user action.
CREATE OR REPLACE FUNCTION public.finalize_transcript_cleanup(
    p_transcript_id UUID,
    p_worker_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    UPDATE public.transcripts
    SET cleanup_state = 'complete',
        cleanup_completed_at = now(),
        cleanup_claimed_by = NULL,
        cleanup_claimed_at = NULL,
        cleanup_last_error = NULL,
        updated_at = now()
    WHERE id = p_transcript_id
      AND tombstoned_at IS NOT NULL
      AND cleanup_state = 'pending'
      AND cleanup_claimed_by = p_worker_id;
    RETURN FOUND;
END;
$$;

ALTER FUNCTION public.finalize_transcript_cleanup(UUID, TEXT) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.finalize_transcript_cleanup(UUID, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.finalize_transcript_cleanup(UUID, TEXT) TO app_worker;

-- Release a claim whose Storage delete failed or whose outcome is unknown. The
-- row stays `pending` — an ambiguous Storage result is never recorded as
-- reconciled — and becomes immediately claimable again. The error is a bounded,
-- redacted category string (an exception class name), never a raw error, path,
-- or response body.
CREATE OR REPLACE FUNCTION public.release_transcript_cleanup(
    p_transcript_id UUID,
    p_worker_id TEXT,
    p_error TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    UPDATE public.transcripts
    SET cleanup_claimed_by = NULL,
        cleanup_claimed_at = NULL,
        cleanup_last_error = left(COALESCE(p_error, ''), 200),
        updated_at = now()
    WHERE id = p_transcript_id
      AND cleanup_state = 'pending'
      AND cleanup_claimed_by = p_worker_id;
    RETURN FOUND;
END;
$$;

ALTER FUNCTION public.release_transcript_cleanup(UUID, TEXT, TEXT) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.release_transcript_cleanup(UUID, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.release_transcript_cleanup(UUID, TEXT, TEXT) TO app_worker;

-- ---------------------------------------------------------------------------
-- Exact-target Storage maintenance, now bucket-bound
-- ---------------------------------------------------------------------------
-- Transcript cleanup needs the same capability the output cleanup path already
-- has, for a different private bucket. The alternative — a second maintenance
-- role — would duplicate the whole policy surface for no added confinement,
-- because the credential is already scoped to a single object. What was missing
-- is an explicit *bucket* binding: until now a token was implicitly an outputs
-- token because only outputs policies existed. Each maintenance token now
-- carries `maintenance_bucket`, and every policy requires the bucket it governs
-- to equal that claim, so:
--
--   * a transcripts token is invalid against `outputs` and vice versa;
--   * a token still authorizes exactly one object under exactly one org path;
--   * absent/empty claims authorize nothing (`NULLIF`);
--   * the role keeps only SELECT + DELETE, no INSERT/UPDATE, and prefix/list
--     access remains impossible because `name` must match exactly.
--
-- The credential is minted server-side per cleanup claim and never reaches the
-- browser, `app_user`, `app_worker`, a database row, audit metadata, the
-- sandbox, or logs. No service-role key is introduced.
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

    EXECUTE 'DROP POLICY IF EXISTS outputs_maintenance_select ON storage.objects';
    EXECUTE 'DROP POLICY IF EXISTS outputs_maintenance_delete ON storage.objects';
    EXECUTE 'DROP POLICY IF EXISTS transcripts_maintenance_select ON storage.objects';
    EXECUTE 'DROP POLICY IF EXISTS transcripts_maintenance_delete ON storage.objects';

    EXECUTE 'CREATE POLICY outputs_maintenance_select ON storage.objects
        FOR SELECT TO app_storage_maintenance
        USING (
            bucket_id = ''outputs''
            AND bucket_id = NULLIF(
                current_setting(''request.jwt.claims'', true)::jsonb
                    ->> ''maintenance_bucket'',
                ''''
            )
            AND (storage.foldername(name))[1]::uuid = auth.uid()
            AND name = NULLIF(
                current_setting(''request.jwt.claims'', true)::jsonb
                    ->> ''maintenance_object'',
                ''''
            )
        )';

    EXECUTE 'CREATE POLICY outputs_maintenance_delete ON storage.objects
        FOR DELETE TO app_storage_maintenance
        USING (
            bucket_id = ''outputs''
            AND bucket_id = NULLIF(
                current_setting(''request.jwt.claims'', true)::jsonb
                    ->> ''maintenance_bucket'',
                ''''
            )
            AND (storage.foldername(name))[1]::uuid = auth.uid()
            AND name = NULLIF(
                current_setting(''request.jwt.claims'', true)::jsonb
                    ->> ''maintenance_object'',
                ''''
            )
        )';

    EXECUTE 'CREATE POLICY transcripts_maintenance_select ON storage.objects
        FOR SELECT TO app_storage_maintenance
        USING (
            bucket_id = ''transcripts''
            AND bucket_id = NULLIF(
                current_setting(''request.jwt.claims'', true)::jsonb
                    ->> ''maintenance_bucket'',
                ''''
            )
            AND (storage.foldername(name))[1]::uuid = auth.uid()
            AND name = NULLIF(
                current_setting(''request.jwt.claims'', true)::jsonb
                    ->> ''maintenance_object'',
                ''''
            )
        )';

    EXECUTE 'CREATE POLICY transcripts_maintenance_delete ON storage.objects
        FOR DELETE TO app_storage_maintenance
        USING (
            bucket_id = ''transcripts''
            AND bucket_id = NULLIF(
                current_setting(''request.jwt.claims'', true)::jsonb
                    ->> ''maintenance_bucket'',
                ''''
            )
            AND (storage.foldername(name))[1]::uuid = auth.uid()
            AND name = NULLIF(
                current_setting(''request.jwt.claims'', true)::jsonb
                    ->> ''maintenance_object'',
                ''''
            )
        )';
END $$;
