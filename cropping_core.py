"""
cropping_core.py
-----------------
Reusable core logic for the QCC Auto-Crop tool.
"""
import base64
import io
import logging
import shutil
import time
from pathlib import Path
from utils.image_utils import list_images , count_images , BACKUP_DIR_NAME , IMAGE_EXTS
from db import *
import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger("qcc_autocrop")


# ---------------------------------------------------------------------------
# Core crop-detection logic
# ---------------------------------------------------------------------------

def _detect_crop_box(img_np, threshold):
    """Bounding box of the largest foreground contour, or None."""
    gray = cv2.cvtColor(img_np, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if not contours:
        return None

    largest_contour = max(contours, key=cv2.contourArea)
    x, y, w, h = cv2.boundingRect(largest_contour)

    if w < img_np.shape[1] * 0.4 or h < img_np.shape[0] * 0.4:
        return None

    return x, y, w, h


def _crop_frame(img_pil_frame, threshold):
    """Crop a single PIL frame. Returns (cropped_pil, status, orig_size)."""
    original_mode = img_pil_frame.mode
    rgb_frame = img_pil_frame.convert('RGB') if original_mode != 'RGB' else img_pil_frame.copy()

    img_np = cv2.cvtColor(np.array(rgb_frame), cv2.COLOR_RGB2BGR)
    orig_w, orig_h = rgb_frame.width, rgb_frame.height

    box = _detect_crop_box(img_np, threshold)

    if box is not None:
        x, y, w, h = box
        if (w, h) != (orig_w, orig_h):
            img_np = img_np[y:y + h, x:x + w]
            status = 'cropped'
        else:
            status = 'unchanged'
    else:
        status = 'unchanged'

    cropped_rgb = cv2.cvtColor(img_np, cv2.COLOR_BGR2RGB)
    cropped_pil = Image.fromarray(cropped_rgb)

    if original_mode in ['1', 'L', 'P', 'RGB', 'RGBA']:
        cropped_pil = cropped_pil.convert(original_mode)

    return cropped_pil, status, (orig_w, orig_h)


def remove_black_borders(image_path, output_path, threshold=100):
    """Crop image_path and save to output_path. Returns (success, message)."""
    try:
        with Image.open(image_path) as img_pil:
            n_frames = getattr(img_pil, "n_frames", 1)
            default_dpi = img_pil.info.get('dpi', (300, 300))

            cropped_frames, statuses, sizes = [], [], []

            for frame_idx in range(n_frames):
                img_pil.seek(frame_idx)
                cropped_pil, status, orig_size = _crop_frame(img_pil, threshold)
                cropped_frames.append(cropped_pil)
                statuses.append(status)
                sizes.append((orig_size, cropped_pil.size))

            first, rest = cropped_frames[0], cropped_frames[1:]
            if rest:
                first.save(output_path, dpi=default_dpi, quality=95, save_all=True, append_images=rest)
            else:
                first.save(output_path, dpi=default_dpi, quality=95)

            any_cropped = any(s == 'cropped' for s in statuses)
            if any_cropped:
                return True, None
            else:
                return True, f"UNCHANGED: no reliable border found on any of {n_frames} page(s)"

    except Exception as e:
        return False, f"Error processing {image_path}: {str(e)}"


def make_thumb_b64(pil_img, max_dim=380):
    """Downscale a PIL image and return it as a base64 JPEG string."""
    thumb = pil_img.copy()
    thumb.thumbnail((max_dim, max_dim))
    if thumb.mode not in ('RGB', 'L'):
        thumb = thumb.convert('RGB')
    buf = io.BytesIO()
    thumb.save(buf, format='JPEG', quality=82)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------





def discover_batches_from_folder(parent_folder):
    """Walk parent_folder/<box>/<batch> and return batch descriptors."""
    parent = Path(parent_folder)
    discovered = []

    if not parent.exists():
        raise FileNotFoundError(f"Folder does not exist: {parent_folder}")
    if not parent.is_dir():
        raise NotADirectoryError(f"Not a folder: {parent_folder}")

    box_dirs = sorted(p for p in parent.iterdir() if p.is_dir())
    if not box_dirs:
        return discovered

    for box_dir in box_dirs:
        batch_dirs = sorted(p for p in box_dir.iterdir() if p.is_dir())
        if batch_dirs:
            for batch_dir in batch_dirs:
                discovered.append({
                    'BatchID': None,
                    'BatchName': f"{box_dir.name}/{batch_dir.name}",
                    'BatchDirectory': str(batch_dir),
                    'ImageCount': count_images(batch_dir),
                })
        else:
            if count_images(box_dir) > 0:
                discovered.append({
                    'BatchID': None,
                    'BatchName': box_dir.name,
                    'BatchDirectory': str(box_dir),
                    'ImageCount': count_images(box_dir),
                })

    return discovered


def ensure_backup_folder(batch_dir):
    """Create QCCBackups under batch_dir. Skip (no-op) if it already exists."""
    backup_dir = Path(batch_dir) / BACKUP_DIR_NAME
    if backup_dir.exists():
        return {'created': False, 'path': str(backup_dir), 'status': 'skipped (already exists)'}
    backup_dir.mkdir(parents=True, exist_ok=True)
    return {'created': True, 'path': str(backup_dir), 'status': 'created'}


def prepare_backup_folders(batches):
    """For every batch that has images, ensure its QCCBackups folder exists."""
    results = []
    for b in batches:
        batch_dir = Path(b['BatchDirectory'])
        if not batch_dir.exists():
            results.append({
                'batch_name': b['BatchName'], 'batch_directory': str(batch_dir),
                'status': 'skipped (directory missing)', 'image_count': 0,
            })
            continue
        n_images = count_images(batch_dir)
        if n_images == 0:
            results.append({
                'batch_name': b['BatchName'], 'batch_directory': str(batch_dir),
                'status': 'skipped (no images)', 'image_count': 0,
            })
            continue
        r = ensure_backup_folder(batch_dir)
        results.append({
            'batch_name': b['BatchName'],
            'batch_directory': str(batch_dir),
            'status': r['status'],
            'backup_path': r['path'],
            'image_count': n_images,
        })
    return results


# ---------------------------------------------------------------------------
# Preview (read-only)
# ---------------------------------------------------------------------------

def generate_preview(batch_dir, threshold=100, sample_size=6):
    batch_dir = Path(batch_dir)
    images = list_images(batch_dir)
    sample = images[:max(0, sample_size)]
    previews = []

    for p in sample:
        try:
            with Image.open(p) as img:
                img.seek(0)
                frame = img.convert('RGB') if img.mode != 'RGB' else img.copy()
                cropped_pil, status, orig_size = _crop_frame(frame, threshold)
                previews.append({
                    'filename': p.name,
                    'status': status,
                    'orig_size': list(orig_size),
                    'new_size': list(cropped_pil.size),
                    'before': make_thumb_b64(frame),
                    'after': make_thumb_b64(cropped_pil),
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
# Live run: backup-then-crop
# ---------------------------------------------------------------------------

# def process_batch_with_backup(batch_dir, threshold=100, progress_cb=None, cancel_check=None):
#     batch_dir = Path(batch_dir)
#     backup_dir = batch_dir / BACKUP_DIR_NAME
#     backup_dir.mkdir(parents=True, exist_ok=True)

#     image_files = list_images(batch_dir)
#     total = len(image_files)

#     stats = {
#         'total_images': total,
#         'moved_to_backup': 0,
#         'already_backed_up': 0,
#         'cropped_success': 0,
#         'cropped_unchanged': 0,
#         'cropped_failed': 0,
#         'backup_dir': str(backup_dir),
#         'errors': [],
#     }

#     for idx, img_path in enumerate(image_files, start=1):
#         if cancel_check and cancel_check():
#             stats['cancelled_at'] = idx
#             break

#         rel = img_path.relative_to(batch_dir)
#         backup_path = backup_dir / rel
#         backup_path.parent.mkdir(parents=True, exist_ok=True)

#         try:
#             if backup_path.exists():
#                 stats['already_backed_up'] += 1
#                 source_path = img_path      # <-- read the current (already-processed) file, not the stale backup
#             else:
#                 shutil.move(str(img_path), str(backup_path))
#                 stats['moved_to_backup'] += 1
#                 source_path = backup_path   # <-- first stage: original just moved here, read from here

#             success, msg = remove_black_borders(str(source_path), str(img_path), threshold=threshold)
#             if not success:
#                 stats['cropped_failed'] += 1
#                 stats['errors'].append(f"{rel}: {msg}")
#             elif msg and msg.startswith("UNCHANGED:"):
#                 stats['cropped_unchanged'] += 1
#             else:
#                 stats['cropped_success'] += 1
#         except Exception as e:
#             stats['cropped_failed'] += 1
#             stats['errors'].append(f"{rel}: {str(e)}")

#         if progress_cb:
#             progress_cb(idx, total, str(rel))

#     return stats


# import re

# _SUFFIX_RE = re.compile(r'_1$')


# def _canonical_and_output_paths(batch_dir, img_path):
#     """Given a discovered image path (which may already carry the '_1'
#     "processed" suffix from an earlier run or an earlier pipeline stage),
#     work out:

#       - rel:            path relative to batch_dir, as found
#       - canonical_rel:  the image's TRUE identity, with any existing '_1'
#                          suffix stripped off. This is what we key the
#                          backup off of, so re-runs and chained pipeline
#                          stages (rotate -> crop -> autofill) always back up
#                          / detect the same true original, no matter how
#                          many times it's already been processed.
#       - output_path:    where the processed result gets written -- always
#                          canonical name + '_1', so re-processing overwrites
#                          that same file in place instead of stacking
#                          suffixes (page003_1_1_1.jpg).
#     """
#     rel = img_path.relative_to(batch_dir)
#     canonical_stem = _SUFFIX_RE.sub('', img_path.stem)
#     canonical_rel = rel.with_name(canonical_stem + rel.suffix)
#     output_path = batch_dir / rel.with_name(canonical_stem + '_1' + rel.suffix)
#     return rel, canonical_rel, output_path


# def process_batch_with_backup(batch_dir, threshold=100, progress_cb=None, cancel_check=None):
#     batch_dir = Path(batch_dir)
#     backup_dir = batch_dir / BACKUP_DIR_NAME
#     backup_dir.mkdir(parents=True, exist_ok=True)

#     image_files = list_images(batch_dir)

#     # De-dupe: if both a plain original and its already-processed "_1"
#     # sibling somehow both exist in the folder, prefer the "_1" version --
#     # it reflects the latest processed state -- so we don't process the
#     # same logical image twice in one run.
#     by_canonical = {}
#     for p in image_files:
#         canonical_stem = _SUFFIX_RE.sub('', p.stem)
#         key = str(p.relative_to(batch_dir).with_name(canonical_stem + p.suffix))
#         if key not in by_canonical or p.stem.endswith('_1'):
#             by_canonical[key] = p
#     image_files = list(by_canonical.values())
#     total = len(image_files)

#     stats = {
#         'total_images': total,
#         'moved_to_backup': 0,
#         'already_backed_up': 0,
#         'cropped_success': 0,
#         'cropped_unchanged': 0,
#         'cropped_failed': 0,
#         'backup_dir': str(backup_dir),
#         'errors': [],
#     }

#     for idx, img_path in enumerate(image_files, start=1):
#         if cancel_check and cancel_check():
#             stats['cancelled_at'] = idx
#             break

#         rel, canonical_rel, output_path = _canonical_and_output_paths(batch_dir, img_path)
#         backup_path = backup_dir / canonical_rel
#         backup_path.parent.mkdir(parents=True, exist_ok=True)
#         output_path.parent.mkdir(parents=True, exist_ok=True)

#         try:
#             if backup_path.exists():
#                 stats['already_backed_up'] += 1
#                 source_path = img_path      # already-processed "_1" file (re-run / chained stage)
#             else:
#                 shutil.move(str(img_path), str(backup_path))
#                 stats['moved_to_backup'] += 1
#                 source_path = backup_path   # first time: pristine original just moved here

#             success, msg = remove_black_borders(str(source_path), str(output_path), threshold=threshold)
#             if not success:
#                 stats['cropped_failed'] += 1
#                 stats['errors'].append(f"{rel}: {msg}")
#             elif msg and msg.startswith("UNCHANGED:"):
#                 stats['cropped_unchanged'] += 1
#             else:
#                 stats['cropped_success'] += 1
#         except Exception as e:
#             stats['cropped_failed'] += 1
#             stats['errors'].append(f"{rel}: {str(e)}")

#         if progress_cb:
#             progress_cb(idx, total, str(rel))

#     return stats

import shutil
from pathlib import Path
import re

_SUFFIX_RE = re.compile(r'_1$')
BACKUP_DIR_NAME = "QCCBackups"


def _canonical_and_output_paths(batch_dir, img_path):
    """
    Returns:
      rel: relative path as discovered
      output_path: working image path in batch_dir (canonical name without '_1')
      backup_path: backup image path inside QCCBackups (with '_1' suffix)
    """
    rel = img_path.relative_to(batch_dir)
    canonical_stem = _SUFFIX_RE.sub('', img_path.stem)
    
    # Active output in batch_dir uses the canonical name (e.g., page001.jpg)
    canonical_rel = rel.with_name(canonical_stem + rel.suffix)
    output_path = batch_dir / canonical_rel
    
    # Backup copy inside QCCBackups carries the '_1' suffix (e.g., page001_1.jpg)
    backup_rel = rel.with_name(canonical_stem + '_1' + rel.suffix)
    backup_path = batch_dir / BACKUP_DIR_NAME / backup_rel
    
    return rel, output_path, backup_path


def process_batch_with_backup(batch_dir, threshold=100, progress_cb=None, cancel_check=None):
    """Backup original with '_1' suffix into QCCBackups, then process into batch_dir."""
    batch_dir = Path(batch_dir)
    backup_dir = batch_dir / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    image_files = list_images(batch_dir)

    # De-dupe: prefer canonical plain file if both exist in batch_dir
    by_canonical = {}
    for p in image_files:
        canonical_stem = _SUFFIX_RE.sub('', p.stem)
        key = str(p.relative_to(batch_dir).with_name(canonical_stem + p.suffix))
        if key not in by_canonical or not p.stem.endswith('_1'):
            by_canonical[key] = p
    image_files = list(by_canonical.values())
    total = len(image_files)

    stats = {
        'total_images': total,
        'moved_to_backup': 0,
        'already_backed_up': 0,
        'cropped_success': 0,
        'cropped_unchanged': 0,
        'cropped_failed': 0,
        'backup_dir': str(backup_dir),
        'errors': [],
    }

    for idx, img_path in enumerate(image_files, start=1):
        if cancel_check and cancel_check():
            stats['cancelled_at'] = idx
            break

        rel, output_path, backup_path = _canonical_and_output_paths(batch_dir, img_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if backup_path.exists():
                stats['already_backed_up'] += 1
                source_path = backup_path
            else:
                # First run: Move un-suffixed file into QCCBackups under the '_1' name
                shutil.move(str(img_path), str(backup_path))
                stats['moved_to_backup'] += 1
                source_path = backup_path

            success, msg = remove_black_borders(str(source_path), str(output_path), threshold=threshold)
            if not success:
                stats['cropped_failed'] += 1
                stats['errors'].append(f"{rel}: {msg}")
            elif msg and msg.startswith("UNCHANGED:"):
                stats['cropped_unchanged'] += 1
            else:
                stats['cropped_success'] += 1
        except Exception as e:
            stats['cropped_failed'] += 1
            stats['errors'].append(f"{rel}: {str(e)}")

        if progress_cb:
            progress_cb(idx, total, str(rel))

    return stats

# ---------------------------------------------------------------------------
# SQL Server helpers with status column support
# ---------------------------------------------------------------------------

from db import _connect



def format_time(seconds):
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours)}h {int(minutes)}m {int(seconds)}s"