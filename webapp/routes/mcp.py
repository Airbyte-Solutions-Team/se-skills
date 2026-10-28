"""MCP server health routes for the header status chip (local mode only)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from integrations.mcp_health import TRACKED_SERVERS, McpHealthService

router = APIRouter()


def _get_mcp(request: Request) -> McpHealthService:
    return request.app.state.mcp_health_service


def _tracked(name: str) -> str:
    if name not in TRACKED_SERVERS:
        raise HTTPException(status_code=404, detail="Unknown MCP server")
    return name


@router.get("/api/mcp/status")
async def api_mcp_status(request: Request, force: bool = False):
    """Per-server MCP health as skill runs (launched from the workspace folder) see it."""
    return await _get_mcp(request).status(force=force)


@router.post("/api/mcp/{name}/register")
async def api_mcp_register(name: str, request: Request):
    """Register a remote MCP server at user scope so every folder (incl. the workspace) sees it."""
    return await _get_mcp(request).register(_tracked(name))


@router.post("/api/mcp/{name}/signin")
async def api_mcp_signin(name: str, request: Request):
    """Open Claude Code in a terminal so the user can complete the server's OAuth sign-in."""
    return await _get_mcp(request).signin(_tracked(name))
