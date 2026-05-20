"""
Dr.SushamHealthAI — services/lesion_tracker.py
Longitudinal Lesion Tracking Service.

Tracks the same skin lesion over time, computing:
  - Pixel-level change maps (image registration via ORB features)
  - Growth rate (diameter change over time)
  - Score trend analysis (ABCDE scores over time)
  - Malignancy trajectory (are scores trending toward high risk?)
  - Alert generation (>20% growth in 3 months → urgent)

Novel contribution: No existing consumer dermatology AI does longitudinal
lesion tracking with quantified growth metrics.
"""

import os
import uuid
import json
import base64
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.core.logging_config import logger

try:
    import cv2
except ImportError:
    cv2 = None


# ═══════════════════════════════════════════════════════════════
# STORAGE PATHS
# ═══════════════════════════════════════════════════════════════

_LESION_STORAGE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "storage", "lesion_images"
)


def _ensure_storage():
    """Ensure the lesion storage directory exists."""
    os.makedirs(_LESION_STORAGE_DIR, exist_ok=True)


# ═══════════════════════════════════════════════════════════════
# SNAPSHOT STORAGE (File-based for simplicity)
# ═══════════════════════════════════════════════════════════════

_LESION_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "storage", "lesion_db.json"
)


def _load_lesion_db() -> Dict:
    """Load the lesion tracking database from JSON file."""
    if os.path.exists(_LESION_DB_PATH):
        try:
            with open(_LESION_DB_PATH, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"lesions": {}, "snapshots": {}}


def _save_lesion_db(db: Dict):
    """Save the lesion tracking database."""
    os.makedirs(os.path.dirname(_LESION_DB_PATH), exist_ok=True)
    with open(_LESION_DB_PATH, "w") as f:
        json.dump(db, f, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════
# IMAGE REGISTRATION
# ═══════════════════════════════════════════════════════════════

def _align_images(reference: np.ndarray, target: np.ndarray) -> Tuple[np.ndarray, bool]:
    """
    Align target image to reference using ORB feature matching + homography.

    This corrects for slight differences in camera angle/position when the
    patient photographs the same lesion at different times.

    Returns: (aligned_target, success_flag)
    """
    if cv2 is None:
        return target, False

    try:
        # Convert to grayscale
        ref_gray = cv2.cvtColor(reference, cv2.COLOR_BGR2GRAY)
        tgt_gray = cv2.cvtColor(target, cv2.COLOR_BGR2GRAY)

        # ORB feature detection
        orb = cv2.ORB_create(nfeatures=500)
        kp1, des1 = orb.detectAndCompute(ref_gray, None)
        kp2, des2 = orb.detectAndCompute(tgt_gray, None)

        if des1 is None or des2 is None or len(kp1) < 10 or len(kp2) < 10:
            return target, False

        # Match features
        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = bf.match(des1, des2)
        matches = sorted(matches, key=lambda m: m.distance)

        if len(matches) < 10:
            return target, False

        # Use top matches for homography
        good_matches = matches[:min(50, len(matches))]

        src_pts = np.float32([kp1[m.queryIdx].pt for m in good_matches]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp2[m.trainIdx].pt for m in good_matches]).reshape(-1, 1, 2)

        M, mask = cv2.findHomography(dst_pts, src_pts, cv2.RANSAC, 5.0)

        if M is None:
            return target, False

        h, w = reference.shape[:2]
        aligned = cv2.warpPerspective(target, M, (w, h))

        return aligned, True

    except Exception as e:
        logger.error("Image alignment error: %s", str(e))
        return target, False


def _compute_change_map(previous: np.ndarray, current: np.ndarray) -> Dict:
    """
    Compute pixel-level change between two aligned images.

    Returns metrics about what changed: area growth, colour shift,
    border changes.
    """
    if cv2 is None:
        return {}

    try:
        # Ensure same size
        h, w = previous.shape[:2]
        current_resized = cv2.resize(current, (w, h))

        # Absolute difference
        diff = cv2.absdiff(previous, current_resized)
        diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)

        # Threshold significant changes
        _, change_mask = cv2.threshold(diff_gray, 30, 255, cv2.THRESH_BINARY)

        # Compute change metrics
        total_pixels = h * w
        changed_pixels = int(np.sum(change_mask > 0))
        change_percentage = (changed_pixels / total_pixels) * 100

        # Mean change intensity in changed regions
        mean_change_intensity = float(np.mean(diff_gray[change_mask > 0])) if changed_pixels > 0 else 0

        # Colour shift analysis (in L*a*b*)
        prev_lab = cv2.cvtColor(previous, cv2.COLOR_BGR2Lab).astype(np.float32)
        curr_lab = cv2.cvtColor(current_resized, cv2.COLOR_BGR2Lab).astype(np.float32)

        l_shift = float(np.mean(curr_lab[:, :, 0]) - np.mean(prev_lab[:, :, 0]))
        a_shift = float(np.mean(curr_lab[:, :, 1]) - np.mean(prev_lab[:, :, 1]))
        b_shift = float(np.mean(curr_lab[:, :, 2]) - np.mean(prev_lab[:, :, 2]))

        # Generate change map image
        change_vis = cv2.applyColorMap(diff_gray, cv2.COLORMAP_JET)
        _, buf = cv2.imencode(".png", change_vis)
        change_map_b64 = base64.b64encode(buf).decode("utf-8")

        return {
            "change_percentage": round(change_percentage, 2),
            "changed_pixels": changed_pixels,
            "mean_change_intensity": round(mean_change_intensity, 2),
            "color_shift": {
                "luminance": round(l_shift, 2),
                "red_green": round(a_shift, 2),
                "blue_yellow": round(b_shift, 2),
            },
            "change_map_b64": change_map_b64,
        }
    except Exception as e:
        logger.error("Change map computation error: %s", str(e))
        return {}


# ═══════════════════════════════════════════════════════════════
# MAIN TRACKING API
# ═══════════════════════════════════════════════════════════════

def save_snapshot(
    session_id: str,
    lesion_id: Optional[str],
    image_bgr: np.ndarray,
    abcde_scores: Dict,
    fitzpatrick: Dict,
    disease_prediction: Dict,
    body_location: Optional[str] = None,
) -> Dict:
    """
    Save a new snapshot of a lesion for longitudinal tracking.

    If lesion_id is None, creates a new lesion profile.
    If lesion_id is provided, adds a new snapshot to existing profile.

    Returns: lesion tracking data including trajectory if previous data exists.
    """
    _ensure_storage()
    db = _load_lesion_db()

    # Create or find lesion profile
    if not lesion_id or lesion_id not in db["lesions"]:
        lesion_id = str(uuid.uuid4())
        db["lesions"][lesion_id] = {
            "id": lesion_id,
            "session_id": session_id,
            "body_location": body_location,
            "first_seen": datetime.utcnow().isoformat(),
            "is_new": True,
        }

    # Save image
    image_filename = f"{lesion_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.png"
    image_path = os.path.join(_LESION_STORAGE_DIR, image_filename)

    if cv2 is not None:
        cv2.imwrite(image_path, image_bgr)

    # Create snapshot
    snapshot_id = str(uuid.uuid4())
    overall = abcde_scores.get("overall_risk", {})

    snapshot = {
        "id": snapshot_id,
        "lesion_id": lesion_id,
        "timestamp": datetime.utcnow().isoformat(),
        "image_path": image_path,
        "asymmetry_score": abcde_scores.get("asymmetry", {}).get("score"),
        "border_score": abcde_scores.get("border", {}).get("score"),
        "color_score": abcde_scores.get("color", {}).get("score"),
        "diameter_score": abcde_scores.get("diameter", {}).get("score"),
        "diameter_mm": abcde_scores.get("diameter", {}).get("estimated_diameter_mm"),
        "overall_risk": overall.get("score"),
        "uncertainty": overall.get("uncertainty"),
        "fitzpatrick_type": fitzpatrick.get("type"),
        "fitzpatrick_ita": fitzpatrick.get("ita"),
        "predicted_disease": disease_prediction.get("disease"),
        "prediction_confidence": disease_prediction.get("confidence"),
    }

    if lesion_id not in db["snapshots"]:
        db["snapshots"][lesion_id] = []
    db["snapshots"][lesion_id].append(snapshot)

    _save_lesion_db(db)

    # Compute trajectory if previous snapshots exist
    all_snapshots = db["snapshots"].get(lesion_id, [])
    trajectory = _compute_trajectory(all_snapshots, image_bgr, lesion_id)

    logger.info(
        "Lesion snapshot saved: lesion=%s, snapshot=%s, snapshots_total=%d",
        lesion_id[:8], snapshot_id[:8], len(all_snapshots),
    )

    return {
        "lesion_id": lesion_id,
        "snapshot_id": snapshot_id,
        "snapshot_count": len(all_snapshots),
        "trajectory": trajectory,
        "is_first_upload": len(all_snapshots) <= 1,
    }


def get_lesion_history(session_id: str, lesion_id: Optional[str] = None) -> Dict:
    """
    Get the tracking history for a patient's lesions.

    If lesion_id is provided, returns history for that specific lesion.
    Otherwise returns all lesions for the session.
    """
    db = _load_lesion_db()

    if lesion_id:
        lesion = db["lesions"].get(lesion_id)
        if not lesion:
            return {"error": "Lesion not found", "lesions": []}

        snapshots = db["snapshots"].get(lesion_id, [])
        return {
            "lesion": lesion,
            "snapshots": snapshots,
            "snapshot_count": len(snapshots),
        }

    # All lesions for session
    session_lesions = [
        {
            "lesion": l,
            "snapshot_count": len(db["snapshots"].get(lid, [])),
            "latest_snapshot": (db["snapshots"].get(lid, [{}])[-1]
                                if db["snapshots"].get(lid) else None),
        }
        for lid, l in db["lesions"].items()
        if l.get("session_id") == session_id
    ]

    return {"lesions": session_lesions}


def _compute_trajectory(snapshots: List[Dict], current_image: Optional[np.ndarray],
                         lesion_id: str) -> Dict:
    """
    Compute the malignancy trajectory from a series of snapshots.

    Analyses:
    - Growth rate (diameter change per week)
    - ABCDE score trends (increasing / stable / decreasing)
    - Overall risk trend
    - Alerts for concerning patterns
    """
    if len(snapshots) < 2:
        return {
            "has_history": False,
            "message": "Upload more photos over time to track changes",
        }

    try:
        # Sort by timestamp
        sorted_snaps = sorted(snapshots, key=lambda s: s.get("timestamp", ""))

        # Timeline data
        timeline = []
        for snap in sorted_snaps:
            ts = snap.get("timestamp", "")
            timeline.append({
                "timestamp": ts,
                "diameter_mm": snap.get("diameter_mm"),
                "asymmetry": snap.get("asymmetry_score"),
                "border": snap.get("border_score"),
                "color": snap.get("color_score"),
                "overall_risk": snap.get("overall_risk"),
                "predicted_disease": snap.get("predicted_disease"),
            })

        # Growth analysis
        first_diameter = sorted_snaps[0].get("diameter_mm")
        last_diameter = sorted_snaps[-1].get("diameter_mm")
        growth_rate = None
        growth_alert = False

        if first_diameter and last_diameter and first_diameter > 0:
            growth_pct = ((last_diameter - first_diameter) / first_diameter) * 100

            # Compute time span
            try:
                first_time = datetime.fromisoformat(sorted_snaps[0]["timestamp"])
                last_time = datetime.fromisoformat(sorted_snaps[-1]["timestamp"])
                days_elapsed = max((last_time - first_time).days, 1)
                weeks_elapsed = max(days_elapsed / 7, 0.14)

                growth_rate = {
                    "total_growth_pct": round(growth_pct, 1),
                    "days_tracked": days_elapsed,
                    "growth_per_week_pct": round(growth_pct / weeks_elapsed, 1),
                    "first_diameter_mm": round(first_diameter, 1),
                    "last_diameter_mm": round(last_diameter, 1),
                }

                # Alert: >20% growth in <90 days
                growth_alert = growth_pct > 20 and days_elapsed < 90
            except (ValueError, TypeError):
                pass

        # Score trends
        def _trend(values):
            """Compute trend direction from a series of values."""
            clean = [v for v in values if v is not None]
            if len(clean) < 2:
                return "INSUFFICIENT_DATA"
            diff = clean[-1] - clean[0]
            if abs(diff) < 5:
                return "STABLE"
            return "INCREASING" if diff > 0 else "DECREASING"

        score_trends = {
            "asymmetry": _trend([s.get("asymmetry_score") for s in sorted_snaps]),
            "border": _trend([s.get("border_score") for s in sorted_snaps]),
            "color": _trend([s.get("color_score") for s in sorted_snaps]),
            "overall_risk": _trend([s.get("overall_risk") for s in sorted_snaps]),
        }

        # Concerning pattern: multiple dimensions increasing
        increasing_count = sum(1 for v in score_trends.values() if v == "INCREASING")
        pattern_alert = increasing_count >= 2

        # Overall alerts
        alerts = []
        if growth_alert:
            alerts.append({
                "level": "URGENT",
                "message": (
                    f"This lesion has grown {growth_rate['total_growth_pct']:.0f}% "
                    f"in {growth_rate['days_tracked']} days — dermatologist visit is recommended"
                ),
            })
        if pattern_alert:
            alerts.append({
                "level": "WARNING",
                "message": (
                    f"Multiple ABCDE dimensions are trending upward "
                    f"({increasing_count} increasing). This pattern needs monitoring."
                ),
            })

        # Change map with previous image
        change_map = {}
        if cv2 is not None and current_image is not None and len(sorted_snaps) >= 2:
            prev_path = sorted_snaps[-2].get("image_path")
            if prev_path and os.path.exists(prev_path):
                prev_img = cv2.imread(prev_path)
                if prev_img is not None:
                    aligned, _ = _align_images(prev_img, current_image)
                    change_map = _compute_change_map(prev_img, aligned)

        return {
            "has_history": True,
            "snapshot_count": len(sorted_snaps),
            "timeline": timeline,
            "growth_rate": growth_rate,
            "score_trends": score_trends,
            "alerts": alerts,
            "change_map": change_map if change_map else None,
            "trajectory_pattern": (
                "CONCERNING" if (growth_alert or pattern_alert)
                else "STABLE" if all(v in ("STABLE", "DECREASING", "INSUFFICIENT_DATA")
                                     for v in score_trends.values())
                else "MONITOR"
            ),
        }

    except Exception as e:
        logger.error("Trajectory computation error: %s", str(e))
        return {
            "has_history": True,
            "error": str(e),
        }
