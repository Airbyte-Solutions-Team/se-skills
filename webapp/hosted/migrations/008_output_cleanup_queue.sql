-- 008_output_cleanup_queue.sql
-- Add a durable, self-discovering cleanup queue for tombstoned outputs.
-- Workers can poll `claim_next_tombstoned_output` without knowing the output id
-- in advance.  The lease-bound claim/finalize path remains the same.

-- Tighten the targeted tombstone claim with non-empty worker id, strictly
-- positive lease, and an upper bound so callers cannot bypass exclusion with 0
-- or hold a row indefinitely.
CREATE OR REPLACE FUNCTION public.claim_tombstoned_output(
    p_output_id UUID,
    p_worker_id TEXT,
    p_lease_seconds INTEGER DEFAULT 60
)
RETURNS TABLE(content_storage_path TEXT, org_id UUID, requester_id UUID)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_output RECORD;
    v_lease TIMESTAMPTZ;
    v_max_lease INTEGER := 3600;
BEGIN
    IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0 THEN
        RAISE EXCEPTION 'Worker id is required';
    END IF;
    IF p_lease_seconds IS NULL OR p_lease_seconds <= 0 OR p_lease_seconds > v_max_lease THEN
        RAISE EXCEPTION 'Lease seconds must be between 1 and %', v_max_lease;
    END IF;

    v_lease := clock_timestamp() - make_interval(secs => p_lease_seconds);

    SELECT * INTO v_output
    FROM public.outputs
    WHERE id = p_output_id
      AND validation_status = 'unvalidated'
      AND tombstoned_at IS NOT NULL
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    IF v_output.cleanup_claimed_by IS NOT NULL
       AND v_output.cleanup_claimed_by <> p_worker_id
       AND v_output.cleanup_claimed_at > v_lease THEN
        RAISE EXCEPTION 'Tombstoned output % is already claimed by %',
            p_output_id, v_output.cleanup_claimed_by;
    END IF;

    UPDATE public.outputs
       SET cleanup_claimed_by = p_worker_id,
           cleanup_claimed_at = clock_timestamp()
     WHERE id = p_output_id;

    content_storage_path := v_output.content_storage_path;
    org_id := v_output.org_id;
    requester_id := v_output.requester_id;
    RETURN NEXT;
END;
$$;

ALTER FUNCTION public.claim_tombstoned_output(UUID, TEXT, INTEGER) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.claim_tombstoned_output(UUID, TEXT, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.claim_tombstoned_output(UUID, TEXT, INTEGER) TO app_worker;

-- Atomically discover and claim the next tombstoned output that is not already
-- held by another worker within its lease.  Same-worker re-claims are allowed
-- so a worker can retry a failed Storage delete; other workers must wait for
-- the lease to expire.  Uses FOR UPDATE SKIP LOCKED for concurrent polling.
CREATE OR REPLACE FUNCTION public.claim_next_tombstoned_output(
    p_worker_id TEXT,
    p_lease_seconds INTEGER DEFAULT 60
)
RETURNS TABLE(output_id UUID, content_storage_path TEXT, org_id UUID, requester_id UUID)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_output RECORD;
    v_lease TIMESTAMPTZ;
    v_max_lease INTEGER := 3600;
BEGIN
    IF p_worker_id IS NULL OR length(trim(p_worker_id)) = 0 THEN
        RAISE EXCEPTION 'Worker id is required';
    END IF;
    IF p_lease_seconds IS NULL OR p_lease_seconds <= 0 OR p_lease_seconds > v_max_lease THEN
        RAISE EXCEPTION 'Lease seconds must be between 1 and %', v_max_lease;
    END IF;

    v_lease := clock_timestamp() - make_interval(secs => p_lease_seconds);

    SELECT * INTO v_output
    FROM public.outputs
    WHERE validation_status = 'unvalidated'
      AND tombstoned_at IS NOT NULL
      AND (
          cleanup_claimed_by IS NULL
          OR cleanup_claimed_at < v_lease
          OR cleanup_claimed_by = p_worker_id
      )
    ORDER BY tombstoned_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1;

    IF NOT FOUND THEN
        RETURN;
    END IF;

    UPDATE public.outputs
       SET cleanup_claimed_by = p_worker_id,
           cleanup_claimed_at = clock_timestamp()
     WHERE id = v_output.id;

    output_id := v_output.id;
    content_storage_path := v_output.content_storage_path;
    org_id := v_output.org_id;
    requester_id := v_output.requester_id;
    RETURN NEXT;
END;
$$;

ALTER FUNCTION public.claim_next_tombstoned_output(TEXT, INTEGER) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.claim_next_tombstoned_output(TEXT, INTEGER) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.claim_next_tombstoned_output(TEXT, INTEGER) TO app_worker;
