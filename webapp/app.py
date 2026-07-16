#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi", "uvicorn[standard]", "pyyaml",
#   "faster-whisper", "sounddevice", "numpy", "sse-starlette", "anthropic",
#   "markdown", "nh3", "keyring",
# ]
# ///
# NOTE: live-transcribe needs the PortAudio system lib for sounddevice:
#   brew install portaudio   (one-time)
# and BlackHole for system-audio capture (see README -> Live Transcribe setup).
"""SE Skills — local web app.

A thin UI over the filesystem the SE skills already produce, plus a button to
invoke a skill via Claude Code headless (`claude -p`).

Structure:
  Main page      -> solutions team members (from team-members.yaml + .se-config.yaml)
  Member page    -> that member's accounts (folders in 01-customers/) + create account
  Account page   -> all outputs for that account + invoke a skill

Run:
  cd webapp && uv run app.py
  (or: uvicorn app:app --reload --port 8787)

This is LOCAL ONLY. It runs as you, on your machine, using your already-authed
Claude Code + MCPs + local files. Do not deploy this to a shared server without
solving multi-user auth + data isolation first (see README).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

import config
from integrations.salesforce import SalesforceIntegration
from routes.accounts import router as accounts_router
from routes.ask import router as ask_router
from routes.feedback import router as feedback_router
from routes.jobs import router as jobs_router
from routes.outputs import router as outputs_router
from routes.overview import router as overview_router
from routes.salesforce import router as salesforce_router
from routes.skills import router as skills_router
from routes.transcription import router as transcription_router
from services.account_service import AccountService
from services.ask_service import AskService, anthropic_api_key
from services.feedback_service import FeedbackService
from services.job_service import JobService
from services.output_service import OutputService
from services.overview_service import OverviewService
from services.skill_runtime_service import SkillRuntimeService
from services.transcription_service import TranscriptionService

logger = logging.getLogger(__name__)

# Favicon: inline SVG (also linked in index.html <head>). Serving it here too
# silences the browser's default GET /favicon.ico even for clients that ignore
# the <link>. Must be registered before the catch-all static mount below.
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='7' fill='#151a24'/>"
    "<text x='16' y='22' font-family='Inter,system-ui,sans-serif' font-size='19' "
    "font-weight='700' fill='#4263eb' text-anchor='middle'>S</text></svg>"
)


def _build_services(app: FastAPI) -> None:
    """Construct shared services once and wire them to `app.state`.

    Each stateful service is created exactly once per app so identity is preserved
    across all routes and consumers (AccountService, OverviewService, AskService,
    and the skill runtime all share the same JobService and OutputService).
    """
    output_service = OutputService(
        customers_dir=config.CUSTOMERS_DIR,
        workspace=config.WORKSPACE,
        repo_root=config.WEBAPP_DIR.parent,
        se_config=config._se_config,
        safe_name=config._safe,
        slug=config._slug,
        run_cmd=config._run_cmd,
        internal_repo=config._internal_repo,
    )
    feedback_service = FeedbackService(customers_dir=config.CUSTOMERS_DIR)
    job_service = JobService(
        workspace=config.WORKSPACE,
        model_for=config._model_for,
        persist_run=output_service.persist_run,
    )
    salesforce_integration = SalesforceIntegration(
        customers_dir=config.CUSTOMERS_DIR,
        workspace=config.WORKSPACE,
        sf_config=lambda: (config._se_config().get("salesforce") or {}),
        titlecase=config._titlecase_folder,
        slug=config._slug,
    )
    account_service = AccountService(
        customers_dir=config.CUSTOMERS_DIR,
        webapp_dir=config.WEBAPP_DIR,
        output_service=output_service,
        job_service=job_service,
        safe_name=config._safe,
        titlecase=config._titlecase_folder,
        slug=config._slug,
        team_file=config.TEAM_FILE,
        member_prefs_dir=config.WEBAPP_DIR / ".member-prefs",
        se_config_file=config.SE_CONFIG,
        sfdc_opportunities=salesforce_integration.opportunities_for_account,
    )
    overview_service = OverviewService(
        account_service=account_service,
        output_service=output_service,
        job_service=job_service,
    )
    ask_service = AskService(
        output_service=output_service,
        job_service=job_service,
        api_key=anthropic_api_key,
        model_for=config._model_for,
    )
    transcription_service = TranscriptionService(
        customers_dir=config.CUSTOMERS_DIR,
        workspace=config.WORKSPACE,
        safe_name=config._safe,
        titlecase=config._titlecase_folder,
    )
    skill_runtime_service = SkillRuntimeService(
        customers_dir=config.CUSTOMERS_DIR,
        workspace=config.WORKSPACE,
        output_service=output_service,
        job_service=job_service,
        se_config=config._se_config,
        se_config_clear=config._se_config_clear,
        safe_name=config._safe,
        skills_dir=config.SUITE_SKILLS_DIR,
        skills_dirs=config.SKILLS_DIRS,
    )

    app.state.output_service = output_service
    app.state.feedback_service = feedback_service
    app.state.job_service = job_service
    app.state.salesforce_integration = salesforce_integration
    app.state.account_service = account_service
    app.state.overview_service = overview_service
    app.state.ask_service = ask_service
    app.state.transcription_service = transcription_service
    app.state.skill_runtime_service = skill_runtime_service


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup/shutdown lifecycle.

    Construction of services (and therefore job + transcription recovery from
    persistence) happens at startup; on shutdown we stop any active transcription
    channels so the process can exit cleanly.
    """
    logger.info("se-skills webapp starting up")
    yield
    logger.info("se-skills webapp shutting down")
    svc: TranscriptionService = getattr(app.state, "transcription_service", None)
    if svc:
        svc.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="SE Skills", lifespan=_lifespan)
    _build_services(app)

    # Public routes — registered exactly once.
    app.include_router(skills_router)
    app.include_router(jobs_router)
    app.include_router(accounts_router)
    app.include_router(outputs_router)
    app.include_router(feedback_router)
    app.include_router(overview_router)
    app.include_router(salesforce_router)
    app.include_router(ask_router)
    app.include_router(transcription_router)

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(content=_FAVICON_SVG, media_type="image/svg+xml")

    # Serve the static frontend at root. Must be last because it is a catch-all.
    app.mount(
        "/",
        StaticFiles(directory=str(config.WEBAPP_DIR / "static"), html=True),
        name="static",
    )
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787)
