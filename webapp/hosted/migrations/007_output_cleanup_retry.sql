-- 007_output_cleanup_retry.sql
-- Make tombstoned output cleanup independently retryable after the execution
-- attempt has finished. Cancellation or a failed Storage delete can leave a
-- hidden tombstone row with an orphaned object; a lease-bound cleanup worker
-- can claim the tombstone, delete the object, and finalize deletion without
-- depending on the original attempt still being running.

ALTER TABLE public.outputs
    ADD COLUMN IF NOT EXISTS cleanup_claimed_by TEXT,
    ADD COLUMN IF NOT EXISTS cleanup_claimed_at TIMESTAMPTZ;

-- Claim a tombstoned output for cleanup. The row must be unvalidated and
-- tombstoned and not currently claimed by another worker within the lease.
-- Returns the trusted content_storage_path, org_id, and requester_id so the
-- worker can remove the Storage object before finalizing the tombstone.
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
BEGIN
    IF p_lease_seconds IS NULL OR p_lease_seconds < 0 THEN
        p_lease_seconds := 60;
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

-- Finalize cleanup by deleting a tombstoned output that the caller has claimed.
-- The row must still be unvalidated and tombstoned and claimed by this worker.
CREATE OR REPLACE FUNCTION public.finalize_tombstone_delete(
    p_output_id UUID,
    p_worker_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_output RECORD;
BEGIN
    SELECT * INTO v_output
    FROM public.outputs
    WHERE id = p_output_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF v_output.cleanup_claimed_by <> p_worker_id THEN
        RAISE EXCEPTION 'Tombstoned output % is not claimed by %',
            p_output_id, p_worker_id;
    END IF;

    IF v_output.validation_status <> 'unvalidated' OR v_output.tombstoned_at IS NULL THEN
        RAISE EXCEPTION 'Tombstoned output % is not eligible for cleanup', p_output_id;
    END IF;

    DELETE FROM public.outputs
    WHERE id = p_output_id
      AND cleanup_claimed_by = p_worker_id
      AND validation_status = 'unvalidated'
      AND tombstoned_at IS NOT NULL;

    RETURN FOUND;
END;
$$;

ALTER FUNCTION public.finalize_tombstone_delete(UUID, TEXT) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.finalize_tombstone_delete(UUID, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.finalize_tombstone_delete(UUID, TEXT) TO app_worker;
