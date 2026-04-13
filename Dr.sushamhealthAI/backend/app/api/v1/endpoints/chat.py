"""
Dr.SushamHealthAI — api/v1/endpoints/chat.py
Clinical Intelligence endpoints: /chat, /clear, /new-chat.
"""

import uuid

from fastapi import APIRouter, HTTPException, Request

from app.schemas.chat import ChatRequest, ChatResponse
from app.services.chat_service import clinical_engine

router = APIRouter(tags=["Chat"])


def _get_session_id(request: Request) -> str:
    """Get or create a session ID from X-Session-ID header or cookie session."""
    session_id = request.headers.get("X-Session-ID")
    if session_id:
        return session_id
    if "session_id" not in request.session:
        request.session["session_id"] = str(uuid.uuid4())
    return request.session["session_id"]


@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest, req: Request):
    """Process a user message through the clinical intelligence pipeline."""
    if not clinical_engine.workflow_app:
        raise HTTPException(status_code=503, detail="System not initialized")
    session_id = _get_session_id(req)
    return await clinical_engine.process_message(session_id, request.message)


@router.post("/clear")
async def clear_endpoint(req: Request):
    """Clear the in-memory clinical state for the current session."""
    clinical_engine.clear_conversation(_get_session_id(req))
    return {"message": "Clinical state cleared", "success": True}


@router.post("/new-chat")
async def new_chat_endpoint(req: Request):
    """Create a new clinical session with a fresh session ID."""
    new_id = str(uuid.uuid4())
    req.session["session_id"] = new_id
    return {"message": "New clinical session created", "session_id": new_id, "success": True}
