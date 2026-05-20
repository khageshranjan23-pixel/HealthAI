"""
Dr.SushamHealthAI — agents/dual_agent_debate.py
Dual Agent Debate System: Dermatologist vs Oncologist Persona.

Runs two sequential LLM calls with different clinical perspectives:
  1. Dermatologist Agent — focuses on benign explanations, common conditions
  2. Oncologist Agent — focuses on malignancy risk, red flags, worst-case

Their conclusions are compared to identify areas of agreement and divergence.
The disagreement itself is clinically meaningful — it highlights cases that
need careful professional review.

Uses 2 Groq LLM calls (free tier supports this).
"""

import json
import re
from typing import Dict, List, Optional

from app.core.logging_config import logger
from app.tools.llm_client import get_llm


def _extract_json(text: str) -> Optional[Dict]:
    """Pull JSON from LLM text."""
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


def run_dual_debate(
    disease_predictions: List[Dict],
    abcde_scores: Dict,
    patient_symptoms: List[str],
    fitzpatrick: Optional[Dict] = None,
) -> Dict:
    """
    Run the dual agent debate between Dermatologist and Oncologist perspectives.

    Args:
        disease_predictions: Top disease predictions from image model
        abcde_scores: ABCDE+ scoring results
        patient_symptoms: Collected patient information
        fitzpatrick: Skin tone data

    Returns:
        Dict with both agents' assessments, areas of agreement/disagreement,
        and a unified consensus.
    """
    llm = get_llm()
    if not llm:
        return {"error": "LLM unavailable", "debate_completed": False}

    # Build shared context
    pred_text = "\n".join([
        f"  - {p.get('disease', '?')}: {p.get('confidence', 0) * 100:.1f}%"
        for p in (disease_predictions or [])[:5]
    ])

    abcde_text = ""
    for dim in ["asymmetry", "border", "color", "diameter"]:
        data = abcde_scores.get(dim, {})
        if isinstance(data, dict) and "score" in data:
            abcde_text += f"  {dim.capitalize()}: {data['score']}/100 ({data.get('risk_level', '?')})\n"
    overall = abcde_scores.get("overall_risk", {})
    if overall:
        abcde_text += f"  Overall Risk: {overall.get('display', '?')}\n"

    symptoms_text = "\n".join([f"  - {s}" for s in (patient_symptoms or [])])
    fitz_text = f"Fitzpatrick Type: {fitzpatrick.get('type', '?')}" if fitzpatrick else ""

    shared_context = f"""CASE DATA:
AI Image Predictions:
{pred_text}

ABCDE+ Scores:
{abcde_text}

{fitz_text}

Patient-Reported Symptoms:
{symptoms_text}
"""

    # ── Agent 1: Dermatologist ──────────────────────────────────
    derm_prompt = f"""{shared_context}

You are a CONSERVATIVE DERMATOLOGIST. Your default assumption is BENIGN.
Analyze this case from the perspective of common, benign skin conditions.

Focus on:
- Most likely BENIGN explanation for the findings
- Common conditions that mimic serious ones
- Why the high ABCDE scores might be falsely elevated
- Reasons NOT to worry

RESPOND WITH THIS JSON:
{{
  "primary_diagnosis": "Most likely benign condition",
  "confidence": 0.0,
  "reasoning": "Why this is most likely benign",
  "differential": ["Alternative benign conditions"],
  "reassuring_factors": ["Reasons not to worry"],
  "recommended_action": "What the patient should do"
}}"""

    # ── Agent 2: Oncologist ────────────────────────────────────
    onc_prompt = f"""{shared_context}

You are a CAUTIOUS ONCOLOGIST. You look for the WORST-CASE scenario.
Analyze this case from the perspective of malignancy and serious conditions.

Focus on:
- Any signs that could indicate malignancy
- Red flags in the ABCDE scores
- Symptom patterns associated with serious conditions
- Why this case MIGHT need urgent attention

RESPOND WITH THIS JSON:
{{
  "primary_concern": "Most concerning possible diagnosis",
  "risk_level": "LOW|MODERATE|HIGH|URGENT",
  "reasoning": "Why there might be cause for concern",
  "red_flags": ["Specific concerning findings"],
  "recommended_action": "What urgent steps should be taken",
  "urgency": "ROUTINE|SOON|URGENT|EMERGENCY"
}}"""

    try:
        # Run both agents
        derm_response = llm.invoke(derm_prompt)
        derm_text = (
            derm_response.content.strip()
            if hasattr(derm_response, "content")
            else str(derm_response).strip()
        )
        derm_parsed = _extract_json(derm_text) or {}

        onc_response = llm.invoke(onc_prompt)
        onc_text = (
            onc_response.content.strip()
            if hasattr(onc_response, "content")
            else str(onc_response).strip()
        )
        onc_parsed = _extract_json(onc_text) or {}

        # Compute agreement/disagreement
        agreement = _analyze_agreement(derm_parsed, onc_parsed)

        # Generate consensus
        consensus = _generate_consensus(derm_parsed, onc_parsed, agreement)

        result = {
            "debate_completed": True,
            "dermatologist": {
                "summary": derm_parsed.get("primary_diagnosis", "Analysis unavailable"),
                "confidence": derm_parsed.get("confidence", 0),
                "reasoning": derm_parsed.get("reasoning", ""),
                "differential": derm_parsed.get("differential", []),
                "reassuring_factors": derm_parsed.get("reassuring_factors", []),
                "recommended_action": derm_parsed.get("recommended_action", ""),
            },
            "oncologist": {
                "summary": onc_parsed.get("primary_concern", "Analysis unavailable"),
                "risk_level": onc_parsed.get("risk_level", "UNKNOWN"),
                "reasoning": onc_parsed.get("reasoning", ""),
                "red_flags": onc_parsed.get("red_flags", []),
                "recommended_action": onc_parsed.get("recommended_action", ""),
                "urgency": onc_parsed.get("urgency", "ROUTINE"),
            },
            "agreement": agreement,
            "consensus": consensus,
        }

        logger.info(
            "Dual debate completed: derm=%s, onc=%s (%s), agreement=%s",
            derm_parsed.get("primary_diagnosis", "?")[:30],
            onc_parsed.get("primary_concern", "?")[:30],
            onc_parsed.get("risk_level", "?"),
            agreement.get("level", "?"),
        )

        return result

    except Exception as e:
        logger.error("Dual debate error: %s", str(e))
        return {
            "debate_completed": False,
            "error": str(e),
        }


def _analyze_agreement(derm: Dict, onc: Dict) -> Dict:
    """Analyze the level of agreement between the two agents."""
    derm_diag = derm.get("primary_diagnosis", "").lower()
    onc_concern = onc.get("primary_concern", "").lower()
    onc_risk = onc.get("risk_level", "LOW").upper()
    onc_urgency = onc.get("urgency", "ROUTINE").upper()

    # Check if both agents mention similar conditions
    derm_words = set(derm_diag.split())
    onc_words = set(onc_concern.split())
    common_words = derm_words & onc_words
    stop_words = {"the", "a", "an", "of", "and", "or", "in", "is", "are", "skin"}
    meaningful_common = common_words - stop_words

    if meaningful_common:
        level = "AGREE"
        summary = "Both agents identify similar conditions"
    elif onc_risk in ("LOW", "MODERATE") and onc_urgency == "ROUTINE":
        level = "MOSTLY_AGREE"
        summary = "Different diagnoses but both consider low risk"
    elif onc_risk in ("HIGH", "URGENT") or onc_urgency in ("URGENT", "EMERGENCY"):
        level = "DISAGREE"
        summary = "Dermatologist sees benign, Oncologist flags serious concerns"
    else:
        level = "PARTIAL"
        summary = "Some differences in assessment — professional review recommended"

    return {
        "level": level,
        "summary": summary,
        "dermatologist_sees_benign": True,
        "oncologist_risk_level": onc_risk,
        "oncologist_urgency": onc_urgency,
    }


def _generate_consensus(derm: Dict, onc: Dict, agreement: Dict) -> Dict:
    """Generate a unified consensus from both agents' assessments."""
    level = agreement.get("level", "PARTIAL")
    onc_urgency = onc.get("urgency", "ROUTINE")

    if level == "AGREE":
        recommendation = (
            f"Both clinical perspectives agree. Most likely: "
            f"{derm.get('primary_diagnosis', 'assessment pending')}. "
            f"{derm.get('recommended_action', '')}"
        )
        urgency = "ROUTINE"
    elif level == "DISAGREE":
        recommendation = (
            f"Clinical perspectives DISAGREE. The dermatologist suggests "
            f"'{derm.get('primary_diagnosis', '?')}' (benign), but the oncologist "
            f"flags '{onc.get('primary_concern', '?')}' as a concern. "
            f"Given this disagreement, professional dermatological evaluation "
            f"is STRONGLY RECOMMENDED."
        )
        urgency = onc_urgency
    else:
        recommendation = (
            f"Most likely benign ({derm.get('primary_diagnosis', '?')}), "
            f"but some features warrant monitoring. "
            f"{onc.get('recommended_action', 'Follow up if changes occur.')}"
        )
        urgency = "SOON" if onc_urgency != "ROUTINE" else "ROUTINE"

    return {
        "recommendation": recommendation,
        "urgency": urgency,
        "needs_professional_review": level in ("DISAGREE", "PARTIAL"),
    }
