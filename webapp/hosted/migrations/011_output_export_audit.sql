-- 011_output_export_audit.sql
-- Hosted export of an approved output, with append-only export audit (Slice 6B1).
--
--   - `output_export` joins the `audit_events` action allowlist.
--   - `public.authorize_output_export` is the atomic authorization snapshot: it
--     locks the output row, derives the actor from the signed tenant context,
--     verifies active membership, resolves the current version of the linear
--     correction chain, requires an approval of *that exact version*, and
--     returns the trusted Storage path of the immutable artifact to export.
--   - `public.record_output_export` appends the export audit event after the
--     artifact has been materialized, revalidating the
--     organization/output/version relationship in SQL and staying idempotent per
--     request id.
--
-- The browser supplies only an output id, an account id, a format enum, and an
-- idempotency key. Version identity, approval state, provenance, and Storage
-- paths are all derived here. `app_user` still has no direct
-- INSERT/UPDATE/DELETE on `audit_events`.
--
-- Authorization and audit are deliberately two calls in two transactions: the
-- exported bytes come from an immutable version, so no lock has to be held
-- across the Storage read or the PDF render, and a Storage or renderer failure
-- cannot leave a successful export audit behind.

ALTER TABLE public.audit_events DROP CONSTRAINT IF EXISTS audit_events_action_check;
ALTER TABLE public.audit_events ADD CONSTRAINT audit_events_action_check
    CHECK (action IN ('output_comment', 'output_correction', 'output_approval', 'output_export'));

-- ---------------------------------------------------------------------------
-- Authorization snapshot
--
-- SE001 output is not accessible (indistinguishable from missing)
-- SE002 the recorded export target is no longer the current version
-- SE003 request id reused with a different export intent
-- SE004 malformed version chain
-- SE005 invalid input
-- SE006 the current version is not approved
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.authorize_output_export(
    p_output_id UUID,
    p_account_id UUID,
    p_format TEXT,
    p_request_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_output public.outputs;
    v_user_id UUID;
    v_current UUID;
    v_path TEXT;
    v_ordinal INTEGER;
    v_audit public.audit_events;
    v_replayed BOOLEAN;
BEGIN
    IF p_request_id IS NULL THEN
        RAISE EXCEPTION 'request id is required' USING ERRCODE = 'SE005';
    END IF;
    IF p_format IS NULL OR p_format NOT IN ('md', 'pdf') THEN
        RAISE EXCEPTION 'unsupported export format' USING ERRCODE = 'SE005';
    END IF;

    -- Locking the output row serializes this snapshot against a concurrent
    -- correction or approval, so the version, its approval, and its Storage path
    -- are all read from one consistent state.
    v_output := app_private.reviewable_output(p_output_id, p_account_id, TRUE);
    v_user_id := public.current_request_user_id();

    v_current := app_private.current_output_version(p_output_id);

    IF NOT EXISTS (
        SELECT 1 FROM public.reviews r
        WHERE r.org_id = v_output.org_id
          AND r.output_id = p_output_id
          AND r.action = 'approve'
          AND r.output_version_id IS NOT DISTINCT FROM v_current
    ) THEN
        RAISE EXCEPTION 'current version is not approved' USING ERRCODE = 'SE006';
    END IF;

    IF v_current IS NULL THEN
        v_path := v_output.content_storage_path;
        v_ordinal := 0;
    ELSE
        SELECT v.content_storage_path INTO v_path
        FROM public.output_versions v
        WHERE v.id = v_current
          AND v.output_id = p_output_id
          AND v.org_id = v_output.org_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'output not accessible' USING ERRCODE = 'SE001';
        END IF;
        SELECT count(*) INTO v_ordinal
        FROM public.output_versions v
        WHERE v.output_id = p_output_id
          AND v.org_id = v_output.org_id;
    END IF;

    IF v_path IS NULL OR length(v_path) = 0 THEN
        RAISE EXCEPTION 'output not accessible' USING ERRCODE = 'SE001';
    END IF;

    -- A replayed request id must describe the same export. The recorded version
    -- wins over anything the browser could ask for, and an export whose recorded
    -- version is no longer current is a stale conflict rather than a silent
    -- export of superseded content.
    SELECT a.* INTO v_audit
    FROM public.audit_events a
    WHERE a.org_id = v_output.org_id
      AND a.action = 'output_export'
      AND a.request_id = p_request_id;
    v_replayed := FOUND;

    IF v_replayed THEN
        IF (v_audit.metadata ->> 'output_id') IS DISTINCT FROM p_output_id::text
            OR (v_audit.metadata ->> 'format') IS DISTINCT FROM p_format THEN
            RAISE EXCEPTION 'idempotency key reused with a different export' USING ERRCODE = 'SE003';
        END IF;
        IF (v_audit.metadata ->> 'output_version_id') IS DISTINCT FROM
                (CASE WHEN v_current IS NULL THEN NULL ELSE v_current::text END) THEN
            RAISE EXCEPTION 'recorded export target is not the current version' USING ERRCODE = 'SE002';
        END IF;
    END IF;

    RETURN jsonb_build_object(
        'org_id', v_output.org_id,
        'output_id', p_output_id,
        'output_version_id', v_current,
        'version_ordinal', v_ordinal,
        'content_storage_path', v_path,
        'format', p_format,
        'replayed', v_replayed
    );
END;
$$;

ALTER FUNCTION public.authorize_output_export(UUID, UUID, TEXT, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.authorize_output_export(UUID, UUID, TEXT, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.authorize_output_export(UUID, UUID, TEXT, UUID) TO app_user;

-- ---------------------------------------------------------------------------
-- Export audit event
--
-- Called only once the exact authorized bytes exist, so a Storage or renderer
-- failure never produces a successful export record. The version is *not*
-- required to still be current: a correction committed while the artifact was
-- being rendered must not rewrite history, and the audit has to describe the
-- bytes the caller actually received.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.record_output_export(
    p_output_id UUID,
    p_account_id UUID,
    p_output_version_id UUID,
    p_format TEXT,
    p_request_id UUID
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_output public.outputs;
    v_user_id UUID;
    v_audit public.audit_events;
    v_audit_id UUID;
BEGIN
    IF p_request_id IS NULL THEN
        RAISE EXCEPTION 'request id is required' USING ERRCODE = 'SE005';
    END IF;
    IF p_format IS NULL OR p_format NOT IN ('md', 'pdf') THEN
        RAISE EXCEPTION 'unsupported export format' USING ERRCODE = 'SE005';
    END IF;

    v_output := app_private.reviewable_output(p_output_id, p_account_id, FALSE);
    v_user_id := public.current_request_user_id();
    PERFORM app_private.assert_version_of_output(p_output_id, v_output.org_id, p_output_version_id);

    SELECT a.* INTO v_audit
    FROM public.audit_events a
    WHERE a.org_id = v_output.org_id
      AND a.action = 'output_export'
      AND a.request_id = p_request_id;

    IF FOUND THEN
        IF (v_audit.metadata ->> 'output_id') IS DISTINCT FROM p_output_id::text
            OR (v_audit.metadata ->> 'format') IS DISTINCT FROM p_format
            OR (v_audit.metadata ->> 'output_version_id') IS DISTINCT FROM
                (CASE WHEN p_output_version_id IS NULL THEN NULL ELSE p_output_version_id::text END)
            OR v_audit.user_id IS DISTINCT FROM v_user_id THEN
            RAISE EXCEPTION 'idempotency key reused with a different export' USING ERRCODE = 'SE003';
        END IF;
        RETURN jsonb_build_object(
            'audit_id', v_audit.id,
            'output_version_id', p_output_version_id,
            'created_at', v_audit.created_at,
            'replayed', true
        );
    END IF;

    -- Metadata carries safe identifiers and the format enum only: no Markdown,
    -- comments, filenames, customer names, Storage paths, or signed URLs.
    BEGIN
        v_audit_id := app_private.record_audit_event(
            v_output.org_id,
            v_user_id,
            'output_export',
            'outputs',
            p_output_id,
            p_request_id,
            jsonb_build_object(
                'output_id', p_output_id,
                'output_version_id', p_output_version_id,
                'format', p_format,
                'request_id', p_request_id
            )
        );
    EXCEPTION WHEN unique_violation THEN
        -- Two concurrent retries of the same request: the first insert wins and
        -- the second reads it back rather than duplicating the evidence.
        SELECT a.* INTO v_audit
        FROM public.audit_events a
        WHERE a.org_id = v_output.org_id
          AND a.action = 'output_export'
          AND a.request_id = p_request_id;
        IF NOT FOUND
            OR (v_audit.metadata ->> 'output_id') IS DISTINCT FROM p_output_id::text
            OR (v_audit.metadata ->> 'format') IS DISTINCT FROM p_format
            OR (v_audit.metadata ->> 'output_version_id') IS DISTINCT FROM
                (CASE WHEN p_output_version_id IS NULL THEN NULL ELSE p_output_version_id::text END) THEN
            RAISE EXCEPTION 'idempotency key reused with a different export' USING ERRCODE = 'SE003';
        END IF;
        RETURN jsonb_build_object(
            'audit_id', v_audit.id,
            'output_version_id', p_output_version_id,
            'created_at', v_audit.created_at,
            'replayed', true
        );
    END;

    RETURN jsonb_build_object(
        'audit_id', v_audit_id,
        'output_version_id', p_output_version_id,
        'created_at', now(),
        'replayed', false
    );
END;
$$;

ALTER FUNCTION public.record_output_export(UUID, UUID, UUID, TEXT, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.record_output_export(UUID, UUID, UUID, TEXT, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.record_output_export(UUID, UUID, UUID, TEXT, UUID) TO app_user;
