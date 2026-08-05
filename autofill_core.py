"""
autofill_core.py
----------------
Auto-Fill stage: fill outer torn corners and hole-punch damage patches with solid white
without eroding interior text or table gridlines.
"""
import cv2
import numpy as np
import shutil
from pathlib import Path
from PIL import Image
import logging
import re

from cropping_core import BACKUP_DIR_NAME, list_images, make_thumb_b64

logger = logging.getLogger("qcc_autocrop")

DARK_THRESHOLD = 48          # Base dark cutoff
MIN_BLOB_AREA = 20           # Minimum area to skip JPEG noise specks
MAX_BLOB_AREA_RATIO = 0.35   # Maximum safe ratio cap for flat white fill
FILL_COLOR = (255, 255, 255) # White in BGR
_SUFFIX_RE = re.compile(r'_1$')
BACKUP_DIR_NAME = "QCCBackups"


def get_dpi(src_path, default=(300, 300)):
    """Extract DPI resolution metadata from source image using PIL."""
    try:
        with Image.open(src_path) as im:
            dpi = im.info.get("dpi")
            if dpi:
                return dpi
    except Exception:
        pass
    return default


def save_with_dpi(cv2_img_bgr, out_path, dpi=(300, 300)):
    """Save a cv2 (BGR numpy array) image to out_path preserving DPI metadata."""
    rgb = cv2.cvtColor(cv2_img_bgr, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb)
    pil_img.save(out_path, dpi=dpi, quality=95)


def fill_damage(image_path, output_path, dark_threshold=DARK_THRESHOLD,
                max_blob_ratio=MAX_BLOB_AREA_RATIO, min_blob_area=MIN_BLOB_AREA):
    """
    Target outer edge damage patches (hole punches, torn corners) and fill solid white.
    Filters out internal dark text/lines to prevent erosion.
    """
    try:
        dpi = get_dpi(image_path)
        
        with Image.open(image_path) as pil_img:
            frame = pil_img.convert('RGB')
            img_np = np.array(frame)
            img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
            orig_h, orig_w = img_bgr.shape[:2]

            gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
            
            # 1. Dark threshold to capture dark damage regions
            _, dark_mask = cv2.threshold(gray, dark_threshold, 255, cv2.THRESH_BINARY_INV)

            # 2. Connected components inspection
            n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(dark_mask, connectivity=8)
            safe_mask = np.zeros_like(dark_mask)

            # Edge boundary margin (pixels from image border to identify hole-punch/corner damage)
            margin = 30  
            skipped_small = 0
            skipped_large = 0
            skipped_interior = 0
            kept_blobs = 0

            for i in range(1, n_labels):
                area = stats[i, cv2.CC_STAT_AREA]
                x = stats[i, cv2.CC_STAT_LEFT]
                y = stats[i, cv2.CC_STAT_TOP]
                w = stats[i, cv2.CC_STAT_WIDTH]
                h = stats[i, cv2.CC_STAT_HEIGHT]

                if area < min_blob_area:
                    skipped_small += 1
                    continue
                
                if area / (orig_w * orig_h) > max_blob_ratio:
                    skipped_large += 1
                    continue

                # Check if the blob touches or sits near any page boundary
                is_on_edge = (x <= margin or y <= margin or 
                              (x + w) >= (orig_w - margin) or 
                              (y + h) >= (orig_h - margin))

                if not is_on_edge:
                    # Skip internal blobs (text, table gridlines, handwriting)
                    skipped_interior += 1
                    continue

                safe_mask[labels == i] = 255
                kept_blobs += 1

            if kept_blobs == 0:
                save_with_dpi(img_bgr, output_path, dpi=dpi)
                return True, "unchanged", f"no outer damage found (skipped {skipped_small} noise, {skipped_interior} interior text)"

            # 3. Dilate mask slightly to feather outer hole edge halos
            safe_mask = cv2.dilate(safe_mask, np.ones((5, 5), np.uint8), iterations=1)

            # 4. Fill identified damage regions with flat white
            result_bgr = img_bgr.copy()
            result_bgr[safe_mask == 255] = FILL_COLOR

            # 5. Save with preserved DPI
            save_with_dpi(result_bgr, output_path, dpi=dpi)

            return True, "filled", f"filled {kept_blobs} edge blob(s) (skipped {skipped_interior} interior text blobs)"

    except Exception as e:
        return False, "error", str(e)


def generate_preview(batch_dir, threshold=DARK_THRESHOLD, sample_size=6):
    """Read-only preview: returns before/after thumbnails for a sample."""
    batch_dir = Path(batch_dir)
    images = list_images(batch_dir)
    sample = images[:max(0, sample_size)]
    previews = []

    temp_dir = batch_dir / "__autofill_temp__"
    temp_dir.mkdir(exist_ok=True)

    try:
        for p in sample:
            try:
                with Image.open(p) as im:
                    im.seek(0)
                    frame = im.convert('RGB') if im.mode != 'RGB' else im.copy()
                    before_b64 = make_thumb_b64(frame)

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
        shutil.rmtree(temp_dir, ignore_errors=True)

    return {
        'batch_directory': str(batch_dir),
        'total_images': len(images),
        'sample_count': len(sample),
        'previews': previews,
    }


# def _canonical_and_output_paths(batch_dir, img_path):
#     rel = img_path.relative_to(batch_dir)
#     canonical_stem = _SUFFIX_RE.sub('', img_path.stem)
    
#     canonical_rel = rel.with_name(canonical_stem + rel.suffix)
#     output_path = batch_dir / canonical_rel
    
#     backup_rel = rel.with_name(canonical_stem + '_1' + rel.suffix)
#     backup_path = batch_dir / BACKUP_DIR_NAME / backup_rel
    
#     return rel, output_path, backup_path


# def process_batch_with_backup(batch_dir, threshold=DARK_THRESHOLD,
#                               progress_cb=None, cancel_check=None):
#     """Backup original with '_1' suffix into QCCBackups, then process into batch_dir."""
#     batch_dir = Path(batch_dir)
#     backup_dir = batch_dir / BACKUP_DIR_NAME
#     backup_dir.mkdir(parents=True, exist_ok=True)

#     image_files = list_images(batch_dir)

#     by_canonical = {}
#     for p in image_files:
#         canonical_stem = _SUFFIX_RE.sub('', p.stem)
#         key = str(p.relative_to(batch_dir).with_name(canonical_stem + p.suffix))
#         if key not in by_canonical or not p.stem.endswith('_1'):
#             by_canonical[key] = p
#     image_files = list(by_canonical.values())
#     total = len(image_files)

#     stats = {
#         'total_images': total,
#         'moved_to_backup': 0,
#         'already_backed_up': 0,
#         'filled_success': 0,
#         'filled_unchanged': 0,
#         'filled_failed': 0,
#         'backup_dir': str(backup_dir),
#         'errors': [],
#     }

#     for idx, img_path in enumerate(image_files, start=1):
#         if cancel_check and cancel_check():
#             stats['cancelled_at'] = idx
#             break

#         rel, output_path, backup_path = _canonical_and_output_paths(batch_dir, img_path)
#         backup_path.parent.mkdir(parents=True, exist_ok=True)
#         output_path.parent.mkdir(parents=True, exist_ok=True)

#         try:
#             if backup_path.exists():
#                 stats['already_backed_up'] += 1
#                 source_path = backup_path
#             else:
#                 shutil.move(str(img_path), str(backup_path))
#                 stats['moved_to_backup'] += 1
#                 source_path = backup_path

#             success, status, detail = fill_damage(str(source_path), str(output_path), threshold)
            
#             if not success:
#                 stats['filled_failed'] += 1
#                 stats['errors'].append(f"{rel}: {detail}")
#             elif status == 'unchanged':
#                 stats['filled_unchanged'] += 1
#             else:
#                 stats['filled_success'] += 1
#         except Exception as e:
#             stats['filled_failed'] += 1
#             stats['errors'].append(f"{rel}: {str(e)}")

#         if progress_cb:
#             progress_cb(idx, total, str(rel))

#     return stats


import shutil
from pathlib import Path
import re

_SUFFIX_RE = re.compile(r"_1$")
BACKUP_DIR_NAME = "QCCBackups"


def _canonical_and_output_paths(batch_dir, img_path):
    """
    Common naming convention used by all pipeline stages.

    Original:
        page001.jpg

    Backup:
        QCCBackups/page001_1.jpg

    Working image:
        page001.jpg

    If page001.jpg is processed again, the existing backup is reused and
    the working image is overwritten.
    """
    rel = img_path.relative_to(batch_dir)

    canonical_stem = _SUFFIX_RE.sub("", img_path.stem)
    canonical_rel = rel.with_name(canonical_stem + rel.suffix)

    # Working image (always original filename)
    output_path = batch_dir / canonical_rel

    # Backup (always _1)
    backup_path = (
        batch_dir
        / BACKUP_DIR_NAME
        / rel.with_name(canonical_stem + "_1" + rel.suffix)
    )

    return rel, output_path, backup_path


def process_batch_with_backup(
    batch_dir,
    threshold=DARK_THRESHOLD,
    progress_cb=None,
    cancel_check=None,
):
    """
    Backup original into QCCBackups/page001_1.jpg

    Working folder always contains:

        page001.jpg
    """

    batch_dir = Path(batch_dir)

    backup_dir = batch_dir / BACKUP_DIR_NAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    image_files = list_images(batch_dir)

    #
    # De-dupe.
    # If both page001.jpg and page001_1.jpg somehow exist,
    # prefer page001.jpg because that's the working image.
    #
    by_canonical = {}

    for p in image_files:
        canonical_stem = _SUFFIX_RE.sub("", p.stem)
        key = str(
            p.relative_to(batch_dir).with_name(
                canonical_stem + p.suffix
            )
        )

        if key not in by_canonical or not p.stem.endswith("_1"):
            by_canonical[key] = p

    image_files = list(by_canonical.values())

    total = len(image_files)

    stats = {
        "total_images": total,
        "moved_to_backup": 0,
        "already_backed_up": 0,
        "filled_success": 0,
        "filled_unchanged": 0,
        "filled_failed": 0,
        "backup_dir": str(backup_dir),
        "errors": [],
    }

    for idx, img_path in enumerate(image_files, start=1):

        if cancel_check and cancel_check():
            stats["cancelled_at"] = idx
            break

        rel, output_path, backup_path = _canonical_and_output_paths(
            batch_dir,
            img_path,
        )

        backup_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:

            #
            # First run
            #
            if backup_path.exists():

                stats["already_backed_up"] += 1

                #
                # Re-run or later pipeline stage.
                # Process the latest working image if present.
                #
                if output_path.exists():
                    source_path = output_path
                else:
                    source_path = backup_path

            else:

                #
                # Preserve the original once.
                #
                shutil.move(str(img_path), str(backup_path))
                stats["moved_to_backup"] += 1

                #
                # First processing starts from the original.
                #
                source_path = backup_path

            success, status, detail = fill_damage(
                str(source_path),
                str(output_path),
                threshold,
            )

            if not success:
                stats["filled_failed"] += 1
                stats["errors"].append(f"{rel}: {detail}")

            elif status == "unchanged":
                stats["filled_unchanged"] += 1

            else:
                stats["filled_success"] += 1

        except Exception as e:
            stats["filled_failed"] += 1
            stats["errors"].append(f"{rel}: {e}")

        if progress_cb:
            progress_cb(idx, total, str(rel))

    return stats