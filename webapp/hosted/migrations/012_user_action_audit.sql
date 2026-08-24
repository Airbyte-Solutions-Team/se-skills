-- 012_user_action_audit.sql
-- Hosted user-action audit completeness (Slice 6B2A).
--
-- Slice 6A/6B1 made review and export actions durable audit evidence. This
-- migration closes the remaining customer-data and execution actions of the
-- hosted workflow:
--
--     transcript_upload → job_run_requested → (review) → job_cancel_requested
--     transcript_delete
--
--   - Transcript upload and delete are audited by app_admin-owned triggers on
--     `public.transcripts`, so the metadata row and its audit event commit or
--     roll back together and no `app_user` DML can bypass the event.
--     `app_user` still has no direct INSERT/UPDATE/DELETE on `audit_events`.
--   - `public.enqueue_job` and `public.request_job_cancellation` are replaced
--     with the same bodies plus one audit event in the same transaction as the
--     durable state change they already owned. A replayed idempotency key, an
--     idempotency conflict, an already-cancelled job, or a lost transition race
--     changes no durable state and therefore records nothing.
--
-- Deliberately out of scope: worker lifecycle events. `public.jobs` and
-- `public.job_attempts` remain the operational lifecycle and provenance ledger;
-- `job_run_requested`/`job_cancel_requested` record the authenticated user's
-- request, not every internal attempt, heartbeat, retry, or outcome.
--
-- Audit metadata carries server-side identifiers only: organization, actor,
-- action, entity, and the account/opportunity/transcript/job UUIDs needed to
-- follow the workflow. Every recorded metadata value is a UUID, so no
-- client-supplied string can reach the audit log. No transcript content,
-- filenames, Storage paths, source manifests, payloads, prompts, generated
-- Markdown, errors, tokens, `jobs.skill`, or idempotency strings.

ALTER TABLE public.audit_events DROP CONSTRAINT IF EXISTS audit_events_action_check;
ALTER TABLE public.audit_events ADD CONSTRAINT audit_events_action_check
    CHECK (action IN (
        'output_comment',
        'output_correction',
        'output_approval',
        'output_export',
        'transcript_upload',
        'transcript_delete',
        'job_run_requested',
        'job_cancel_requested'
    ));

-- These four actions describe a once-per-entity lifecycle transition and carry
-- no request id (the browser supplies no UUID request id for them, and job
-- idempotency keys are arbitrary text that must never become audit identity).
-- The unique index is the database-side backstop against duplicate evidence
-- from a retry or a concurrent request.
CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_events_entity_lifecycle
    ON public.audit_events(org_id, action, entity_id)
    WHERE action IN (
        'transcript_upload',
        'transcript_delete',
        'job_run_requested',
        'job_cancel_requested'
    );

-- ---------------------------------------------------------------------------
-- Transcript upload/delete audit
--
-- Owned by the database as AFTER INSERT/DELETE row triggers on
-- `public.transcripts` rather than by a second application write, so a metadata
-- row and its audit event always commit or roll back together: file validation
-- failure, Storage failure, or a failed metadata transaction leaves no event,
-- and a repeated delete of an already-removed transcript touches no row and so
-- records nothing.
--
-- The actor comes from the signed tenant context, never from a request field,
-- and the organization is the row's own org_id, which row-level security and the
-- composite account foreign key already constrain to the caller's active
-- organization. `app_user` cannot mutate transcript metadata without producing
-- the matching event: its RLS policy requires an active membership resolved from
-- that same signed context, so DML without a valid context fails outright.
--
-- Privileged activity with no tenant context (migrations and admin repair)
-- deliberately records nothing. These events describe authenticated user
-- actions, and an event with no actor would be evidence of nothing; such
-- activity belongs to database-level audit logging instead.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_private.audit_transcript_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_row public.transcripts;
    v_action TEXT;
    v_user_id UUID;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_row := NEW;
        v_action := 'transcript_upload';
    ELSE
        v_row := OLD;
        v_action := 'transcript_delete';
    END IF;

    v_user_id := public.current_request_user_id();
    IF v_user_id IS NULL THEN
        RETURN NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM public.memberships m
        WHERE m.user_id = v_user_id
          AND m.org_id = v_row.org_id
          AND m.active = true
    ) THEN
        RETURN NULL;
    END IF;

    -- Safe identifiers only: no filename, transcript content, size, Storage
    -- path, or customer names.
    PERFORM app_private.record_audit_event(
        v_row.org_id,
        v_user_id,
        v_action,
        'transcripts',
        v_row.id,
        NULL,
        jsonb_build_object(
            'transcript_id', v_row.id,
            'account_id', v_row.account_id,
            'opportunity_id', v_row.opportunity_id
        )
    );
    RETURN NULL;
END;
$$;

ALTER FUNCTION app_private.audit_transcript_mutation() OWNER TO app_admin;
-- Trigger function privileges are checked when the trigger is created, so no
-- role needs EXECUTE for the audit to run on its own DML.
REVOKE ALL ON FUNCTION app_private.audit_transcript_mutation() FROM PUBLIC;

DROP TRIGGER IF EXISTS audit_transcript_insert ON public.transcripts;
CREATE TRIGGER audit_transcript_insert
    AFTER INSERT ON public.transcripts
    FOR EACH ROW EXECUTE FUNCTION app_private.audit_transcript_mutation();

DROP TRIGGER IF EXISTS audit_transcript_delete ON public.transcripts;
CREATE TRIGGER audit_transcript_delete
    AFTER DELETE ON public.transcripts
    FOR EACH ROW EXECUTE FUNCTION app_private.audit_transcript_mutation();

-- ---------------------------------------------------------------------------
-- Job audit metadata
--
-- One helper for both job actions so the metadata contract is written once:
-- durable job id plus the account/transcript/opportunity UUIDs needed to follow
-- the workflow. `jobs.skill` is deliberately excluded: the hosted API still
-- accepts it as unconstrained client text, and audit metadata must not carry a
-- value the browser controls. No payload, source manifest, Storage path,
-- idempotency key, error, or token accounting.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_private.record_job_action_audit(
    p_org_id UUID,
    p_user_id UUID,
    p_action TEXT,
    p_job_id UUID,
    p_account_id UUID,
    p_transcript_id UUID,
    p_opportunity_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    PERFORM app_private.record_audit_event(
        p_org_id,
        p_user_id,
        p_action,
        'jobs',
        p_job_id,
        NULL,
        jsonb_build_object(
            'job_id', p_job_id,
            'account_id', p_account_id,
            'transcript_id', p_transcript_id,
            'opportunity_id', p_opportunity_id
        )
    );
END;
$$;

-- Drop the earlier eight-argument shape if a pre-review install created it, so
-- reapplying this migration cannot leave a skill-carrying overload behind.
DROP FUNCTION IF EXISTS app_private.record_job_action_audit(UUID, UUID, TEXT, UUID, UUID, UUID, UUID, TEXT);

ALTER FUNCTION app_private.record_job_action_audit(UUID, UUID, TEXT, UUID, UUID, UUID, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION app_private.record_job_action_audit(UUID, UUID, TEXT, UUID, UUID, UUID, UUID) FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Post-call run request audit
--
-- Same body as migration 003 plus one `job_run_requested` event, written in the
-- transaction that creates the durable job row. Only the branch whose INSERT
-- wins records an event, so an idempotent replay (including a concurrent one
-- that loses the unique-index race) returns the original job and adds nothing,
-- and an idempotency conflict or validation failure records nothing at all.
-- ---------------------------------------------------------------------------
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

    -- Validate transcript belongs to the account and organization.
    PERFORM 1
    FROM public.transcripts t
    WHERE t.id = p_transcript_id
      AND t.account_id = p_account_id
      AND t.org_id = v_org_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Transcript not found for this account';
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
-- Cancellation request audit
--
-- Same body as migration 003 plus one `job_cancel_requested` event on the two
-- branches that actually change durable state (queued → cancelled, and the
-- first cancellation request against a running job). An already-cancelled job,
-- a terminal job, or a lost transition race records nothing. All worker
-- claim/complete/fail race conditions and `app_worker` privileges are unchanged.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.request_job_cancellation(
    p_context_token TEXT,
    p_job_id UUID
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job RECORD;
    v_user_id UUID;
BEGIN
    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    IF v_job IS NULL THEN
        RAISE EXCEPTION 'Job not found';
    END IF;

    v_user_id := public.require_active_member_for_org(p_context_token, v_job.org_id);

    IF v_job.status = 'cancelled' THEN
        RETURN true;
    END IF;

    IF v_job.status IN ('success', 'failure', 'timeout') THEN
        RAISE EXCEPTION 'Cannot cancel a terminal job';
    END IF;

    IF v_job.status = 'queued' THEN
        UPDATE public.jobs
        SET status = 'cancelled',
            cancelled_at = clock_timestamp(),
            cancelled_by = v_user_id,
            finished_at = clock_timestamp(),
            timeout_at = NULL,
            next_attempt_after = NULL
        WHERE id = p_job_id
          AND status = 'queued';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'Job transition race: no longer queued';
        END IF;
        PERFORM app_private.record_job_action_audit(
            v_job.org_id, v_user_id, 'job_cancel_requested', v_job.id,
            v_job.account_id, v_job.transcript_id, v_job.opportunity_id
        );
        RETURN true;
    END IF;

    -- Running job: record the request; the worker finalizes it.
    UPDATE public.jobs
    SET cancel_requested_at = clock_timestamp(),
        cancelled_by = v_user_id
    WHERE id = p_job_id
      AND status = 'running'
      AND cancel_requested_at IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Job transition race: no longer running';
    END IF;
    PERFORM app_private.record_job_action_audit(
        v_job.org_id, v_user_id, 'job_cancel_requested', v_job.id,
        v_job.account_id, v_job.transcript_id, v_job.opportunity_id
    );
    RETURN true;
END;
$$;

ALTER FUNCTION public.request_job_cancellation(TEXT, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.request_job_cancellation(TEXT, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.request_job_cancellation(TEXT, UUID) TO app_user;
