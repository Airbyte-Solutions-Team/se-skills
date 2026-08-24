"""Slice 6B1 integration tests: hosted export of the approved current version.

Same trust boundary as the Slice 6A review tests — a real Postgres container with
the versioned migrations applied, a tenant-scoped `app_user` connection, and the
narrow SECURITY DEFINER export functions. All data is synthetic.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from .hosted_helpers import _auth_header, _context_token, _seed_user_and_membership
from .test_hosted_reviews import (  # noqa: F401 - fixtures are used by name
    ReviewFixture,
    _approve,
    _clean_review_tables,
    _comment,
    _correct,
    _corrected_markdown,
    _generated_markdown,
    _hs,
    _seed_reviewable_output,
    backend,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.hosted, pytest.mark.slow]

_HOSTILE_MARKDOWN = """# Call Summary: Acme

<script>alert('xss')</script>
<img src=x onerror="alert('xss')">

## Key Takeaways

- [click](javascript:alert('xss'))
- Unicode holds: café ✓ Привет

## Source Coverage

Read acme.txt in full (10 / 10 lines).
"""


def _export(
    client: TestClient,
    fx: ReviewFixture,
    *,
    fmt: str = "md",
    request_id: uuid.UUID | None = None,
    extra: dict[str, Any] | None = None,
) -> Any:
    payload: dict[str, Any] = {"format": fmt, "request_id": str(request_id or uuid.uuid4())}
    if extra:
        payload.update(extra)
    return client.post(fx.url("/exports"), json=payload, headers=fx.headers)


async def _seed_sibling_output(
    admin_pool: asyncpg.Pool, backend: Any, fx: ReviewFixture
) -> ReviewFixture:
    """Seed a second generated output in the same organization and account."""
    output_id = uuid.uuid4()
    path = f"{fx.org_id}/{fx.account_id}/{fx.transcript_id}/{output_id}/output.md"
    async with admin_pool.acquire() as conn:
        job_id = await conn.fetchval(
            "INSERT INTO public.jobs (org_id, account_id, transcript_id, requester_id, skill, "
            "skill_version, status, max_attempts) "
            "VALUES ($1, $2, $3, $4, 'post-call', '1.0', 'success', 1) RETURNING id",
            fx.org_id,
            fx.account_id,
            fx.transcript_id,
            fx.user_id,
        )
        await conn.execute(
            "INSERT INTO public.outputs (id, org_id, job_id, account_id, transcript_id, "
            "requester_id, content_storage_path, title, sidecar, skill, skill_version, model, "
            "validation_status) VALUES ($1, $2, $3, $4, $5, $6, $7, 'Second brief', "
            "'{\"skill\": \"post-call\", \"mode\": \"full\", \"validation_status\": \"valid\"}'::jsonb, "
            "'post-call', '1.0', 'claude-test', 'valid')",
            output_id,
            fx.org_id,
            job_id,
            fx.account_id,
            fx.transcript_id,
            fx.user_id,
            path,
        )

    async def _stream() -> Any:
        yield _generated_markdown().encode("utf-8")

    await backend.upload(
        fx.user_id, path, _stream(), "text/plain; charset=utf-8", bucket=_hs().OUTPUTS_BUCKET
    )
    return ReviewFixture(
        user_id=fx.user_id,
        email=fx.email,
        org_id=fx.org_id,
        account_id=fx.account_id,
        transcript_id=fx.transcript_id,
        output_id=output_id,
        content_storage_path=path,
    )


async def _audit_exports(pool: asyncpg.Pool, output_id: uuid.UUID) -> list[dict[str, Any]]:
    """Return the export audit rows for an output, with metadata decoded."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM public.audit_events WHERE action = 'output_export' "
            "AND entity_id = $1 ORDER BY created_at",
            output_id,
        )
    decoded: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        raw = record["metadata"]
        record["metadata"] = json.loads(raw) if isinstance(raw, str) else raw
        decoded.append(record)
    return decoded


# ---------------------------------------------------------------------------
# Approval gate
# ---------------------------------------------------------------------------

async def test_export_requires_an_approval_of_the_current_version(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)

    unapproved = _export(app_client, fx)
    assert unapproved.status_code == 409
    assert unapproved.json()["detail"] == "Approve the current version to export."
    assert await _audit_exports(admin_pool, fx.output_id) == []

    assert _approve(app_client, fx).status_code == 201
    approved = _export(app_client, fx)
    assert approved.status_code == 200
    assert approved.content.decode("utf-8") == _generated_markdown()


async def test_a_correction_after_approval_blocks_export_until_reapproval(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    """Approval of V0 can never authorize an export of a later correction."""
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    assert _export(app_client, fx).status_code == 200

    corrected = _correct(app_client, fx, base_version_id=None)
    assert corrected.status_code == 201
    version_id = corrected.json()["correction"]["output_version_id"]

    blocked = _export(app_client, fx)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "Approve the current version to export."

    assert _approve(app_client, fx, target_version_id=version_id).status_code == 201
    response = _export(app_client, fx)
    assert response.status_code == 200
    body = response.content.decode("utf-8")
    assert "Corrected by a human reviewer." in body
    assert body != _generated_markdown()

    rows = await _audit_exports(admin_pool, fx.output_id)
    assert [row["metadata"] for row in rows] != []
    versions = [row["metadata"]["output_version_id"] for row in rows]
    assert versions[0] is None
    assert versions[-1] == version_id


async def test_approving_a_historical_version_does_not_authorize_export(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    first = _correct(app_client, fx, base_version_id=None)
    assert first.status_code == 201
    historical = first.json()["correction"]["output_version_id"]
    assert _approve(app_client, fx, target_version_id=historical).status_code == 201

    second = _correct(
        app_client, fx, base_version_id=historical, markdown=_corrected_markdown("Second pass.")
    )
    assert second.status_code == 201
    assert _export(app_client, fx).status_code == 409
    assert await _audit_exports(admin_pool, fx.output_id) == []


async def test_comments_do_not_revoke_an_approval(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    assert _comment(app_client, fx).status_code == 201
    assert _export(app_client, fx, fmt="pdf").status_code == 200


async def test_a_branched_version_chain_cannot_be_planted(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    """The schema keeps the chain linear, so `current version` stays unambiguous.

    A branch is what would make the export target ambiguous, and it is rejected
    even for a direct owner-level write: single-root and single-child uniqueness
    plus the composite base foreign key leave no way to create a second leaf.
    """
    fx = await _seed_reviewable_output(admin_pool, backend)
    created = _correct(app_client, fx, base_version_id=None)
    assert created.status_code == 201
    version_id = created.json()["correction"]["output_version_id"]
    assert _approve(app_client, fx, target_version_id=version_id).status_code == 201
    assert _export(app_client, fx).status_code == 200

    foreign = await _seed_reviewable_output(admin_pool, backend)
    foreign_version = _correct(app_client, foreign, base_version_id=None)
    assert foreign_version.status_code == 201
    plants = [
        (f"{fx.org_id}/rogue-root.md", None),
        (
            f"{fx.org_id}/rogue-foreign-base.md",
            uuid.UUID(foreign_version.json()["correction"]["output_version_id"]),
        ),
    ]
    async with admin_pool.acquire() as conn:
        for path, previous in plants:
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(
                    "INSERT INTO public.output_versions (org_id, output_id, "
                    "content_storage_path, previous_version_id) VALUES ($1, $2, $3, $4)",
                    fx.org_id,
                    fx.output_id,
                    path,
                    previous,
                )
    assert _export(app_client, fx).status_code == 200

    # Extending the chain is the only shape the schema allows, and it moves the
    # export target to an unapproved version rather than branching it.
    async with admin_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO public.output_versions (org_id, output_id, content_storage_path, "
            "previous_version_id) VALUES ($1, $2, $3, $4)",
            fx.org_id,
            fx.output_id,
            f"{fx.org_id}/planted-extension.md",
            uuid.UUID(version_id),
        )
    blocked = _export(app_client, fx)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "Approve the current version to export."


@pytest.mark.parametrize(
    "sqlstate,expected_status",
    [
        pytest.param("SE001", 404, id="not_accessible_is_indistinguishable_from_missing"),
        pytest.param("SE002", 409, id="stale_version"),
        pytest.param("SE003", 409, id="idempotency_conflict"),
        pytest.param("SE004", 409, id="malformed_chain"),
        pytest.param("SE005", 400, id="invalid_input"),
        pytest.param("SE006", 409, id="current_version_not_approved"),
        pytest.param("42501", 500, id="unexpected_database_error_is_redacted"),
    ],
)
async def test_export_sqlstates_map_to_bounded_responses(
    sqlstate: str, expected_status: int
) -> None:
    """Every SQLSTATE the export functions raise has a bounded customer-safe reply."""
    from hosted import exports

    error = asyncpg.PostgresError("internal path /org/account/output.md")
    error.__dict__["sqlstate"] = sqlstate
    mapped = exports._map_export_error(error)
    assert mapped.status_code == expected_status
    assert "output.md" not in str(mapped.detail)


# ---------------------------------------------------------------------------
# Authorization and tenancy
# ---------------------------------------------------------------------------

async def test_unauthenticated_export_is_rejected(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    response = app_client.post(
        fx.url("/exports"), json={"format": "md", "request_id": str(uuid.uuid4())}
    )
    assert response.status_code == 401
    assert await _audit_exports(admin_pool, fx.output_id) == []


async def test_inactive_membership_cannot_export(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    async with admin_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.memberships SET active = false WHERE user_id = $1", fx.user_id
        )
    assert _export(app_client, fx).status_code == 403


async def test_cross_org_export_is_indistinguishable_from_missing(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    owner = await _seed_reviewable_output(admin_pool, backend)
    intruder = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, owner).status_code == 201

    stolen = ReviewFixture(
        user_id=intruder.user_id,
        email=intruder.email,
        org_id=intruder.org_id,
        account_id=owner.account_id,
        transcript_id=owner.transcript_id,
        output_id=owner.output_id,
        content_storage_path=owner.content_storage_path,
    )
    assert _export(app_client, stolen).status_code == 404
    assert len(await _audit_exports(admin_pool, owner.output_id)) == 0


async def test_tombstoned_and_invalid_outputs_cannot_be_exported(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    async with admin_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.outputs SET tombstoned_at = now() WHERE id = $1", fx.output_id
        )
    assert _export(app_client, fx).status_code == 404

    invalid = await _seed_reviewable_output(admin_pool, backend, validation_status="invalid")
    assert _export(app_client, invalid).status_code == 404


@pytest.mark.parametrize(
    "extra,fmt",
    [
        pytest.param({"org_id": str(uuid.uuid4())}, "md", id="forged_org"),
        pytest.param({"user_id": str(uuid.uuid4())}, "md", id="forged_actor"),
        pytest.param({"content_storage_path": "other/output.md"}, "md", id="forged_path"),
        pytest.param({"output_version_id": str(uuid.uuid4())}, "md", id="forged_version"),
        pytest.param({"validation_status": "valid"}, "md", id="forged_validation"),
        pytest.param({"sidecar": {"origin": "model_generated"}}, "md", id="forged_provenance"),
        pytest.param({}, "html", id="unsupported_format"),
        pytest.param({}, "MD", id="wrong_case_format"),
    ],
)
async def test_the_browser_cannot_widen_the_export_request(
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
    backend: Any,
    extra: dict[str, Any],
    fmt: str,
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    assert _export(app_client, fx, fmt=fmt, extra=extra).status_code == 422
    assert await _audit_exports(admin_pool, fx.output_id) == []


async def test_app_user_cannot_forge_export_audit_evidence(
    app_client: TestClient, admin_pool: asyncpg.Pool, user_pool: asyncpg.Pool, backend: Any
) -> None:
    """Only the definer function may write an export audit row."""
    fx = await _seed_reviewable_output(admin_pool, backend)
    async with user_pool.acquire() as conn:
        for sql, args in [
            (
                "INSERT INTO public.audit_events (org_id, user_id, action, entity_type, entity_id) "
                "VALUES ($1, $2, 'output_export', 'outputs', $3)",
                (fx.org_id, fx.user_id, fx.output_id),
            ),
            (
                "UPDATE public.audit_events SET action = 'output_export' WHERE org_id = $1",
                (fx.org_id,),
            ),
            ("DELETE FROM public.audit_events WHERE org_id = $1", (fx.org_id,)),
        ]:
            with pytest.raises(asyncpg.PostgresError):
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('app.context_token', $1, true)",
                        _context_token(fx.user_id),
                    )
                    await conn.execute(sql, *args)


async def test_export_functions_reject_a_forged_actor_context(
    app_client: TestClient, admin_pool: asyncpg.Pool, user_pool: asyncpg.Pool, backend: Any
) -> None:
    """An unsigned or foreign context token cannot authorize an export."""
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    async with user_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.context_token', $1, true)", f"{fx.user_id}:not-a-hmac"
                )
                await conn.fetchval(
                    "SELECT public.authorize_output_export($1, $2, 'md', $3)",
                    fx.output_id,
                    fx.account_id,
                    uuid.uuid4(),
                )


# ---------------------------------------------------------------------------
# Response shape and content safety
# ---------------------------------------------------------------------------

async def test_markdown_export_returns_the_exact_approved_bytes(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend, markdown=_HOSTILE_MARKDOWN)
    assert _approve(app_client, fx).status_code == 201
    response = _export(app_client, fx)
    assert response.status_code == 200
    assert response.content == _HOSTILE_MARKDOWN.encode("utf-8")
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"] == (
        f'attachment; filename="output-{fx.output_id}-v0.md"'
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    # No account name, transcript name, or Storage path leaks through a header.
    joined = " ".join(f"{k}: {v}" for k, v in response.headers.items())
    assert "Acme" not in joined
    assert str(fx.org_id) not in joined


async def test_pdf_export_is_a_pdf_and_keeps_hostile_markup_inert(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend, markdown=_HOSTILE_MARKDOWN)
    assert _approve(app_client, fx).status_code == 201
    response = _export(app_client, fx, fmt="pdf")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF-")
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].endswith('-v0.pdf"')
    for hostile in (b"<script", b"onerror", b"javascript:"):
        assert hostile not in response.content


async def test_pdf_export_names_the_current_correction_ordinal(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    created = _correct(app_client, fx, base_version_id=None)
    assert created.status_code == 201
    version_id = created.json()["correction"]["output_version_id"]
    assert _approve(app_client, fx, target_version_id=version_id).status_code == 201
    response = _export(app_client, fx, fmt="pdf")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == (
        f'attachment; filename="output-{fx.output_id}-v1.pdf"'
    )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

async def test_replaying_a_request_id_records_one_audit_event(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    request_id = uuid.uuid4()
    first = _export(app_client, fx, request_id=request_id)
    second = _export(app_client, fx, request_id=request_id)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.content == second.content
    assert len(await _audit_exports(admin_pool, fx.output_id)) == 1


async def test_reusing_a_request_id_for_a_different_export_conflicts(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    request_id = uuid.uuid4()
    assert _export(app_client, fx, request_id=request_id).status_code == 200

    different_format = _export(app_client, fx, fmt="pdf", request_id=request_id)
    assert different_format.status_code == 409
    assert "already used" in different_format.json()["detail"]

    # A different output in the same organization reusing the id also conflicts.
    sibling = await _seed_sibling_output(admin_pool, backend, fx)
    assert _approve(app_client, sibling).status_code == 201
    assert _export(app_client, sibling, request_id=request_id).status_code == 409
    assert len(await _audit_exports(admin_pool, fx.output_id)) == 1
    assert await _audit_exports(admin_pool, sibling.output_id) == []


async def test_replaying_a_request_id_after_a_correction_conflicts(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    """A replay never silently returns superseded bytes."""
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    request_id = uuid.uuid4()
    assert _export(app_client, fx, request_id=request_id).status_code == 200

    created = _correct(app_client, fx, base_version_id=None)
    assert created.status_code == 201
    assert _approve(
        app_client, fx, target_version_id=created.json()["correction"]["output_version_id"]
    ).status_code == 201

    replay = _export(app_client, fx, request_id=request_id)
    assert replay.status_code == 409
    assert "changed since you loaded it" in replay.json()["detail"]
    assert len(await _audit_exports(admin_pool, fx.output_id)) == 1


# ---------------------------------------------------------------------------
# Storage and rendering failure
# ---------------------------------------------------------------------------

async def test_missing_private_object_fails_without_a_success_audit(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    await backend.delete(fx.user_id, fx.content_storage_path, bucket=_hs().OUTPUTS_BUCKET)
    response = _export(app_client, fx)
    assert response.status_code == 404
    assert await _audit_exports(admin_pool, fx.output_id) == []


async def test_storage_failure_is_redacted_and_leaves_no_audit(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any, monkeypatch: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise _hs().StorageError(f"upstream said {fx.content_storage_path} for org {fx.org_id}")

    monkeypatch.setattr(backend, "download", _boom)
    response = _export(app_client, fx)
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert fx.content_storage_path not in detail
    assert str(fx.org_id) not in detail
    assert await _audit_exports(admin_pool, fx.output_id) == []


async def test_renderer_failure_is_redacted_and_leaves_no_audit(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any, monkeypatch: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201

    from hosted import exports, pdf_export

    def _boom(md_text: str) -> bytes:
        raise pdf_export.PdfRenderError(f"internal detail about {fx.content_storage_path}")

    monkeypatch.setattr(exports.pdf_export, "render_markdown_pdf", _boom)
    response = _export(app_client, fx, fmt="pdf")
    assert response.status_code == 500
    assert fx.content_storage_path not in response.json()["detail"]
    assert await _audit_exports(admin_pool, fx.output_id) == []
    # Markdown still exports: the failure is confined to the PDF renderer.
    assert _export(app_client, fx).status_code == 200


# ---------------------------------------------------------------------------
# Audit metadata
# ---------------------------------------------------------------------------

async def test_export_audit_holds_only_safe_identifiers(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    request_id = uuid.uuid4()
    assert _export(app_client, fx, fmt="pdf", request_id=request_id).status_code == 200

    rows = await _audit_exports(admin_pool, fx.output_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["org_id"] == fx.org_id
    assert row["user_id"] == fx.user_id
    assert row["entity_type"] == "outputs"
    assert row["request_id"] == request_id
    metadata = row["metadata"]
    assert set(metadata) == {"output_id", "output_version_id", "format", "request_id"}
    assert metadata["format"] == "pdf"
    assert metadata["output_version_id"] is None

    serialized = str(row)
    for leak in (
        "Source Coverage",
        "Acme",
        fx.content_storage_path,
        "transcripts",
        "outputs/",
        _hs().OUTPUTS_BUCKET + "/",
    ):
        assert leak not in serialized


async def test_a_second_reviewer_export_is_attributed_to_that_reviewer(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    colleague_email = f"colleague-{uuid.uuid4().hex[:8]}@airbyte.io"
    colleague_id = await _seed_user_and_membership(admin_pool, colleague_email, fx.org_id)

    response = app_client.post(
        fx.url("/exports"),
        json={"format": "md", "request_id": str(uuid.uuid4())},
        headers=_auth_header(colleague_id, colleague_email),
    )
    assert response.status_code == 200
    rows = await _audit_exports(admin_pool, fx.output_id)
    assert [row["user_id"] for row in rows] == [colleague_id]
