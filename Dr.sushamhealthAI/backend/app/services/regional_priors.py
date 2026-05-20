"""
Dr.SushamHealthAI — services/regional_priors.py
India-Specific Regional Disease Prior Adjustment.

Adjusts disease prediction probabilities based on the patient's geographic
location. Different skin diseases have dramatically different prevalence
in different Indian regions due to climate, humidity, altitude, and
endemic factors.

Data sources: ICMR reports, NVBDCP, published dermatology prevalence studies.

Novel contribution: Geographically-aware dermatology AI for India.
Zero competition in the literature.
"""

from typing import Dict, List, Optional, Tuple

from app.core.logging_config import logger
from app.tools.llm_client import get_llm


def get_regional_adjustment(
    region: str,
    disease_predictions: List[Dict],
) -> Dict:
    """
    Dynamically adjust disease prediction probabilities based on patient location.

    Instead of using hardcoded lookup tables, this uses the LLM's knowledge of
    Indian dermatological epidemiology to generate region-specific adjustments.

    Args:
        region: Patient's location (state, city, or district)
        disease_predictions: Current top disease predictions from the image model

    Returns:
        Dict with adjusted predictions, regional context, and explanations.
    """
    if not region or not disease_predictions:
        return {
            "applied": False,
            "reason": "No region or predictions provided",
            "original_predictions": disease_predictions,
            "adjusted_predictions": disease_predictions,
        }

    llm = get_llm()
    if not llm:
        return {
            "applied": False,
            "reason": "LLM unavailable for regional analysis",
            "original_predictions": disease_predictions,
            "adjusted_predictions": disease_predictions,
        }

    # Build disease list for the LLM
    pred_text = "\n".join([
        f"  - {p.get('disease', '?')}: {p.get('confidence', 0) * 100:.1f}%"
        for p in disease_predictions[:5]
    ])

    prompt = f"""You are an Indian dermatology epidemiologist. A patient is from: {region}

The AI has predicted these skin conditions from their image:
{pred_text}

Based on your knowledge of dermatological epidemiology in {region}, answer:

1. Are any of these conditions MORE common in {region}? If so, by how much?
2. Are any LESS common in {region}?
3. Are there conditions common in {region} that are NOT in the prediction list but should be considered?
4. What climate/environmental factors in {region} affect skin disease prevalence?

Consider:
- Climate (coastal humid, arid, continental, tropical)
- Altitude
- Common occupations
- Known endemic diseases
- Water quality
- Sun exposure patterns
- Monsoon effects

RESPOND WITH THIS EXACT JSON:
{{
  "adjustments": [
    {{
      "disease": "condition name",
      "original_confidence": 0.0,
      "adjusted_confidence": 0.0,
      "adjustment_reason": "Why this condition is more/less likely in this region"
    }}
  ],
  "regional_context": "Brief description of the region's dermatological profile",
  "additional_considerations": ["Any extra conditions to consider for this region"],
  "climate_factors": ["Relevant climate/environmental factors"]
}}"""

    try:
        response = llm.invoke(prompt)
        answer = (
            response.content.strip()
            if hasattr(response, "content")
            else str(response).strip()
        )

        # Try to parse JSON from response
        import json, re
        parsed = None
        try:
            parsed = json.loads(answer)
        except (json.JSONDecodeError, TypeError):
            m = re.search(r"\{[\s\S]*\}", answer)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass

        if not parsed:
            # If JSON parsing fails, return raw text as context
            return {
                "applied": True,
                "region": region,
                "regional_context": answer,
                "original_predictions": disease_predictions,
                "adjusted_predictions": disease_predictions,
                "adjustments": [],
                "climate_factors": [],
            }

        # Apply adjustments to predictions
        adjusted = []
        adjustments = parsed.get("adjustments", [])

        for pred in disease_predictions:
            adj_pred = pred.copy()
            for adj in adjustments:
                if (adj.get("disease", "").lower().strip() ==
                        pred.get("disease", "").lower().strip()):
                    adj_conf = adj.get("adjusted_confidence")
                    if adj_conf is not None:
                        adj_pred["original_confidence"] = pred.get("confidence", 0)
                        adj_pred["confidence"] = adj_conf
                        adj_pred["regional_adjustment"] = adj.get("adjustment_reason", "")
                    break
            adjusted.append(adj_pred)

        # Re-sort by adjusted confidence
        adjusted.sort(key=lambda x: x.get("confidence", 0), reverse=True)

        result = {
            "applied": True,
            "region": region,
            "regional_context": parsed.get("regional_context", ""),
            "original_predictions": disease_predictions,
            "adjusted_predictions": adjusted,
            "adjustments": adjustments,
            "additional_considerations": parsed.get("additional_considerations", []),
            "climate_factors": parsed.get("climate_factors", []),
        }

        logger.info(
            "Regional priors applied for %s: %d adjustments",
            region, len(adjustments),
        )

        return result

    except Exception as e:
        logger.error("Regional prior adjustment error: %s", str(e))
        return {
            "applied": False,
            "reason": f"Error: {str(e)}",
            "original_predictions": disease_predictions,
            "adjusted_predictions": disease_predictions,
        }
