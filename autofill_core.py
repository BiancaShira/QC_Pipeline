"""
autofill_core.py
----------------
Auto-Fill stage: fill small black damage patches with solid white.
Uses PIL for robust image reading, converts to numpy for OpenCV processing.
"""
import cv2
import numpy as np
import shutil
import tempfile
from pathlib import Path
from PIL import Image
import logging
import os

from cropping_core import BACKUP_DIR_NAME, IMAGE_EXTS, list_images, make_thumb_b64

logger = logging.getLogger("qcc_autocrop")

DARK_THRESHOLD = 45
MAX_BLOB_AREA_RATIO = 0.35
FILL_COLOR = (255,255,255)  # white in BGR


def fill_damage(image_path, output_path, dark_threshold=DARK_THRESHOLD,
                max_blob_ratio=MAX_BLOB_AREA_RATIO):
    """
    Apply white fill to black damage blobs.
    Uses PIL to read the image, then cv2 for processing.
    Returns (success, status, detail) where status is 'filled', 'unchanged', or 'error'.
    """
    try:
        # Open with PIL to support all formats
        with Image.open(image_path) as pil_img:
            # Get DPI early
            dpi = pil_img.info.get('dpi', (300, 300))
            # Convert to RGB (or keep as is) and get numpy array
            frame = pil_img.convert('RGB')
            img_np = np.array(frame)  # shape (h,w,3) RGB
            # Convert RGB to BGR for OpenCV
            img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
            orig_h, orig_w = img_bgr.shape[:2]

            # ---- damage detection ----
            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            _, dark_mask = cv2.threshold(gray, dark_threshold, 255, cv2.THRESH_BINARY_INV)

            n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(dark_mask, connectivity=8)
            safe_mask = np.zeros_like(dark_mask)
            kept = 0

            for i in range(1, n_labels):
                area = stats[i, cv2.CC_STAT_AREA]
                if area / (orig_w * orig_h) > max_blob_ratio:
                    continue
                safe_mask[labels == i] = 255
                kept += 1

            if kept == 0:
                # No damage: copy original unchanged
                pil_img.save(output_path, dpi=dpi, quality=95)
                return True, "unchanged", "no fillable blobs"

            # Dilate mask
            safe_mask = cv2.dilate(safe_mask, np.ones((2, 2), np.uint8), iterations=1)

            # Apply fill
            result_bgr = img_bgr.copy()
            result_bgr[safe_mask == 255] = FILL_COLOR

            # Convert back to RGB PIL image
            result_rgb = cv2.cvtColor(result_bgr, cv2.COLOR_BGR2RGB)
            result_pil = Image.fromarray(result_rgb)
            result_pil.save(output_path, dpi=dpi, quality=95)

            return True, "filled", f"filled {kept} blob(s)"
    except Exception as e:
        return False, "error", str(e)


def generate_preview(batch_dir, threshold=DARK_THRESHOLD, sample_size=6):
    """Read‑only preview: returns before/after thumbnails for a sample."""
    batch_dir = Path(batch_dir)
    images = list_images(batch_dir)
    sample = images[:max(0, sample_size)]
    previews = []

    # Create a temporary directory for output files (inside batch dir to avoid permission issues)
    temp_dir = batch_dir / "__autofill_temp__"
    temp_dir.mkdir(exist_ok=True)

    try:
        for p in sample:
            try:
                # Get the "before" thumbnail directly from PIL
                with Image.open(p) as im:
                    im.seek(0)
                    frame = im.convert('RGB') if im.mode != 'RGB' else im.copy()
                    before_b64 = make_thumb_b64(frame)

                # Output file inside temp dir with same name but .jpg extension
                out_path = temp_dir / (p.stem + ".jpg")
                success, status, detail = fill_damage(str(p), str(out_path), threshold)
                if not success:
                    previews.append({'filename': p.name, 'status': 'error', 'error': detail})
                    continue
                if out_path.exists():
                    filled_img = Image.open(out_path)
                    after_b64 = make_thumb_b64(filled_img)
                    previews.append({
                        'filename': p.name,
                        'status': status,
                        'detail': detail,
                        'before': before_b64,
                        'after': after_b64,
                    })
                else:
                    previews.append({'filename': p.name, 'status': 'error', 'error': 'Output file not created'})
            except Exception as e:
                previews.append({'filename': p.name, 'status': 'error', 'error': str(e)})
    finally:
        # Clean up temp directory
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    return {
        'batch_directory': str(batch_dir),
        'total_images': len(images),
        'sample_count': len(sample),
        'previews': previews,
    }

import re

_SUFFIX_RE = re.compile(r'_1$')


def _canonical_and_output_paths(batch_dir, img_path):
    """Given a discovered image path (which may already carry the '_1'
    "processed" suffix from an earlier run or an earlier pipeline stage),
    work out:

      - rel:            path relative to batch_dir, as found
      - canonical_rel:  the image's TRUE identity, with any existing '_1'
                         suffix stripped off. This is what we key the
                         backup off of, so re-runs and chained pipeline
                         stages (rotate -> crop -> autofill) always back up
                         / detect the same true original, no matter how
                         many times it's already been processed.
      - output_path:    where the processed result gets written -- always
                         canonical name + '_1', so re-processing overwrites
                         that same file in place instead of stacking
                         suffixes (page003_1_1_1.jpg).
    """
    rel = img_path.relative_to(batch_dir)
    canonical_stem = _SUFFIX_RE.sub('', img_path.stem)
    canonical_rel = rel.with_name(canonical_stem + rel.suffix)
    output_path = batch_dir / rel.with_name(canonical_stem + '_1' + rel.suffix)
    return rel, canonical_rel, output_path


def process_batch_with_backup(batch_dir, threshold=DARK_THRESHOLD,
                              progress_cb=None, cancel_check=None):
    """Backup then fill damage for each image in batch_dir."""
    batch_dir = Path(batch_dir)
    backup_dir = batch_dir / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    image_files = list_images(batch_dir)

    # De-dupe: if both a plain original and its already-processed "_1"
    # sibling somehow both exist in the folder, prefer the "_1" version --
    # it reflects the latest processed state -- so we don't process the
    # same logical image twice in one run.
    by_canonical = {}
    for p in image_files:
        canonical_stem = _SUFFIX_RE.sub('', p.stem)
        key = str(p.relative_to(batch_dir).with_name(canonical_stem + p.suffix))
        if key not in by_canonical or p.stem.endswith('_1'):
            by_canonical[key] = p
    image_files = list(by_canonical.values())
    total = len(image_files)

    stats = {
        'total_images': total,
        'moved_to_backup': 0,
        'already_backed_up': 0,
        'filled_success': 0,
        'filled_unchanged': 0,
        'filled_failed': 0,
        'backup_dir': str(backup_dir),
        'errors': [],
    }

    for idx, img_path in enumerate(image_files, start=1):
        if cancel_check and cancel_check():
            stats['cancelled_at'] = idx
            break

        rel, canonical_rel, output_path = _canonical_and_output_paths(batch_dir, img_path)
        backup_path = backup_dir / canonical_rel
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if backup_path.exists():
                stats['already_backed_up'] += 1
                source_path = img_path      # already-processed "_1" file (re-run / chained stage)
            else:
                shutil.move(str(img_path), str(backup_path))
                stats['moved_to_backup'] += 1
                source_path = backup_path   # first time: pristine original just moved here

            success, status, detail = fill_damage(str(source_path), str(output_path), threshold)
            if not success:
                stats['filled_failed'] += 1
                stats['errors'].append(f"{rel}: {detail}")
            elif status == 'unchanged':
                stats['filled_unchanged'] += 1
            else:
                stats['filled_success'] += 1
        except Exception as e:
            stats['filled_failed'] += 1
            stats['errors'].append(f"{rel}: {e}")

        if progress_cb:
            progress_cb(idx, total, str(rel))

    return stats