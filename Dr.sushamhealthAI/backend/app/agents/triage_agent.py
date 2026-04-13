"""
Dr.SushamHealthAI — agents/triage_agent.py
TriageAgent: Clinical interview agent that asks follow-up questions like a real
doctor — ONE question at a time — before arriving at a diagnosis.

KEY: Asks exactly ONE question per round, for up to 3 rounds.
Round 1 & 2: ALWAYS ask a question (unless patient gave a very detailed message).
Round 3: Ask if still missing critical info, otherwise diagnose.
"""

import json
import re
from typing import Any, Dict, List, Optional

from app.core.logging_config import logger
from app.core.state import AgentState
from app.tools.llm_client import get_llm

MAX_TRIAGE_ROUNDS = 3


def _extract_json(text: str) -> Optional[Dict]:
    """Try to pull a JSON object out of LLM freeform text."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def TriageAgent(state: AgentState) -> AgentState:
    """
    Clinical triage: ask ONE follow-up question per round, then proceed to diagnosis.
    Non-medical queries pass through transparently.
    """

    if not state.get("is_medical_query", False):
        state["triage_status"] = "ready_to_diagnose"
        logger.info("Triage: non-medical query — skipping")
        return state

    llm = get_llm()
    if not llm:
        state["triage_status"] = "ready_to_diagnose"
        return state

    question = state["question"]
    triage_round = state.get("triage_round", 0)
    collected = state.get("collected_symptoms", [])

    # Append current message to collected symptoms
    if question.strip():
        collected.append(question.strip())
        state["collected_symptoms"] = collected

    # Force diagnosis after MAX rounds
    if triage_round >= MAX_TRIAGE_ROUNDS:
        logger.info("Triage: max rounds reached — forcing diagnosis")
        state["triage_status"] = "ready_to_diagnose"
        state["question"] = _build_enriched_question(collected)
        return state

    # Build conversation recap
    history_lines = []
    for item in state.get("conversation_history", [])[-8:]:
        role = "Patient" if item.get("role") == "user" else "Doctor"
        history_lines.append(f"{role}: {item.get('content', '')}")
    history_text = "\n".join(history_lines) if history_lines else "(first message)"
    collected_text = "\n".join(f"- {s}" for s in collected) if collected else "(none yet)"

    # Determine how aggressive to be about asking questions
    if triage_round == 0:
        round_instruction = (
            "This is the FIRST interaction. The patient just described their initial complaint. "
            "You almost CERTAINLY need more information. Ask about DURATION — how long they've "
            "had these symptoms. You should set status to 'needs_info' unless the patient "
            "already provided an extremely detailed description with duration, severity, "
            "and associated symptoms all in one message."
        )
    elif triage_round == 1:
        round_instruction = (
            "This is round 2. You know their complaint and one detail. "
            "You likely still need more information. Ask about SEVERITY or ASSOCIATED SYMPTOMS "
            "(e.g., other symptoms they're experiencing alongside the main one). "
            "Set status to 'needs_info' unless you truly have enough for a differential diagnosis."
        )
    else:
        round_instruction = (
            "This is the FINAL round. Ask ONE more question about anything still missing "
            "(triggers, medical history, medications). If you already have enough info, "
            "set status to 'ready_to_diagnose'."
        )

    triage_prompt = f"""You are Dr.SushamHealthAI, a senior clinical physician conducting a patient intake.

PATIENT'S CURRENT MESSAGE: "{question}"

CONVERSATION SO FAR:
{history_text}

ALL INFORMATION COLLECTED:
{collected_text}

ROUND: {triage_round + 1} of {MAX_TRIAGE_ROUNDS}

ROUND-SPECIFIC INSTRUCTION:
{round_instruction}

RULES:
1. Ask EXACTLY ONE question — never multiple questions.
2. Provide 3-5 short answer options (2-5 words each).
3. Be warm, empathetic, and conversational.
4. Do NOT repeat questions about things already answered.

RESPOND WITH THIS EXACT JSON (no other text):
{{
  "status": "needs_info",
  "question_text": "Your single follow-up question",
  "options": ["Option A", "Option B", "Option C", "Option D"]
}}

OR if you have enough information:
{{
  "status": "ready_to_diagnose",
  "question_text": "",
  "options": []
}}"""

    try:
        response = llm.invoke(triage_prompt)
        answer = (
            response.content.strip()
            if hasattr(response, "content")
            else str(response).strip()
        )

        parsed = _extract_json(answer)

        if not parsed:
            logger.warning("Triage: couldn't parse JSON — defaulting to ready")
            state["triage_status"] = "ready_to_diagnose"
            state["question"] = _build_enriched_question(collected)
            return state

        status = parsed.get("status", "ready_to_diagnose")
        question_text = parsed.get("question_text", "").strip()
        options = parsed.get("options", [])

        if status == "needs_info" and question_text:
            state["triage_status"] = "needs_info"
            state["triage_round"] = triage_round + 1

            state["generation"] = f"I'd like to understand your condition better.\n\n**{question_text}**"
            state["source"] = "Clinical Triage"

            state["follow_up_data"] = {
                "questions": [
                    {
                        "text": question_text,
                        "options": options[:5],
                    }
                ],
                "triage_round": triage_round + 1,
                "max_rounds": MAX_TRIAGE_ROUNDS,
            }

            logger.info("Triage: round %d — asking: '%s'", triage_round + 1, question_text[:60])

        else:
            state["triage_status"] = "ready_to_diagnose"
            state["question"] = _build_enriched_question(collected)
            logger.info("Triage: sufficient info — proceeding to diagnosis")

    except Exception as e:
        logger.error("Triage: LLM call failed: %s", str(e))
        state["triage_status"] = "ready_to_diagnose"
        state["question"] = _build_enriched_question(collected)

    return state


def _build_enriched_question(collected: List[str]) -> str:
    """Combine all collected patient statements into a single enriched query."""
    if not collected:
        return ""
    if len(collected) == 1:
        return collected[0]

    parts = [
        "The patient reported the following during clinical intake:",
    ]
    for i, symptom in enumerate(collected, 1):
        parts.append(f"  {i}. {symptom}")
    parts.append(
        "\nBased on ALL the information above, provide a comprehensive medical assessment "
        "including: possible diagnoses (differential diagnosis), recommended treatments, "
        "home remedies, when to seek urgent care, and preventive measures."
    )
    return "\n".join(parts)
