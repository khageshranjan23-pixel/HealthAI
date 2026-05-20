"""
Dr.SushamHealthAI — agents/skin_triage_agent.py
Disease-Adaptive Dynamic Clinical Questioning for Skin Conditions.

Unlike the generic triage_agent which asks fixed-pattern questions, this agent
generates questions CONDITIONED ON the visual analysis results. It uses the
ABCDE+ scores and disease predictions to select the most diagnostically
informative question that would help differentiate the top predicted conditions.

Key innovation: Questions are dynamically selected to maximize diagnostic
information gain — if the model sees something that could be eczema or
psoriasis, it asks questions that specifically differentiate those two.

Uses Groq LLM (free tier) for reasoning — no paid APIs.
"""

import json
import re
from typing import Any, Dict, List, Optional

from app.core.logging_config import logger
from app.tools.llm_client import get_llm

MAX_SKIN_TRIAGE_ROUNDS = 4  # One more than general because skin needs more detail


def _extract_json(text: str) -> Optional[Dict]:
    """Pull a JSON object out of LLM freeform text."""
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


def generate_skin_questions(
    disease_predictions: List[Dict],
    abcde_scores: Optional[Dict],
    collected_info: List[str],
    round_number: int,
    patient_region: Optional[str] = None,
    fitzpatrick: Optional[Dict] = None,
) -> Dict:
    """
    Generate the next clinically optimal question for a skin condition.

    The question is dynamically selected based on:
    - What the image model predicted (top diseases)
    - What the ABCDE+ analysis found
    - What information has already been collected
    - The triage round (earlier = broader, later = more specific)
    - Patient's geographic region (for regional disease priors)

    Returns:
        Dict with:
        - status: "needs_info" or "ready_to_diagnose"
        - question_text: The question to ask
        - options: Answer options (3-5)
        - reasoning: Why this question was chosen
    """
    llm = get_llm()
    if not llm:
        return {
            "status": "ready_to_diagnose",
            "question_text": "",
            "options": [],
            "reasoning": "LLM unavailable",
        }

    # Build context for the LLM
    # Disease predictions context
    if disease_predictions:
        pred_text = "\n".join([
            f"  - {p.get('disease', 'Unknown')}: {p.get('confidence', 0) * 100:.1f}% confidence"
            for p in disease_predictions[:5]
        ])
    else:
        pred_text = "  (No image predictions available)"

    # ABCDE scores context
    if abcde_scores:
        abcde_text = ""
        for dim in ["asymmetry", "border", "color", "diameter"]:
            data = abcde_scores.get(dim, {})
            if isinstance(data, dict) and "score" in data:
                abcde_text += f"  {dim.capitalize()}: {data['score']}/100 ({data.get('risk_level', '?')})\n"
        overall = abcde_scores.get("overall_risk", {})
        if overall:
            abcde_text += f"  Overall Risk: {overall.get('display', '?')}\n"
    else:
        abcde_text = "  (Not yet computed)"

    # Collected info context
    if collected_info:
        collected_text = "\n".join([f"  - {info}" for info in collected_info])
    else:
        collected_text = "  (Nothing collected yet — this is the first question)"

    # Regional context
    region_text = f"Patient's region: {patient_region}" if patient_region else "Region: Not specified"

    # Fitzpatrick context
    fitz_text = ""
    if fitzpatrick:
        fitz_text = f"Detected skin tone: Fitzpatrick Type {fitzpatrick.get('type', '?')} ({fitzpatrick.get('label', '?')})"

    prompt = f"""You are Dr.SushamHealthAI, a senior dermatologist conducting a clinical intake.

VISUAL ANALYSIS OF PATIENT'S SKIN IMAGE:
AI Disease Predictions:
{pred_text}

ABCDE+ Lesion Scoring:
{abcde_text}

{fitz_text}
{region_text}

INFORMATION ALREADY COLLECTED FROM PATIENT:
{collected_text}

CURRENT ROUND: {round_number + 1} of {MAX_SKIN_TRIAGE_ROUNDS}

YOUR TASK:
Based on the AI predictions and what you already know, generate the SINGLE most
diagnostically informative question. The question should help DIFFERENTIATE between
the top predicted conditions.

QUESTION TAXONOMY (choose the most useful category for this round):
- MORPHOLOGY: How does the lesion feel? (flat/raised/rough/smooth/scaly/crusty)
- TEMPORAL: When did you first notice this? Is it changing?
- SENSATION: Any itching, pain, burning, numbness, or tenderness?
- DISTRIBUTION: Has it spread? Are there similar spots elsewhere?
- TRIGGERS: Any known triggers? (sun, stress, food, new products, medications)
- HISTORY: Any personal/family history of skin conditions or cancer?
- SYSTEMIC: Any other symptoms? (fever, fatigue, joint pain, weight loss)

RULES:
1. Ask EXACTLY ONE question — not multiple.
2. Provide 3-5 answer options, each 2-6 words.
3. Do NOT repeat questions about things already answered.
4. Make the question SPECIFIC to the predicted conditions — not generic.
5. Include a brief reasoning for why you chose this question.
6. If you have ENOUGH information after round 3+, set status to "ready_to_diagnose".

RESPOND WITH THIS EXACT JSON:
{{
  "status": "needs_info",
  "question_text": "Your specific question here",
  "options": ["Option A", "Option B", "Option C", "Option D"],
  "reasoning": "Brief reason why this question helps differentiate the conditions",
  "question_category": "MORPHOLOGY|TEMPORAL|SENSATION|DISTRIBUTION|TRIGGERS|HISTORY|SYSTEMIC"
}}

OR if sufficient info gathered:
{{
  "status": "ready_to_diagnose",
  "question_text": "",
  "options": [],
  "reasoning": "Sufficient information collected for differential diagnosis"
}}"""

    try:
        response = llm.invoke(prompt)
        answer = (
            response.content.strip()
            if hasattr(response, "content")
            else str(response).strip()
        )

        parsed = _extract_json(answer)

        if not parsed:
            logger.warning("Skin triage: couldn't parse JSON — defaulting to ready")
            return {
                "status": "ready_to_diagnose",
                "question_text": "",
                "options": [],
                "reasoning": "Parse failure",
            }

        status = parsed.get("status", "ready_to_diagnose")
        question_text = parsed.get("question_text", "").strip()
        options = parsed.get("options", [])
        reasoning = parsed.get("reasoning", "")
        category = parsed.get("question_category", "GENERAL")

        # Force diagnosis after max rounds
        if round_number >= MAX_SKIN_TRIAGE_ROUNDS - 1 and status != "ready_to_diagnose":
            status = "ready_to_diagnose"
            logger.info("Skin triage: max rounds reached — forcing diagnosis")

        logger.info(
            "Skin triage round %d: status=%s, category=%s, question='%s'",
            round_number + 1, status, category, question_text[:60]
        )

        return {
            "status": status,
            "question_text": question_text,
            "options": options[:5],
            "reasoning": reasoning,
            "question_category": category,
            "triage_round": round_number + 1,
            "max_rounds": MAX_SKIN_TRIAGE_ROUNDS,
        }

    except Exception as e:
        logger.error("Skin triage error: %s", str(e))
        return {
            "status": "ready_to_diagnose",
            "question_text": "",
            "options": [],
            "reasoning": f"Error: {str(e)}",
        }


def generate_skin_diagnosis(
    disease_predictions: List[Dict],
    abcde_scores: Optional[Dict],
    collected_info: List[str],
    fitzpatrick: Optional[Dict] = None,
    region: Optional[str] = None,
    discordance: Optional[Dict] = None,
    debate_result: Optional[Dict] = None,
) -> str:
    """
    Generate a comprehensive post-triage skin diagnosis using all available
    clinical data. This is the final response after all questions have been asked.

    Returns a markdown-formatted diagnosis string.
    """
    llm = get_llm()
    if not llm:
        return "Medical AI service temporarily unavailable. Please consult a dermatologist."

    # Build comprehensive context
    pred_text = "\n".join([
        f"  - {p.get('disease', 'Unknown')}: {p.get('confidence', 0) * 100:.1f}%"
        for p in (disease_predictions or [])[:5]
    ])

    collected_text = "\n".join([f"  - {info}" for info in (collected_info or [])])

    abcde_text = ""
    if abcde_scores:
        for dim in ["asymmetry", "border", "color", "diameter", "evolution"]:
            data = abcde_scores.get(dim, {})
            if isinstance(data, dict) and data.get("score") is not None:
                abcde_text += f"  {dim.capitalize()}: {data['score']}/100 ({data.get('risk_level', '?')}, confidence: {data.get('confidence', '?')}%)\n"
        overall = abcde_scores.get("overall_risk", {})
        if overall:
            abcde_text += f"  Overall Malignancy Risk: {overall.get('display', '?')}\n"

    fitz_text = ""
    if fitzpatrick:
        fitz_text = f"Skin tone: Fitzpatrick Type {fitzpatrick.get('type', '?')} ({fitzpatrick.get('label', '?')})"
        if fitzpatrick.get("correction_factors", {}).get("dark_skin_correction_applied"):
            fitz_text += " — Dark skin correction was applied to colour analysis"

    discordance_text = ""
    if discordance and discordance.get("alert"):
        discordance_text = (
            f"\n⚠️ DISCORDANCE ALERT: Visual analysis and symptom analysis disagree.\n"
            f"  Visual suggests: {discordance.get('visual_top', '?')}\n"
            f"  Symptoms suggest: {discordance.get('symptom_top', '?')}\n"
            f"  Concordance: {discordance.get('concordance_score', 0) * 100:.0f}%\n"
        )

    debate_text = ""
    if debate_result:
        debate_text = (
            f"\nDual Agent Analysis:\n"
            f"  Dermatologist view: {debate_result.get('dermatologist_summary', 'N/A')}\n"
            f"  Oncologist view: {debate_result.get('oncologist_summary', 'N/A')}\n"
            f"  Consensus: {debate_result.get('consensus', 'N/A')}\n"
        )

    prompt = f"""You are Dr.SushamHealthAI, a senior dermatologist providing a comprehensive assessment.

COMPLETE CLINICAL DATA:

AI Image Analysis Results:
{pred_text}

ABCDE+ Lesion Scoring:
{abcde_text}

{fitz_text}
Region: {region or 'Not specified'}

Patient-Reported Information (from clinical interview):
{collected_text}
{discordance_text}
{debate_text}

Provide a COMPREHENSIVE, DETAILED dermatological assessment in this format:

## 🔬 Clinical Assessment
Summarize findings from both visual analysis and patient history.

## 🩺 Differential Diagnosis
List the 2-4 most likely conditions with:
- Probability estimate
- Why this diagnosis fits (visual + symptom evidence)
- What argues against it

## 📊 ABCDE+ Analysis Summary
Interpret the ABCDE scores in clinical context. Explain what each score means.

## 💊 Recommended Treatment
For the most likely diagnosis:
- First-line treatment
- OTC options
- When prescription is needed

## 🏠 Home Care & Self-Management
Practical, actionable advice.

## ⚠️ Red Flags — See a Dermatologist If...
Specific warning signs that need professional evaluation.

## 🛡️ Prevention & Monitoring
How to prevent worsening and what to monitor.

Be thorough, empathetic, evidence-based. This is the patient's complete consultation."""

    try:
        response = llm.invoke(prompt)
        return (
            response.content.strip()
            if hasattr(response, "content")
            else str(response).strip()
        )
    except Exception as e:
        logger.error("Skin diagnosis generation error: %s", str(e))
        return (
            "Based on the analysis, I recommend consulting with a dermatologist "
            "for a proper examination. Please visit your nearest skin clinic."
        )
