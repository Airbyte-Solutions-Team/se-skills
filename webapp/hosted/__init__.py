"""Hosted-mode extension for the SE Skills webapp.

Imports of optional dependencies are deferred until hosted mode is actually
enabled so the local-only app does not fail if asyncpg/pyJWT are not installed.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config


def _auth_config(request: Request) -> JSONResponse:
    return JSONResponse(config.hosted_public_config())


async def _session(request: Request) -> JSONResponse:
    # Lazy import keeps the local app free of optional hosted deps.
    from .auth import require_org

    org = await require_org(request)
    return JSONResponse(
        {
            "user": {"id": str(org.user.id), "email": org.user.email},
            "org": {"id": str(org.org_id)},
            "membership": {"role": org.role},
        }
    )


def add_hosted_routers(app: FastAPI) -> None:
    """Register hosted-mode routes when HOSTED_MODE is enabled."""
    if not config.is_hosted():
        return

    from fastapi import APIRouter
    from . import accounts, jobs, storage, transcripts

    app.state.storage_backend = storage.get_backend()

    auth_router = APIRouter(prefix="/api/auth", tags=["auth"])
    auth_router.add_api_route("/config", _auth_config, methods=["GET"])
    auth_router.add_api_route("/session", _session, methods=["GET"])

    app.include_router(auth_router)
    app.include_router(accounts.router)
    app.include_router(jobs.router)
    app.include_router(transcripts.router)
