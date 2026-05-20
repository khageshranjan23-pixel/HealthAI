"""
Dr.SushamHealthAI — services/dermoscopy_simulator.py
Phone Camera → Simulated Dermoscopic View.

Implements a multi-stage image enhancement pipeline that simulates the visual
characteristics of a dermoscopic examination, making subsurface skin structures
visible from a regular phone photo.

Pipeline:
  1. Specular reflection removal (inpainting highlights)
  2. Subsurface structure enhancement (CLAHE on L-channel)
  3. Pigment network enhancement (morphological top-hat transform)
  4. Color normalization (adaptive histogram equalization per channel)
  5. Hair/artifact removal (morphological black-hat + inpainting)

All processing uses OpenCV — zero cost, clinically meaningful.

Reference: Celebi et al., "Dermoscopy Image Analysis: Overview and Future
Directions", IEEE J. Biomed Health Inform, 2019.
"""

import base64
import numpy as np
from typing import Dict, List, Tuple

from app.core.logging_config import logger

try:
    import cv2
except ImportError:
    cv2 = None
    logger.warning("OpenCV not installed — dermoscopy simulation unavailable")


def _remove_specular_reflections(image_bgr: np.ndarray) -> Tuple[np.ndarray, bool]:
    """
    Detect and remove specular (shiny) reflections that occur on skin
    photographed with flash or ambient light.

    Method: Threshold the V-channel of HSV to find bright spots, then
    inpaint them using surrounding pixel information.
    """
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    v_channel = hsv[:, :, 2]

    # Find very bright spots (top 2% brightest pixels)
    threshold = np.percentile(v_channel, 98)
    bright_mask = (v_channel > threshold).astype(np.uint8) * 255

    # Dilate slightly to cover edges of reflections
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    bright_mask = cv2.dilate(bright_mask, kernel, iterations=1)

    num_bright = np.sum(bright_mask > 0)
    total = bright_mask.size
    has_reflections = (num_bright / total) > 0.005  # More than 0.5% of image

    if has_reflections:
        result = cv2.inpaint(image_bgr, bright_mask, 7, cv2.INPAINT_TELEA)
        return result, True
    else:
        return image_bgr.copy(), False


def _remove_hair_artifacts(image_bgr: np.ndarray) -> Tuple[np.ndarray, bool]:
    """
    Detect and remove hair artifacts that obstruct the lesion view.

    Method: Black-hat morphological transform highlights dark thin structures
    (hair) against lighter skin. Then inpaint to fill.
    """
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)

    # Black-hat transform with elongated kernel to detect thin dark lines (hair)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (17, 17))
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)

    # Threshold the black-hat result
    _, hair_mask = cv2.threshold(blackhat, 15, 255, cv2.THRESH_BINARY)

    # Clean up small artifacts
    small_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    hair_mask = cv2.morphologyEx(hair_mask, cv2.MORPH_CLOSE, small_kernel)

    num_hair = np.sum(hair_mask > 0)
    total = hair_mask.size
    has_hair = (num_hair / total) > 0.01  # More than 1% hair coverage

    if has_hair:
        result = cv2.inpaint(image_bgr, hair_mask, 5, cv2.INPAINT_TELEA)
        return result, True
    else:
        return image_bgr.copy(), False


def _enhance_subsurface(image_bgr: np.ndarray) -> np.ndarray:
    """
    Enhance subsurface skin structures using CLAHE on the L-channel
    of L*a*b* colour space.

    This simulates what a dermoscope reveals by making the pigment patterns
    below the skin surface more visible.
    """
    lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2Lab)

    l_channel = lab[:, :, 0]

    # CLAHE: adaptive contrast enhancement
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    l_enhanced = clahe.apply(l_channel)

    lab[:, :, 0] = l_enhanced
    result = cv2.cvtColor(lab, cv2.COLOR_Lab2BGR)

    return result


def _enhance_pigment_network(image_bgr: np.ndarray) -> np.ndarray:
    """
    Enhance pigment network visibility using morphological top-hat transform.

    The pigment network (reticular pattern) is the most important dermoscopic
    feature. Top-hat transform isolates these fine structures.
    """
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)

    # White top-hat: reveals bright structures on dark background
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    tophat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)

    # Black top-hat (close - original): reveals dark structures on bright background
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel)

    # Enhanced: original + white_tophat - black_tophat
    enhanced = cv2.add(gray, tophat)
    enhanced = cv2.subtract(enhanced, blackhat)

    # Merge back to BGR
    result = image_bgr.copy()
    # Blend with original (50% enhanced + 50% original for natural look)
    gray_3ch = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2BGR)
    result = cv2.addWeighted(result, 0.5, gray_3ch, 0.5, 0)

    return result


def _normalize_colors(image_bgr: np.ndarray) -> np.ndarray:
    """
    Apply adaptive histogram equalization per channel to normalize
    lighting conditions and enhance colour differentiation.
    """
    # Convert to L*a*b* for perceptually uniform colour handling
    lab = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2Lab)

    channels = list(cv2.split(lab))

    # Equalize only the L channel (lightness) to preserve colour hues
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    channels[0] = clahe.apply(channels[0])

    # Slight boost to a and b channels for colour saturation
    for i in [1, 2]:
        ch = channels[i].astype(np.float32)
        # Stretch around the centre (128)
        ch = ((ch - 128) * 1.15 + 128)
        channels[i] = np.clip(ch, 0, 255).astype(np.uint8)

    lab = cv2.merge(channels)
    result = cv2.cvtColor(lab, cv2.COLOR_Lab2BGR)

    return result


def simulate_dermoscopy(image_bgr: np.ndarray) -> Dict:
    """
    Full dermoscopy simulation pipeline.

    Args:
        image_bgr: OpenCV BGR image from phone camera

    Returns:
        Dict with:
        - enhanced_image_b64: base64 PNG of the enhanced dermoscopic view
        - original_b64: base64 PNG of the original (for side-by-side)
        - enhancements_applied: list of applied enhancement steps
        - enhancement_details: per-step metadata
    """
    if cv2 is None:
        return {
            "error": "OpenCV not available",
            "enhanced_image_b64": "",
            "original_b64": "",
            "enhancements_applied": [],
        }

    try:
        enhancements = []
        details = {}

        # Original for comparison
        _, orig_buf = cv2.imencode(".png", image_bgr)
        original_b64 = base64.b64encode(orig_buf).decode("utf-8")

        result = image_bgr.copy()

        # Step 1: Remove specular reflections
        result, had_reflections = _remove_specular_reflections(result)
        if had_reflections:
            enhancements.append("Specular reflection removal")
            details["reflection_removal"] = "Applied — bright spots inpainted"

        # Step 2: Remove hair artifacts
        result, had_hair = _remove_hair_artifacts(result)
        if had_hair:
            enhancements.append("Hair/artifact removal")
            details["hair_removal"] = "Applied — dark thin structures removed"

        # Step 3: Enhance subsurface structures
        result = _enhance_subsurface(result)
        enhancements.append("Subsurface structure enhancement (CLAHE)")
        details["subsurface"] = "L-channel CLAHE applied"

        # Step 4: Enhance pigment network
        result = _enhance_pigment_network(result)
        enhancements.append("Pigment network enhancement")
        details["pigment_network"] = "Top-hat morphological transform applied"

        # Step 5: Normalize colours
        result = _normalize_colors(result)
        enhancements.append("Color normalization")
        details["color_normalization"] = "Adaptive histogram equalization"

        # Encode result
        _, result_buf = cv2.imencode(".png", result)
        enhanced_b64 = base64.b64encode(result_buf).decode("utf-8")

        logger.info("Dermoscopy simulation: %d enhancements applied", len(enhancements))

        return {
            "enhanced_image_b64": enhanced_b64,
            "original_b64": original_b64,
            "enhancements_applied": enhancements,
            "enhancement_details": details,
        }

    except Exception as e:
        logger.error("Dermoscopy simulation error: %s", str(e))
        return {
            "error": str(e),
            "enhanced_image_b64": "",
            "original_b64": "",
            "enhancements_applied": [],
        }
