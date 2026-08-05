"""
orientation_core.py
--------------------
Auto-rotation engine with optional deskew.
"""
import base64
import io
import logging
import re
import shutil
import time
from pathlib import Path

import cv2
import numpy as np
import requests
import torch
import torch.nn as nn
from PIL import Image, ImageOps
from torchvision import models, transforms

from cropping_core import BACKUP_DIR_NAME, IMAGE_EXTS, list_images, make_thumb_b64

logger = logging.getLogger("qcc_autocrop")

IMAGE_SIZE = 512
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

CORRECTION_ANGLE = {0: 0, 1: -90, 2: -180, 3: -270}

_TTA_TRANSFORMS = [
    transforms.Compose([
        transforms.Resize(IMAGE_SIZE), transforms.CenterCrop(IMAGE_SIZE),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ]),
    transforms.Compose([
        transforms.Resize(IMAGE_SIZE), transforms.CenterCrop(IMAGE_SIZE),
        transforms.RandomHorizontalFlip(p=1.0), transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ]),
    transforms.Compose([
        transforms.Resize(IMAGE_SIZE), transforms.CenterCrop(IMAGE_SIZE),
        transforms.RandomVerticalFlip(p=1.0), transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ]),
]

# ---------------------------------------------------------------------------
# Model loading / caching
# ---------------------------------------------------------------------------

_MODEL_CACHE = {}

def _create_model(num_classes=4):
    model = models.efficientnet_b0(weights=None)
    model.classifier[1] = nn.Linear(model.classifier[1].in_features, num_classes)
    return model

def _load_ensemble(model_paths):
    key = tuple(sorted(model_paths))
    if key in _MODEL_CACHE:
        return _MODEL_CACHE[key]
    ensemble = []
    for path in model_paths:
        if not path or not Path(path).exists():
            raise FileNotFoundError(f"Orientation model checkpoint not found: {path}")
        model = _create_model()
        model.load_state_dict(torch.load(path, map_location=DEVICE))
        model.to(DEVICE)
        model.eval()
        ensemble.append(model)
    _MODEL_CACHE[key] = ensemble
    return ensemble

def clear_model_cache():
    _MODEL_CACHE.clear()

def pick_profile(profiles, document_type):
    doc = (document_type or "").strip().lower()
    default = None
    for p in profiles:
        m = (p.get("match") or "").strip().lower()
        if not m:
            default = default or p
            continue
        if m in doc:
            return p
    return default or (profiles[0] if profiles else None)


# ---------------------------------------------------------------------------
# Ollama fallback
# ---------------------------------------------------------------------------

_ANGLE_WORD_RE = re.compile(r"(0|90|180|270)")

def ollama_classify_angle(image_path, base_url, model, timeout=45):
    try:
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        prompt = (
            "You are reviewing a scanned document page. Looking at the text and "
            "layout, how many degrees CLOCKWISE must this image be rotated so the "
            "text reads normally, upright, left-to-right? Answer with only one "
            "number: 0, 90, 180, or 270. No other words."
        )
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={"model": model, "prompt": prompt, "images": [b64], "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "")
        match = _ANGLE_WORD_RE.search(text)
        if not match:
            return None
        degrees_cw = int(match.group(1))
        return degrees_cw % 360
    except Exception as e:
        logger.warning(f"Ollama orientation check failed for {image_path}: {e}")
        return None

def _cw_degrees_to_pil_angle(degrees_cw):
    return -(degrees_cw % 360)


# ---------------------------------------------------------------------------
# Core classify+rotate for a single frame
# ---------------------------------------------------------------------------

def classify_frame(rgb_frame, ensemble):
    probs_sum = torch.zeros(4, device=DEVICE)
    n_passes = 0
    with torch.no_grad():
        for tta in _TTA_TRANSFORMS:
            img_t = tta(rgb_frame).unsqueeze(0).to(DEVICE)
            for model in ensemble:
                out = torch.softmax(model(img_t), dim=1)
                probs_sum += out.squeeze(0)
                n_passes += 1
    probs = (probs_sum / max(n_passes, 1)).cpu().numpy()
    pred_class = int(np.argmax(probs))
    return pred_class, float(probs[pred_class]), probs.tolist()

def _rotate_frame(rgb_frame, pil_angle):
    if pil_angle == 0:
        return rgb_frame
    return rgb_frame.rotate(pil_angle, expand=True, resample=Image.BILINEAR)


# ---------------------------------------------------------------------------
# Deskew helpers (from rot_crop.py)
# ---------------------------------------------------------------------------

def estimate_skew_angle(gray, dark_thresh=60):
    bright_mask = (gray > dark_thresh).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
    bright_mask = cv2.morphologyEx(bright_mask, cv2.MORPH_CLOSE, kernel)
    bright_mask = cv2.morphologyEx(bright_mask, cv2.MORPH_OPEN, kernel)
    contours, _ = cv2.findContours(bright_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return 0.0
    largest = max(contours, key=cv2.contourArea)
    if cv2.contourArea(largest) < 0.01 * gray.shape[0] * gray.shape[1]:
        return 0.0
    angle = cv2.minAreaRect(largest)[-1]
    if angle < -45:
        angle = 90 + angle
    if angle > 45:
        angle = angle - 90
    return angle

def deskew_image(img_np, dark_thresh=60, border_color=(255,255,255)):
    gray = cv2.cvtColor(img_np, cv2.COLOR_BGR2GRAY)
    angle = estimate_skew_angle(gray, dark_thresh)
    if abs(angle) < 0.05:
        return img_np, 0.0
    h, w = img_np.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    cos = abs(M[0, 0])
    sin = abs(M[0, 1])
    new_w = int((h * sin) + (w * cos))
    new_h = int((h * cos) + (w * sin))
    M[0, 2] += (new_w / 2) - center[0]
    M[1, 2] += (new_h / 2) - center[1]
    rotated = cv2.warpAffine(img_np, M, (new_w, new_h),
                             flags=cv2.INTER_CUBIC,
                             borderMode=cv2.BORDER_CONSTANT,
                             borderValue=border_color)
    return rotated, angle


# ---------------------------------------------------------------------------
# Process single image with rotation + optional deskew
# ---------------------------------------------------------------------------

def process_image(image_path, output_path, ensemble, ollama_cfg=None, deskew=False):
    try:
        with Image.open(image_path) as img_pil:
            n_frames = getattr(img_pil, "n_frames", 1)
            default_dpi = img_pil.info.get('dpi', (300, 300))

            out_frames = []
            any_rotated = False
            detail_bits = []

            for frame_idx in range(n_frames):
                img_pil.seek(frame_idx)
                frame = ImageOps.exif_transpose(img_pil).convert("RGB")

                # Coarse orientation
                pred_class, confidence, _probs = classify_frame(frame, ensemble)
                source = "model"
                use_ollama = ollama_cfg and ollama_cfg.get("enabled")
                if use_ollama:
                    trigger = ollama_cfg.get("trigger", "low_confidence")
                    should_ask = (
                        trigger == "always"
                        or (trigger == "low_confidence" and confidence < ollama_cfg.get("confidence_threshold", 0.55))
                    )
                    if should_ask:
                        degrees_cw = ollama_classify_angle(
                            image_path, ollama_cfg.get("base_url"), ollama_cfg.get("model"),
                            timeout=ollama_cfg.get("timeout_seconds", 45),
                        )
                        if degrees_cw is not None:
                            pil_angle = _cw_degrees_to_pil_angle(degrees_cw)
                            source = "ollama"
                        else:
                            pil_angle = CORRECTION_ANGLE[pred_class]
                    else:
                        pil_angle = CORRECTION_ANGLE[pred_class]
                else:
                    pil_angle = CORRECTION_ANGLE[pred_class]

                rotated = _rotate_frame(frame, pil_angle)
                if pil_angle != 0:
                    any_rotated = True
                    detail_bits.append(f"page {frame_idx+1}: {pil_angle}° ({source}, conf {confidence:.2f})")

                # Optional deskew
                if deskew:
                    rot_np = np.array(rotated)
                    deskewed_np, angle = deskew_image(rot_np)
                    if abs(angle) > 0.05:
                        any_rotated = True
                        detail_bits.append(f"page {frame_idx+1}: deskewed by {angle:.1f}°")
                        rotated = Image.fromarray(cv2.cvtColor(deskewed_np, cv2.COLOR_BGR2RGB))

                out_frames.append(rotated)

            first, rest = out_frames[0], out_frames[1:]
            if rest:
                first.save(output_path, dpi=default_dpi, quality=95, save_all=True, append_images=rest)
            else:
                first.save(output_path, dpi=default_dpi, quality=95)

            if any_rotated:
                return True, "rotated", "; ".join(detail_bits)
            return True, "unchanged", f"no correction needed on any of {n_frames} page(s)"
    except Exception as e:
        return False, "error", f"Error processing {image_path}: {e}"


# ---------------------------------------------------------------------------
# Preview (read-only)
# ---------------------------------------------------------------------------

def generate_preview(batch_dir, model_paths, sample_size=6, ollama_cfg=None, deskew=False):
    batch_dir = Path(batch_dir)
    ensemble = _load_ensemble(model_paths)
    images = list_images(batch_dir)
    sample = images[:max(0, sample_size)]
    previews = []

    for p in sample:
        try:
            with Image.open(p) as img:
                img.seek(0)
                frame = ImageOps.exif_transpose(img).convert("RGB")

                pred_class, confidence, _probs = classify_frame(frame, ensemble)
                source = "model"
                pil_angle = CORRECTION_ANGLE[pred_class]

                if ollama_cfg and ollama_cfg.get("enabled"):
                    trigger = ollama_cfg.get("trigger", "low_confidence")
                    should_ask = (
                        trigger == "always"
                        or (trigger == "low_confidence" and confidence < ollama_cfg.get("confidence_threshold", 0.55))
                    )
                    if should_ask:
                        degrees_cw = ollama_classify_angle(
                            str(p), ollama_cfg.get("base_url"), ollama_cfg.get("model"),
                            timeout=ollama_cfg.get("timeout_seconds", 45),
                        )
                        if degrees_cw is not None:
                            pil_angle = _cw_degrees_to_pil_angle(degrees_cw)
                            source = "ollama"

                rotated = _rotate_frame(frame, pil_angle)

                # Deskew
                if deskew:
                    rot_np = np.array(rotated)
                    deskewed_np, angle = deskew_image(rot_np)
                    if abs(angle) > 0.05:
                        rotated = Image.fromarray(cv2.cvtColor(deskewed_np, cv2.COLOR_BGR2RGB))
                        source = f"{source}+deskew"

                status = "rotated" if pil_angle != 0 or (deskew and abs(angle) > 0.05) else "unchanged"
                previews.append({
                    'filename': p.name,
                    'status': status,
                    'angle': pil_angle,
                    'confidence': round(confidence, 3),
                    'source': source,
                    'before': make_thumb_b64(frame),
                    'after': make_thumb_b64(rotated),
                })
        except Exception as e:
            previews.append({'filename': p.name, 'status': 'error', 'error': str(e)})

    return {
        'batch_directory': str(batch_dir),
        'total_images': len(images),
        'sample_count': len(sample),
        'previews': previews,
    }


# ---------------------------------------------------------------------------
# Live run: backup-then-rotate
# ---------------------------------------------------------------------------
import shutil
from pathlib import Path
import re

_SUFFIX_RE = re.compile(r'_1$')
BACKUP_DIR_NAME = "QCCBackups"


def _canonical_and_output_paths(batch_dir, img_path):
    """
    Common naming convention used by all pipeline stages.

    Original:
        page001.jpg

    Backup:
        QCCBackups/page001.jpg

    Processed:
        page001_1.jpg

    If page001_1.jpg is encountered on a later pipeline stage or rerun,
    page001.jpg is treated as its canonical identity.
    """
    rel = img_path.relative_to(batch_dir)

    canonical_stem = _SUFFIX_RE.sub("", img_path.stem)
    canonical_rel = rel.with_name(canonical_stem + rel.suffix)

    backup_path = batch_dir / BACKUP_DIR_NAME / canonical_rel
    output_path = batch_dir / rel.with_name(
        canonical_stem + "_1" + rel.suffix
    )

    return rel, backup_path, output_path


def process_batch_with_backup(
    batch_dir,
    model_paths,
    ollama_cfg=None,
    progress_cb=None,
    cancel_check=None,
    deskew=False,
):
    batch_dir = Path(batch_dir)

    backup_dir = batch_dir / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    ensemble = _load_ensemble(model_paths)

    image_files = list_images(batch_dir)

    #
    # De-dupe.
    # If both page001.jpg and page001_1.jpg exist,
    # process only page001_1.jpg because it is the newest.
    #
    by_canonical = {}

    for p in image_files:
        canonical_stem = _SUFFIX_RE.sub("", p.stem)
        key = str(
            p.relative_to(batch_dir).with_name(
                canonical_stem + p.suffix
            )
        )

        if key not in by_canonical or p.stem.endswith("_1"):
            by_canonical[key] = p

    image_files = list(by_canonical.values())

    total = len(image_files)

    stats = {
        "total_images": total,
        "moved_to_backup": 0,
        "already_backed_up": 0,
        "rotated_success": 0,
        "rotated_unchanged": 0,
        "rotated_failed": 0,
        "ollama_assisted": 0,
        "backup_dir": str(backup_dir),
        "errors": [],
    }

    for idx, img_path in enumerate(image_files, start=1):

        if cancel_check and cancel_check():
            stats["cancelled_at"] = idx
            break

        rel, backup_path, output_path = _canonical_and_output_paths(
            batch_dir,
            img_path,
        )

        backup_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:

            #
            # First run:
            #
            # page001.jpg
            #      ↓
            # QCCBackups/page001.jpg
            #      ↓
            # write page001_1.jpg
            #
            if backup_path.exists():

                stats["already_backed_up"] += 1

                #
                # Re-run or next pipeline stage.
                #
                # Use the latest processed file if it exists.
                #
                if output_path.exists():
                    source_path = output_path
                else:
                    source_path = backup_path

            else:

                shutil.move(str(img_path), str(backup_path))
                stats["moved_to_backup"] += 1
                source_path = backup_path

            success, status, detail = process_image(
                str(source_path),
                str(output_path),
                ensemble,
                ollama_cfg,
                deskew=deskew,
            )

            if not success:
                stats["rotated_failed"] += 1
                stats["errors"].append(f"{rel}: {detail}")

            elif status == "unchanged":
                stats["rotated_unchanged"] += 1

            else:
                stats["rotated_success"] += 1

                if detail and "ollama" in detail:
                    stats["ollama_assisted"] += 1

        except Exception as e:
            stats["rotated_failed"] += 1
            stats["errors"].append(f"{rel}: {e}")

        if progress_cb:
            progress_cb(idx, total, str(rel))

    return stats