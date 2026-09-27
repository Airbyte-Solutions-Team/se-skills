#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi", "uvicorn[standard]", "pyyaml",
#   "faster-whisper", "sounddevice", "numpy", "sse-starlette", "anthropic",
#   "markdown", "nh3", "keyring",
#   "asyncpg", "pyjwt[crypto]",
# ]
# ///
# NOTE: live-transcribe needs the PortAudio system lib for sounddevice:
#   brew install portaudio   (one-time)
# and BlackHole for system-audio capture (see README -> Live Transcribe setup).
"""SE Skills — FastAPI composition root.

In local mode the app wires the existing filesystem-backed services and routes.
In hosted mode (`HOSTED_MODE=1`) it builds only the hosted-safe auth/org/account
surface and static assets; local filesystem, integration, transcription, skill,
and shell routes are not registered.

Run:
  cd webapp && uv run app.py
  (or: uvicorn app:app --reload --port 8787)
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

import config

logger = logging.getLogger(__name__)

# Optional hosted-mode extension. If HOSTED_MODE is requested and the module is
# missing or invalid, fail closed immediately instead of silently booting the
# local app.
try:
    import hosted
    _HOSTED_AVAILABLE = True
    _HOSTED_IMPORT_ERROR: BaseException | None = None
except Exception as _hosted_exc:
    _HOSTED_AVAILABLE = False
    _HOSTED_IMPORT_ERROR = _hosted_exc


_HOSTED_MODE_REQUESTED = (os.environ.get("HOSTED_MODE") or "").lower() in ("1", "true", "yes")


if _HOSTED_MODE_REQUESTED and not _HOSTED_AVAILABLE:
    raise RuntimeError(
        "HOSTED_MODE is enabled but the hosted extension could not be imported. "
        "Verify that asyncpg and pyjwt are installed."
    ) from _HOSTED_IMPORT_ERROR


# Favicon: inline SVG (also linked in index.html <head>). Serving it here too
# silences the browser's default GET /favicon.ico even for clients that ignore
# the <link>. Must be registered before the catch-all static mount below.
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='7' fill='#151a24'/>"
    "<text x='16' y='22' font-family='Inter,system-ui,sans-serif' font-size='19' "
    "font-weight='700' fill='#4263eb' text-anchor='middle'>S</text></svg>"
)


def _build_local_services(app: FastAPI) -> None:
    """Construct filesystem-backed local services and wire them to `app.state`.

    Imports are deferred so the hosted mode surface can start without optional
    local dependencies such as faster-whisper or anthropic.
    """
    from integrations.gmail import GmailSourceAdapter, SyntheticGmailTransport, UnavailableGmailTransport
    from integrations.gmail_live import TRANSPORT_ENV as GMAIL_TRANSPORT_ENV
    from integrations.gmail_live import LiveGmailReadOnlyTransport, credentials_path
    from integrations.granola import ManualGranolaImportAdapter
    from integrations.granola_mcp_relay import (
        ClaudeCodeMcpRelay,
        scoped_server_definition,
    )
    from integrations.salesforce import SalesforceIntegration
    from routes.accounts import router as accounts_router
    from routes.ask import router as ask_router
    from routes.command_center import router as command_center_router
    from routes.feedback import router as feedback_router
    from routes.gallery import router as gallery_router
    from routes.jobs import router as jobs_router
    from routes.opportunity_state import router as opportunity_state_router
    from routes.outputs import router as outputs_router
    from routes.overview import router as overview_router
    from routes.salesforce import router as salesforce_router
    from routes.skills import router as skills_router
    from routes.tech_eval import router as tech_eval_router
    from routes.transcription import router as transcription_router
    from services.account_service import AccountService
    from services.ask_service import AskService, anthropic_api_key
    from services.command_center_operations_service import CommandCenterOperationsService
    from services.command_center_read_service import CommandCenterReadService
    from services.salesforce_portfolio_service import SalesforcePortfolioService
    from services.calendar_snapshot_service import CalendarSnapshotService
    from integrations.calendar_readonly import (
        TRANSPORT_ENV as CALENDAR_TRANSPORT_ENV, GoogleCalendarReadOnlyTransport,
        UnavailableCalendarTransport, credentials_path as calendar_credentials_path,
    )
    from services.evidence_ledger_service import EvidenceLedgerService
    from services.feedback_service import FeedbackService
    from services.gmail_intake_service import GmailIntakeService
    from services.gmail_forget_service import GmailForgetService
    from services.granola_retrieval_service import GranolaRetrievalService
    from services.job_service import JobService
    from services.opportunity_workspace_service import OpportunityWorkspaceService
    from services.opportunity_state_create_service import OpportunityStateCreateService
    from services.opportunity_state_executor import ClaudeCanonicalStateExecutor
    from services.opportunity_state_service import OpportunityStateService
    from services.opportunity_state_update_service import OpportunityStateUpdateService
    from services.output_service import OutputService
    from services.overview_service import OverviewService
    from services.skill_runtime_service import SkillRuntimeService
    from services.tech_eval_service import TechEvalService
    from services.transcription_service import TranscriptionService

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
        has_output_since=output_service.has_output_since,
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
    opportunity_state_service = OpportunityStateService(
        customers_dir=config.CUSTOMERS_DIR,
        safe_name=config._safe,
    )
    tech_eval_service = TechEvalService(
        customers_dir=config.CUSTOMERS_DIR,
        safe_name=config._safe,
    )
    opportunity_workspace_service = OpportunityWorkspaceService(
        account_service=account_service,
        output_service=output_service,
        state_service=opportunity_state_service,
        job_service=job_service,
        transcription_service=transcription_service,
        tech_eval_service=tech_eval_service,
    )
    opportunity_state_executor = ClaudeCanonicalStateExecutor(
        model=config._model_for("opportunity-state"),
        forbidden_roots=[config.WEBAPP_DIR.parent, config.WORKSPACE, config.CUSTOMERS_DIR],
    )
    opportunity_state_create_service = OpportunityStateCreateService(
        workspace_service=opportunity_workspace_service,
        transcription_service=transcription_service,
        state_service=opportunity_state_service,
        job_service=job_service,
        executor=opportunity_state_executor,
    )
    opportunity_state_update_service = OpportunityStateUpdateService(
        workspace_service=opportunity_workspace_service,
        transcription_service=transcription_service,
        state_service=opportunity_state_service,
        job_service=job_service,
        executor=opportunity_state_executor,
    )
    opportunity_workspace_service.set_update_service(opportunity_state_update_service)
    # Command Center pilot: local, single-user, manually triggered intake only.
    evidence_ledger_service = EvidenceLedgerService(customers_dir=config.CUSTOMERS_DIR)
    granola_adapter = ManualGranolaImportAdapter()
    # User-triggered Granola retrieval: one restricted `claude -p` per MCP tool call, using
    # the credentials Claude Code already holds for the signed-in user. No polling, no keys.
    granola_relay = ClaudeCodeMcpRelay(
        model=config._model_for("quick-ask"),
        forbidden_roots=[config.WEBAPP_DIR.parent, config.WORKSPACE, config.CUSTOMERS_DIR],
        server_definition=scoped_server_definition([config.WEBAPP_DIR.parent]),
    )
    granola_retrieval_service = GranolaRetrievalService(
        transport=granola_relay,
        adapter=granola_adapter,
        ledger=evidence_ledger_service,
        job_service=job_service,
    )
    # Reconciliation reuses the same local overview runtime/provider as Opportunity Overview.
    command_center_operations_service = CommandCenterOperationsService(
        ledger=evidence_ledger_service,
        workspace_service=opportunity_workspace_service,
        state_service=opportunity_state_service,
        job_service=job_service,
        executor=opportunity_state_executor,
    )
    salesforce_portfolio_service = SalesforcePortfolioService(
        customers_dir=config.CUSTOMERS_DIR, accounts=account_service,
        salesforce=salesforce_integration,
    )
    calendar_mode = (os.environ.get(CALENDAR_TRANSPORT_ENV) or "unavailable").strip().lower()
    if calendar_mode == "live_readonly":
        calendar_transport = GoogleCalendarReadOnlyTransport(credentials_file=calendar_credentials_path())
    elif calendar_mode == "unavailable":
        calendar_transport = UnavailableCalendarTransport()
    else:
        raise RuntimeError(f"Unknown {CALENDAR_TRANSPORT_ENV}={calendar_mode!r}; use unavailable or live_readonly.")
    calendar_snapshot_service = CalendarSnapshotService(
        customers_dir=config.CUSTOMERS_DIR, transport=calendar_transport,
        configured=calendar_mode == "live_readonly",
    )
    opportunity_workspace_service.set_crm_identity_lookup(salesforce_portfolio_service.local_identity)
    # Aggregate Command Center reads (Today/Portfolio/Actions/Changes): persisted local records only.
    command_center_read_service = CommandCenterReadService(
        customers_dir=config.CUSTOMERS_DIR,
        ledger=evidence_ledger_service,
        operations=command_center_operations_service,
        state_service=opportunity_state_service,
        tech_eval_summary=tech_eval_service.peek_summary,
        salesforce_portfolio=salesforce_portfolio_service,
    )
    # PR E Gmail intake. The default transport fails closed and the UI says so.
    # `SE_GMAIL_TRANSPORT=live_readonly` is the only way to select the local HTTP
    # transport, and it still fails closed until the user has run
    # scripts/gmail_local_authorize.py (refresh token in their own 0600 file).
    # `SE_GMAIL_SYNTHETIC_FIXTURE=<path>` swaps in the in-memory synthetic mailbox for
    # local review; it never reads real mail. Neither is ever chosen implicitly.
    gmail_mode = (os.environ.get(GMAIL_TRANSPORT_ENV) or "").strip().lower()
    gmail_fixture = os.environ.get("SE_GMAIL_SYNTHETIC_FIXTURE")
    if gmail_mode == "live_readonly":
        gmail_transport = LiveGmailReadOnlyTransport(credentials_file=credentials_path())
    elif gmail_mode == "synthetic_fixture" or (not gmail_mode and gmail_fixture):
        if not gmail_fixture:
            raise RuntimeError("SE_GMAIL_TRANSPORT=synthetic_fixture needs SE_GMAIL_SYNTHETIC_FIXTURE=<path>.")
        gmail_transport = SyntheticGmailTransport.from_fixture(Path(gmail_fixture))
    elif gmail_mode in ("", "unavailable"):
        gmail_transport = UnavailableGmailTransport()
    else:
        raise RuntimeError(f"Unknown {GMAIL_TRANSPORT_ENV}={gmail_mode!r}; use unavailable, synthetic_fixture or live_readonly.")
    gmail_intake_service = GmailIntakeService(
        transport=gmail_transport,
        adapter=GmailSourceAdapter(),
        ledger=evidence_ledger_service,
        job_service=job_service,
        local_opportunities=command_center_read_service.local_opportunities,
    )
    gmail_forget_service = GmailForgetService(
        ledger=evidence_ledger_service, gmail=gmail_intake_service,
        operations=command_center_operations_service, state=opportunity_state_service,
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
    app.state.opportunity_workspace_service = opportunity_workspace_service
    app.state.opportunity_state_service = opportunity_state_service
    app.state.opportunity_state_create_service = opportunity_state_create_service
    app.state.opportunity_state_update_service = opportunity_state_update_service
    app.state.opportunity_state_executor = opportunity_state_executor
    app.state.tech_eval_service = tech_eval_service
    app.state.overview_service = overview_service
    app.state.ask_service = ask_service
    app.state.transcription_service = transcription_service
    app.state.skill_runtime_service = skill_runtime_service
    app.state.evidence_ledger_service = evidence_ledger_service
    app.state.granola_adapter = granola_adapter
    app.state.granola_retrieval_service = granola_retrieval_service
    app.state.gmail_intake_service = gmail_intake_service
    app.state.gmail_forget_service = gmail_forget_service
    app.state.command_center_operations_service = command_center_operations_service
    app.state.command_center_read_service = command_center_read_service
    app.state.salesforce_portfolio_service = salesforce_portfolio_service
    app.state.calendar_snapshot_service = calendar_snapshot_service

    # Public local routes — registered exactly once.
    app.include_router(skills_router)
    app.include_router(jobs_router)
    app.include_router(accounts_router)
    app.include_router(opportunity_state_router)
    app.include_router(tech_eval_router)
    app.include_router(outputs_router)
    app.include_router(feedback_router)
    app.include_router(overview_router)
    app.include_router(salesforce_router)
    app.include_router(ask_router)
    app.include_router(transcription_router)
    app.include_router(command_center_router)
    # Developer-only: renders committed synthetic fixtures through the real
    # reader path. Local mode only — never registered in hosted mode.
    app.include_router(gallery_router)


def _register_hosted_routers(app: FastAPI) -> None:
    """Register hosted-mode routes when the hosted module is available."""
    if _HOSTED_AVAILABLE:
        hosted.add_hosted_routers(app)


def _register_common_routes(app: FastAPI) -> None:
    """Register favicon and static assets."""

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(content=_FAVICON_SVG, media_type="image/svg+xml")

    # Serve the static frontend at root. Must be last because it is a catch-all.
    app.mount(
        "/",
        StaticFiles(directory=str(config.WEBAPP_DIR / "static"), html=True),
        name="static",
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup/shutdown lifecycle.

    In hosted mode the only runtime state is the `app_user` connection pool.
    In local mode we also stop any active transcription channels on shutdown.
    """
    logger.info("se-skills webapp starting up")
    if _HOSTED_AVAILABLE and hosted.config.is_hosted():
        user_pool = await hosted.db.create_pool()
        app.state.hosted_user_pool = user_pool
    yield
    logger.info("se-skills webapp shutting down")
    svc = getattr(app.state, "transcription_service", None)
    if svc:
        svc.shutdown()
    user_pool = getattr(app.state, "hosted_user_pool", None)
    if user_pool:
        await user_pool.close()
    storage_backend = getattr(app.state, "storage_backend", None)
    if storage_backend is not None:
        try:
            await storage_backend.close()
        except Exception as exc:
            logger.warning("Error closing storage backend: %s", exc)


def create_app() -> FastAPI:
    """Build and return the FastAPI application for the current mode."""
    app = FastAPI(title="SE Skills", lifespan=_lifespan)

    if _HOSTED_AVAILABLE and hosted.config.is_hosted():
        _register_hosted_routers(app)
    else:
        _build_local_services(app)

    _register_common_routes(app)
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787)
