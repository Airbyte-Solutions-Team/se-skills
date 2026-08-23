"""Slice 6A integration tests: hosted output review, correction, approval, audit.

These tests run against a real Postgres container with the versioned migrations
applied, so the trust boundary they exercise is the production one:

    authenticated FastAPI -> tenant-scoped `app_user` connection
    -> narrow SECURITY DEFINER function -> append-only review/version/audit rows

All data is synthetic. No customer transcript, model call, or hosted deployment
is involved.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from fastapi.testclient import TestClient

from .hosted_helpers import (
    _auth_header,
    _context_token,
    _seed_account,
    _seed_member,
    _seed_transcript,
    _seed_user_and_membership,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.hosted, pytest.mark.slow]

_FIXTURE = Path("eval/fixtures/outputs/post-call-full.md")


def _hs() -> Any:
    """Return the live `hosted.storage` module.

    The hosted fixtures drop `hosted*` from `sys.modules` before the app is
    built, so a module-level import here would capture stale classes and the
    `StorageError` raised by a test double would not be the class the app
    catches.
    """
    import hosted.storage

    return hosted.storage

# Synthetic transcript text. It mentions systems/APIs so the deterministic
# conditional-section triggers in `output_schema` behave the same way they do at
# generation time.
TRANSCRIPT_TEXT = (
    "Discovery call with Acme. They use Salesforce and Snowflake systems and APIs. "
    "Security review and pricing were discussed."
)


def _generated_markdown() -> str:
    return _FIXTURE.read_text(encoding="utf-8")


def _corrected_markdown(marker: str = "Corrected by a human reviewer.") -> str:
    """A valid post-call output with one extra takeaway line."""
    text = _generated_markdown()
    return text.replace("## Key Takeaways", f"## Key Takeaways\n\n- {marker}", 1)


def _invalid_markdown() -> str:
    """A correction that violates the authoritative post-call contract."""
    return _generated_markdown().replace("## Source Coverage", "## Sources Read", 1)


@dataclass
class ReviewFixture:
    """A seeded, reviewable generated output plus its owning identities."""

    user_id: uuid.UUID
    email: str
    org_id: uuid.UUID
    account_id: uuid.UUID
    transcript_id: uuid.UUID
    output_id: uuid.UUID
    content_storage_path: str

    @property
    def headers(self) -> dict[str, str]:
        return _auth_header(self.user_id, self.email)

    def url(self, suffix: str) -> str:
        return f"/api/hosted/accounts/{self.account_id}/outputs/{self.output_id}{suffix}"


@pytest.fixture
def backend(app_client: TestClient) -> Any:
    """Return the in-memory Storage backend installed by `app_client`."""
    from hosted import storage

    return storage.get_backend()


@pytest.fixture(autouse=True)
async def _clean_review_tables(admin_pool: asyncpg.Pool) -> None:
    """Reset review evidence between tests."""
    async with admin_pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE public.audit_events, public.output_correction_uploads, "
            "public.reviews, public.output_versions, public.outputs CASCADE"
        )
        await conn.execute("TRUNCATE public.job_attempts, public.jobs CASCADE")


async def _upload(backend: Any, user_id: uuid.UUID, path: str, text: str, bucket: str) -> None:
    data = text.encode("utf-8")

    async def _stream() -> AsyncGenerator[bytes, None]:
        yield data

    await backend.upload(user_id, path, _stream(), "text/plain; charset=utf-8", bucket=bucket)


async def _seed_reviewable_output(
    admin_pool: asyncpg.Pool,
    backend: Any,
    *,
    email: str | None = None,
    validation_status: str = "valid",
    tombstoned: bool = False,
    skill: str = "post-call",
    markdown: str | None = None,
    with_transcript_object: bool = True,
) -> ReviewFixture:
    """Seed an org, account, transcript, job, and one generated output."""
    email = email or f"reviewer-{uuid.uuid4().hex[:8]}@airbyte.io"
    user_id, org_id, _ = await _seed_member(admin_pool, email)
    account_id = await _seed_account(admin_pool, org_id, user_id)
    transcript_id = await _seed_transcript(admin_pool, org_id, account_id, None, user_id)
    if with_transcript_object:
        await _upload(
            backend,
            user_id,
            f"{org_id}/{account_id}/{transcript_id}-test-transcript.txt",
            TRANSCRIPT_TEXT,
            _hs().DEFAULT_BUCKET,
        )

    output_id = uuid.uuid4()
    path = f"{org_id}/{account_id}/{transcript_id}/{output_id}/output.md"
    sidecar = {"skill": skill, "mode": "full", "validation_status": validation_status}
    async with admin_pool.acquire() as conn:
        job_id = await conn.fetchval(
            """
            INSERT INTO public.jobs (
                org_id, account_id, transcript_id, requester_id,
                skill, skill_version, status, max_attempts
            ) VALUES ($1, $2, $3, $4, $5, '1.0', 'success', 1)
            RETURNING id
            """,
            org_id,
            account_id,
            transcript_id,
            user_id,
            skill,
        )
        await conn.execute(
            """
            INSERT INTO public.outputs (
                id, org_id, job_id, account_id, transcript_id, requester_id,
                content_storage_path, title, sidecar, skill, skill_version,
                model, validation_status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, '1.0',
                      'claude-test', $11)
            """,
            output_id,
            org_id,
            job_id,
            account_id,
            transcript_id,
            user_id,
            path,
            "Acme post-call brief",
            json.dumps(sidecar),
            skill,
            validation_status,
        )
        if tombstoned:
            await conn.execute(
                "UPDATE public.outputs SET tombstoned_at = now() WHERE id = $1", output_id
            )

    await _upload(
        backend, user_id, path, markdown or _generated_markdown(), _hs().OUTPUTS_BUCKET
    )
    return ReviewFixture(
        user_id=user_id,
        email=email,
        org_id=org_id,
        account_id=account_id,
        transcript_id=transcript_id,
        output_id=output_id,
        content_storage_path=path,
    )


def _correct(
    client: TestClient,
    fx: ReviewFixture,
    *,
    base_version_id: str | None,
    markdown: str | None = None,
    change_summary: str | None = "Fixed an owner name",
    request_id: uuid.UUID | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "markdown": markdown if markdown is not None else _corrected_markdown(),
        "request_id": str(request_id or uuid.uuid4()),
    }
    if base_version_id is not None:
        payload["base_version_id"] = base_version_id
    if change_summary is not None:
        payload["change_summary"] = change_summary
    return client.post(fx.url("/corrections"), json=payload, headers=fx.headers)


def _comment(
    client: TestClient,
    fx: ReviewFixture,
    *,
    body: str = "Looks good, one nit.",
    target_version_id: str | None = None,
    request_id: uuid.UUID | None = None,
) -> Any:
    payload: dict[str, Any] = {"body": body, "request_id": str(request_id or uuid.uuid4())}
    if target_version_id is not None:
        payload["target_version_id"] = target_version_id
    return client.post(fx.url("/comments"), json=payload, headers=fx.headers)


def _approve(
    client: TestClient,
    fx: ReviewFixture,
    *,
    target_version_id: str | None = None,
    request_id: uuid.UUID | None = None,
) -> Any:
    payload: dict[str, Any] = {"request_id": str(request_id or uuid.uuid4())}
    if target_version_id is not None:
        payload["target_version_id"] = target_version_id
    return client.post(fx.url("/approvals"), json=payload, headers=fx.headers)


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------

async def test_review_state_starts_at_generated_v0_needing_review(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    response = app_client.get(fx.url("/review"), headers=fx.headers)
    assert response.status_code == 200
    body = response.json()
    assert body["review_state"] == "needs_review"
    assert body["current_version_id"] is None
    assert [v["kind"] for v in body["versions"]] == ["generated"]
    assert body["versions"][0]["label"] == "Generated (V0)"
    assert body["activity"] == []


async def test_session_route_resolves_the_verified_user_and_org(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    """`/api/auth/session` is the first call the hosted UI makes.

    It calls `require_org` directly instead of through FastAPI's dependency
    injection, so the verified user has to be resolved explicitly — otherwise
    every hosted page (including the review entry point) renders an error card.
    """
    fx = await _seed_reviewable_output(admin_pool, backend)
    response = app_client.get("/api/auth/session", headers=fx.headers)
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == str(fx.user_id)
    assert body["org"]["id"] == str(fx.org_id)
    assert body["membership"]["role"]
    assert app_client.get("/api/auth/session").status_code == 401


async def test_generated_version_content_is_rendered_from_trusted_path(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    response = app_client.get(fx.url("/versions/generated/content"), headers=fx.headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "generated"
    assert "Source Coverage" in body["markdown"]
    assert "<h2" in body["html"]


async def test_unauthenticated_mutations_are_rejected(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    request_id = str(uuid.uuid4())
    assert app_client.get(fx.url("/review")).status_code == 401
    assert (
        app_client.post(fx.url("/comments"), json={"body": "hi", "request_id": request_id}).status_code
        == 401
    )
    assert app_client.post(fx.url("/approvals"), json={"request_id": request_id}).status_code == 401
    assert (
        app_client.post(
            fx.url("/corrections"), json={"markdown": "# x", "request_id": request_id}
        ).status_code
        == 401
    )


async def test_inactive_membership_cannot_review(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    async with admin_pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.memberships SET active = false WHERE user_id = $1", fx.user_id
        )
    assert app_client.get(fx.url("/review"), headers=fx.headers).status_code == 403
    assert _comment(app_client, fx).status_code == 403
    assert _approve(app_client, fx).status_code == 403


async def test_cross_org_output_is_indistinguishable_from_missing(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    owner = await _seed_reviewable_output(admin_pool, backend)
    intruder = await _seed_reviewable_output(admin_pool, backend)

    stolen = ReviewFixture(
        user_id=intruder.user_id,
        email=intruder.email,
        org_id=intruder.org_id,
        account_id=owner.account_id,
        transcript_id=owner.transcript_id,
        output_id=owner.output_id,
        content_storage_path=owner.content_storage_path,
    )
    assert app_client.get(stolen.url("/review"), headers=stolen.headers).status_code == 404
    assert _comment(app_client, stolen).status_code == 404
    assert _approve(app_client, stolen).status_code == 404
    assert _correct(app_client, stolen, base_version_id=None).status_code == 404
    assert (
        app_client.get(stolen.url("/versions/generated/content"), headers=stolen.headers).status_code
        == 404
    )


async def test_version_id_from_another_output_or_org_is_rejected(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    first = await _seed_reviewable_output(admin_pool, backend)
    second = await _seed_reviewable_output(admin_pool, backend, email="second@airbyte.io")

    created = _correct(app_client, second, base_version_id=None)
    assert created.status_code == 201
    other_version_id = created.json()["correction"]["output_version_id"]

    # Same-org-but-other-output and cross-org version ids are both unusable.
    assert _comment(app_client, first, target_version_id=other_version_id).status_code == 404
    assert _approve(app_client, first, target_version_id=other_version_id).status_code == 404
    assert _correct(app_client, first, base_version_id=other_version_id).status_code == 404
    assert (
        app_client.get(
            first.url(f"/versions/{other_version_id}/content"), headers=first.headers
        ).status_code
        == 404
    )


async def test_forged_identity_and_provenance_fields_are_rejected(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    """The browser cannot supply identity, Storage, or provenance fields."""
    fx = await _seed_reviewable_output(admin_pool, backend)
    other = await _seed_reviewable_output(admin_pool, backend, email="victim@airbyte.io")

    forged = {
        "body": "hello",
        "request_id": str(uuid.uuid4()),
        "org_id": str(other.org_id),
        "user_id": str(other.user_id),
    }
    assert app_client.post(fx.url("/comments"), json=forged, headers=fx.headers).status_code == 422

    forged_correction = {
        "markdown": _corrected_markdown(),
        "request_id": str(uuid.uuid4()),
        "content_storage_path": f"{other.org_id}/evil/output.md",
        "validation_status": "valid",
        "sidecar": {"origin": "model_generated"},
        "created_by": str(other.user_id),
    }
    assert (
        app_client.post(fx.url("/corrections"), json=forged_correction, headers=fx.headers).status_code
        == 422
    )


async def test_tombstoned_and_invalid_outputs_are_not_reviewable(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    tombstoned = await _seed_reviewable_output(admin_pool, backend, tombstoned=True)
    invalid = await _seed_reviewable_output(admin_pool, backend, validation_status="invalid")
    for fx in (tombstoned, invalid):
        assert app_client.get(fx.url("/review"), headers=fx.headers).status_code == 404
        assert _comment(app_client, fx).status_code == 404
        assert _approve(app_client, fx).status_code == 404
        assert _correct(app_client, fx, base_version_id=None).status_code == 404


# ---------------------------------------------------------------------------
# Direct database boundary
# ---------------------------------------------------------------------------

async def test_app_user_cannot_mutate_evidence_tables_directly(
    app_client: TestClient, admin_pool: asyncpg.Pool, user_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    statements = [
        (
            "INSERT INTO public.reviews (org_id, output_id, user_id, action, comment) "
            "VALUES ($1, $2, $3, 'approve', NULL)",
            (fx.org_id, fx.output_id, fx.user_id),
        ),
        (
            "INSERT INTO public.output_versions (org_id, output_id, content_storage_path) "
            "VALUES ($1, $2, 'x/y.md')",
            (fx.org_id, fx.output_id),
        ),
        (
            "INSERT INTO public.audit_events (org_id, user_id, action, entity_type) "
            "VALUES ($1, $2, 'output_comment', 'outputs')",
            (fx.org_id, fx.user_id),
        ),
        ("UPDATE public.outputs SET title = 'hacked' WHERE id = $1", (fx.output_id,)),
        ("DELETE FROM public.reviews WHERE org_id = $1", (fx.org_id,)),
        ("SELECT * FROM public.output_correction_uploads", ()),
    ]
    async with user_pool.acquire() as conn:
        for sql, args in statements:
            with pytest.raises(asyncpg.PostgresError):
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('app.context_token', $1, true)",
                        _context_token(fx.user_id),
                    )
                    await conn.execute(sql, *args)


async def test_narrow_definer_functions_succeed_for_app_user(
    app_client: TestClient, admin_pool: asyncpg.Pool, user_pool: asyncpg.Pool, backend: Any
) -> None:
    """The only writable path is the narrow SECURITY DEFINER function."""
    fx = await _seed_reviewable_output(admin_pool, backend)
    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)", _context_token(fx.user_id)
            )
            raw = await conn.fetchval(
                "SELECT public.add_output_comment($1, $2, $3, $4, $5)",
                fx.output_id,
                fx.account_id,
                None,
                "definer path works",
                uuid.uuid4(),
            )
    result = json.loads(raw) if isinstance(raw, str) else raw
    assert result["replayed"] is False

    async with admin_pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM public.reviews WHERE output_id = $1 AND action = 'comment'",
            fx.output_id,
        )
        audit = await conn.fetchval(
            "SELECT count(*) FROM public.audit_events WHERE action = 'output_comment' AND org_id = $1",
            fx.org_id,
        )
    assert count == 1
    assert audit == 1


async def test_forged_context_token_cannot_act(
    app_client: TestClient, admin_pool: asyncpg.Pool, user_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    async with user_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError):
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.context_token', $1, true)",
                    f"{fx.user_id}:deadbeef",
                )
                await conn.fetchval(
                    "SELECT public.add_output_comment($1, $2, $3, $4, $5)",
                    fx.output_id,
                    fx.account_id,
                    None,
                    "forged",
                    uuid.uuid4(),
                )


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

async def test_comment_on_v0_is_appended_and_visible(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    response = _comment(app_client, fx, body="Please fix the owner of action 2.")
    assert response.status_code == 201
    state = response.json()["review"]
    assert state["review_state"] == "needs_review"
    assert len(state["activity"]) == 1
    entry = state["activity"][0]
    assert entry["action"] == "comment"
    assert entry["output_version_id"] is None
    assert entry["comment"] == "Please fix the owner of action 2."
    assert entry["user_email"] == fx.email


async def test_comment_html_is_stored_and_returned_as_inert_text(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    malicious = "<script>alert('x')</script><img src=x onerror=alert(1)>"
    response = _comment(app_client, fx, body=malicious)
    assert response.status_code == 201
    activity = response.json()["review"]["activity"][0]
    # Comments are plain text: stored verbatim, never rendered as Markdown/HTML.
    assert activity["comment"] == malicious
    assert "html" not in activity


async def test_oversized_and_control_character_comments_are_rejected(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _comment(app_client, fx, body="x" * 4001).status_code == 422
    assert _comment(app_client, fx, body="   ").status_code == 422
    assert _comment(app_client, fx, body="bad\x00null").status_code == 422
    assert _comment(app_client, fx, body="bell\x07here").status_code == 422


async def test_comment_does_not_revoke_approval(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _approve(app_client, fx).status_code == 201
    response = _comment(app_client, fx, body="Still fine.")
    assert response.status_code == 201
    assert response.json()["review"]["review_state"] == "approved"


async def test_comments_on_historical_versions_remain_visible(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _comment(app_client, fx, body="On V0").status_code == 201
    correction = _correct(app_client, fx, base_version_id=None)
    assert correction.status_code == 201
    version_id = correction.json()["correction"]["output_version_id"]

    # A comment can still target the historical generated version.
    assert _comment(app_client, fx, body="Late note on V0").status_code == 201
    state = app_client.get(fx.url("/review"), headers=fx.headers).json()
    comments = [a for a in state["activity"] if a["action"] == "comment"]
    assert [c["comment"] for c in comments] == ["On V0", "Late note on V0"]
    assert all(c["output_version_id"] is None for c in comments)
    assert state["current_version_id"] == version_id


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------

async def test_first_correction_from_v0_then_from_current(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    first = _correct(app_client, fx, base_version_id=None, markdown=_corrected_markdown("First"))
    assert first.status_code == 201
    v1 = first.json()["correction"]["output_version_id"]
    assert first.json()["review"]["current_version_id"] == v1

    second = _correct(app_client, fx, base_version_id=v1, markdown=_corrected_markdown("Second"))
    assert second.status_code == 201
    body = second.json()
    v2 = body["correction"]["output_version_id"]
    assert body["correction"]["previous_version_id"] == v1
    assert body["review"]["current_version_id"] == v2
    assert [v["label"] for v in body["review"]["versions"]] == [
        "Generated (V0)",
        "Correction 1",
        "Correction 2",
    ]

    content = app_client.get(fx.url(f"/versions/{v2}/content"), headers=fx.headers)
    assert content.status_code == 200
    assert "Second" in content.json()["markdown"]


async def test_stale_correction_base_conflicts(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _correct(app_client, fx, base_version_id=None).status_code == 201
    stale = _correct(app_client, fx, base_version_id=None, markdown=_corrected_markdown("Stale"))
    assert stale.status_code == 409
    assert "Refresh" in stale.json()["detail"]


async def test_concurrent_same_base_corrections_allow_exactly_one_child(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)

    def _submit(marker: str) -> Any:
        return _correct(app_client, fx, base_version_id=None, markdown=_corrected_markdown(marker))

    results = await asyncio.gather(
        asyncio.to_thread(_submit, "A"), asyncio.to_thread(_submit, "B")
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409]

    async with admin_pool.acquire() as conn:
        roots = await conn.fetchval(
            "SELECT count(*) FROM public.output_versions "
            "WHERE output_id = $1 AND previous_version_id IS NULL",
            fx.output_id,
        )
        objects = [
            key
            for key in backend.objects
            if key.startswith(f"{_hs().OUTPUTS_BUCKET}:{fx.org_id}")
            and "/versions/" in key
        ]
    assert roots == 1
    assert len(objects) == 1


async def test_invalid_correction_has_no_durable_side_effects(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    before = dict(backend.objects)
    response = _correct(app_client, fx, base_version_id=None, markdown=_invalid_markdown())
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["validation_errors"]
    assert any("Source Coverage" in e for e in detail["validation_errors"])

    async with admin_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM public.output_versions WHERE output_id = $1", fx.output_id
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM public.reviews WHERE output_id = $1", fx.output_id
        ) == 0
        assert await conn.fetchval("SELECT count(*) FROM public.audit_events") == 0
        assert await conn.fetchval("SELECT count(*) FROM public.output_correction_uploads") == 0
    assert backend.objects == before


async def test_correction_markdown_html_is_sanitized_on_render(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    hostile = _corrected_markdown(
        "<script>alert('x')</script> <img src=x onerror=alert(1)> [x](javascript:alert(1))"
    )
    response = _correct(app_client, fx, base_version_id=None, markdown=hostile)
    assert response.status_code == 201
    version_id = response.json()["correction"]["output_version_id"]

    content = app_client.get(fx.url(f"/versions/{version_id}/content"), headers=fx.headers).json()
    assert "<script" not in content["html"]
    assert "onerror" not in content["html"]
    assert "javascript:" not in content["html"]
    # The stored Markdown is preserved verbatim; only rendering is sanitized.
    assert "<script>alert('x')</script>" in content["markdown"]

    preview = app_client.post(
        fx.url("/preview"), json={"markdown": hostile}, headers=fx.headers
    ).json()
    assert "<script" not in preview["html"]
    assert "onerror" not in preview["html"]


async def test_oversized_correction_and_change_summary_are_rejected(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert (
        _correct(
            app_client, fx, base_version_id=None, markdown="#" + "x" * 400_001
        ).status_code
        == 422
    )
    assert (
        _correct(
            app_client, fx, base_version_id=None, change_summary="s" * 2001
        ).status_code
        == 422
    )
    assert (
        _correct(
            app_client, fx, base_version_id=None, markdown="# ok\x00hidden"
        ).status_code
        == 422
    )
    assert _correct(app_client, fx, base_version_id=None, markdown="   ").status_code == 422


async def test_original_generated_evidence_is_never_overwritten(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    original_key = f"{_hs().OUTPUTS_BUCKET}:{fx.content_storage_path}"
    original_bytes = backend.objects[original_key]
    async with admin_pool.acquire() as conn:
        original_row = await conn.fetchrow(
            "SELECT content_storage_path, sidecar, generated_at, validation_status, model "
            "FROM public.outputs WHERE id = $1",
            fx.output_id,
        )

    assert _correct(app_client, fx, base_version_id=None).status_code == 201

    async with admin_pool.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT content_storage_path, sidecar, generated_at, validation_status, model "
            "FROM public.outputs WHERE id = $1",
            fx.output_id,
        )
        version = await conn.fetchrow(
            "SELECT content_storage_path, sidecar FROM public.output_versions WHERE output_id = $1",
            fx.output_id,
        )
    assert dict(after) == dict(original_row)
    assert backend.objects[original_key] == original_bytes
    assert version["content_storage_path"] != original_row["content_storage_path"]
    sidecar = json.loads(version["sidecar"]) if isinstance(version["sidecar"], str) else version["sidecar"]
    assert sidecar["origin"] == "human_correction"
    assert sidecar["validation_status"] == "valid"
    assert "model" not in sidecar


async def test_correction_requires_the_generation_time_transcript(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    """Validation must not silently weaken when the transcript is unavailable."""
    fx = await _seed_reviewable_output(admin_pool, backend, with_transcript_object=False)
    response = _correct(app_client, fx, base_version_id=None)
    assert response.status_code == 503
    async with admin_pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM public.output_correction_uploads") == 0


async def test_malformed_version_chain_fails_closed(
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
    superuser_pool: asyncpg.Pool,
    backend: Any,
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _correct(app_client, fx, base_version_id=None).status_code == 201

    # Simulate legacy/branched history that the current indexes would reject.
    async with superuser_pool.acquire() as conn:
        await conn.execute("DROP INDEX IF EXISTS idx_output_versions_single_root")
        try:
            await conn.execute(
                """
                INSERT INTO public.output_versions (
                    org_id, output_id, previous_version_id, content_storage_path, created_by
                ) VALUES ($1, $2, NULL, 'branch/output.md', $3)
                """,
                fx.org_id,
                fx.output_id,
                fx.user_id,
            )
            assert app_client.get(fx.url("/review"), headers=fx.headers).status_code == 409
            assert _correct(app_client, fx, base_version_id=None).status_code == 409
        finally:
            await conn.execute(
                "DELETE FROM public.output_versions "
                "WHERE output_id = $1 AND content_storage_path = 'branch/output.md'",
                fx.output_id,
            )
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_output_versions_single_root "
                "ON public.output_versions(output_id) WHERE previous_version_id IS NULL"
            )


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

async def test_approve_generated_version_then_correction_needs_review(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    approved = _approve(app_client, fx)
    assert approved.status_code == 201
    assert approved.json()["review"]["review_state"] == "approved"

    corrected = _correct(app_client, fx, base_version_id=None)
    assert corrected.status_code == 201
    state = corrected.json()["review"]
    assert state["review_state"] == "needs_review"
    # The historical approval evidence is preserved, not deleted.
    approvals = [a for a in state["activity"] if a["action"] == "approve"]
    assert len(approvals) == 1
    assert approvals[0]["output_version_id"] is None

    version_id = corrected.json()["correction"]["output_version_id"]
    final = _approve(app_client, fx, target_version_id=version_id)
    assert final.status_code == 201
    assert final.json()["review"]["review_state"] == "approved"


async def test_approving_a_stale_version_conflicts(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _correct(app_client, fx, base_version_id=None).status_code == 201
    stale = _approve(app_client, fx, target_version_id=None)
    assert stale.status_code == 409


async def test_concurrent_approval_and_correction_never_approve_a_stale_version(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)

    results = await asyncio.gather(
        asyncio.to_thread(lambda: _approve(app_client, fx, target_version_id=None)),
        asyncio.to_thread(lambda: _correct(app_client, fx, base_version_id=None)),
    )
    assert all(r.status_code in (201, 409) for r in results)

    state = app_client.get(fx.url("/review"), headers=fx.headers).json()
    approvals = [
        a for a in state["activity"] if a["action"] == "approve"
    ]
    if state["current_version_id"] is None:
        assert state["review_state"] == ("approved" if approvals else "needs_review")
    else:
        # A correction landed, so the V0 approval (if any) is only historical.
        assert state["review_state"] == "needs_review"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

async def test_replayed_requests_are_idempotent(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)

    comment_key = uuid.uuid4()
    first = _comment(app_client, fx, body="same", request_id=comment_key)
    replay = _comment(app_client, fx, body="same", request_id=comment_key)
    assert first.status_code == 201 and replay.status_code == 201
    assert first.json()["comment"]["review_id"] == replay.json()["comment"]["review_id"]
    assert replay.json()["comment"]["replayed"] is True

    correction_key = uuid.uuid4()
    markdown = _corrected_markdown("Idempotent")
    c1 = _correct(app_client, fx, base_version_id=None, markdown=markdown, request_id=correction_key)
    c2 = _correct(app_client, fx, base_version_id=None, markdown=markdown, request_id=correction_key)
    assert c1.status_code == 201 and c2.status_code == 201
    assert (
        c1.json()["correction"]["output_version_id"] == c2.json()["correction"]["output_version_id"]
    )

    version_id = c1.json()["correction"]["output_version_id"]
    approval_key = uuid.uuid4()
    a1 = _approve(app_client, fx, target_version_id=version_id, request_id=approval_key)
    a2 = _approve(app_client, fx, target_version_id=version_id, request_id=approval_key)
    assert a1.status_code == 201 and a2.status_code == 201
    assert a1.json()["approval"]["review_id"] == a2.json()["approval"]["review_id"]

    async with admin_pool.acquire() as conn:
        counts = await conn.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM public.reviews WHERE output_id = $1) AS reviews,
                (SELECT count(*) FROM public.output_versions WHERE output_id = $1) AS versions,
                (SELECT count(*) FROM public.audit_events WHERE org_id = $2) AS audits
            """,
            fx.output_id,
            fx.org_id,
        )
        objects = [
            key
            for key in backend.objects
            if key.startswith(f"{_hs().OUTPUTS_BUCKET}:{fx.org_id}")
            and "/versions/" in key
        ]
    assert counts["reviews"] == 3  # comment + correct + approve
    assert counts["versions"] == 1
    assert counts["audits"] == 3
    assert len(objects) == 1


async def test_same_idempotency_key_with_different_payload_conflicts(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)

    comment_key = uuid.uuid4()
    assert _comment(app_client, fx, body="first", request_id=comment_key).status_code == 201
    assert _comment(app_client, fx, body="different", request_id=comment_key).status_code == 409

    correction_key = uuid.uuid4()
    assert (
        _correct(
            app_client,
            fx,
            base_version_id=None,
            markdown=_corrected_markdown("One"),
            request_id=correction_key,
        ).status_code
        == 201
    )
    changed = _correct(
        app_client,
        fx,
        base_version_id=None,
        markdown=_corrected_markdown("Two"),
        request_id=correction_key,
    )
    assert changed.status_code == 409


# ---------------------------------------------------------------------------
# Storage/DB atomicity
# ---------------------------------------------------------------------------

async def test_storage_failure_before_commit_leaves_no_evidence(
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
    backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)

    async def _fail(*args: Any, **kwargs: Any) -> None:
        raise _hs().StorageError("upload unavailable")

    monkeypatch.setattr(backend, "upload", _fail)
    response = _correct(app_client, fx, base_version_id=None)
    assert response.status_code == 503

    async with admin_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM public.output_versions WHERE output_id = $1", fx.output_id
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM public.reviews WHERE output_id = $1", fx.output_id
        ) == 0
        assert await conn.fetchval("SELECT count(*) FROM public.audit_events") == 0
        state = await conn.fetchval(
            "SELECT state FROM public.output_correction_uploads WHERE output_id = $1", fx.output_id
        )
    assert state == "aborted"


async def test_commit_failure_deletes_the_uploaded_object(
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
    backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    import hosted.reviews as reviews_module

    original = reviews_module.tenant_connection
    calls = {"n": 0}

    class _Boom:
        async def __aenter__(self) -> Any:
            raise asyncpg.PostgresConnectionError("db unavailable")

        async def __aexit__(self, *args: Any) -> None:
            return None

    def _patched(request: Any, org: Any) -> Any:
        calls["n"] += 1
        # The commit is the third tenant connection: load output, reserve, commit.
        if calls["n"] == 3:
            return _Boom()
        return original(request, org)

    monkeypatch.setattr(reviews_module, "tenant_connection", _patched)
    response = _correct(app_client, fx, base_version_id=None)
    assert response.status_code >= 500

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, content_storage_path FROM public.output_correction_uploads "
            "WHERE output_id = $1",
            fx.output_id,
        )
        assert await conn.fetchval(
            "SELECT count(*) FROM public.output_versions WHERE output_id = $1", fx.output_id
        ) == 0
    assert row["state"] == "aborted"
    assert f"{_hs().OUTPUTS_BUCKET}:{row['content_storage_path']}" not in backend.objects


async def test_cleanup_delete_failure_leaves_retryable_orphan_evidence(
    app_client: TestClient,
    admin_pool: asyncpg.Pool,
    worker_pool: asyncpg.Pool,
    backend: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    import hosted.reviews as reviews_module

    original = reviews_module.tenant_connection
    calls = {"n": 0}

    class _Boom:
        async def __aenter__(self) -> Any:
            raise asyncpg.PostgresConnectionError("db unavailable")

        async def __aexit__(self, *args: Any) -> None:
            return None

    def _patched(request: Any, org: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 3:
            return _Boom()
        return original(request, org)

    async def _fail_delete(*args: Any, **kwargs: Any) -> None:
        raise _hs().StorageError("delete unavailable")

    monkeypatch.setattr(reviews_module, "tenant_connection", _patched)
    monkeypatch.setattr(backend, "delete", _fail_delete)
    assert _correct(app_client, fx, base_version_id=None).status_code >= 500

    async with admin_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, state, cleanup_attempts, content_storage_path "
            "FROM public.output_correction_uploads WHERE output_id = $1",
            fx.output_id,
        )
    assert row["state"] == "orphaned"
    assert row["cleanup_attempts"] == 1
    # The object is still there, which is exactly why the evidence is durable.
    assert f"{_hs().OUTPUTS_BUCKET}:{row['content_storage_path']}" in backend.objects

    # The worker-side cleanup path can claim and finalize it after a restart.
    async with worker_pool.acquire() as conn:
        claimed = await conn.fetchrow(
            "SELECT * FROM public.claim_orphaned_correction_upload($1, $2)", "cleanup-1", 10
        )
        assert claimed["reservation_id"] == row["id"]
        # The claim is a lease: a second worker cannot take the same row and
        # race the first worker's delete.
        again = await conn.fetchrow(
            "SELECT * FROM public.claim_orphaned_correction_upload($1, $2)", "cleanup-2", 10
        )
        assert again is None
        finalized = await conn.fetchval(
            "SELECT public.finalize_correction_cleanup($1, $2)", row["id"], "cleanup-1"
        )
    assert finalized is True

    async with admin_pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT state FROM public.output_correction_uploads WHERE id = $1", row["id"]
            )
            == "aborted"
        )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

async def test_audit_metadata_contains_only_safe_identifiers(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    secret_comment = "Do not leak: ACME renewal at risk"
    assert _comment(app_client, fx, body=secret_comment).status_code == 201
    correction = _correct(app_client, fx, base_version_id=None, markdown=_corrected_markdown("Secret"))
    assert correction.status_code == 201
    version_id = correction.json()["correction"]["output_version_id"]
    assert _approve(app_client, fx, target_version_id=version_id).status_code == 201

    async with admin_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action, entity_type, entity_id, user_id, org_id, metadata "
            "FROM public.audit_events WHERE org_id = $1 ORDER BY created_at",
            fx.org_id,
        )
    actions = [r["action"] for r in rows]
    assert actions == ["output_comment", "output_correction", "output_approval"]
    allowed_keys = {
        "output_id",
        "output_version_id",
        "previous_version_id",
        "review_id",
        "request_id",
    }
    for row in rows:
        assert row["org_id"] == fx.org_id
        assert row["user_id"] == fx.user_id
        metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        assert set(metadata) <= allowed_keys
        serialized = json.dumps(metadata)
        assert secret_comment not in serialized
        assert "Secret" not in serialized
        assert "Source Coverage" not in serialized
        assert fx.content_storage_path not in serialized
        assert "versions/" not in serialized
        assert "Acme" not in serialized


async def test_audit_events_are_readable_only_within_the_organization(
    app_client: TestClient, admin_pool: asyncpg.Pool, user_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    other = await _seed_reviewable_output(admin_pool, backend, email="outsider@airbyte.io")
    assert _comment(app_client, fx, body="in org").status_code == 201

    async with user_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)", _context_token(other.user_id)
            )
            visible = await conn.fetchval("SELECT count(*) FROM public.audit_events")
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.context_token', $1, true)", _context_token(fx.user_id)
            )
            own = await conn.fetchval("SELECT count(*) FROM public.audit_events")
    assert visible == 0
    assert own == 1


# ---------------------------------------------------------------------------
# Migration behavior
# ---------------------------------------------------------------------------

async def test_migration_reapply_preserves_existing_review_history(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any, hosted_env: dict[str, str]
) -> None:
    """Re-running migration 009 over populated tables preserves prior evidence."""
    import hosted
    import hosted.migrations as migrations

    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _comment(app_client, fx, body="before upgrade").status_code == 201
    correction = _correct(app_client, fx, base_version_id=None)
    assert correction.status_code == 201
    version_id = uuid.UUID(correction.json()["correction"]["output_version_id"])

    await migrations.migrate(
        hosted_env["MIGRATE_DATABASE_URL"],
        hosted.config.MIGRATIONS_DIR,
        app_user_password="app_user_password",
        app_admin_password="app_admin_password",
        app_worker_password="app_worker_password",
        context_secret=hosted_env["HOSTED_CONTEXT_SECRET"],
    )

    async with admin_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM public.output_versions WHERE id = $1", version_id
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM public.reviews WHERE output_id = $1", fx.output_id
        ) == 2
        assert await conn.fetchval(
            "SELECT count(*) FROM public.audit_events WHERE org_id = $1", fx.org_id
        ) == 2

    state = app_client.get(fx.url("/review"), headers=fx.headers).json()
    assert state["current_version_id"] == str(version_id)
    assert [v["label"] for v in state["versions"]] == ["Generated (V0)", "Correction 1"]


async def test_second_member_of_the_org_sees_shared_review_history(
    app_client: TestClient, admin_pool: asyncpg.Pool, backend: Any
) -> None:
    fx = await _seed_reviewable_output(admin_pool, backend)
    assert _comment(app_client, fx, body="from member one").status_code == 201
    colleague_id = await _seed_user_and_membership(
        admin_pool, f"colleague-{uuid.uuid4().hex[:6]}@airbyte.io", fx.org_id
    )
    response = app_client.get(
        fx.url("/review"), headers=_auth_header(colleague_id, "colleague@airbyte.io")
    )
    assert response.status_code == 200
    assert [a["comment"] for a in response.json()["activity"]] == ["from member one"]
