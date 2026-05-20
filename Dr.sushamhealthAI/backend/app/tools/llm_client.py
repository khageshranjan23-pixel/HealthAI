"""
Dr.SushamHealthAI — tools/llm_client.py
Groq LLM client with automatic model fallback for rate limits.

Model hierarchy (Groq free tier):
  1. llama-3.3-70b-versatile  — Best quality, 100K TPD limit
  2. llama-3.1-8b-instant     — Fast fallback, ~500K TPD limit
  3. gemma2-9b-it             — Backup fallback, separate limit pool

When the primary model hits rate limits, automatically falls back to the
smaller model. This ensures the app never shows "System Message" errors
to the patient.
"""

from app.core.config import GROQ_API_KEY
from app.core.logging_config import logger

_llm_primary = None
_llm_fallback = None


def get_llm(fast=False):
    """
    Return a ChatGroq LLM instance.

    Args:
        fast: If True, use the smaller/faster model (for Pass 1 differential
              updates where speed matters more than quality). If False, use
              the primary 70b model.

    Returns:
        ChatGroq instance, or None if API key is missing.
    """
    global _llm_primary, _llm_fallback

    if not GROQ_API_KEY:
        logger.warning("GROQ_API_KEY not found in environment variables")
        return None

    from langchain_groq import ChatGroq

    if _llm_primary is None:
        _llm_primary = ChatGroq(
            api_key=GROQ_API_KEY,
            model_name="llama-3.3-70b-versatile",
            temperature=0.3,
            max_tokens=2048,
        )
        logger.info("LLM client initialized (Groq / llama-3.3-70b-versatile)")

    if _llm_fallback is None:
        _llm_fallback = ChatGroq(
            api_key=GROQ_API_KEY,
            model_name="llama-3.1-8b-instant",
            temperature=0.3,
            max_tokens=2048,
        )
        logger.info("LLM fallback initialized (Groq / llama-3.1-8b-instant)")

    if fast:
        return _llm_fallback

    return _llm_primary


def get_llm_with_fallback():
    """
    Return (primary_llm, fallback_llm) tuple.
    The caller can try primary first, and if it gets a rate limit error,
    retry with the fallback model.
    """
    get_llm()  # ensure both are initialized
    return _llm_primary, _llm_fallback


def invoke_with_fallback(prompt: str, fast=False) -> str:
    """
    Invoke LLM with automatic fallback on rate limit errors.
    Returns the text response, or empty string on total failure.

    This handles the Groq 429 rate_limit_exceeded error transparently.
    """
    primary = get_llm(fast=fast)
    if not primary:
        return ""

    try:
        response = primary.invoke(prompt)
        return (
            response.content.strip()
            if hasattr(response, "content")
            else str(response).strip()
        )
    except Exception as e:
        error_str = str(e)
        if "rate_limit" in error_str.lower() or "429" in error_str:
            logger.warning("Primary LLM rate-limited — falling back to 8b model")
            fallback = get_llm(fast=True)
            if fallback and fallback != primary:
                try:
                    response = fallback.invoke(prompt)
                    return (
                        response.content.strip()
                        if hasattr(response, "content")
                        else str(response).strip()
                    )
                except Exception as e2:
                    logger.error("Fallback LLM also failed: %s", str(e2))
                    return ""
        logger.error("LLM invoke failed: %s", error_str)
        return ""
