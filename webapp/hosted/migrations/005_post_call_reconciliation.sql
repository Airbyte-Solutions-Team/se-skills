-- 005_post_call_reconciliation.sql
-- Harden the post-call persistence boundary: require valid prior outputs,
-- reconcile staged outputs after a failed completion, and clean up unvalidated
-- evidence when a job is cancelled.

-- -----------------------------------------------------------------------------
-- Reconcile an unvalidated output staged by a previous attempt.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.get_staged_output(
    p_job_id UUID,
    p_attempt_number INTEGER,
    p_lease_token UUID,
    p_output_id UUID
)
RETURNS TABLE(
    output_id UUID,
    content_storage_path TEXT,
    sidecar JSONB,
    validation_status TEXT,
    runtime_version TEXT,
    model TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job RECORD;
    v_attempt RECORD;
    v_output RECORD;
BEGIN
    SELECT * INTO v_job
    FROM public.jobs
    WHERE id = p_job_id
    FOR UPDATE;

    IF v_job IS NULL THEN
        RAISE EXCEPTION 'Job not found';
    END IF;

    IF v_job.status NOT IN ('running', 'success') THEN
        RAISE EXCEPTION 'Job is not running or completed';
    END IF;

    SELECT * INTO v_attempt
    FROM public.job_attempts
    WHERE job_id = p_job_id
      AND attempt_number = p_attempt_number
    FOR UPDATE;

    IF v_attempt IS NULL OR v_attempt.lease_token <> p_lease_token THEN
        RAISE EXCEPTION 'Invalid attempt or lease token';
    END IF;

    -- If the attempt is already finished, the only valid use of this function is
    -- to read back a completed output for the same attempt.
    IF v_attempt.outcome IS NOT NULL AND v_attempt.outcome <> 'success' THEN
        RAISE EXCEPTION 'Attempt already finished';
    END IF;

    SELECT o.id, o.content_storage_path, o.sidecar, o.validation_status,
           o.runtime_version, o.model
      INTO v_output
      FROM public.outputs o
     WHERE o.id = p_output_id
       AND o.job_id = p_job_id
       AND o.org_id = v_job.org_id;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    output_id := v_output.id;
    content_storage_path := v_output.content_storage_path;
    sidecar := v_output.sidecar;
    validation_status := v_output.validation_status;
    runtime_version := v_output.runtime_version;
    model := v_output.model;
    RETURN NEXT;
END;
$$;

ALTER FUNCTION public.get_staged_output(UUID, INTEGER, UUID, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.get_staged_output(UUID, INTEGER, UUID, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.get_staged_output(UUID, INTEGER, UUID, UUID) TO app_worker;

-- -----------------------------------------------------------------------------
-- Require that prior-context references point to validated outputs.
-- -----------------------------------------------------------------------------
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

            SELECT o.id, o.content_storage_path INTO v_prior
            FROM public.outputs o
            WHERE o.id = v_prior_id
              AND o.org_id = v_job.org_id
              AND o.account_id = v_job.account_id
              AND (o.opportunity_id IS NOT DISTINCT FROM v_job.opportunity_id)
              AND o.validation_status = 'valid'
              AND EXISTS (
                  SELECT 1 FROM public.jobs j
                  WHERE j.id = o.job_id AND j.status = 'success'
              );

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

-- -----------------------------------------------------------------------------
-- Complete a job, returning whether it completed or was cancelled.
-- On cancellation, remove the unvalidated staged output row so it cannot be
-- read or listed until a successful attempt re-creates it.
-- -----------------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.complete_job(uuid, integer, uuid, uuid, text, jsonb, numeric, text, text) CASCADE;

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
RETURNS TEXT
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

        IF p_result_output_id IS NOT NULL THEN
            DELETE FROM public.outputs
            WHERE id = p_result_output_id
              AND job_id = p_job_id
              AND org_id = v_job.org_id
              AND validation_status = 'unvalidated';
        END IF;

        RETURN 'cancelled';
    END IF;

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

    RETURN 'completed';
END;
$$;

ALTER FUNCTION public.complete_job(UUID, INTEGER, UUID, UUID, TEXT, JSONB, NUMERIC, TEXT, TEXT) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.complete_job(UUID, INTEGER, UUID, UUID, TEXT, JSONB, NUMERIC, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.complete_job(UUID, INTEGER, UUID, UUID, TEXT, JSONB, NUMERIC, TEXT, TEXT) TO app_worker;

-- -----------------------------------------------------------------------------
-- Cancel a job and remove any unvalidated output evidence for the job.
-- This is idempotent with the trusted orchestrator's own cleanup.
-- -----------------------------------------------------------------------------
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
    v_job RECORD;
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

    DELETE FROM public.outputs
    WHERE job_id = p_job_id
      AND org_id = v_job.org_id
      AND validation_status = 'unvalidated';

    RETURN true;
END;
$$;

ALTER FUNCTION public.cancel_job(UUID, INTEGER, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.cancel_job(UUID, INTEGER, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.cancel_job(UUID, INTEGER, UUID) TO app_worker;
