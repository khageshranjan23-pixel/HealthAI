"""
Dr.SushamHealthAI — services/skin_tone_detector.py
Automatic Fitzpatrick Skin Tone Detection using Individual Typology Angle (ITA).

Uses L*a*b* colour-space analysis on auto-detected skin pixels to compute the
ITA (arctan((L-50)/b) × 180/π) and maps it to Fitzpatrick scale I–VI.

References:
  - Chardon et al., "Skin Colour Typology and Suntanning Pathways", 1991
  - Kinyanjui et al., "Fairness of Classifiers Across Skin Tones", MICCAI 2020

Zero external API calls — pure OpenCV + NumPy image processing.
"""

import io
import base64
import numpy as np
from typing import Dict, Optional, Tuple

from app.core.logging_config import logger

try:
    import cv2
except ImportError:
    cv2 = None
    logger.warning("OpenCV not installed — skin tone detection unavailable")


# ═══════════════════════════════════════════════════════════════
# ITA → FITZPATRICK MAPPING (from published dermatology literature)
# ═══════════════════════════════════════════════════════════════
# ITA thresholds sourced from Chardon et al. 1991 + Kinyanjui MICCAI 2020
FITZPATRICK_ITA_MAP = [
    # (min_ita, max_ita, type, label, description, hex_representative)
    (55,  90,  "I",   "Very Light",   "Always burns, never tans. Celtic type.",           "#FDEBD3"),
    (41,  55,  "II",  "Light",        "Usually burns, tans minimally. Nordic type.",       "#F5D0A9"),
    (28,  41,  "III", "Medium Light",  "Sometimes burns, gradually tans. European type.",  "#E8BA8B"),
    (10,  28,  "IV",  "Medium Dark",   "Rarely burns, tans easily. Mediterranean type.",  "#C49A6C"),
    (-30, 10,  "V",   "Dark",          "Very rarely burns, tans deeply. South Asian.",    "#8D6E4C"),
    (-90, -30, "VI",  "Very Dark",     "Never burns. Deeply pigmented. African type.",    "#4A312C"),
]


def _detect_skin_pixels(image_bgr: np.ndarray) -> np.ndarray:
    """
    Dynamically detect skin-region pixels using adaptive HSV + YCrCb dual-space
    thresholding. Returns a binary mask of skin pixels.

    The thresholds are NOT hardcoded to a single skin tone — they use the broad
    physiological range of human skin across all ethnicities.
    """
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    ycrcb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2YCrCb)

    # HSV range: covers all human skin tones from very light to very dark
    # Hue 0-25 covers reddish-yellowish tones (skin across all Fitzpatrick types)
    # Saturation 20-255 eliminates pure white/grey areas
    # Value 30-255 eliminates very dark shadows
    hsv_mask = cv2.inRange(hsv, np.array([0, 20, 30]), np.array([25, 255, 255]))

    # Extend hue range to include some pinkish tones
    hsv_mask2 = cv2.inRange(hsv, np.array([160, 20, 30]), np.array([180, 255, 255]))
    hsv_mask = cv2.bitwise_or(hsv_mask, hsv_mask2)

    # YCrCb range: well-established for skin detection across tones
    # Cr (red chroma) 133-173, Cb (blue chroma) 77-127
    ycrcb_mask = cv2.inRange(ycrcb, np.array([0, 133, 77]), np.array([255, 173, 127]))

    # Intersection of both colour spaces for robustness
    combined = cv2.bitwise_and(hsv_mask, ycrcb_mask)

    # Morphological cleanup: remove noise, fill small gaps
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    combined = cv2.morphologyEx(combined, cv2.MORPH_CLOSE, kernel, iterations=2)
    combined = cv2.morphologyEx(combined, cv2.MORPH_OPEN, kernel, iterations=1)

    return combined


def _compute_ita(image_bgr: np.ndarray, skin_mask: np.ndarray) -> Tuple[float, float]:
    """
    Compute Individual Typology Angle (ITA) from skin pixels.

    ITA = arctan((L* - 50) / b*) × (180 / π)

    Where L* and b* are from the CIE L*a*b* colour space.
    Higher ITA = lighter skin; lower ITA = darker skin.

    Returns: (mean_ita, std_ita) for the skin region.
    """
    lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2Lab)

    # Extract skin pixels only
    skin_pixels = lab[skin_mask > 0]

    if len(skin_pixels) < 100:
        # Too few skin pixels — return neutral
        return 25.0, 15.0

    L_vals = skin_pixels[:, 0].astype(np.float64) * 100.0 / 255.0  # Scale to 0–100
    b_vals = skin_pixels[:, 2].astype(np.float64) - 128.0           # Centre around 0

    # Avoid division by zero
    b_safe = np.where(np.abs(b_vals) < 0.5, 0.5, b_vals)

    ita_vals = np.degrees(np.arctan2(L_vals - 50.0, b_safe))

    # Robust statistics: use trimmed mean (remove outliers)
    q5, q95 = np.percentile(ita_vals, [5, 95])
    trimmed = ita_vals[(ita_vals >= q5) & (ita_vals <= q95)]

    if len(trimmed) < 50:
        trimmed = ita_vals

    mean_ita = float(np.mean(trimmed))
    std_ita = float(np.std(trimmed))

    return mean_ita, std_ita


def _ita_to_fitzpatrick(ita: float) -> Dict:
    """Map ITA value to Fitzpatrick type dynamically."""
    for min_ita, max_ita, ftype, label, description, hex_color in FITZPATRICK_ITA_MAP:
        if min_ita <= ita < max_ita:
            # Compute confidence: higher when ITA is near the centre of the range
            range_width = max_ita - min_ita
            centre = (min_ita + max_ita) / 2.0
            distance_from_centre = abs(ita - centre) / (range_width / 2.0)
            confidence = max(0.5, 1.0 - distance_from_centre * 0.5)

            return {
                "type": ftype,
                "label": label,
                "description": description,
                "hex_color": hex_color,
                "confidence": round(confidence, 3),
            }

    # Edge case: ITA outside known range
    if ita >= 90:
        return {**FITZPATRICK_ITA_MAP[0][2:], "type": "I", "label": "Very Light",
                "description": FITZPATRICK_ITA_MAP[0][4],
                "hex_color": FITZPATRICK_ITA_MAP[0][5], "confidence": 0.5}
    else:
        return {"type": "VI", "label": "Very Dark",
                "description": FITZPATRICK_ITA_MAP[5][4],
                "hex_color": FITZPATRICK_ITA_MAP[5][5], "confidence": 0.5}


def detect_skin_tone(image_bgr: np.ndarray) -> Dict:
    """
    Full skin tone detection pipeline.

    Args:
        image_bgr: OpenCV BGR image (numpy array)

    Returns:
        Dict with fitzpatrick type, ITA value, confidence, skin pixel coverage,
        and colour correction parameters for downstream ABCDE analysis.
    """
    if cv2 is None:
        return {"error": "OpenCV not available", "type": "Unknown", "confidence": 0.0}

    try:
        h, w = image_bgr.shape[:2]
        total_pixels = h * w

        # 1. Detect skin pixels
        skin_mask = _detect_skin_pixels(image_bgr)
        skin_pixel_count = int(np.sum(skin_mask > 0))
        skin_coverage = skin_pixel_count / total_pixels

        if skin_coverage < 0.02:
            logger.warning("Skin tone detection: very low skin pixel coverage (%.1f%%)",
                           skin_coverage * 100)
            return {
                "type": "Unknown",
                "label": "Insufficient skin pixels",
                "confidence": 0.0,
                "ita": 0.0,
                "ita_std": 0.0,
                "skin_coverage": round(skin_coverage, 4),
                "hex_color": "#888888",
                "correction_factors": _get_default_correction(),
            }

        # 2. Compute ITA
        mean_ita, std_ita = _compute_ita(image_bgr, skin_mask)

        # 3. Map to Fitzpatrick
        fitz = _ita_to_fitzpatrick(mean_ita)

        # 4. Compute colour correction factors for ABCDE analysis
        correction_factors = _compute_correction_factors(mean_ita, fitz["type"])

        result = {
            **fitz,
            "ita": round(mean_ita, 2),
            "ita_std": round(std_ita, 2),
            "skin_coverage": round(skin_coverage, 4),
            "correction_factors": correction_factors,
        }

        logger.info("Skin tone detected: Fitzpatrick %s (%s), ITA=%.1f ± %.1f, coverage=%.1f%%",
                     fitz["type"], fitz["label"], mean_ita, std_ita, skin_coverage * 100)

        return result

    except Exception as e:
        logger.error("Skin tone detection error: %s", str(e))
        return {
            "type": "Unknown",
            "label": "Detection failed",
            "confidence": 0.0,
            "ita": 0.0,
            "ita_std": 0.0,
            "skin_coverage": 0.0,
            "hex_color": "#888888",
            "correction_factors": _get_default_correction(),
            "error": str(e),
        }


def _compute_correction_factors(ita: float, fitz_type: str) -> Dict:
    """
    Dynamically compute colour correction factors based on detected skin tone.

    These factors are used by the ABCDE scoring engine to adjust thresholds:
    - Darker skin naturally shows less visible redness → adjust redness threshold
    - Melanin-rich skin has different colour distributions → adjust colour clustering
    - Border detection on dark skin needs different contrast settings

    The corrections are computed as continuous functions of ITA, NOT as discrete
    per-type lookups, to avoid artificial boundaries.
    """
    # Normalize ITA to 0-1 range where 0 = darkest, 1 = lightest
    ita_normalized = np.clip((ita + 60) / 120.0, 0.0, 1.0)

    return {
        # Redness visibility decreases with melanin — scale up redness detection
        "redness_sensitivity_boost": round(1.0 + (1.0 - ita_normalized) * 0.8, 3),
        # Colour variation threshold: darker skin needs wider range to detect true variation
        "color_variation_threshold_scale": round(1.0 + (1.0 - ita_normalized) * 0.5, 3),
        # Border contrast enhancement: need more for darker skin
        "border_contrast_boost": round(1.0 + (1.0 - ita_normalized) * 0.4, 3),
        # Luminance adjustment for asymmetry computation
        "luminance_offset": round((1.0 - ita_normalized) * 15.0, 2),
        # Dark skin correction applied flag
        "dark_skin_correction_applied": ita_normalized < 0.5,
        "ita_normalized": round(float(ita_normalized), 4),
    }


def _get_default_correction() -> Dict:
    """Default correction factors when skin tone cannot be determined."""
    return {
        "redness_sensitivity_boost": 1.0,
        "color_variation_threshold_scale": 1.0,
        "border_contrast_boost": 1.0,
        "luminance_offset": 0.0,
        "dark_skin_correction_applied": False,
        "ita_normalized": 0.5,
    }
