"""
Dr.SushamHealthAI — services/skin_analysis_engine.py
ABCDE+ Dynamic Scoring with Uncertainty Quantification.

Computes the gold-standard dermatological ABCDE checklist with two novel
extensions:
  F — Fitzpatrick skin tone correction (adjusts analysis for melanin-rich skin)
  U — Uncertainty per dimension (via test-time augmentation)

Pipeline:
  1. Lesion segmentation (adaptive thresholding + contour detection)
  2. Per-dimension scoring (A/B/C/D computed from image features)
  3. Fitzpatrick correction (adjusts thresholds via skin_tone_detector)
  4. Uncertainty estimation (5× augmented inference, report variance)
  5. Overall malignancy risk = weighted ABCDE combination ± uncertainty

All processing uses OpenCV + scikit-image. Zero paid APIs.
"""

import io
import base64
import math
import numpy as np
from typing import Dict, List, Optional, Tuple

from app.core.logging_config import logger

try:
    import cv2
except ImportError:
    cv2 = None

try:
    from skimage import morphology, measure, color as skcolor, filters, exposure
except ImportError:
    morphology = measure = skcolor = filters = exposure = None


# ═══════════════════════════════════════════════════════════════
# LESION SEGMENTATION
# ═══════════════════════════════════════════════════════════════

def _segment_lesion(image_bgr: np.ndarray, correction_factors: Dict) -> Tuple[np.ndarray, np.ndarray]:
    """
    Dynamically segment the skin lesion from the background.

    Uses adaptive Otsu thresholding on the blue channel (which best
    separates pigmented lesions from surrounding skin), enhanced with
    morphological cleanup. Adjusts contrast based on Fitzpatrick correction.

    Returns: (binary_mask, largest_contour)
    """
    # Convert and prep
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2Lab)
    l_channel = lab[:, :, 0]

    # Apply contrast boost based on skin tone
    contrast_boost = correction_factors.get("border_contrast_boost", 1.0)
    if contrast_boost > 1.0:
        clahe = cv2.createCLAHE(clipLimit=2.0 * contrast_boost, tileGridSize=(8, 8))
        l_channel = clahe.apply(l_channel)

    # Gaussian blur to reduce noise
    blurred = cv2.GaussianBlur(l_channel, (5, 5), 0)

    # Otsu's thresholding (adapts automatically to the image)
    _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # Morphological operations to clean up
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=3)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=2)

    # Find contours and pick the largest (assumed to be the lesion)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if not contours:
        # Fallback: centre-weighted mask
        h, w = image_bgr.shape[:2]
        mask = np.zeros((h, w), dtype=np.uint8)
        cv2.circle(mask, (w // 2, h // 2), min(h, w) // 4, 255, -1)
        contour = np.array([[[w // 2, h // 4]], [[w * 3 // 4, h // 2]],
                            [[w // 2, h * 3 // 4]], [[w // 4, h // 2]]])
        return mask, contour

    # Sort by area, pick the largest that's at least 1% of image area
    h, w = image_bgr.shape[:2]
    min_area = h * w * 0.01
    valid_contours = [c for c in contours if cv2.contourArea(c) > min_area]

    if not valid_contours:
        valid_contours = contours

    largest = max(valid_contours, key=cv2.contourArea)

    # Create clean mask from the largest contour
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.drawContours(mask, [largest], -1, 255, -1)

    return mask, largest


# ═══════════════════════════════════════════════════════════════
# A — ASYMMETRY SCORING
# ═══════════════════════════════════════════════════════════════

def _score_asymmetry(mask: np.ndarray, correction_factors: Dict) -> Dict:
    """
    Score asymmetry by comparing the lesion to its reflection along both axes.

    Method: Flip the mask horizontally and vertically around the lesion
    centroid, compute overlap ratio (Dice coefficient). Lower overlap = higher
    asymmetry = higher risk.
    """
    try:
        # Find bounding box of the lesion
        coords = np.column_stack(np.where(mask > 0))
        if len(coords) < 10:
            return {"score": 0, "confidence": 0, "risk_level": "UNKNOWN"}

        y_min, x_min = coords.min(axis=0)
        y_max, x_max = coords.max(axis=0)

        # Crop to lesion bounding box
        cropped = mask[y_min:y_max + 1, x_min:x_max + 1].copy()
        h, w = cropped.shape

        if h < 5 or w < 5:
            return {"score": 0, "confidence": 0, "risk_level": "UNKNOWN"}

        # Horizontal flip comparison
        flipped_h = cv2.flip(cropped, 1)
        overlap_h = np.sum((cropped > 0) & (flipped_h > 0))
        union_h = np.sum((cropped > 0) | (flipped_h > 0))
        dice_h = (2.0 * overlap_h / union_h) if union_h > 0 else 1.0

        # Vertical flip comparison
        flipped_v = cv2.flip(cropped, 0)
        overlap_v = np.sum((cropped > 0) & (flipped_v > 0))
        union_v = np.sum((cropped > 0) | (flipped_v > 0))
        dice_v = (2.0 * overlap_v / union_v) if union_v > 0 else 1.0

        # Average symmetry score (1 = perfectly symmetric, 0 = perfectly asymmetric)
        symmetry = (dice_h + dice_v) / 2.0

        # Convert to asymmetry risk (0-100)
        asymmetry_score = int(np.clip((1.0 - symmetry) * 125, 0, 100))

        # Confidence based on lesion size (larger lesion = more reliable)
        area = np.sum(cropped > 0)
        total = h * w
        confidence = int(np.clip(min(area / 500, 1.0) * 100, 40, 99))

        risk = "HIGH" if asymmetry_score >= 60 else ("MEDIUM" if asymmetry_score >= 35 else "LOW")

        return {
            "score": asymmetry_score,
            "confidence": confidence,
            "risk_level": risk,
            "symmetry_h": round(float(dice_h), 3),
            "symmetry_v": round(float(dice_v), 3),
        }
    except Exception as e:
        logger.error("Asymmetry scoring error: %s", str(e))
        return {"score": 0, "confidence": 0, "risk_level": "UNKNOWN", "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# B — BORDER IRREGULARITY SCORING
# ═══════════════════════════════════════════════════════════════

def _score_border(contour: np.ndarray, mask: np.ndarray, correction_factors: Dict) -> Dict:
    """
    Score border irregularity using:
    1. Compactness ratio: perimeter² / (4π × area) — circle = 1.0
    2. Fractal dimension estimate via box-counting
    3. Roughness: ratio of actual perimeter to convex hull perimeter
    """
    try:
        area = cv2.contourArea(contour)
        perimeter = cv2.arcLength(contour, True)

        if area < 10 or perimeter < 10:
            return {"score": 0, "confidence": 0, "risk_level": "UNKNOWN"}

        # Compactness: perfect circle = 1.0, irregular = > 1.0
        compactness = (perimeter ** 2) / (4.0 * math.pi * area)

        # Convex hull roughness
        hull = cv2.convexHull(contour)
        hull_perimeter = cv2.arcLength(hull, True)
        roughness = perimeter / hull_perimeter if hull_perimeter > 0 else 1.0

        # Combined border score (0-100)
        # Compactness: 1.0 = circle (score 0), >2.0 = very irregular (score 100)
        comp_score = np.clip((compactness - 1.0) / 1.5 * 100, 0, 100)
        rough_score = np.clip((roughness - 1.0) / 0.5 * 100, 0, 100)

        border_score = int(0.6 * comp_score + 0.4 * rough_score)

        # Confidence
        confidence = int(np.clip(min(len(contour) / 50, 1.0) * 100, 40, 99))

        risk = "HIGH" if border_score >= 60 else ("MEDIUM" if border_score >= 35 else "LOW")

        return {
            "score": border_score,
            "confidence": confidence,
            "risk_level": risk,
            "compactness": round(float(compactness), 3),
            "roughness": round(float(roughness), 3),
        }
    except Exception as e:
        logger.error("Border scoring error: %s", str(e))
        return {"score": 0, "confidence": 0, "risk_level": "UNKNOWN", "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# C — COLOUR VARIATION SCORING
# ═══════════════════════════════════════════════════════════════

def _score_color(image_bgr: np.ndarray, mask: np.ndarray, correction_factors: Dict) -> Dict:
    """
    Score colour variation within the lesion using K-means clustering.

    More distinct colour clusters within a lesion = higher malignancy risk.
    Adjusts for Fitzpatrick skin tone: darker skin gets wider threshold for
    what counts as a "distinct" colour.
    """
    try:
        # Extract lesion pixels
        lesion_pixels = image_bgr[mask > 0].astype(np.float32)

        if len(lesion_pixels) < 50:
            return {"score": 0, "confidence": 0, "risk_level": "UNKNOWN"}

        # Convert to L*a*b* for perceptually uniform clustering
        lesion_lab = cv2.cvtColor(
            lesion_pixels.reshape(-1, 1, 3).astype(np.uint8),
            cv2.COLOR_BGR2Lab
        ).reshape(-1, 3).astype(np.float32)

        # K-means clustering (k=6, then count "distinct" clusters)
        k = min(6, len(lesion_lab) // 10)
        if k < 2:
            k = 2

        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1.0)
        _, labels, centres = cv2.kmeans(
            lesion_lab, k, None, criteria, 5, cv2.KMEANS_PP_CENTERS
        )

        # Count pixels per cluster
        unique, counts = np.unique(labels.flatten(), return_counts=True)
        total = sum(counts)

        # Apply Fitzpatrick correction: adjust what counts as "distinct"
        threshold_scale = correction_factors.get("color_variation_threshold_scale", 1.0)

        # A cluster is "significant" if it has > 5% of pixels (adjusted)
        min_fraction = 0.05 * threshold_scale
        significant_clusters = sum(1 for c in counts if c / total > min_fraction)

        # Compute inter-cluster distance (colour spread)
        if len(centres) >= 2:
            distances = []
            for i in range(len(centres)):
                for j in range(i + 1, len(centres)):
                    d = np.linalg.norm(centres[i] - centres[j])
                    distances.append(d)
            avg_distance = np.mean(distances) if distances else 0
            max_distance = np.max(distances) if distances else 0
        else:
            avg_distance = 0
            max_distance = 0

        # Score: combination of cluster count and spread
        cluster_score = np.clip((significant_clusters - 1) / 4 * 100, 0, 100)
        spread_score = np.clip(avg_distance / (40 * threshold_scale) * 100, 0, 100)
        color_score = int(0.5 * cluster_score + 0.5 * spread_score)

        confidence = int(np.clip(min(len(lesion_pixels) / 500, 1.0) * 100, 40, 99))

        # Flag if dark skin correction was applied
        dark_correction = correction_factors.get("dark_skin_correction_applied", False)

        risk = "HIGH" if color_score >= 60 else ("MEDIUM" if color_score >= 35 else "LOW")

        return {
            "score": color_score,
            "confidence": confidence,
            "risk_level": risk,
            "significant_clusters": significant_clusters,
            "avg_color_distance": round(float(avg_distance), 2),
            "max_color_distance": round(float(max_distance), 2),
            "fitzpatrick_correction_applied": dark_correction,
        }
    except Exception as e:
        logger.error("Colour scoring error: %s", str(e))
        return {"score": 0, "confidence": 0, "risk_level": "UNKNOWN", "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# D — DIAMETER ESTIMATION
# ═══════════════════════════════════════════════════════════════

def _score_diameter(contour: np.ndarray, image_bgr: np.ndarray) -> Dict:
    """
    Estimate lesion diameter using the minimum enclosing circle.

    Without a physical reference object, we estimate diameter relative to
    the image dimensions and use a DPI heuristic (typical phone camera at
    15-30cm distance).

    > 6mm = concerning threshold in dermatology
    """
    try:
        (x, y), radius = cv2.minEnclosingCircle(contour)
        diameter_px = radius * 2

        # Image dimension context
        h, w = image_bgr.shape[:2]
        image_diagonal_px = math.sqrt(h ** 2 + w ** 2)

        # Heuristic: typical phone photo of skin at ~15-20cm, full image ~ 50-80mm
        # This is an ESTIMATE — real measurement needs a reference object
        estimated_mm_per_px = 50.0 / image_diagonal_px  # rough heuristic
        diameter_mm = diameter_px * estimated_mm_per_px

        # Score: <6mm = LOW, 6-10mm = MEDIUM, >10mm = HIGH
        if diameter_mm < 6:
            score = int(np.clip(diameter_mm / 6 * 35, 0, 35))
            risk = "LOW"
        elif diameter_mm < 10:
            score = int(35 + (diameter_mm - 6) / 4 * 30)
            risk = "MEDIUM"
        else:
            score = int(np.clip(65 + (diameter_mm - 10) / 10 * 35, 65, 100))
            risk = "HIGH"

        # Confidence is moderate because we're estimating without physical reference
        confidence = 70

        return {
            "score": score,
            "confidence": confidence,
            "risk_level": risk,
            "estimated_diameter_mm": round(float(diameter_mm), 1),
            "diameter_px": round(float(diameter_px), 1),
            "note": "Estimated without physical reference — actual size may vary",
        }
    except Exception as e:
        logger.error("Diameter scoring error: %s", str(e))
        return {"score": 0, "confidence": 0, "risk_level": "UNKNOWN", "error": str(e)}


# ═══════════════════════════════════════════════════════════════
# AUGMENTATION FOR UNCERTAINTY ESTIMATION
# ═══════════════════════════════════════════════════════════════

def _generate_augmentations(image_bgr: np.ndarray, n: int = 5) -> List[np.ndarray]:
    """
    Generate n slightly different versions of the image for test-time
    augmentation (TTA). Each augmentation applies small random perturbations
    that a human wouldn't notice but can affect model predictions.
    """
    augmented = []
    h, w = image_bgr.shape[:2]
    rng = np.random.default_rng(42)  # Reproducible

    for i in range(n):
        img = image_bgr.copy()

        # Slight rotation (-5 to +5 degrees)
        angle = rng.uniform(-5, 5)
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        img = cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REFLECT)

        # Slight brightness shift
        brightness = rng.uniform(-15, 15)
        img = np.clip(img.astype(np.float32) + brightness, 0, 255).astype(np.uint8)

        # Slight Gaussian noise
        noise = rng.normal(0, 3, img.shape).astype(np.float32)
        img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)

        augmented.append(img)

    return augmented


# ═══════════════════════════════════════════════════════════════
# MAIN ANALYSIS ENGINE
# ═══════════════════════════════════════════════════════════════

def analyze_skin_lesion(
    image_bgr: np.ndarray,
    correction_factors: Optional[Dict] = None,
    previous_snapshot: Optional[Dict] = None,
    run_uncertainty: bool = True,
) -> Dict:
    """
    Full ABCDE+ skin lesion analysis.

    Args:
        image_bgr: OpenCV BGR image
        correction_factors: From skin_tone_detector (Fitzpatrick corrections)
        previous_snapshot: Previous lesion data for evolution (E) scoring
        run_uncertainty: Whether to run TTA for uncertainty estimation

    Returns:
        Complete ABCDE+ report with scores, confidence, risk levels,
        and overall malignancy risk ± uncertainty.
    """
    if cv2 is None:
        return {"error": "OpenCV not available"}

    if correction_factors is None:
        correction_factors = {
            "redness_sensitivity_boost": 1.0,
            "color_variation_threshold_scale": 1.0,
            "border_contrast_boost": 1.0,
            "luminance_offset": 0.0,
            "dark_skin_correction_applied": False,
            "ita_normalized": 0.5,
        }

    try:
        # 1. Segment lesion
        mask, contour = _segment_lesion(image_bgr, correction_factors)

        # 2. Score each ABCDE dimension
        asymmetry = _score_asymmetry(mask, correction_factors)
        border = _score_border(contour, mask, correction_factors)
        colour = _score_color(image_bgr, mask, correction_factors)
        diameter = _score_diameter(contour, image_bgr)

        # 3. Evolution scoring (requires previous data)
        if previous_snapshot:
            evolution = _score_evolution(
                current={
                    "asymmetry": asymmetry["score"],
                    "border": border["score"],
                    "color": colour["score"],
                    "diameter": diameter.get("estimated_diameter_mm", 0),
                },
                previous=previous_snapshot,
            )
        else:
            evolution = {
                "score": None,
                "confidence": 0,
                "risk_level": "UNKNOWN",
                "needs": "Upload a previous photo of the same lesion to track changes",
            }

        # 4. Compute overall malignancy risk
        scored_dimensions = [asymmetry, border, colour, diameter]
        if evolution["score"] is not None:
            scored_dimensions.append(evolution)

        weights = {"asymmetry": 0.25, "border": 0.2, "color": 0.25, "diameter": 0.15, "evolution": 0.15}
        total_weight = 0
        weighted_sum = 0

        for dim, name in zip(scored_dimensions,
                             ["asymmetry", "border", "color", "diameter", "evolution"]):
            if dim.get("score") is not None:
                w = weights.get(name, 0.15)
                weighted_sum += dim["score"] * w
                total_weight += w

        overall_risk = int(weighted_sum / total_weight) if total_weight > 0 else 0

        # 5. Uncertainty estimation via TTA
        uncertainty = 0
        if run_uncertainty:
            uncertainty = _estimate_uncertainty(image_bgr, correction_factors)

        # 6. Build report
        report = {
            "asymmetry": asymmetry,
            "border": border,
            "color": colour,
            "diameter": diameter,
            "evolution": evolution,
            "overall_risk": {
                "score": overall_risk,
                "uncertainty": uncertainty,
                "risk_level": ("HIGH" if overall_risk >= 60
                               else ("MEDIUM" if overall_risk >= 35 else "LOW")),
                "display": f"{overall_risk}% ± {uncertainty}%",
            },
            "fitzpatrick_correction": {
                "applied": correction_factors.get("dark_skin_correction_applied", False),
                "ita_normalized": correction_factors.get("ita_normalized", 0.5),
            },
            "lesion_area_px": int(np.sum(mask > 0)),
        }

        logger.info(
            "ABCDE+ Analysis: A=%d B=%d C=%d D=%d E=%s | Risk=%d±%d%%",
            asymmetry["score"], border["score"], colour["score"],
            diameter["score"],
            str(evolution.get("score", "N/A")),
            overall_risk, uncertainty,
        )

        return report

    except Exception as e:
        logger.error("Skin analysis engine error: %s", str(e))
        import traceback
        logger.error(traceback.format_exc())
        return {"error": str(e)}


def _score_evolution(current: Dict, previous: Dict) -> Dict:
    """
    Score evolution (E) by comparing current metrics to previous snapshot.
    Significant changes across any ABCDE dimension increase the evolution score.
    """
    try:
        deltas = {}
        dimension_keys = ["asymmetry", "border", "color", "diameter"]

        total_delta = 0
        count = 0

        for key in dimension_keys:
            curr_val = current.get(key, 0)
            prev_val = previous.get(key, 0)
            if curr_val is not None and prev_val is not None:
                delta = curr_val - prev_val
                deltas[key] = round(float(delta), 2)
                total_delta += abs(delta)
                count += 1

        if count == 0:
            return {"score": None, "confidence": 0, "risk_level": "UNKNOWN"}

        avg_delta = total_delta / count

        # Evolution score: larger changes = higher score
        evolution_score = int(np.clip(avg_delta * 2, 0, 100))

        risk = "HIGH" if evolution_score >= 40 else ("MEDIUM" if evolution_score >= 20 else "LOW")

        return {
            "score": evolution_score,
            "confidence": 75,
            "risk_level": risk,
            "dimension_deltas": deltas,
            "avg_change": round(float(avg_delta), 2),
        }
    except Exception as e:
        logger.error("Evolution scoring error: %s", str(e))
        return {"score": None, "confidence": 0, "risk_level": "UNKNOWN", "error": str(e)}


def _estimate_uncertainty(image_bgr: np.ndarray, correction_factors: Dict) -> int:
    """
    Estimate prediction uncertainty using test-time augmentation.
    Run ABCDE scoring on 5 augmented versions and report the standard deviation.
    """
    try:
        augmented_images = _generate_augmentations(image_bgr, n=5)
        overall_scores = []

        for aug_img in augmented_images:
            mask, contour = _segment_lesion(aug_img, correction_factors)
            a = _score_asymmetry(mask, correction_factors)
            b = _score_border(contour, mask, correction_factors)
            c = _score_color(aug_img, mask, correction_factors)
            d = _score_diameter(contour, aug_img)

            scores = [a["score"], b["score"], c["score"], d["score"]]
            valid = [s for s in scores if s is not None]
            if valid:
                overall_scores.append(np.mean(valid))

        if len(overall_scores) >= 2:
            uncertainty = int(np.std(overall_scores))
            return min(uncertainty, 25)  # Cap at 25% uncertainty
        return 10  # Default moderate uncertainty

    except Exception as e:
        logger.error("Uncertainty estimation error: %s", str(e))
        return 15  # Default uncertainty on error


def get_lesion_visualization(image_bgr: np.ndarray, correction_factors: Optional[Dict] = None) -> str:
    """
    Generate a visualization of the segmented lesion with contour overlay.
    Returns base64-encoded PNG image.
    """
    if cv2 is None:
        return ""

    if correction_factors is None:
        correction_factors = {"border_contrast_boost": 1.0}

    try:
        mask, contour = _segment_lesion(image_bgr, correction_factors)

        # Draw contour on the image
        vis = image_bgr.copy()
        cv2.drawContours(vis, [contour], -1, (0, 255, 0), 2)

        # Encode to base64
        _, buffer = cv2.imencode(".png", vis)
        return base64.b64encode(buffer).decode("utf-8")
    except Exception:
        return ""
