-- 003_jobs_and_worker.sql
-- Durable job ledger, append-only attempts, dedicated worker identity, and
-- database-owned claim/heartbeat/complete/cancel/recovery functions.

-- Job state is owned by an organization and tied to an account, an uploaded
-- transcript, and an optional opportunity. Transcript input is a normalized
-- same-organization relationship, not an arbitrary browser-supplied path.

-- Unique indexes required for composite foreign keys from jobs and attempts.
CREATE UNIQUE INDEX IF NOT EXISTS idx_transcripts_id_account_org
    ON public.transcripts(id, account_id, org_id);

-- Application worker: least-privilege identity used only by the worker process.
-- It has no direct table access; all queue operations go through narrow
-- SECURITY DEFINER functions owned by the migration role.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'app_worker') THEN
        EXECUTE format('CREATE ROLE app_worker WITH LOGIN PASSWORD %L NOBYPASSRLS', current_setting('migration.app_worker_password'));
    ELSE
        EXECUTE format('ALTER ROLE app_worker WITH LOGIN PASSWORD %L NOBYPASSRLS', current_setting('migration.app_worker_password'));
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO app_worker;

-- Primary job ledger. `status` is limited to the six primary lifecycle states.
-- `retry-wait` and dead-letter are scheduling metadata, not additional statuses.
CREATE TABLE IF NOT EXISTS public.jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    account_id UUID NOT NULL,
    opportunity_id UUID,
    transcript_id UUID NOT NULL,
    requester_id UUID NOT NULL,
    skill TEXT NOT NULL,
    skill_version TEXT NOT NULL DEFAULT '1.0',
    model TEXT,
    runtime_version TEXT,
    worker_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    payload JSONB NOT NULL DEFAULT '{}',
    input_refs JSONB NOT NULL DEFAULT '{}',
    source_manifest JSONB NOT NULL DEFAULT '{}',
    result_output_id UUID,
    validation_status TEXT NOT NULL DEFAULT 'unvalidated',
    token_usage JSONB NOT NULL DEFAULT '{}',
    cost NUMERIC(12, 4),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    timeout_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    cancelled_by UUID REFERENCES public.users(id) ON DELETE SET NULL,
    cancel_requested_at TIMESTAMPTZ,
    next_attempt_after TIMESTAMPTZ,
    dead_lettered BOOLEAN NOT NULL DEFAULT false,
    idempotency_key TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status IN ('queued', 'running', 'success', 'failure', 'cancelled', 'timeout')),
    CHECK (attempts >= 0),
    CHECK (max_attempts > 0),
    UNIQUE (org_id, idempotency_key),
    FOREIGN KEY (account_id, org_id) REFERENCES public.accounts(id, org_id) ON DELETE RESTRICT,
    FOREIGN KEY (opportunity_id, account_id, org_id) REFERENCES public.opportunities(id, account_id, org_id) ON DELETE RESTRICT,
    FOREIGN KEY (transcript_id, account_id, org_id) REFERENCES public.transcripts(id, account_id, org_id) ON DELETE RESTRICT,
    FOREIGN KEY (requester_id, org_id) REFERENCES public.memberships(user_id, org_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_jobs_org_id ON public.jobs(org_id);
CREATE INDEX IF NOT EXISTS idx_jobs_account_id ON public.jobs(account_id, org_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON public.jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_next_attempt_after ON public.jobs(next_attempt_after);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_id_org ON public.jobs(id, org_id);
ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS worker_id TEXT;

-- Append-only attempt history. Each row records one execution attempt, its lease
-- token, heartbeat, outcome, and redacted failure metadata.
CREATE TABLE IF NOT EXISTS public.job_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    job_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    worker_id TEXT,
    runtime_version TEXT,
    lease_token UUID,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    outcome TEXT,
    error_category TEXT,
    error TEXT,
    token_usage JSONB NOT NULL DEFAULT '{}',
    cost NUMERIC(12, 4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (attempt_number > 0),
    CHECK (outcome IS NULL OR outcome IN ('success', 'failure', 'timeout', 'cancelled')),
    FOREIGN KEY (job_id, org_id) REFERENCES public.jobs(id, org_id) ON DELETE CASCADE,
    FOREIGN KEY (job_id) REFERENCES public.jobs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_job_attempts_org_id ON public.job_attempts(org_id);
CREATE INDEX IF NOT EXISTS idx_job_attempts_job_id ON public.job_attempts(job_id);
CREATE INDEX IF NOT EXISTS idx_job_attempts_lease_token ON public.job_attempts(lease_token);

-- Row-level security: tenant access for the application role.
ALTER TABLE public.jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.job_attempts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_tenant_jobs ON public.jobs;
CREATE POLICY org_tenant_jobs ON public.jobs
    FOR SELECT TO app_user
    USING (public.is_active_org_member(org_id));

DROP POLICY IF EXISTS org_tenant_job_attempts ON public.job_attempts;
CREATE POLICY org_tenant_job_attempts ON public.job_attempts
    FOR SELECT TO app_user
    USING (public.is_active_org_member(org_id));

-- Application role can read the job ledger; all mutations happen through
-- narrow SECURITY DEFINER functions.
GRANT SELECT ON TABLE public.jobs, public.job_attempts TO app_user;

-- Helper: resolve the user from a signed context token and verify active
-- membership in a specific organization. This keeps enqueue/cancel logic in
-- one trusted place even though the calling app_user role cannot read the
-- signing secret.
CREATE OR REPLACE FUNCTION public.require_active_member_for_org(p_context_token TEXT, p_org_id UUID)
RETURNS UUID
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_membership RECORD;
    v_user_id UUID;
BEGIN
    SELECT * INTO v_membership FROM public.resolve_active_membership(p_context_token);
    IF v_membership IS NULL THEN
        RAISE EXCEPTION 'No active organization membership';
    END IF;
    IF v_membership.org_id <> p_org_id THEN
        RAISE EXCEPTION 'Active membership does not match requested organization';
    END IF;
    v_user_id := (SELECT user_id FROM app_private.verify_context_token(p_context_token));
    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'Invalid tenant context token';
    END IF;
    RETURN v_user_id;
END;
$$;

ALTER FUNCTION public.require_active_member_for_org(TEXT, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.require_active_member_for_org(TEXT, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.require_active_member_for_org(TEXT, UUID) TO app_user;

-- Enqueue a job for a transcript. Verifies the caller's active membership,
-- the account/opportunity/transcript organization chain, and idempotency key
-- semantics. Payload, input_refs, and source_manifest must contain only
-- stable references and metadata; transcript bodies are never stored here.
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

    -- Idempotency: same key in the same organization must repeat the same scope.
    IF p_idempotency_key IS NOT NULL THEN
        SELECT id, j.status AS job_status, j.created_at AS job_created_at,
               account_id, transcript_id, opportunity_id, skill
          INTO v_existing
        FROM public.jobs j
        WHERE j.org_id = v_org_id AND j.idempotency_key = p_idempotency_key;

        IF FOUND THEN
            IF v_existing.account_id <> p_account_id
                OR v_existing.transcript_id <> p_transcript_id
                OR COALESCE(v_existing.opportunity_id, '00000000-0000-0000-0000-000000000000'::uuid) <> COALESCE(p_opportunity_id, '00000000-0000-0000-0000-000000000000'::uuid)
                OR v_existing.skill <> p_skill THEN
                RAISE EXCEPTION 'Idempotency key conflict: same key reused with different scope'
                    USING ERRCODE = '40901';
            END IF;
            job_id := v_existing.id;
            job_status := v_existing.job_status;
            job_created_at := v_existing.job_created_at;
            RETURN NEXT;
            RETURN;
        END IF;
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

-- Request job cancellation from the API. Queued jobs move immediately to
-- `cancelled`; running jobs record a cancellation request for the worker.
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
    v_membership RECORD;
BEGIN
    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id;
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
        WHERE id = p_job_id;
        RETURN true;
    END IF;

    -- Running job: record the request; the worker finalizes it.
    UPDATE public.jobs
    SET cancel_requested_at = clock_timestamp(),
        cancelled_by = v_user_id
    WHERE id = p_job_id;
    RETURN true;
END;
$$;

ALTER FUNCTION public.request_job_cancellation(TEXT, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.request_job_cancellation(TEXT, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.request_job_cancellation(TEXT, UUID) TO app_user;

-- Worker helper: compute bounded exponential backoff based on the number of
-- completed attempts. Returns an interval.
CREATE OR REPLACE FUNCTION public.worker_backoff_interval(p_attempt_count INTEGER)
RETURNS INTERVAL
LANGUAGE sql
IMMUTABLE
SECURITY DEFINER
SET search_path = ''
AS $$
    SELECT make_interval(secs => LEAST(GREATEST(p_attempt_count, 1), 6) * LEAST(GREATEST(p_attempt_count, 1), 6));
$$;

ALTER FUNCTION public.worker_backoff_interval(INTEGER) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.worker_backoff_interval(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.worker_backoff_interval(INTEGER) TO app_worker;

-- Recover expired running leases. Marks abandoned attempts, then either
-- requeues with a backoff or moves the job to a terminal timeout/dead-letter
-- state when no attempts remain.
CREATE OR REPLACE FUNCTION public.recover_expired_leases(
    p_backoff_seconds INTEGER DEFAULT NULL
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job RECORD;
    v_count INTEGER := 0;
    v_backoff INTERVAL;
BEGIN
    FOR v_job IN
        SELECT id, org_id, attempts, max_attempts
        FROM public.jobs
        WHERE status = 'running'
          AND timeout_at IS NOT NULL
          AND timeout_at < clock_timestamp()
          AND cancel_requested_at IS NULL
        ORDER BY timeout_at
        FOR UPDATE SKIP LOCKED
    LOOP
        -- Mark the abandoned attempt as a timeout.
        UPDATE public.job_attempts
        SET outcome = 'timeout',
            error_category = 'lease_timeout',
            error = 'Worker lease expired before heartbeat or completion',
            finished_at = clock_timestamp()
        WHERE job_id = v_job.id
          AND attempt_number = v_job.attempts
          AND outcome IS NULL;

        IF v_job.attempts >= v_job.max_attempts THEN
            UPDATE public.jobs
            SET status = 'timeout',
                finished_at = clock_timestamp(),
                timeout_at = NULL,
                dead_lettered = true,
                error = 'Lease timeout after maximum attempts'
            WHERE id = v_job.id;
        ELSE
            IF p_backoff_seconds IS NOT NULL THEN
                v_backoff := make_interval(secs => p_backoff_seconds);
            ELSE
                v_backoff := public.worker_backoff_interval(v_job.attempts);
            END IF;

            UPDATE public.jobs
            SET status = 'queued',
                timeout_at = NULL,
                started_at = NULL,
                worker_id = NULL,
                next_attempt_after = clock_timestamp() + v_backoff
            WHERE id = v_job.id;
        END IF;

        v_count := v_count + 1;
    END LOOP;

    RETURN v_count;
END;
$$;

ALTER FUNCTION public.recover_expired_leases(INTEGER) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.recover_expired_leases(INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.recover_expired_leases(INTEGER) TO app_worker;

-- Atomically claim the next eligible job. Transitions it to running, creates
-- an append-only attempt row, assigns a lease token, and sets the heartbeat
-- timeout. Only one worker can claim a given attempt because of FOR UPDATE SKIP LOCKED.
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
           j.transcript_id, j.opportunity_id, j.payload, j.input_refs,
           j.source_manifest, v_timeout
    FROM public.jobs j
    WHERE j.id = v_job_id;
END;
$$;

ALTER FUNCTION public.claim_next_job(TEXT, INTEGER) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.claim_next_job(TEXT, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.claim_next_job(TEXT, INTEGER) TO app_worker;

-- Extend a running attempt's lease. Requires the current attempt number and
-- lease token, so a stale or reclaimed lease cannot be renewed.
CREATE OR REPLACE FUNCTION public.worker_heartbeat(
    p_job_id UUID,
    p_attempt_number INTEGER,
    p_lease_token UUID,
    p_timeout_seconds INTEGER DEFAULT 300
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_attempt RECORD;
    v_status TEXT;
    v_timeout TIMESTAMPTZ;
BEGIN
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

    SELECT status INTO v_status FROM public.jobs WHERE id = p_job_id;
    IF v_status IS DISTINCT FROM 'running' THEN
        RAISE EXCEPTION 'Job is not running';
    END IF;

    v_timeout := clock_timestamp() + make_interval(secs => p_timeout_seconds);

    UPDATE public.job_attempts
    SET heartbeat_at = clock_timestamp()
    WHERE id = v_attempt.id;

    UPDATE public.jobs
    SET timeout_at = v_timeout
    WHERE id = p_job_id;

    RETURN true;
END;
$$;

ALTER FUNCTION public.worker_heartbeat(UUID, INTEGER, UUID, INTEGER) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.worker_heartbeat(UUID, INTEGER, UUID, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.worker_heartbeat(UUID, INTEGER, UUID, INTEGER) TO app_worker;

-- Complete a running attempt. If cancellation was requested, the job becomes
-- `cancelled` regardless of the supplied result. Otherwise the job reaches a
-- successful terminal state and records aggregate metadata.
CREATE OR REPLACE FUNCTION public.complete_job(
    p_job_id UUID,
    p_attempt_number INTEGER,
    p_lease_token UUID,
    p_result_output_id UUID,
    p_validation_status TEXT,
    p_token_usage JSONB,
    p_cost NUMERIC
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_attempt RECORD;
    v_job RECORD;
BEGIN
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

    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    IF v_job IS NULL OR v_job.status <> 'running' THEN
        RAISE EXCEPTION 'Job is not running';
    END IF;

    IF v_job.cancel_requested_at IS NOT NULL THEN
        UPDATE public.job_attempts
        SET outcome = 'cancelled', finished_at = clock_timestamp()
        WHERE id = v_attempt.id;

        UPDATE public.jobs
        SET status = 'cancelled',
            cancelled_at = clock_timestamp(),
            finished_at = clock_timestamp(),
            timeout_at = NULL
        WHERE id = p_job_id;
        RETURN true;
    END IF;

    UPDATE public.job_attempts
    SET outcome = 'success',
        finished_at = clock_timestamp(),
        token_usage = COALESCE(p_token_usage, '{}'),
        cost = p_cost
    WHERE id = v_attempt.id;

    UPDATE public.jobs
    SET status = 'success',
        finished_at = clock_timestamp(),
        timeout_at = NULL,
        result_output_id = p_result_output_id,
        validation_status = COALESCE(p_validation_status, 'unvalidated'),
        token_usage = COALESCE(p_token_usage, '{}'),
        cost = p_cost
    WHERE id = p_job_id;

    RETURN true;
END;
$$;

ALTER FUNCTION public.complete_job(UUID, INTEGER, UUID, UUID, TEXT, JSONB, NUMERIC) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.complete_job(UUID, INTEGER, UUID, UUID, TEXT, JSONB, NUMERIC) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.complete_job(UUID, INTEGER, UUID, UUID, TEXT, JSONB, NUMERIC) TO app_worker;

-- Mark a running attempt as failed. Retryable errors requeue the job with a
-- backoff; exhausted attempts produce a terminal `failure` or `timeout` state
-- depending on the error category.
CREATE OR REPLACE FUNCTION public.fail_job(
    p_job_id UUID,
    p_attempt_number INTEGER,
    p_lease_token UUID,
    p_error_category TEXT,
    p_error TEXT,
    p_backoff_seconds INTEGER DEFAULT NULL
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

    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    IF v_job IS NULL OR v_job.status <> 'running' THEN
        RAISE EXCEPTION 'Job is not running';
    END IF;

    v_outcome := CASE WHEN p_error_category = 'timeout' THEN 'timeout' ELSE 'failure' END;

    UPDATE public.job_attempts
    SET outcome = v_outcome,
        error_category = p_error_category,
        error = p_error,
        finished_at = clock_timestamp()
    WHERE id = v_attempt.id;

    -- Cancellation takes precedence over retry logic.
    IF v_job.cancel_requested_at IS NOT NULL THEN
        UPDATE public.jobs
        SET status = 'cancelled',
            cancelled_at = clock_timestamp(),
            finished_at = clock_timestamp(),
            timeout_at = NULL,
            next_attempt_after = NULL
        WHERE id = p_job_id;
        RETURN true;
    END IF;

    IF v_job.attempts >= v_job.max_attempts THEN
        v_status := CASE WHEN p_error_category = 'timeout' THEN 'timeout' ELSE 'failure' END;
        UPDATE public.jobs
        SET status = v_status,
            finished_at = clock_timestamp(),
            timeout_at = NULL,
            next_attempt_after = NULL,
            dead_lettered = true,
            error = p_error
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
            error = p_error
        WHERE id = p_job_id;
    END IF;

    RETURN true;
END;
$$;

ALTER FUNCTION public.fail_job(UUID, INTEGER, UUID, TEXT, TEXT, INTEGER) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.fail_job(UUID, INTEGER, UUID, TEXT, TEXT, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.fail_job(UUID, INTEGER, UUID, TEXT, TEXT, INTEGER) TO app_worker;

-- Worker finalizer for cancellation when the worker wants to stop cleanly.
CREATE OR REPLACE FUNCTION public.cancel_job(
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
    v_attempt RECORD;
BEGIN
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

    UPDATE public.job_attempts
    SET outcome = 'cancelled', finished_at = clock_timestamp()
    WHERE id = v_attempt.id;

    UPDATE public.jobs
    SET status = 'cancelled',
        cancel_requested_at = COALESCE(cancel_requested_at, clock_timestamp()),
        cancelled_at = clock_timestamp(),
        finished_at = clock_timestamp(),
        timeout_at = NULL,
        next_attempt_after = NULL
    WHERE id = p_job_id
      AND status = 'running';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Job is not running';
    END IF;

    RETURN true;
END;
$$;

ALTER FUNCTION public.cancel_job(UUID, INTEGER, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.cancel_job(UUID, INTEGER, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.cancel_job(UUID, INTEGER, UUID) TO app_worker;

-- Prevent updates/deletes of terminal jobs by the application role. The worker
-- functions lock and mutate only running rows through their own path.
GRANT ALL ON TABLE public.jobs, public.job_attempts TO app_admin;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO app_admin;

-- Default privileges remain app_admin-only; any future public tables are not
-- automatically reachable by app_user or app_worker.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO app_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO app_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO app_admin;
