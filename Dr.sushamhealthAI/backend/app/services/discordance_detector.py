"""
Dr.SushamHealthAI — services/discordance_detector.py
Symptom-Visual Discordance Detection.

Novel contribution: Uses the DISAGREEMENT between visual analysis and symptom
analysis as a clinical signal. When image-based predictions and symptom-based
reasoning point to different conditions, the discordance itself is
diagnostically meaningful.

Examples of clinically significant discordance:
  - Image says "eczema" but symptoms say "psoriasis" → could be psoriasiform eczema
  - Image says "benign nevus" but patient reports rapid growth → needs urgent review
  - Image says "fungal" but symptoms suggest "contact dermatitis" → mixed presentation

Uses Groq LLM (free tier) for symptom-based reasoning.
"""

import json
import re
from typing import Dict, List, Optional

from app.core.logging_config import logger
from app.tools.llm_client import get_llm


def _extract_json(text: str) -> Optional[Dict]:
    """Pull JSON from LLM output."""
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


def detect_discordance(
    visual_predictions: List[Dict],
    patient_symptoms: List[str],
    abcde_scores: Optional[Dict] = None,
    fitzpatrick: Optional[Dict] = None,
) -> Dict:
    """
    Detect discordance between visual AI predictions and symptom analysis.

    1. Get the visual model's top predictions (from image)
    2. Ask LLM to generate symptom-based differential (from patient info)
    3. Compare the two lists → compute concordance
    4. If discordance detected, generate explanation of what it might mean

    Args:
        visual_predictions: Top disease predictions from image model
        patient_symptoms: Collected symptom descriptions from patient
        abcde_scores: ABCDE+ analysis results (optional)
        fitzpatrick: Skin tone data (optional)

    Returns:
        Dict with concordance_score, alert flag, and explanation.
    """
    if not visual_predictions or not patient_symptoms:
        return {
            "concordance_score": 1.0,
            "alert": False,
            "explanation": "Insufficient data for discordance analysis",
            "visual_top": None,
            "symptom_top": None,
        }

    llm = get_llm()
    if not llm:
        return {
            "concordance_score": 1.0,
            "alert": False,
            "explanation": "LLM unavailable for discordance analysis",
        }

    # Step 1: Get visual top predictions
    visual_top_3 = [p.get("disease", "").lower().strip()
                    for p in visual_predictions[:3]
                    if p.get("disease")]

    visual_text = ", ".join([
        f"{p.get('disease', '?')} ({p.get('confidence', 0) * 100:.0f}%)"
        for p in visual_predictions[:3]
    ])

    # Step 2: Ask LLM for symptom-based differential
    symptoms_text = "\n".join([f"  - {s}" for s in patient_symptoms])

    abcde_context = ""
    if abcde_scores:
        overall = abcde_scores.get("overall_risk", {})
        if overall:
            abcde_context = f"\nABCDE+ Overall Risk: {overall.get('display', '?')}"

    symptom_prompt = f"""You are a dermatologist analyzing ONLY the patient's reported symptoms
(ignore what the image shows).

Patient's symptoms and history:
{symptoms_text}
{abcde_context}

Based ONLY on these symptoms, what are the top 3 most likely skin conditions?

RESPOND WITH THIS EXACT JSON:
{{
  "symptom_predictions": [
    {{"disease": "Condition name", "reasoning": "Brief explanation"}},
    {{"disease": "Condition name", "reasoning": "Brief explanation"}},
    {{"disease": "Condition name", "reasoning": "Brief explanation"}}
  ]
}}"""

    try:
        response = llm.invoke(symptom_prompt)
        answer = (
            response.content.strip()
            if hasattr(response, "content")
            else str(response).strip()
        )

        parsed = _extract_json(answer)
        if not parsed or "symptom_predictions" not in parsed:
            return {
                "concordance_score": 1.0,
                "alert": False,
                "explanation": "Could not parse symptom analysis",
            }

        symptom_preds = parsed["symptom_predictions"]
        symptom_top_3 = [p.get("disease", "").lower().strip()
                         for p in symptom_preds[:3]
                         if p.get("disease")]

    except Exception as e:
        logger.error("Discordance symptom analysis error: %s", str(e))
        return {
            "concordance_score": 1.0,
            "alert": False,
            "explanation": f"Symptom analysis error: {str(e)}",
        }

    # Step 3: Compute concordance score
    # Use fuzzy matching: check if any visual prediction appears in symptom predictions
    concordance = _compute_concordance(visual_top_3, symptom_top_3)

    # Step 4: If discordance, generate clinical explanation
    alert = concordance < 0.4
    explanation = ""

    if alert:
        explanation = _generate_discordance_explanation(
            visual_text,
            symptom_preds,
            patient_symptoms,
            visual_predictions,
        )

    result = {
        "concordance_score": round(concordance, 3),
        "alert": alert,
        "visual_top": visual_predictions[0].get("disease", "Unknown") if visual_predictions else None,
        "symptom_top": symptom_preds[0].get("disease", "Unknown") if symptom_preds else None,
        "visual_predictions": visual_text,
        "symptom_predictions": [
            {"disease": p.get("disease", ""), "reasoning": p.get("reasoning", "")}
            for p in symptom_preds[:3]
        ],
        "explanation": explanation,
        "discordance_level": (
            "NONE" if concordance >= 0.7
            else ("LOW" if concordance >= 0.4
                  else ("MODERATE" if concordance >= 0.2 else "HIGH"))
        ),
    }

    logger.info(
        "Discordance analysis: concordance=%.2f, alert=%s, visual=%s, symptom=%s",
        concordance, alert,
        visual_top_3[0] if visual_top_3 else "?",
        symptom_top_3[0] if symptom_top_3 else "?",
    )

    return result


def _compute_concordance(visual: List[str], symptom: List[str]) -> float:
    """
    Compute concordance between two lists of condition names.

    Uses fuzzy matching: a condition matches if any significant word appears
    in both lists (e.g., "eczema" matches "atopic eczema").
    """
    if not visual or not symptom:
        return 0.5  # Unknown concordance

    # Extract significant words from each prediction
    def extract_keywords(name: str) -> set:
        # Remove common non-diagnostic words
        stop_words = {"skin", "disease", "condition", "type", "the", "of", "and",
                       "with", "or", "a", "an", "in", "on", "at"}
        words = set(name.lower().replace("_", " ").replace("-", " ").split())
        return words - stop_words

    visual_keywords = set()
    for v in visual:
        visual_keywords.update(extract_keywords(v))

    symptom_keywords = set()
    for s in symptom:
        symptom_keywords.update(extract_keywords(s))

    if not visual_keywords or not symptom_keywords:
        return 0.5

    # Jaccard-like similarity
    intersection = visual_keywords & symptom_keywords
    union = visual_keywords | symptom_keywords

    if not union:
        return 0.5

    return len(intersection) / len(union)


def _generate_discordance_explanation(
    visual_text: str,
    symptom_preds: List[Dict],
    patient_symptoms: List[str],
    visual_predictions: List[Dict],
) -> str:
    """
    Generate a clinical explanation when visual and symptom analyses disagree.
    """
    llm = get_llm()
    if not llm:
        return "Visual and symptom analyses show different results. Please consult a dermatologist."

    symptom_text = ", ".join([p.get("disease", "?") for p in symptom_preds[:3]])
    symptoms_detail = "\n".join([f"  - {s}" for s in patient_symptoms])

    prompt = f"""You are a dermatologist. The AI image analysis and symptom analysis DISAGREE.

VISUAL ANALYSIS predicts: {visual_text}
SYMPTOM ANALYSIS predicts: {symptom_text}

Patient symptoms:
{symptoms_detail}

This discordance is clinically significant. Explain:
1. Why might the visual and symptom analyses disagree for this case?
2. What conditions could present with this specific discordance pattern?
3. What additional information or tests would resolve this ambiguity?

Keep your response concise (3-4 sentences). Be specific to these conditions."""

    try:
        response = llm.invoke(prompt)
        return (
            response.content.strip()
            if hasattr(response, "content")
            else str(response).strip()
        )
    except Exception as e:
        logger.error("Discordance explanation error: %s", str(e))
        return (
            "Your lesion's appearance and reported symptoms suggest different conditions. "
            "This pattern may indicate a mixed presentation. Dermatologist review is recommended."
        )
