"""Overview HTTP route for the SE Skills webapp."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request

from services.overview_service import OverviewService

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/api/overview")
async def api_overview(request: Request) -> dict:
    """Operational overview for the team landing page.

    Runs the aggregation in a thread pool because it walks the filesystem and
    may briefly block the event loop. A failure anywhere in the aggregation
    returns a safe empty fallback rather than a 500 so the landing page still
    renders.
    """
    overview_service: OverviewService = request.app.state.overview_service
    try:
        return await asyncio.to_thread(overview_service.build_overview)
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("Overview aggregation failed: %s", type(exc).__name__)
        return {
            "summary": {
                "members": 0,
                "active_accounts": 0,
                "archived_accounts": 0,
                "opportunities": 0,
                "outputs": 0,
                "running_jobs": 0,
                "recent_failures": 0,
                "needs_attention": 0,
                "last_activity": 0.0,
            },
            "attention": [],
            "recent": [],
            "members": [],
            "empty": {
                "members": True,
                "accounts": True,
                "attention": True,
                "recent": True,
            },
        }
