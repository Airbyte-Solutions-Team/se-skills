-- 006_output_tombstones_and_rls.sql
-- Harden output cleanup and tenant visibility.
--   - Add tombstoned_at to outputs to track staged/cancelled evidence.
--   - Only allow app_user to see valid, non-tombstone outputs and their
--     dependent output_versions/reviews rows.
--   - Replace destructive DELETEs in cancel/complete with tombstoning so
--     unvalidated storage objects are not orphaned without a metadata record.
--   - Add tombstone_job_output and require a tombstone before delete_job_output.

ALTER TABLE public.outputs
    ADD COLUMN IF NOT EXISTS tombstoned_at TIMESTAMPTZ;

DROP POLICY IF EXISTS org_tenant_outputs ON public.outputs;
CREATE POLICY org_tenant_outputs ON public.outputs
    FOR SELECT TO app_user
    USING (public.is_active_org_member(org_id)
           AND validation_status = 'valid'
           AND tombstoned_at IS NULL);

DROP POLICY IF EXISTS org_tenant_output_versions ON public.output_versions;
CREATE POLICY org_tenant_output_versions ON public.output_versions
    FOR SELECT TO app_user
    USING (public.is_active_org_member(org_id)
           AND EXISTS (
               SELECT 1 FROM public.outputs o
               WHERE o.id = output_versions.output_id
                 AND o.org_id = output_versions.org_id
                 AND o.validation_status = 'valid'
                 AND o.tombstoned_at IS NULL
           ));

DROP POLICY IF EXISTS org_tenant_reviews ON public.reviews;
CREATE POLICY org_tenant_reviews ON public.reviews
    FOR SELECT TO app_user
    USING (public.is_active_org_member(org_id)
           AND EXISTS (
               SELECT 1 FROM public.outputs o
               WHERE o.id = reviews.output_id
                 AND o.org_id = reviews.org_id
                 AND o.validation_status = 'valid'
                 AND o.tombstoned_at IS NULL
           ));

-- Tombstone an unvalidated output row for a running attempt.
-- Returns the content_storage_path so the host can delete the storage object.
CREATE OR REPLACE FUNCTION public.tombstone_job_output(
    p_output_id UUID,
    p_job_id UUID,
    p_attempt_number INTEGER,
    p_lease_token UUID
)
RETURNS TEXT
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

    SELECT content_storage_path
      INTO v_output
      FROM public.outputs
     WHERE id = p_output_id
       AND job_id = p_job_id
       AND org_id = v_job.org_id
       AND validation_status = 'unvalidated';

    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    UPDATE public.outputs
       SET tombstoned_at = clock_timestamp()
     WHERE id = p_output_id
       AND job_id = p_job_id
       AND org_id = v_job.org_id
       AND validation_status = 'unvalidated';

    RETURN v_output.content_storage_path;
END;
$$;

ALTER FUNCTION public.tombstone_job_output(UUID, UUID, INTEGER, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.tombstone_job_output(UUID, UUID, INTEGER, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.tombstone_job_output(UUID, UUID, INTEGER, UUID) TO app_worker;

-- delete_job_output now only removes rows that have been tombstoned or are still
-- unvalidated.  The tombstone requirement prevents deleting the only metadata
-- record before the host has removed the corresponding storage object.
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
      AND org_id = v_job.org_id
      AND (validation_status = 'unvalidated' OR tombstoned_at IS NOT NULL);

    RETURN FOUND;
END;
$$;

ALTER FUNCTION public.delete_job_output(UUID, UUID, INTEGER, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.delete_job_output(UUID, UUID, INTEGER, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.delete_job_output(UUID, UUID, INTEGER, UUID) TO app_worker;

-- complete_job cancellation branch: tombstone instead of delete.
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
            UPDATE public.outputs
               SET tombstoned_at = clock_timestamp()
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
        SET validation_status = COALESCE(p_validation_status, 'unvalidated'),
            tombstoned_at = NULL
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

-- cancel_job: tombstone any unvalidated outputs for the job instead of deleting.
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

    UPDATE public.outputs
       SET tombstoned_at = clock_timestamp()
     WHERE job_id = p_job_id
       AND org_id = v_job.org_id
       AND validation_status = 'unvalidated'
       AND tombstoned_at IS NULL;

    RETURN true;
END;
$$;

ALTER FUNCTION public.cancel_job(UUID, INTEGER, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.cancel_job(UUID, INTEGER, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.cancel_job(UUID, INTEGER, UUID) TO app_worker;

-- fail_job: tombstone any unvalidated outputs for the job so they are never readable.
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
    ELSE
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
    END IF;

    UPDATE public.outputs
       SET tombstoned_at = clock_timestamp()
     WHERE job_id = p_job_id
       AND org_id = v_job.org_id
       AND validation_status = 'unvalidated'
       AND tombstoned_at IS NULL;

    RETURN true;
END;
$$;

ALTER FUNCTION public.fail_job(UUID, INTEGER, UUID, TEXT, TEXT, INTEGER, TEXT, TEXT, TEXT) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.fail_job(UUID, INTEGER, UUID, TEXT, TEXT, INTEGER, TEXT, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.fail_job(UUID, INTEGER, UUID, TEXT, TEXT, INTEGER, TEXT, TEXT, TEXT) TO app_worker;
