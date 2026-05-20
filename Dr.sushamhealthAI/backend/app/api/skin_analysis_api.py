"""
Dr.SushamHealthAI — api/skin_analysis_api.py
SkinSeva Analysis API — Publication-Grade Skin Disease Detection Endpoint.

Orchestrates the full SkinSeva pipeline:
  1. Fitzpatrick skin tone detection
  2. ABCDE+ scoring with uncertainty (TTA)
  3. Dermoscopy simulation
  4. Existing skin disease classification
  5. Regional prior adjustment (if location provided)
  6. Dynamic clinical questioning
  7. Symptom-Visual discordance detection (if symptoms provided)
  8. Longitudinal lesion tracking (if session has history)
  9. Dual agent debate (for high-risk scores)
  10. Comprehensive diagnosis generation

Single endpoint: POST /api/v1/analyze-skin
"""

import io
import json
import numpy as np
from typing import Optional

from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse
from PIL import Image

from app.core.logging_config import logger

# Import all SkinSeva modules
from app.services.skin_tone_detector import detect_skin_tone
from app.services.skin_analysis_engine import analyze_skin_lesion, get_lesion_visualization
from app.services.dermoscopy_simulator import simulate_dermoscopy
from app.services.disease_classifier import disease_classifier
from app.services.discordance_detector import detect_discordance
from app.services.regional_priors import get_regional_adjustment
from app.services.lesion_tracker import save_snapshot, get_lesion_history
from app.agents.skin_triage_agent import generate_skin_questions, generate_skin_diagnosis
from app.agents.dual_agent_debate import run_dual_debate

router = APIRouter()

# In-memory skin triage state per session
_skin_triage_state = {}


def _sanitize_for_json(obj):
    """
    Recursively convert numpy types to Python native types so
    json.dumps() can serialize the response without errors.
    numpy.bool_ → bool, numpy.int64 → int, numpy.float64 → float, etc.
    """
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(item) for item in obj]
    elif isinstance(obj, np.bool_):
        return bool(obj)
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


@router.post("/api/v1/analyze-skin")
async def analyze_skin(
    file: UploadFile = File(...),
    symptoms: Optional[str] = Form(None),
    region: Optional[str] = Form(None),
    body_location: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None),
    lesion_id: Optional[str] = Form(None),
    triage_round: Optional[int] = Form(0),
    collected_info: Optional[str] = Form(None),  # JSON array of previous answers
    run_debate: Optional[bool] = Form(False),
):
    """
    Full SkinSeva analysis pipeline.

    Form fields:
      - file: Skin image (required)
      - symptoms: Comma-separated symptoms (optional)
      - region: Patient's location in India (optional)
      - body_location: Where on the body (optional)
      - session_id: For longitudinal tracking (optional)
      - lesion_id: For tracking same lesion (optional)
      - triage_round: Current triage round (0 = first analysis)
      - collected_info: JSON array of previous triage answers
      - run_debate: Whether to run dual agent debate (optional)
    """
    try:
        # ── Read and convert image ─────────────────────────────────
        contents = await file.read()
        pil_image = Image.open(io.BytesIO(contents)).convert("RGB")
        image_bgr = np.array(pil_image)[:, :, ::-1].copy()  # RGB → BGR for OpenCV

        logger.info("SkinSeva analysis started: region=%s, triage_round=%d",
                     region, triage_round or 0)

        response = {"success": True, "type": "skinseva_analysis"}

        # ── 1. Fitzpatrick Skin Tone Detection ──────────────────────
        fitzpatrick = detect_skin_tone(image_bgr)
        response["fitzpatrick"] = {
            "type": fitzpatrick.get("type", "Unknown"),
            "label": fitzpatrick.get("label", "Unknown"),
            "description": fitzpatrick.get("description", ""),
            "hex_color": fitzpatrick.get("hex_color", "#888888"),
            "confidence": fitzpatrick.get("confidence", 0),
            "ita": fitzpatrick.get("ita", 0),
        }
        correction_factors = fitzpatrick.get("correction_factors", {})

        # ── 2. ABCDE+ Scoring ──────────────────────────────────────
        # Check for previous snapshot for evolution scoring
        previous_snapshot = None
        if session_id and lesion_id:
            history = get_lesion_history(session_id, lesion_id)
            snapshots = history.get("snapshots", [])
            if snapshots:
                prev = snapshots[-1]
                previous_snapshot = {
                    "asymmetry": prev.get("asymmetry_score"),
                    "border": prev.get("border_score"),
                    "color": prev.get("color_score"),
                    "diameter": prev.get("diameter_mm"),
                }

        abcde_scores = analyze_skin_lesion(
            image_bgr,
            correction_factors=correction_factors,
            previous_snapshot=previous_snapshot,
            run_uncertainty=True,
        )
        response["abcde_plus"] = abcde_scores

        # ── 3. Dermoscopy Simulation ───────────────────────────────
        dermoscopy = simulate_dermoscopy(image_bgr)
        response["dermoscopy"] = {
            "enhanced_image_b64": dermoscopy.get("enhanced_image_b64", ""),
            "original_b64": dermoscopy.get("original_b64", ""),
            "enhancements_applied": dermoscopy.get("enhancements_applied", []),
        }

        # ── 4. Disease Classification ─────────────────────────────
        prediction = disease_classifier.classify(pil_image)
        disease = prediction.get("disease")
        confidence = prediction.get("confidence", 0)
        category = prediction.get("category", "non_medical")

        response["disease_prediction"] = {
            "disease": disease,
            "confidence": round(confidence, 4),
            "category": category,
            "predictions": prediction.get("predictions", []),
            "domain_hint": prediction.get("domain_hint", ""),
            "domain_confidence": prediction.get("domain_confidence", 0),
        }

        # Lesion segmentation visualization
        vis_b64 = get_lesion_visualization(image_bgr, correction_factors)
        if vis_b64:
            response["segmentation_overlay_b64"] = vis_b64

        # ── 5. Regional Prior Adjustment ──────────────────────────
        if region and prediction.get("predictions"):
            regional = get_regional_adjustment(region, prediction["predictions"])
            response["regional_adjustment"] = {
                "applied": regional.get("applied", False),
                "region": region,
                "context": regional.get("regional_context", ""),
                "adjusted_predictions": regional.get("adjusted_predictions", []),
                "climate_factors": regional.get("climate_factors", []),
                "additional_considerations": regional.get("additional_considerations", []),
            }

        # ── 6. Parse collected info ───────────────────────────────
        collected_list = []
        if collected_info:
            try:
                collected_list = json.loads(collected_info)
                if not isinstance(collected_list, list):
                    collected_list = [str(collected_list)]
            except (json.JSONDecodeError, TypeError):
                collected_list = [collected_info] if collected_info else []

        # Add current symptoms if provided
        if symptoms:
            symptom_list = [s.strip() for s in symptoms.split(",") if s.strip()]
            collected_list.extend(symptom_list)

        # ── 7. Dynamic Clinical Questioning ───────────────────────
        current_round = triage_round or 0
        triage_result = generate_skin_questions(
            disease_predictions=prediction.get("predictions", []),
            abcde_scores=abcde_scores,
            collected_info=collected_list,
            round_number=current_round,
            patient_region=region,
            fitzpatrick=fitzpatrick,
        )

        response["clinical_questions"] = triage_result

        # ── 8. Discordance Detection ──────────────────────────────
        if collected_list and prediction.get("predictions"):
            discordance = detect_discordance(
                visual_predictions=prediction["predictions"],
                patient_symptoms=collected_list,
                abcde_scores=abcde_scores,
                fitzpatrick=fitzpatrick,
            )
            response["discordance"] = discordance

        # ── 9. Longitudinal Tracking ─────────────────────────────
        if session_id:
            tracking = save_snapshot(
                session_id=session_id,
                lesion_id=lesion_id,
                image_bgr=image_bgr,
                abcde_scores=abcde_scores,
                fitzpatrick=fitzpatrick,
                disease_prediction={"disease": disease, "confidence": confidence},
                body_location=body_location,
            )
            response["longitudinal"] = tracking

        # ── 10. Dual Agent Debate ────────────────────────────────
        # Run debate for high-risk lesions or when explicitly requested
        overall_risk = abcde_scores.get("overall_risk", {}).get("score", 0)
        should_debate = run_debate or overall_risk >= 50

        if should_debate and prediction.get("predictions"):
            debate = run_dual_debate(
                disease_predictions=prediction["predictions"],
                abcde_scores=abcde_scores,
                patient_symptoms=collected_list,
                fitzpatrick=fitzpatrick,
            )
            response["dual_debate"] = debate

        # ── 11. Generate Diagnosis (if triage is complete) ────────
        if triage_result.get("status") == "ready_to_diagnose" and collected_list:
            diagnosis = generate_skin_diagnosis(
                disease_predictions=prediction.get("predictions", []),
                abcde_scores=abcde_scores,
                collected_info=collected_list,
                fitzpatrick=fitzpatrick,
                region=region,
                discordance=response.get("discordance"),
                debate_result=response.get("dual_debate"),
            )
            response["comprehensive_diagnosis"] = diagnosis

        logger.info(
            "SkinSeva analysis complete: disease=%s, risk=%d%%, fitzpatrick=%s",
            disease, overall_risk, fitzpatrick.get("type", "?"),
        )

        return JSONResponse(_sanitize_for_json(response))

    except Exception as e:
        logger.error("SkinSeva analysis error: %s", str(e))
        import traceback
        logger.error(traceback.format_exc())
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)},
        )


@router.get("/api/v1/lesion-history/{session_id}")
async def get_lesion_tracking(session_id: str, lesion_id: Optional[str] = None):
    """Get the longitudinal tracking history for a patient's lesions."""
    try:
        history = get_lesion_history(session_id, lesion_id)
        return JSONResponse({"success": True, **history})
    except Exception as e:
        logger.error("Lesion history error: %s", str(e))
        return JSONResponse(
            status_code=500,
            content={"success": False, "error": str(e)},
        )
