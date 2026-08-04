"""
ollama_selective_rotate.py
---------------------------
Prompt-driven, selective rotation via a local/cloud Ollama vision model
(e.g. qwen2.5vl). This is deliberately a SEPARATE module from the existing
per-page orientation fallback in orientation_core.py (`ollama_classify_angle`)
-- that one runs on every page during the normal Rotation stage when the
EfficientNet classifier is unsure. This one is an optional, on-demand step
you run *after* rotation, driven by a free-text prompt, e.g.:

    "identify all pages that contain a table and rotate them upright"
    "find pages with a mostly green image"

How it's meant to fit the pipeline (backend only in this pass -- no UI yet,
per your "python code first, UI later" note):

  1. select_batch(...)  -- read-only. Runs every page in a batch through the
     model with your prompt, returns which pages matched + the thumbnail +
     the model's suggested rotation. Nothing on disk changes.
  2. apply_selection(...) -- takes the *same* prompt (or an explicit list of
     already-selected filenames from step 1) and only rotates the pages that
     matched. Non-matching pages are left completely untouched. Every
     touched file is copied into a `.selective_backup/` folder first, so
     this is safe to try and revert.

Both functions are also usable independently, e.g. you could later add a
prompt like "find all pages with green color" purely for a search/preview
use case with no rotation step at all -- just call select_batch() and
ignore the rotation part of the response.
"""
import base64
import json
import logging
import re
import shutil
import time
from pathlib import Path

import requests
from PIL import Image

from cropping_core import list_images, make_thumb_b64

logger = logging.getLogger("qcc_autocrop")

SELECTIVE_BACKUP_DIR_NAME = ".selective_backup"

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _encode_image_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _build_prompt(user_prompt):
    return (
        "You are reviewing one page of a scanned document batch.\n"
        f"Selection criteria (from the user): \"{user_prompt}\"\n\n"
        "Answer with ONLY a single JSON object, no other text, no markdown "
        "fences, in exactly this shape:\n"
        '{"matches": true or false, '
        '"rotation_degrees_cw": 0, 90, 180, or 270, '
        '"reason": "short phrase"}\n\n'
        "\"matches\" is whether this page satisfies the selection criteria "
        "above. \"rotation_degrees_cw\" is how many degrees CLOCKWISE this "
        "page would need to be rotated to be upright and correctly "
        "oriented -- set it even if matches is false, judged purely from "
        "the visible text/table orientation. If you cannot tell, use 0."
    )


def classify_page_for_prompt(image_path, user_prompt, base_url, model, timeout=60):
    """
    Runs one page through the vision model against a free-text selection
    prompt. Returns dict: {matches, rotation_degrees_cw, reason} or None on
    failure (never raises -- callers should treat None as "skip this page").
    """
    try:
        b64 = _encode_image_b64(image_path)
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/generate",
            json={
                "model": model,
                "prompt": _build_prompt(user_prompt),
                "images": [b64],
                "stream": False,
                "format": "json",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "")
        match = _JSON_RE.search(text)
        raw = match.group(0) if match else text
        data = json.loads(raw)
        degrees = int(data.get("rotation_degrees_cw", 0)) % 360
        if degrees not in (0, 90, 180, 270):
            degrees = 0
        return {
            "matches": bool(data.get("matches", False)),
            "rotation_degrees_cw": degrees,
            "reason": str(data.get("reason", ""))[:200],
        }
    except Exception as e:
        logger.warning(f"Selective-rotation check failed for {image_path}: {e}")
        return None


def select_batch(batch_dir, user_prompt, base_url, model, sample_size=None,
                 timeout=60, progress_cb=None, cancel_check=None):
    """
    Read-only preview. Runs every page (or up to `sample_size` if given)
    through classify_page_for_prompt(). Nothing is written to disk.

    Returns:
      {
        "prompt": user_prompt,
        "total_checked": int,
        "matched": [ {filename, reason, rotation_degrees_cw, thumb_b64}, ... ],
      }
    """
    images = [p for p in list_images(Path(batch_dir))
              if SELECTIVE_BACKUP_DIR_NAME not in p.parts]
    if sample_size:
        images = images[:sample_size]

    matched = []
    for i, img_path in enumerate(images):
        if cancel_check and cancel_check():
            break
        if progress_cb:
            progress_cb(i, len(images), img_path.name)
        result = classify_page_for_prompt(img_path, user_prompt, base_url, model, timeout=timeout)
        if result and result["matches"]:
            try:
                with Image.open(img_path) as im:
                    thumb = make_thumb_b64(im)
            except Exception:
                thumb = None
            matched.append({
                "filename": img_path.name,
                "reason": result["reason"],
                "rotation_degrees_cw": result["rotation_degrees_cw"],
                "thumb_b64": thumb,
            })

    return {
        "prompt": user_prompt,
        "total_checked": len(images),
        "matched": matched,
    }


def apply_selection(batch_dir, matched_filenames_with_rotation, progress_cb=None, cancel_check=None):
    """
    Rotates only the given pages. `matched_filenames_with_rotation` is the
    list of dicts as returned in select_batch()['matched'] (filename +
    rotation_degrees_cw) -- pass it straight through after the user reviews
    the preview, so nothing is re-classified and nothing un-selected moves.

    Every touched file is copied to <batch_dir>/.selective_backup/ before
    being overwritten, so a run can be undone by restoring from there.

    Returns: {"rotated": int, "skipped_zero_rotation": int, "failed": int, "errors": [...]}
    """
    batch_dir = Path(batch_dir)
    backup_dir = batch_dir / SELECTIVE_BACKUP_DIR_NAME
    stats = {"rotated": 0, "skipped_zero_rotation": 0, "failed": 0, "errors": []}

    total = len(matched_filenames_with_rotation)
    for i, item in enumerate(matched_filenames_with_rotation):
        if cancel_check and cancel_check():
            break
        filename = item["filename"]
        degrees_cw = int(item.get("rotation_degrees_cw", 0)) % 360
        if progress_cb:
            progress_cb(i, total, filename)

        if degrees_cw == 0:
            stats["skipped_zero_rotation"] += 1
            continue

        img_path = batch_dir / filename
        if not img_path.exists():
            stats["failed"] += 1
            stats["errors"].append(f"{filename}: file not found")
            continue

        try:
            backup_dir.mkdir(exist_ok=True)
            backup_path = backup_dir / filename
            if not backup_path.exists():
                shutil.copy2(img_path, backup_path)

            with Image.open(img_path) as im:
                # PIL rotates counter-clockwise for positive angles.
                pil_angle = -degrees_cw
                rotated = im.rotate(pil_angle, expand=True)
                rotated.save(img_path)

            stats["rotated"] += 1
        except Exception as e:
            stats["failed"] += 1
            stats["errors"].append(f"{filename}: {e}")

    return stats