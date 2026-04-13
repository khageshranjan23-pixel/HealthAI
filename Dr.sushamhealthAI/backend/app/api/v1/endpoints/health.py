"""
Dr.SushamHealthAI — api/v1/endpoints/health.py
Health check endpoint.
"""

from fastapi import APIRouter

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check():
    """Returns service health status."""
    return {"status": "healthy", "service": "Dr.SushamHealthAI Backend v3"}
