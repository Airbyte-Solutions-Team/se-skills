-- 004_outputs_and_reviews.sql
-- Generated output storage, immutable output versions, review/correction records,
-- private generated-output Storage bucket, and worker-side persistence functions.

-- Unique composite-key indexes required for the same-organization foreign keys below.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_id_account_org
    ON public.jobs(id, account_id, org_id);

-- Redefine claim_next_job to expose requester_id to the worker so the trusted
-- orchestrator can resolve the manifest and materialize inputs without reading
-- the jobs table directly.
DROP FUNCTION IF EXISTS public.claim_next_job(text, integer) CASCADE;

CREATE OR REPLACE FUNCTION public.claim_next_job(
    p_worker_id TEXT,
    p_timeout_seconds INTEGER DEFAULT 300
)
RETURNS TABLE(
    job_id UUID,
    attempt_number INTEGER,
    lease_token UUID,
    org_id UUID,
    account_id UUID,
    transcript_id UUID,
    opportunity_id UUID,
    requester_id UUID,
    skill_version TEXT,
    model TEXT,
    runtime_version TEXT,
    payload JSONB,
    input_refs JSONB,
    source_manifest JSONB,
    timeout_at TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job_id UUID;
    v_attempt_number INTEGER;
    v_lease_token UUID;
    v_timeout TIMESTAMPTZ;
BEGIN
    SELECT j.id INTO v_job_id
    FROM public.jobs j
    WHERE j.status = 'queued'
      AND (j.next_attempt_after IS NULL OR j.next_attempt_after <= clock_timestamp())
      AND j.cancel_requested_at IS NULL
      AND j.dead_lettered IS NOT TRUE
    ORDER BY j.created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1;

    IF v_job_id IS NULL THEN
        RETURN;
    END IF;

    v_lease_token := gen_random_uuid();
    v_timeout := clock_timestamp() + make_interval(secs => p_timeout_seconds);

    UPDATE public.jobs
    SET attempts = attempts + 1,
        status = 'running',
        worker_id = p_worker_id,
        started_at = clock_timestamp(),
        timeout_at = v_timeout,
        next_attempt_after = NULL,
        finished_at = NULL
    WHERE id = v_job_id
    RETURNING attempts INTO v_attempt_number;

    INSERT INTO public.job_attempts (
        id, org_id, job_id, attempt_number, worker_id, lease_token,
        started_at, heartbeat_at
    )
    SELECT
        gen_random_uuid(),
        j.org_id,
        j.id,
        v_attempt_number,
        p_worker_id,
        v_lease_token,
        clock_timestamp(),
        clock_timestamp()
    FROM public.jobs j
    WHERE j.id = v_job_id;

    RETURN QUERY
    SELECT j.id, v_attempt_number, v_lease_token, j.org_id, j.account_id,
           j.transcript_id, j.opportunity_id, j.requester_id, j.skill_version,
           j.model, j.runtime_version, j.payload, j.input_refs,
           j.source_manifest, v_timeout
    FROM public.jobs j
    WHERE j.id = v_job_id;
END;
$$;

ALTER FUNCTION public.claim_next_job(TEXT, INTEGER) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.claim_next_job(TEXT, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.claim_next_job(TEXT, INTEGER) TO app_worker;

-- ---------------------------------------------------------------------------
-- Generated outputs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.outputs (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    job_id UUID NOT NULL,
    account_id UUID NOT NULL,
    opportunity_id UUID,
    transcript_id UUID NOT NULL,
    requester_id UUID NOT NULL,
    content_storage_path TEXT NOT NULL,
    title TEXT,
    sidecar JSONB NOT NULL DEFAULT '{}',
    validation_status TEXT NOT NULL DEFAULT 'unvalidated',
    skill TEXT NOT NULL,
    skill_version TEXT NOT NULL DEFAULT '1.0',
    model TEXT,
    runtime_version TEXT,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (validation_status IN ('unvalidated', 'valid', 'invalid')),
    FOREIGN KEY (job_id, account_id, org_id) REFERENCES public.jobs(id, account_id, org_id) ON DELETE RESTRICT,
    FOREIGN KEY (account_id, org_id) REFERENCES public.accounts(id, org_id) ON DELETE RESTRICT,
    FOREIGN KEY (opportunity_id, account_id, org_id) REFERENCES public.opportunities(id, account_id, org_id) ON DELETE RESTRICT,
    FOREIGN KEY (transcript_id, account_id, org_id) REFERENCES public.transcripts(id, account_id, org_id) ON DELETE RESTRICT,
    FOREIGN KEY (requester_id, org_id) REFERENCES public.memberships(user_id, org_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_outputs_org_id ON public.outputs(org_id);
CREATE INDEX IF NOT EXISTS idx_outputs_account_id ON public.outputs(account_id, org_id);
CREATE INDEX IF NOT EXISTS idx_outputs_job_id ON public.outputs(job_id);
CREATE INDEX IF NOT EXISTS idx_outputs_transcript_id ON public.outputs(transcript_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outputs_id_org ON public.outputs(id, org_id);

ALTER TABLE public.outputs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_tenant_outputs ON public.outputs;
CREATE POLICY org_tenant_outputs ON public.outputs
    FOR SELECT TO app_user
    USING (public.is_active_org_member(org_id));

-- ---------------------------------------------------------------------------
-- Output versions (append-only corrections)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.output_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    output_id UUID NOT NULL,
    previous_version_id UUID,
    content_storage_path TEXT NOT NULL,
    sidecar JSONB NOT NULL DEFAULT '{}',
    change_summary TEXT,
    created_by UUID REFERENCES public.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (output_id, org_id) REFERENCES public.outputs(id, org_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_output_versions_org_id ON public.output_versions(org_id);
CREATE INDEX IF NOT EXISTS idx_output_versions_output_id ON public.output_versions(output_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_output_versions_id_org ON public.output_versions(id, org_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_output_versions_id_output_org ON public.output_versions(id, output_id, org_id);

-- Idempotent migration support: ensure existing output_versions tables get the
-- correction-chain column and a users-only FK for created_by.
ALTER TABLE public.output_versions ADD COLUMN IF NOT EXISTS previous_version_id UUID;
ALTER TABLE public.output_versions DROP CONSTRAINT IF EXISTS output_versions_created_by_fkey;
ALTER TABLE public.output_versions ADD CONSTRAINT output_versions_created_by_fkey
    FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;
ALTER TABLE public.output_versions ADD CONSTRAINT output_versions_previous_version_fkey
    FOREIGN KEY (previous_version_id, output_id, org_id) REFERENCES public.output_versions(id, output_id, org_id) ON DELETE RESTRICT;

ALTER TABLE public.output_versions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_tenant_output_versions ON public.output_versions;
CREATE POLICY org_tenant_output_versions ON public.output_versions
    FOR SELECT TO app_user
    USING (public.is_active_org_member(org_id));

-- ---------------------------------------------------------------------------
-- Reviews / approvals / corrections metadata
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    output_id UUID NOT NULL,
    output_version_id UUID,
    previous_version_id UUID,
    user_id UUID NOT NULL,
    action TEXT NOT NULL,
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (action IN ('approve', 'comment', 'correct')),
    FOREIGN KEY (output_id, org_id) REFERENCES public.outputs(id, org_id) ON DELETE CASCADE,
    FOREIGN KEY (output_version_id, output_id, org_id) REFERENCES public.output_versions(id, output_id, org_id) ON DELETE RESTRICT,
    FOREIGN KEY (previous_version_id, output_id, org_id) REFERENCES public.output_versions(id, output_id, org_id) ON DELETE RESTRICT,
    FOREIGN KEY (user_id, org_id) REFERENCES public.memberships(user_id, org_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_reviews_org_id ON public.reviews(org_id);
CREATE INDEX IF NOT EXISTS idx_reviews_output_id ON public.reviews(output_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_id_org ON public.reviews(id, org_id);

-- Idempotent migration support: ensure existing reviews tables have the
-- output-version chain FKs scoped to the same output.
ALTER TABLE public.reviews DROP CONSTRAINT IF EXISTS reviews_output_version_id_fkey;
ALTER TABLE public.reviews DROP CONSTRAINT IF EXISTS reviews_previous_version_id_fkey;
ALTER TABLE public.reviews ADD CONSTRAINT reviews_output_version_id_fkey
    FOREIGN KEY (output_version_id, output_id, org_id) REFERENCES public.output_versions(id, output_id, org_id) ON DELETE RESTRICT;
ALTER TABLE public.reviews ADD CONSTRAINT reviews_previous_version_id_fkey
    FOREIGN KEY (previous_version_id, output_id, org_id) REFERENCES public.output_versions(id, output_id, org_id) ON DELETE RESTRICT;

ALTER TABLE public.reviews ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_tenant_reviews ON public.reviews;
CREATE POLICY org_tenant_reviews ON public.reviews
    FOR SELECT TO app_user
    USING (public.is_active_org_member(org_id));

-- ---------------------------------------------------------------------------
-- Worker function: idempotently create an outputs row for a running attempt.
-- This is the only path through which the worker may insert an outputs row.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.create_job_output(
    p_job_id UUID,
    p_attempt_number INTEGER,
    p_lease_token UUID,
    p_output_id UUID,
    p_content_storage_path TEXT,
    p_title TEXT,
    p_sidecar JSONB,
    p_runtime_version TEXT DEFAULT NULL,
    p_model TEXT DEFAULT NULL
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job RECORD;
    v_attempt RECORD;
    v_existing RECORD;
BEGIN
    SELECT * INTO v_job
    FROM public.jobs
    WHERE id = p_job_id
    FOR UPDATE;

    IF v_job IS NULL THEN
        RAISE EXCEPTION 'Job not found';
    END IF;

    IF v_job.status <> 'running' THEN
        RAISE EXCEPTION 'Job is not running';
    END IF;

    SELECT * INTO v_attempt
    FROM public.job_attempts
    WHERE job_id = p_job_id
      AND attempt_number = p_attempt_number
    FOR UPDATE;

    IF v_attempt IS NULL OR v_attempt.lease_token <> p_lease_token THEN
        RAISE EXCEPTION 'Invalid attempt or lease token';
    END IF;

    IF v_attempt.outcome IS NOT NULL THEN
        RAISE EXCEPTION 'Attempt already finished';
    END IF;

    -- Idempotency: if the deterministic output already exists and matches the
    -- new request, return NULL so the caller knows not to re-upload. This lets a
    -- retried attempt recover after a successful persistence but failed completion.
    -- Use FOUND (not the row IS NOT NULL) because nullable columns make a composite
    -- value test unreliable.
    SELECT sidecar, content_storage_path, job_id, org_id
      INTO v_existing
      FROM public.outputs
     WHERE id = p_output_id;

    IF FOUND THEN
        IF v_existing.content_storage_path <> p_content_storage_path
            OR v_existing.sidecar <> p_sidecar
            OR v_existing.job_id <> p_job_id
            OR v_existing.org_id <> v_job.org_id THEN
            RAISE EXCEPTION 'Output exists with mismatched evidence';
        END IF;
        RETURN NULL;
    END IF;

    INSERT INTO public.outputs (
        id, org_id, job_id, account_id, opportunity_id, transcript_id, requester_id,
        content_storage_path, title, sidecar, skill, skill_version, model, runtime_version,
        validation_status, generated_at
    ) VALUES (
        p_output_id,
        v_job.org_id,
        p_job_id,
        v_job.account_id,
        v_job.opportunity_id,
        v_job.transcript_id,
        v_job.requester_id,
        p_content_storage_path,
        p_title,
        COALESCE(p_sidecar, '{}'),
        v_job.skill,
        v_job.skill_version,
        COALESCE(p_model, v_job.model),
        COALESCE(p_runtime_version, v_job.runtime_version),
        'unvalidated',
        clock_timestamp()
    );

    RETURN p_output_id;
END;
$$;

ALTER FUNCTION public.create_job_output(UUID, INTEGER, UUID, UUID, TEXT, TEXT, JSONB, TEXT, TEXT) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.create_job_output(UUID, INTEGER, UUID, UUID, TEXT, TEXT, JSONB, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.create_job_output(UUID, INTEGER, UUID, UUID, TEXT, TEXT, JSONB, TEXT, TEXT) TO app_worker;

-- ---------------------------------------------------------------------------
-- Worker function: roll back an unvalidated outputs row when Storage upload fails.
-- Only unvalidated rows belonging to the current running attempt may be removed.
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.delete_job_output(UUID);

CREATE OR REPLACE FUNCTION public.delete_job_output(
    p_output_id UUID,
    p_job_id UUID,
    p_attempt_number INTEGER,
    p_lease_token UUID
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job RECORD;
    v_attempt RECORD;
BEGIN
    SELECT * INTO v_job
    FROM public.jobs
    WHERE id = p_job_id
    FOR UPDATE;

    IF v_job IS NULL THEN
        RAISE EXCEPTION 'Job not found';
    END IF;

    IF v_job.status <> 'running' THEN
        RAISE EXCEPTION 'Job is not running';
    END IF;

    SELECT * INTO v_attempt
    FROM public.job_attempts
    WHERE job_id = p_job_id
      AND attempt_number = p_attempt_number
    FOR UPDATE;

    IF v_attempt IS NULL OR v_attempt.lease_token <> p_lease_token THEN
        RAISE EXCEPTION 'Invalid attempt or lease token';
    END IF;

    IF v_attempt.outcome IS NOT NULL THEN
        RAISE EXCEPTION 'Attempt already finished';
    END IF;

    DELETE FROM public.outputs
    WHERE id = p_output_id
      AND job_id = p_job_id
      AND validation_status = 'unvalidated';

    RETURN FOUND;
END;
$$;

ALTER FUNCTION public.delete_job_output(UUID, UUID, INTEGER, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.delete_job_output(UUID, UUID, INTEGER, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.delete_job_output(UUID, UUID, INTEGER, UUID) TO app_worker;

-- ---------------------------------------------------------------------------
-- Worker function: resolve canonical transcript and approved prior-context
-- references for a running attempt. Returns a JSONB object with trusted storage
-- paths; any missing, aliased, cross-scope, or duplicate reference fails.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.resolve_job_inputs(
    p_job_id UUID,
    p_attempt_number INTEGER,
    p_lease_token UUID,
    p_prior_output_ids UUID[] DEFAULT NULL
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job RECORD;
    v_attempt RECORD;
    v_transcript RECORD;
    v_prior_id UUID;
    v_prior RECORD;
    v_priors JSONB := '[]'::jsonb;
    v_seen UUID[] := ARRAY[]::UUID[];
BEGIN
    SELECT * INTO v_job
    FROM public.jobs
    WHERE id = p_job_id
    FOR UPDATE;

    IF v_job IS NULL THEN
        RAISE EXCEPTION 'Job not found';
    END IF;

    IF v_job.status <> 'running' THEN
        RAISE EXCEPTION 'Job is not running';
    END IF;

    SELECT * INTO v_attempt
    FROM public.job_attempts
    WHERE job_id = p_job_id
      AND attempt_number = p_attempt_number
    FOR UPDATE;

    IF v_attempt IS NULL OR v_attempt.lease_token <> p_lease_token THEN
        RAISE EXCEPTION 'Invalid attempt or lease token';
    END IF;

    IF v_attempt.outcome IS NOT NULL THEN
        RAISE EXCEPTION 'Attempt already finished';
    END IF;

    SELECT * INTO v_transcript
    FROM public.transcripts
    WHERE id = v_job.transcript_id
      AND account_id = v_job.account_id
      AND org_id = v_job.org_id
      AND (opportunity_id IS NOT DISTINCT FROM v_job.opportunity_id);

    IF v_transcript IS NULL THEN
        RAISE EXCEPTION 'Transcript not found or does not match job scope';
    END IF;

    IF p_prior_output_ids IS NOT NULL THEN
        FOREACH v_prior_id IN ARRAY p_prior_output_ids LOOP
            IF v_prior_id = ANY(v_seen) THEN
                RAISE EXCEPTION 'Duplicate prior context reference: %', v_prior_id;
            END IF;
            v_seen := array_append(v_seen, v_prior_id);

            SELECT id, content_storage_path INTO v_prior
            FROM public.outputs
            WHERE id = v_prior_id
              AND org_id = v_job.org_id
              AND account_id = v_job.account_id
              AND (opportunity_id IS NOT DISTINCT FROM v_job.opportunity_id);

            IF v_prior IS NULL THEN
                RAISE EXCEPTION 'Prior output % not found or not approved for this scope', v_prior_id;
            END IF;

            v_priors := v_priors || jsonb_build_object('output_id', v_prior.id, 'storage_path', v_prior.content_storage_path);
        END LOOP;
    END IF;

    RETURN jsonb_build_object(
        'transcript_id', v_transcript.id,
        'account_id', v_transcript.account_id,
        'org_id', v_transcript.org_id,
        'opportunity_id', v_transcript.opportunity_id,
        'requester_id', v_job.requester_id,
        'storage_path', v_transcript.storage_path,
        'original_filename', v_transcript.original_filename,
        'mime_type', v_transcript.mime_type,
        'size_bytes', v_transcript.size_bytes,
        'prior_outputs', v_priors
    );
END;
$$;

ALTER FUNCTION public.resolve_job_inputs(UUID, INTEGER, UUID, UUID[]) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.resolve_job_inputs(UUID, INTEGER, UUID, UUID[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.resolve_job_inputs(UUID, INTEGER, UUID, UUID[]) TO app_worker;

-- ---------------------------------------------------------------------------
-- Worker function: retrieve an output's storage path for recovery/completion.
-- Used only by the worker role; result must never be logged or returned to SPA.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_job_output_storage_path(
    p_output_id UUID
)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_path TEXT;
BEGIN
    SELECT content_storage_path INTO v_path
    FROM public.outputs
    WHERE id = p_output_id;
    RETURN v_path;
END;
$$;

ALTER FUNCTION public.get_job_output_storage_path(UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.get_job_output_storage_path(UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_job_output_storage_path(UUID) TO app_worker;

-- ---------------------------------------------------------------------------
-- Update complete_job and fail_job to accept validation_status and set it on
-- the job row. The worker always owns the validation result.
-- ---------------------------------------------------------------------------

-- The old 8-argument fail_job overload must be removed before the 9-argument
-- version can replace it cleanly; adding validation_status changes the
-- signature enough that OR REPLACE creates an overload instead.
DROP FUNCTION IF EXISTS public.fail_job(uuid, integer, uuid, text, text, integer, text, text) CASCADE;

CREATE OR REPLACE FUNCTION public.complete_job(
    p_job_id UUID,
    p_attempt_number INTEGER,
    p_lease_token UUID,
    p_result_output_id UUID,
    p_validation_status TEXT,
    p_token_usage JSONB,
    p_cost NUMERIC,
    p_runtime_version TEXT DEFAULT NULL,
    p_model TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_attempt RECORD;
    v_job RECORD;
    v_output RECORD;
BEGIN
    SELECT * INTO v_job
    FROM public.jobs
    WHERE id = p_job_id
    FOR UPDATE;

    IF v_job IS NULL THEN
        RAISE EXCEPTION 'Job not found';
    END IF;

    IF v_job.status <> 'running' THEN
        RAISE EXCEPTION 'Job is not running';
    END IF;

    SELECT * INTO v_attempt
    FROM public.job_attempts
    WHERE job_id = p_job_id
      AND attempt_number = p_attempt_number
    FOR UPDATE;

    IF v_attempt IS NULL OR v_attempt.lease_token <> p_lease_token THEN
        RAISE EXCEPTION 'Invalid attempt or lease token';
    END IF;

    IF v_attempt.outcome IS NOT NULL THEN
        RAISE EXCEPTION 'Attempt already finished';
    END IF;

    IF v_job.cancel_requested_at IS NOT NULL THEN
        UPDATE public.job_attempts
        SET outcome = 'cancelled',
            finished_at = clock_timestamp(),
            runtime_version = COALESCE(p_runtime_version, v_attempt.runtime_version),
            model = COALESCE(p_model, v_attempt.model)
        WHERE id = v_attempt.id;

        UPDATE public.jobs
        SET status = 'cancelled',
            cancelled_at = clock_timestamp(),
            finished_at = clock_timestamp(),
            timeout_at = NULL
        WHERE id = p_job_id;
        RETURN true;
    END IF;

    -- Only mark the output as valid when the job is not cancelled; otherwise the
    -- row remains unvalidated and is not attached as a successful result.
    IF p_result_output_id IS NOT NULL THEN
        SELECT * INTO v_output
        FROM public.outputs
        WHERE id = p_result_output_id
          AND job_id = p_job_id
          AND org_id = v_job.org_id;
        IF v_output IS NULL THEN
            RAISE EXCEPTION 'Result output not found for this job';
        END IF;
        UPDATE public.outputs
        SET validation_status = COALESCE(p_validation_status, 'unvalidated')
        WHERE id = p_result_output_id;
    END IF;

    UPDATE public.job_attempts
    SET outcome = 'success',
        finished_at = clock_timestamp(),
        token_usage = COALESCE(p_token_usage, '{}'),
        cost = p_cost,
        runtime_version = COALESCE(p_runtime_version, v_attempt.runtime_version),
        model = COALESCE(p_model, v_attempt.model)
    WHERE id = v_attempt.id;

    UPDATE public.jobs
    SET status = 'success',
        finished_at = clock_timestamp(),
        timeout_at = NULL,
        result_output_id = p_result_output_id,
        validation_status = COALESCE(p_validation_status, 'unvalidated'),
        token_usage = COALESCE(p_token_usage, '{}'),
        cost = p_cost,
        runtime_version = COALESCE(p_runtime_version, v_attempt.runtime_version, v_job.runtime_version),
        model = COALESCE(p_model, v_attempt.model, v_job.model)
    WHERE id = p_job_id;

    RETURN true;
END;
$$;

ALTER FUNCTION public.complete_job(UUID, INTEGER, UUID, UUID, TEXT, JSONB, NUMERIC, TEXT, TEXT) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.complete_job(UUID, INTEGER, UUID, UUID, TEXT, JSONB, NUMERIC, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.complete_job(UUID, INTEGER, UUID, UUID, TEXT, JSONB, NUMERIC, TEXT, TEXT) TO app_worker;

CREATE OR REPLACE FUNCTION public.fail_job(
    p_job_id UUID,
    p_attempt_number INTEGER,
    p_lease_token UUID,
    p_error_category TEXT,
    p_error TEXT,
    p_backoff_seconds INTEGER DEFAULT NULL,
    p_runtime_version TEXT DEFAULT NULL,
    p_model TEXT DEFAULT NULL,
    p_validation_status TEXT DEFAULT NULL
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_attempt RECORD;
    v_job RECORD;
    v_backoff INTERVAL;
    v_outcome TEXT;
    v_status TEXT;
BEGIN
    SELECT * INTO v_job
    FROM public.jobs
    WHERE id = p_job_id
    FOR UPDATE;

    IF v_job IS NULL THEN
        RAISE EXCEPTION 'Job not found';
    END IF;

    IF v_job.status <> 'running' THEN
        RAISE EXCEPTION 'Job is not running';
    END IF;

    SELECT * INTO v_attempt
    FROM public.job_attempts
    WHERE job_id = p_job_id
      AND attempt_number = p_attempt_number
    FOR UPDATE;

    IF v_attempt IS NULL OR v_attempt.lease_token <> p_lease_token THEN
        RAISE EXCEPTION 'Invalid attempt or lease token';
    END IF;

    IF v_attempt.outcome IS NOT NULL THEN
        RAISE EXCEPTION 'Attempt already finished';
    END IF;

    IF v_job.cancel_requested_at IS NOT NULL THEN
        UPDATE public.job_attempts
        SET outcome = 'cancelled',
            error_category = p_error_category,
            error = p_error,
            finished_at = clock_timestamp(),
            runtime_version = COALESCE(p_runtime_version, v_attempt.runtime_version),
            model = COALESCE(p_model, v_attempt.model)
        WHERE id = v_attempt.id;

        UPDATE public.jobs
        SET status = 'cancelled',
            cancelled_at = clock_timestamp(),
            finished_at = clock_timestamp(),
            timeout_at = NULL,
            next_attempt_after = NULL,
            started_at = NULL,
            worker_id = NULL,
            validation_status = COALESCE(p_validation_status, v_job.validation_status)
        WHERE id = p_job_id;
        RETURN true;
    END IF;

    v_outcome := CASE WHEN p_error_category = 'timeout' THEN 'timeout' ELSE 'failure' END;

    UPDATE public.job_attempts
    SET outcome = v_outcome,
        error_category = p_error_category,
        error = p_error,
        finished_at = clock_timestamp(),
        runtime_version = COALESCE(p_runtime_version, v_attempt.runtime_version),
        model = COALESCE(p_model, v_attempt.model)
    WHERE id = v_attempt.id;

    IF v_job.attempts >= v_job.max_attempts THEN
        v_status := CASE WHEN p_error_category = 'timeout' THEN 'timeout' ELSE 'failure' END;
        UPDATE public.jobs
        SET status = v_status,
            finished_at = clock_timestamp(),
            timeout_at = NULL,
            started_at = NULL,
            worker_id = NULL,
            next_attempt_after = NULL,
            dead_lettered = true,
            error = p_error,
            validation_status = COALESCE(p_validation_status, v_job.validation_status),
            runtime_version = COALESCE(p_runtime_version, v_attempt.runtime_version, v_job.runtime_version),
            model = COALESCE(p_model, v_attempt.model, v_job.model)
        WHERE id = p_job_id;
    ELSE
        IF p_backoff_seconds IS NOT NULL THEN
            v_backoff := make_interval(secs => p_backoff_seconds);
        ELSE
            v_backoff := public.worker_backoff_interval(v_job.attempts);
        END IF;

        UPDATE public.jobs
        SET status = 'queued',
            finished_at = NULL,
            timeout_at = NULL,
            started_at = NULL,
            worker_id = NULL,
            next_attempt_after = clock_timestamp() + v_backoff,
            error = p_error,
            validation_status = COALESCE(p_validation_status, v_job.validation_status),
            runtime_version = COALESCE(p_runtime_version, v_attempt.runtime_version, v_job.runtime_version),
            model = COALESCE(p_model, v_attempt.model, v_job.model)
        WHERE id = p_job_id;
    END IF;

    RETURN true;
END;
$$;

ALTER FUNCTION public.fail_job(UUID, INTEGER, UUID, TEXT, TEXT, INTEGER, TEXT, TEXT, TEXT) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.fail_job(UUID, INTEGER, UUID, TEXT, TEXT, INTEGER, TEXT, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.fail_job(UUID, INTEGER, UUID, TEXT, TEXT, INTEGER, TEXT, TEXT, TEXT) TO app_worker;

-- ---------------------------------------------------------------------------
-- Generated-output Storage bucket and policies
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    storage_exists BOOLEAN := EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'storage');
    auth_exists BOOLEAN := EXISTS (SELECT 1 FROM information_schema.schemata WHERE schema_name = 'auth');
    authenticated_exists BOOLEAN := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated');
    anon_exists BOOLEAN := EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon');
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
    EXECUTE 'GRANT app_storage TO authenticator';

    -- Force the outputs bucket private, creating it if missing.
    IF EXISTS (SELECT 1 FROM storage.buckets WHERE name = 'outputs') THEN
        UPDATE storage.buckets SET public = false WHERE name = 'outputs';
    ELSE
        INSERT INTO storage.buckets (id, name, public, owner)
        VALUES ('outputs', 'outputs', false, NULL);
    END IF;

    IF (SELECT public FROM storage.buckets WHERE name = 'outputs') <> false THEN
        RAISE EXCEPTION 'outputs bucket must remain private';
    END IF;

    ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;

    EXECUTE 'GRANT USAGE ON SCHEMA storage TO app_admin';
    EXECUTE 'GRANT ALL ON ALL TABLES IN SCHEMA storage TO app_admin';

    EXECUTE 'GRANT USAGE ON SCHEMA storage TO app_storage';
    EXECUTE 'GRANT USAGE ON SCHEMA auth TO app_storage';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON storage.objects TO app_storage';

    IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid WHERE n.nspname = 'auth' AND p.proname = 'uid') THEN
        EXECUTE 'GRANT EXECUTE ON FUNCTION auth.uid() TO app_storage';
    END IF;

    EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA storage FROM PUBLIC';
    IF authenticated_exists THEN
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA storage FROM authenticated';
        EXECUTE 'REVOKE USAGE ON SCHEMA storage FROM authenticated';
    END IF;
    IF anon_exists THEN
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA storage FROM anon';
        EXECUTE 'REVOKE USAGE ON SCHEMA storage FROM anon';
    END IF;

    EXECUTE 'DROP POLICY IF EXISTS outputs_select ON storage.objects';
    EXECUTE 'DROP POLICY IF EXISTS outputs_insert ON storage.objects';
    EXECUTE 'DROP POLICY IF EXISTS outputs_update ON storage.objects';
    EXECUTE 'DROP POLICY IF EXISTS outputs_delete ON storage.objects';

    EXECUTE 'CREATE POLICY outputs_select ON storage.objects
        FOR SELECT TO app_storage
        USING (
            bucket_id = ''outputs''
            AND public.is_active_org_member_storage(auth.uid(), (storage.foldername(name))[1]::uuid)
        )';

    EXECUTE 'CREATE POLICY outputs_insert ON storage.objects
        FOR INSERT TO app_storage
        WITH CHECK (
            bucket_id = ''outputs''
            AND public.is_active_org_member_storage(auth.uid(), (storage.foldername(name))[1]::uuid)
        )';

    EXECUTE 'CREATE POLICY outputs_update ON storage.objects
        FOR UPDATE TO app_storage
        USING (
            bucket_id = ''outputs''
            AND public.is_active_org_member_storage(auth.uid(), (storage.foldername(name))[1]::uuid)
        )
        WITH CHECK (
            bucket_id = ''outputs''
            AND public.is_active_org_member_storage(auth.uid(), (storage.foldername(name))[1]::uuid)
        )';

    EXECUTE 'CREATE POLICY outputs_delete ON storage.objects
        FOR DELETE TO app_storage
        USING (
            bucket_id = ''outputs''
            AND public.is_active_org_member_storage(auth.uid(), (storage.foldername(name))[1]::uuid)
        )';
END $$;

-- ---------------------------------------------------------------------------
-- Least-privilege role grants
-- ---------------------------------------------------------------------------
GRANT SELECT ON TABLE public.outputs, public.output_versions, public.reviews TO app_user;
-- The app_user role must never directly insert or update generated output evidence.
-- Workers use the SECURITY DEFINER functions above.

GRANT ALL ON TABLE public.outputs, public.output_versions, public.reviews TO app_admin;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO app_admin;

ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO app_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO app_admin;
