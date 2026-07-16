"""Feedback HTTP routes for the SE Skills webapp."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from services.feedback_service import FeedbackError, FeedbackService, OutputFeedback

router = APIRouter()


def _get_feedback_service(request: Request) -> FeedbackService:
    return request.app.state.feedback_service


@router.get("/api/output/feedback")
def api_output_feedback_get(path: str, request: Request) -> dict:
    try:
        return _get_feedback_service(request).read_feedback(path)
    except FeedbackError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("/api/output/feedback")
def api_output_feedback_post(body: OutputFeedback, request: Request) -> dict:
    try:
        return _get_feedback_service(request).add_feedback(body)
    except FeedbackError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
