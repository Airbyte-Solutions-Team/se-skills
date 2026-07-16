"""Live-transcription HTTP routes for the SE Skills webapp."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.ask_service import AskError, AskService
from services.transcription_service import TranscriptionError, TranscriptionService

router = APIRouter()


class StartLive(BaseModel):
    account: str = Field(max_length=120)
    opp_slug: str | None = Field(default=None, max_length=120)
    opportunity: str | None = Field(default=None, max_length=200)
    mic_device: int
    call_device: int | None = None
    mic_label: str | None = Field(default="You", max_length=80)
    call_label: str | None = Field(default="Call", max_length=80)


class AskLive(BaseModel):
    question: str = Field(max_length=5_000)
    # File-backed ask: when the session_id is the "file" sentinel, the page is
    # querying a SAVED transcript (reopened), not a live recording. The client
    # passes the transcript name + account so the server loads it from disk.
    transcript_name: str | None = Field(default=None, max_length=500)
    account: str | None = Field(default=None, max_length=120)
    opportunity: str | None = Field(default=None, max_length=200)


def _get_transcription_service(request: Request) -> TranscriptionService:
    return request.app.state.transcription_service


def _get_ask_service(request: Request) -> AskService:
    return request.app.state.ask_service


@router.get("/api/audio-devices")
def api_audio_devices(request: Request):
    """Input devices for the mic/call pickers. Flags BlackHole presence."""
    try:
        return _get_transcription_service(request).audio_devices()
    except TranscriptionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e


@router.post("/api/transcribe/start")
async def api_transcribe_start(body: StartLive, request: Request):
    """Start a new live-transcription session and return its session_id."""
    try:
        return await _get_transcription_service(request).start_session(
            account=body.account,
            opp_slug=body.opp_slug,
            mic_device=body.mic_device,
            call_device=body.call_device,
            mic_label=body.mic_label,
            call_label=body.call_label,
            opportunity=body.opportunity,
        )
    except TranscriptionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e


@router.get("/api/transcribe/active")
def api_transcribe_active(request: Request, account: str, opp_slug: str | None = None):
    """If a live session is active or recovered for this opportunity, return it
    so the page can reconnect or save it. 204 if none."""
    svc = _get_transcription_service(request)
    resp = svc.active_session(account=account, opp_slug=opp_slug)
    if resp is None:
        # 204 No Content must have an EMPTY body — a serialized `null` (4 bytes)
        # trips Starlette's "content longer than Content-Length" check.
        return Response(status_code=204)
    return resp


@router.get("/api/transcripts")
def api_list_transcripts(request: Request, account: str):
    """List saved transcripts for this account, newest first."""
    return _get_transcription_service(request).list_transcripts(account=account)


@router.get("/api/transcripts/{name}")
def api_load_transcript(name: str, account: str, request: Request):
    """Load one saved transcript as segments + raw text so the page can render
    it read-only and the copilot can answer questions about it."""
    try:
        return _get_transcription_service(request).load_transcript(
            account=account,
            name=name,
        )
    except TranscriptionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e


@router.get("/api/transcribe/{session_id}/stream")
async def api_transcribe_stream(session_id: str, request: Request):
    """Server-sent event stream of transcript segments for a live session."""
    from sse_starlette.sse import EventSourceResponse
    svc = _get_transcription_service(request)
    try:
        return EventSourceResponse(svc.stream_session(session_id))
    except TranscriptionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e


@router.post("/api/transcribe/{session_id}/stop")
def api_transcribe_stop(session_id: str, request: Request):
    """Stop a live session, write the transcript to disk, and clean up state."""
    try:
        return _get_transcription_service(request).stop_session(session_id)
    except TranscriptionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e


def _sse_bytes(events):
    """Convert AskService's dict-style SSE events into raw SSE frames."""
    async def gen():
        async for ev in events:
            yield f"event: {ev['event']}\ndata: {ev['data']}\n\n".encode()
    return gen()


@router.post("/api/transcribe/{session_id}/ask")
async def api_transcribe_ask(session_id: str, body: AskLive, request: Request):
    """Ask a follow-up question against a live or saved transcript."""
    svc = _get_transcription_service(request)
    ask = _get_ask_service(request)
    try:
        transcript, account, opportunity, live = svc.ask_context(
            session_id=session_id,
            account=body.account,
            transcript_name=body.transcript_name,
            opportunity=body.opportunity,
        )
    except TranscriptionError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e

    try:
        result = await ask.transcript_ask(
            transcript=transcript,
            question=body.question,
            account=account,
            opportunity=opportunity,
            live=live,
            session_id=session_id,
        )
    except AskError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e

    if result.kind == "quick":
        from fastapi.responses import StreamingResponse
        return StreamingResponse(
            _sse_bytes(result.stream),
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
        )

    if result.kind == "deep":
        payload = {"mode": "deep", "job_id": result.job_id}
        if result.persistence_warning:
            payload["persistence_warning"] = result.persistence_warning
        return JSONResponse(payload)

    # needs_deep
    return JSONResponse({"mode": "needs_deep", "reason": result.reason})
