-- 009_review_versioning_and_audit.sql
-- Hosted output review, correction, approval, versioning, and audit (Slice 6A).
--
--   - Durable append-only `audit_events` table.
--   - Idempotency keys on `reviews` and `output_versions`.
--   - Database-enforced linear correction chain (one root, one child per base).
--   - `output_correction_uploads` ledger so a private correction object can
--     never exist without durable, recoverable metadata.
--   - Narrow SECURITY DEFINER functions that derive the actor from the signed
--     tenant context, verify active membership, lock the output row, and write
--     the review/version and its audit event in one transaction.
--
-- `app_user` keeps no direct INSERT/UPDATE/DELETE on outputs, output_versions,
-- reviews, or audit_events.

-- ---------------------------------------------------------------------------
-- Audit events
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    user_id UUID REFERENCES public.users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id UUID,
    request_id UUID,
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE public.audit_events DROP CONSTRAINT IF EXISTS audit_events_action_check;
ALTER TABLE public.audit_events ADD CONSTRAINT audit_events_action_check
    CHECK (action IN ('output_comment', 'output_correction', 'output_approval'));

CREATE INDEX IF NOT EXISTS idx_audit_events_org_id ON public.audit_events(org_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_events_entity ON public.audit_events(entity_type, entity_id);
-- One audit event per (action, request) so a retried submission cannot double-log.
CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_events_request
    ON public.audit_events(org_id, action, request_id)
    WHERE request_id IS NOT NULL;

ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_tenant_audit_events ON public.audit_events;
CREATE POLICY org_tenant_audit_events ON public.audit_events
    FOR SELECT TO app_user
    USING (public.is_active_org_member(org_id));

GRANT SELECT ON TABLE public.audit_events TO app_user;
GRANT ALL ON TABLE public.audit_events TO app_admin;

-- ---------------------------------------------------------------------------
-- Idempotency keys and chain invariants
-- ---------------------------------------------------------------------------
ALTER TABLE public.reviews ADD COLUMN IF NOT EXISTS request_id UUID;
ALTER TABLE public.output_versions ADD COLUMN IF NOT EXISTS request_id UUID;

CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_request
    ON public.reviews(org_id, request_id)
    WHERE request_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_output_versions_request
    ON public.output_versions(org_id, request_id)
    WHERE request_id IS NOT NULL;

-- Linear correction chain, enforced by the database rather than by a
-- read-then-insert sequence in Python:
--   * at most one correction whose base is the immutable generated output;
--   * at most one correction per existing version.
CREATE UNIQUE INDEX IF NOT EXISTS idx_output_versions_single_root
    ON public.output_versions(output_id)
    WHERE previous_version_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_output_versions_single_child
    ON public.output_versions(output_id, previous_version_id)
    WHERE previous_version_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Correction upload ledger
--
-- A correction reserves its version id and server-generated Storage path before
-- any object is uploaded. The reservation is the durable evidence that a private
-- object may exist, so a Storage-success/DB-failure path is always recoverable:
--   pending   -> object may exist, no version/review/audit rows
--   committed -> version/review/audit rows exist and own the object
--   aborted   -> no object (upload failed, or object deleted after a conflict)
--   orphaned  -> object exists but must be deleted; retryable cleanup evidence
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.output_correction_uploads (
    id UUID PRIMARY KEY,
    org_id UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    output_id UUID NOT NULL,
    base_version_id UUID,
    request_id UUID NOT NULL,
    content_storage_path TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    created_by UUID NOT NULL,
    cleanup_attempts INTEGER NOT NULL DEFAULT 0,
    cleanup_claimed_by TEXT,
    cleanup_claimed_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (state IN ('pending', 'committed', 'aborted', 'orphaned')),
    FOREIGN KEY (output_id, org_id) REFERENCES public.outputs(id, org_id) ON DELETE CASCADE,
    FOREIGN KEY (base_version_id, output_id, org_id)
        REFERENCES public.output_versions(id, output_id, org_id) ON DELETE RESTRICT,
    FOREIGN KEY (created_by, org_id) REFERENCES public.memberships(user_id, org_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_correction_uploads_request
    ON public.output_correction_uploads(org_id, request_id);
CREATE INDEX IF NOT EXISTS idx_correction_uploads_state
    ON public.output_correction_uploads(state, cleanup_attempts);

-- The ledger holds server-generated Storage paths, so `app_user` gets no direct
-- access at all: it is reachable only through the SECURITY DEFINER correction
-- functions, which never return a path to a browser.
ALTER TABLE public.output_correction_uploads ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS org_tenant_correction_uploads ON public.output_correction_uploads;

REVOKE ALL ON TABLE public.output_correction_uploads FROM app_user;
GRANT ALL ON TABLE public.output_correction_uploads TO app_admin;

-- ---------------------------------------------------------------------------
-- Internal helpers
-- ---------------------------------------------------------------------------

-- Resolve a reviewable output for the current signed request context. Raises
-- SE001 (mapped to an indistinguishable 404) when the output does not exist, is
-- not in the caller's active organization, is not valid, is tombstoned, or does
-- not belong to the supplied account.
CREATE OR REPLACE FUNCTION app_private.reviewable_output(
    p_output_id UUID,
    p_account_id UUID,
    p_lock BOOLEAN DEFAULT FALSE
)
RETURNS public.outputs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_user_id UUID;
    v_output public.outputs;
BEGIN
    v_user_id := public.current_request_user_id();
    IF v_user_id IS NULL THEN
        RAISE EXCEPTION 'output not accessible' USING ERRCODE = 'SE001';
    END IF;

    IF p_lock THEN
        SELECT o.* INTO v_output
        FROM public.outputs o
        WHERE o.id = p_output_id
          AND o.account_id = p_account_id
          AND o.validation_status = 'valid'
          AND o.tombstoned_at IS NULL
        FOR UPDATE;
    ELSE
        SELECT o.* INTO v_output
        FROM public.outputs o
        WHERE o.id = p_output_id
          AND o.account_id = p_account_id
          AND o.validation_status = 'valid'
          AND o.tombstoned_at IS NULL;
    END IF;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'output not accessible' USING ERRCODE = 'SE001';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM public.memberships m
        WHERE m.user_id = v_user_id
          AND m.org_id = v_output.org_id
          AND m.active = true
    ) THEN
        RAISE EXCEPTION 'output not accessible' USING ERRCODE = 'SE001';
    END IF;

    RETURN v_output;
END;
$$;

ALTER FUNCTION app_private.reviewable_output(UUID, UUID, BOOLEAN) OWNER TO app_admin;
REVOKE ALL ON FUNCTION app_private.reviewable_output(UUID, UUID, BOOLEAN) FROM PUBLIC;

-- Return the current reviewable version id for an output: NULL when no
-- correction exists (the immutable generated output is current), otherwise the
-- single leaf of the correction chain. A branched or multi-root chain fails
-- closed with SE004 instead of silently choosing a winner.
CREATE OR REPLACE FUNCTION app_private.current_output_version(p_output_id UUID)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_roots INTEGER;
    v_leaves UUID[];
BEGIN
    SELECT count(*) INTO v_roots
    FROM public.output_versions v
    WHERE v.output_id = p_output_id
      AND v.previous_version_id IS NULL;

    IF v_roots > 1 THEN
        RAISE EXCEPTION 'malformed version chain' USING ERRCODE = 'SE004';
    END IF;

    SELECT array_agg(v.id) INTO v_leaves
    FROM public.output_versions v
    WHERE v.output_id = p_output_id
      AND NOT EXISTS (
          SELECT 1 FROM public.output_versions c
          WHERE c.output_id = p_output_id
            AND c.previous_version_id = v.id
      );

    IF v_leaves IS NULL THEN
        RETURN NULL;
    END IF;

    IF array_length(v_leaves, 1) > 1 THEN
        RAISE EXCEPTION 'malformed version chain' USING ERRCODE = 'SE004';
    END IF;

    RETURN v_leaves[1];
END;
$$;

ALTER FUNCTION app_private.current_output_version(UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION app_private.current_output_version(UUID) FROM PUBLIC;

-- Append an audit event. Metadata is supplied by the calling function and only
-- ever contains safe identifiers; no content, paths, or customer names.
CREATE OR REPLACE FUNCTION app_private.record_audit_event(
    p_org_id UUID,
    p_user_id UUID,
    p_action TEXT,
    p_entity_type TEXT,
    p_entity_id UUID,
    p_request_id UUID,
    p_metadata JSONB
)
RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_id UUID;
BEGIN
    INSERT INTO public.audit_events (
        org_id, user_id, action, entity_type, entity_id, request_id, metadata
    ) VALUES (
        p_org_id, p_user_id, p_action, p_entity_type, p_entity_id, p_request_id,
        COALESCE(p_metadata, '{}'::jsonb)
    )
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$$;

ALTER FUNCTION app_private.record_audit_event(UUID, UUID, TEXT, TEXT, UUID, UUID, JSONB) OWNER TO app_admin;
REVOKE ALL ON FUNCTION app_private.record_audit_event(UUID, UUID, TEXT, TEXT, UUID, UUID, JSONB) FROM PUBLIC;

-- Validate that a target version belongs to this output (or is NULL for the
-- immutable generated output). A version id from another output or another
-- organization is indistinguishable from a non-existent one.
CREATE OR REPLACE FUNCTION app_private.assert_version_of_output(
    p_output_id UUID,
    p_org_id UUID,
    p_version_id UUID
)
RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF p_version_id IS NULL THEN
        RETURN;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.output_versions v
        WHERE v.id = p_version_id
          AND v.output_id = p_output_id
          AND v.org_id = p_org_id
    ) THEN
        RAISE EXCEPTION 'output not accessible' USING ERRCODE = 'SE001';
    END IF;
END;
$$;

ALTER FUNCTION app_private.assert_version_of_output(UUID, UUID, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION app_private.assert_version_of_output(UUID, UUID, UUID) FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Comment
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.add_output_comment(
    p_output_id UUID,
    p_account_id UUID,
    p_target_version_id UUID,
    p_body TEXT,
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
    v_existing public.reviews;
    v_review_id UUID;
    v_created_at TIMESTAMPTZ;
BEGIN
    IF p_request_id IS NULL THEN
        RAISE EXCEPTION 'request id is required' USING ERRCODE = 'SE005';
    END IF;
    IF p_body IS NULL OR btrim(p_body) = '' THEN
        RAISE EXCEPTION 'comment body is required' USING ERRCODE = 'SE005';
    END IF;
    IF length(p_body) > 4000 THEN
        RAISE EXCEPTION 'comment body is too long' USING ERRCODE = 'SE005';
    END IF;
    -- PostgreSQL `text` cannot hold a NUL byte at all, so a NUL-bearing payload
    -- is rejected before it reaches this function; the API layer rejects the
    -- remaining control characters.

    v_output := app_private.reviewable_output(p_output_id, p_account_id, TRUE);
    v_user_id := public.current_request_user_id();
    PERFORM app_private.assert_version_of_output(p_output_id, v_output.org_id, p_target_version_id);

    SELECT r.* INTO v_existing
    FROM public.reviews r
    WHERE r.org_id = v_output.org_id
      AND r.request_id = p_request_id;

    IF FOUND THEN
        IF v_existing.action <> 'comment'
            OR v_existing.output_id <> p_output_id
            OR v_existing.output_version_id IS DISTINCT FROM p_target_version_id
            OR v_existing.comment IS DISTINCT FROM p_body THEN
            RAISE EXCEPTION 'idempotency key reused with a different payload' USING ERRCODE = 'SE003';
        END IF;
        RETURN jsonb_build_object(
            'review_id', v_existing.id,
            'output_version_id', v_existing.output_version_id,
            'created_at', v_existing.created_at,
            'replayed', true
        );
    END IF;

    INSERT INTO public.reviews (
        org_id, output_id, output_version_id, user_id, action, comment, request_id
    ) VALUES (
        v_output.org_id, p_output_id, p_target_version_id, v_user_id, 'comment', p_body, p_request_id
    )
    RETURNING id, created_at INTO v_review_id, v_created_at;

    PERFORM app_private.record_audit_event(
        v_output.org_id,
        v_user_id,
        'output_comment',
        'outputs',
        p_output_id,
        p_request_id,
        jsonb_build_object(
            'output_id', p_output_id,
            'output_version_id', p_target_version_id,
            'review_id', v_review_id,
            'request_id', p_request_id
        )
    );

    RETURN jsonb_build_object(
        'review_id', v_review_id,
        'output_version_id', p_target_version_id,
        'created_at', v_created_at,
        'replayed', false
    );
END;
$$;

ALTER FUNCTION public.add_output_comment(UUID, UUID, UUID, TEXT, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.add_output_comment(UUID, UUID, UUID, TEXT, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.add_output_comment(UUID, UUID, UUID, TEXT, UUID) TO app_user;

-- ---------------------------------------------------------------------------
-- Approval
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.approve_output_version(
    p_output_id UUID,
    p_account_id UUID,
    p_target_version_id UUID,
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
    v_existing public.reviews;
    v_review_id UUID;
    v_created_at TIMESTAMPTZ;
BEGIN
    IF p_request_id IS NULL THEN
        RAISE EXCEPTION 'request id is required' USING ERRCODE = 'SE005';
    END IF;

    v_output := app_private.reviewable_output(p_output_id, p_account_id, TRUE);
    v_user_id := public.current_request_user_id();
    PERFORM app_private.assert_version_of_output(p_output_id, v_output.org_id, p_target_version_id);

    SELECT r.* INTO v_existing
    FROM public.reviews r
    WHERE r.org_id = v_output.org_id
      AND r.request_id = p_request_id;

    IF FOUND THEN
        IF v_existing.action <> 'approve'
            OR v_existing.output_id <> p_output_id
            OR v_existing.output_version_id IS DISTINCT FROM p_target_version_id THEN
            RAISE EXCEPTION 'idempotency key reused with a different payload' USING ERRCODE = 'SE003';
        END IF;
        RETURN jsonb_build_object(
            'review_id', v_existing.id,
            'output_version_id', v_existing.output_version_id,
            'created_at', v_existing.created_at,
            'replayed', true
        );
    END IF;

    v_current := app_private.current_output_version(p_output_id);
    IF v_current IS DISTINCT FROM p_target_version_id THEN
        RAISE EXCEPTION 'approval target is not the current version' USING ERRCODE = 'SE002';
    END IF;

    INSERT INTO public.reviews (
        org_id, output_id, output_version_id, user_id, action, request_id
    ) VALUES (
        v_output.org_id, p_output_id, p_target_version_id, v_user_id, 'approve', p_request_id
    )
    RETURNING id, created_at INTO v_review_id, v_created_at;

    PERFORM app_private.record_audit_event(
        v_output.org_id,
        v_user_id,
        'output_approval',
        'outputs',
        p_output_id,
        p_request_id,
        jsonb_build_object(
            'output_id', p_output_id,
            'output_version_id', p_target_version_id,
            'review_id', v_review_id,
            'request_id', p_request_id
        )
    );

    RETURN jsonb_build_object(
        'review_id', v_review_id,
        'output_version_id', p_target_version_id,
        'created_at', v_created_at,
        'replayed', false
    );
END;
$$;

ALTER FUNCTION public.approve_output_version(UUID, UUID, UUID, UUID) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.approve_output_version(UUID, UUID, UUID, UUID) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.approve_output_version(UUID, UUID, UUID, UUID) TO app_user;

-- ---------------------------------------------------------------------------
-- Correction: reserve -> upload -> commit (or abandon)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.reserve_output_correction(
    p_output_id UUID,
    p_account_id UUID,
    p_base_version_id UUID,
    p_request_id UUID,
    p_payload_hash TEXT
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
    v_existing public.output_correction_uploads;
    v_version_id UUID;
    v_path TEXT;
BEGIN
    IF p_request_id IS NULL THEN
        RAISE EXCEPTION 'request id is required' USING ERRCODE = 'SE005';
    END IF;
    IF p_payload_hash IS NULL OR length(p_payload_hash) = 0 THEN
        RAISE EXCEPTION 'payload hash is required' USING ERRCODE = 'SE005';
    END IF;

    v_output := app_private.reviewable_output(p_output_id, p_account_id, TRUE);
    v_user_id := public.current_request_user_id();
    PERFORM app_private.assert_version_of_output(p_output_id, v_output.org_id, p_base_version_id);

    SELECT u.* INTO v_existing
    FROM public.output_correction_uploads u
    WHERE u.org_id = v_output.org_id
      AND u.request_id = p_request_id
    FOR UPDATE;

    IF FOUND THEN
        -- The hash covers the replacement Markdown and change summary, so the
        -- same key with materially different content is a conflict, not a replay.
        IF v_existing.output_id <> p_output_id
            OR v_existing.base_version_id IS DISTINCT FROM p_base_version_id
            OR v_existing.created_by <> v_user_id
            OR v_existing.payload_hash <> p_payload_hash THEN
            RAISE EXCEPTION 'idempotency key reused with a different payload' USING ERRCODE = 'SE003';
        END IF;
        IF v_existing.state = 'pending' OR v_existing.state = 'committed' THEN
            RETURN jsonb_build_object(
                'reservation_id', v_existing.id,
                'output_version_id', v_existing.id,
                'content_storage_path', v_existing.content_storage_path,
                'state', v_existing.state
            );
        END IF;
        RAISE EXCEPTION 'idempotency key belongs to a failed submission' USING ERRCODE = 'SE003';
    END IF;

    v_current := app_private.current_output_version(p_output_id);
    IF v_current IS DISTINCT FROM p_base_version_id THEN
        RAISE EXCEPTION 'correction base is not the current version' USING ERRCODE = 'SE002';
    END IF;

    v_version_id := gen_random_uuid();
    v_path := v_output.org_id || '/' || v_output.account_id || '/' || v_output.transcript_id
              || '/' || p_output_id || '/versions/' || v_version_id || '/output.md';

    INSERT INTO public.output_correction_uploads (
        id, org_id, output_id, base_version_id, request_id, content_storage_path,
        payload_hash, state, created_by
    ) VALUES (
        v_version_id, v_output.org_id, p_output_id, p_base_version_id, p_request_id, v_path,
        p_payload_hash, 'pending', v_user_id
    );

    RETURN jsonb_build_object(
        'reservation_id', v_version_id,
        'output_version_id', v_version_id,
        'content_storage_path', v_path,
        'state', 'pending'
    );
END;
$$;

ALTER FUNCTION public.reserve_output_correction(UUID, UUID, UUID, UUID, TEXT) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.reserve_output_correction(UUID, UUID, UUID, UUID, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.reserve_output_correction(UUID, UUID, UUID, UUID, TEXT) TO app_user;

CREATE OR REPLACE FUNCTION public.commit_output_correction(
    p_reservation_id UUID,
    p_change_summary TEXT,
    p_sidecar JSONB
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_reservation public.output_correction_uploads;
    v_output public.outputs;
    v_user_id UUID;
    v_current UUID;
    v_review_id UUID;
    v_created_at TIMESTAMPTZ;
BEGIN
    IF p_change_summary IS NOT NULL AND length(p_change_summary) > 2000 THEN
        RAISE EXCEPTION 'change summary is too long' USING ERRCODE = 'SE005';
    END IF;

    SELECT u.* INTO v_reservation
    FROM public.output_correction_uploads u
    WHERE u.id = p_reservation_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'output not accessible' USING ERRCODE = 'SE001';
    END IF;

    v_user_id := public.current_request_user_id();
    IF v_user_id IS NULL OR v_reservation.created_by <> v_user_id THEN
        RAISE EXCEPTION 'output not accessible' USING ERRCODE = 'SE001';
    END IF;

    v_output := app_private.reviewable_output(
        v_reservation.output_id,
        (SELECT o.account_id FROM public.outputs o WHERE o.id = v_reservation.output_id),
        TRUE
    );

    IF v_reservation.state = 'committed' THEN
        SELECT r.id, r.created_at INTO v_review_id, v_created_at
        FROM public.reviews r
        WHERE r.output_version_id = v_reservation.id
          AND r.action = 'correct';
        RETURN jsonb_build_object(
            'output_version_id', v_reservation.id,
            'previous_version_id', v_reservation.base_version_id,
            'review_id', v_review_id,
            'created_at', v_created_at,
            'replayed', true
        );
    END IF;

    IF v_reservation.state <> 'pending' THEN
        RAISE EXCEPTION 'correction reservation is no longer usable' USING ERRCODE = 'SE003';
    END IF;

    v_current := app_private.current_output_version(v_reservation.output_id);
    IF v_current IS DISTINCT FROM v_reservation.base_version_id THEN
        RAISE EXCEPTION 'correction base is not the current version' USING ERRCODE = 'SE002';
    END IF;

    INSERT INTO public.output_versions (
        id, org_id, output_id, previous_version_id, content_storage_path,
        sidecar, change_summary, created_by, request_id
    ) VALUES (
        v_reservation.id,
        v_reservation.org_id,
        v_reservation.output_id,
        v_reservation.base_version_id,
        v_reservation.content_storage_path,
        COALESCE(p_sidecar, '{}'::jsonb),
        p_change_summary,
        v_user_id,
        v_reservation.request_id
    );

    INSERT INTO public.reviews (
        org_id, output_id, output_version_id, previous_version_id, user_id, action, request_id
    ) VALUES (
        v_reservation.org_id,
        v_reservation.output_id,
        v_reservation.id,
        v_reservation.base_version_id,
        v_user_id,
        'correct',
        v_reservation.request_id
    )
    RETURNING id, created_at INTO v_review_id, v_created_at;

    PERFORM app_private.record_audit_event(
        v_reservation.org_id,
        v_user_id,
        'output_correction',
        'output_versions',
        v_reservation.id,
        v_reservation.request_id,
        jsonb_build_object(
            'output_id', v_reservation.output_id,
            'output_version_id', v_reservation.id,
            'previous_version_id', v_reservation.base_version_id,
            'review_id', v_review_id,
            'request_id', v_reservation.request_id
        )
    );

    UPDATE public.output_correction_uploads
    SET state = 'committed', updated_at = now()
    WHERE id = v_reservation.id;

    RETURN jsonb_build_object(
        'output_version_id', v_reservation.id,
        'previous_version_id', v_reservation.base_version_id,
        'review_id', v_review_id,
        'created_at', v_created_at,
        'replayed', false
    );
END;
$$;

ALTER FUNCTION public.commit_output_correction(UUID, TEXT, JSONB) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.commit_output_correction(UUID, TEXT, JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.commit_output_correction(UUID, TEXT, JSONB) TO app_user;

-- Close out a reservation that will never become a version. `p_object_deleted`
-- records whether the private object was removed: FALSE leaves durable
-- `orphaned` evidence for retryable cleanup instead of silently orphaning
-- customer content.
CREATE OR REPLACE FUNCTION public.abandon_output_correction(
    p_reservation_id UUID,
    p_object_deleted BOOLEAN,
    p_error TEXT
)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_reservation public.output_correction_uploads;
    v_user_id UUID;
    v_state TEXT;
BEGIN
    SELECT u.* INTO v_reservation
    FROM public.output_correction_uploads u
    WHERE u.id = p_reservation_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'output not accessible' USING ERRCODE = 'SE001';
    END IF;

    v_user_id := public.current_request_user_id();
    IF v_user_id IS NULL OR v_reservation.created_by <> v_user_id THEN
        RAISE EXCEPTION 'output not accessible' USING ERRCODE = 'SE001';
    END IF;

    IF v_reservation.state = 'committed' THEN
        RAISE EXCEPTION 'correction is already committed' USING ERRCODE = 'SE003';
    END IF;

    v_state := CASE WHEN p_object_deleted THEN 'aborted' ELSE 'orphaned' END;

    UPDATE public.output_correction_uploads
    SET state = v_state,
        last_error = left(COALESCE(p_error, ''), 200),
        cleanup_attempts = cleanup_attempts + CASE WHEN p_object_deleted THEN 0 ELSE 1 END,
        updated_at = now()
    WHERE id = v_reservation.id;

    RETURN jsonb_build_object('reservation_id', v_reservation.id, 'state', v_state);
END;
$$;

ALTER FUNCTION public.abandon_output_correction(UUID, BOOLEAN, TEXT) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.abandon_output_correction(UUID, BOOLEAN, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.abandon_output_correction(UUID, BOOLEAN, TEXT) TO app_user;

-- ---------------------------------------------------------------------------
-- Orphaned-correction cleanup (worker role)
-- ---------------------------------------------------------------------------
-- Two kinds of row need reconciling, and both may point at a private object:
--
--   orphaned  -> the request knew the object existed and could not delete it
--   pending   -> the request died between reserve and commit/abandon (or its
--                upload failed ambiguously), so object existence is unknown
--
-- A `pending` row is only eligible once it is older than `p_pending_grace`,
-- which bounds how long an in-flight request is protected from its own cleanup.
-- Claiming one flips it to `orphaned` inside the same locked transaction, and
-- `commit_output_correction` only accepts `pending`, so a late commit for a
-- reconciled reservation fails instead of pointing a version at a deleted
-- object. `committed` rows are never eligible.
-- Both overloads are dropped first: the original two-argument signature, and the
-- three-argument one, whose return columns changed (`org_id` instead of
-- `created_by`) and which `CREATE OR REPLACE` therefore cannot redefine in a
-- database where an earlier version of this migration already ran.
DROP FUNCTION IF EXISTS public.claim_orphaned_correction_upload(TEXT, INTEGER);
DROP FUNCTION IF EXISTS public.claim_orphaned_correction_upload(TEXT, INTEGER, INTERVAL);

CREATE OR REPLACE FUNCTION public.claim_orphaned_correction_upload(
    p_worker_id TEXT,
    p_max_attempts INTEGER DEFAULT 10,
    p_pending_grace INTERVAL DEFAULT INTERVAL '30 minutes'
)
RETURNS TABLE(
    reservation_id UUID,
    content_storage_path TEXT,
    org_id UUID,
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
    IF p_pending_grace IS NULL OR p_pending_grace < INTERVAL '1 minute' THEN
        RAISE EXCEPTION 'pending grace must be at least one minute' USING ERRCODE = 'SE005';
    END IF;

    SELECT u.id INTO v_id
    FROM public.output_correction_uploads u
    WHERE (
              u.state = 'orphaned'
              OR (u.state = 'pending' AND u.updated_at < now() - p_pending_grace)
          )
      AND u.cleanup_attempts < p_max_attempts
      -- A claim is a lease, so a second worker (or a restarted one) cannot
      -- delete the same private object concurrently. Once the lease expires the
      -- row becomes claimable again, which is what makes cleanup retryable.
      AND (u.cleanup_claimed_at IS NULL
           OR u.cleanup_claimed_at < now() - INTERVAL '15 minutes')
    ORDER BY u.updated_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1;

    IF v_id IS NULL THEN
        RETURN;
    END IF;

    UPDATE public.output_correction_uploads u
    SET state = 'orphaned',
        cleanup_attempts = u.cleanup_attempts + 1,
        cleanup_claimed_by = p_worker_id,
        cleanup_claimed_at = now(),
        updated_at = now()
    WHERE u.id = v_id;

    -- Only what the worker needs to delete the object: the reservation, its
    -- Storage path, the organization that scopes the maintenance Storage token,
    -- and the attempt count. It never sees the output, the payload hash, the
    -- request id, or the correction author, and it has no direct SELECT on the
    -- ledger. The org id replaces `created_by` deliberately: deletion is
    -- authorized by the org path, so a deactivated author cannot block cleanup.
    RETURN QUERY
    SELECT u.id, u.content_storage_path, u.org_id, u.cleanup_attempts
    FROM public.output_correction_uploads u
    WHERE u.id = v_id;
END;
$$;

ALTER FUNCTION public.claim_orphaned_correction_upload(TEXT, INTEGER, INTERVAL) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.claim_orphaned_correction_upload(TEXT, INTEGER, INTERVAL) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.claim_orphaned_correction_upload(TEXT, INTEGER, INTERVAL) TO app_worker;

CREATE OR REPLACE FUNCTION public.finalize_correction_cleanup(
    p_reservation_id UUID,
    p_worker_id TEXT
)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    UPDATE public.output_correction_uploads
    SET state = 'aborted', updated_at = now()
    WHERE id = p_reservation_id
      AND state = 'orphaned'
      AND cleanup_claimed_by = p_worker_id;
    RETURN FOUND;
END;
$$;

ALTER FUNCTION public.finalize_correction_cleanup(UUID, TEXT) OWNER TO app_admin;
REVOKE ALL ON FUNCTION public.finalize_correction_cleanup(UUID, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.finalize_correction_cleanup(UUID, TEXT) TO app_worker;

-- `app_worker` reaches the ledger only through the two functions above.
REVOKE ALL ON TABLE public.output_correction_uploads FROM app_worker;
