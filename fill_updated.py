"""
Fill small black damage patches (torn corners, hole-punch gaps) with solid
white, so they render clean -- WITHOUT cropping, since on these documents
the black isn't a uniform border margin, it's localized damage within the
page's own footprint. Cropping would risk cutting into real data.

This uses a flat white fill rather than cv2.inpaint's texture-guessing:
  - cv2.inpaint tries to reconstruct plausible texture from surrounding
    pixels, which starts to look smeared/fake over larger areas -- that's
    why the original version capped MAX_BLOB_AREA_RATIO conservatively.
  - A flat white fill doesn't guess anything, so it doesn't have that
    smearing failure mode. It's also more honest: this is genuinely
    missing paper, not something to visually reconstruct.
  - Because of that, MAX_BLOB_AREA_RATIO can be set much higher (or
    removed) without the same risk -- there's no "big fake blur" to worry
    about, just a bigger flat white patch.

Approach:
  1. Threshold to find very dark regions (near-black, not just "not white")
  2. Filter to only blobs below MAX_BLOB_AREA_RATIO (kept as a light
     safety rail in case a huge blob turns out to be real dark content
     rather than damage -- but this can be raised well above the old
     inpaint-based cap since flat-fill doesn't smear)
  3. Paint those regions solid white

DPI handling is inlined below (previously dpi_utils.py): cv2.imwrite()
silently drops JPEG DPI/resolution metadata, so we read the source file's
DPI with PIL and re-apply it when saving the processed (cv2/numpy) image,
to avoid output files reporting 0 DPI.
"""

import cv2
import numpy as np
import sys
import os
from PIL import Image

DARK_THRESHOLD = 50          # pixel value below this is considered "black damage"
MIN_BLOB_AREA = 20              # blobs smaller than this (px) are almost always JPEG
                                 # compression noise / anti-aliasing specks along line and
                                 # text edges, not real damage -- ignore them entirely
MAX_BLOB_AREA_RATIO = 0.35     # much higher than the inpaint version's 0.05 --
                                # flat white fill doesn't smear, so large blobs are safe to fill too
FILL_COLOR = (255, 255, 255)   # BGR white


def get_dpi(src_path, default=(300, 300)):
    try:
        with Image.open(src_path) as im:
            dpi = im.info.get("dpi")
            if dpi:
                return dpi
    except Exception:
        pass
    return default


def save_with_dpi(cv2_img_bgr, out_path, dpi):
    """Save a cv2 (BGR, numpy array) image to out_path preserving DPI metadata."""
    rgb = cv2.cvtColor(cv2_img_bgr, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb)
    pil_img.save(out_path, dpi=dpi, quality=95)


def inpaint_damage(img_path, out_path):
    img = cv2.imread(img_path)
    if img is None:
        print(f"{img_path}: READ FAILED")
        return False

    orig_h, orig_w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # dark mask: pixels that are genuinely black damage, not just shadow/ink
    _, dark_mask = cv2.threshold(gray, DARK_THRESHOLD, 255, cv2.THRESH_BINARY_INV)

    # connected components so we can inspect individual blobs
    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(dark_mask, connectivity=8)

    safe_mask = np.zeros_like(dark_mask)
    skipped_large = 0
    skipped_small = 0
    kept_blobs = 0
    for i in range(1, n_labels):  # skip label 0 = background
        area = stats[i, cv2.CC_STAT_AREA]
        if area < MIN_BLOB_AREA:
            # too small to be real damage -- almost certainly JPEG compression
            # noise / anti-aliasing specks along printed line and text edges.
            # Confirmed on a real sample: 838 "blobs" flagged, ALL of them
            # 1-5px in area -- painting those white is what was eroding the
            # printed table grid. Leaving them alone is strictly safer AND
            # more correct (they were never damage to begin with).
            skipped_small += 1
            continue
        area_ratio = area / (orig_w * orig_h)
        if area_ratio > MAX_BLOB_AREA_RATIO:
            skipped_large += 1
            continue  # don't touch big regions -- likely a real border/frame, not damage
        safe_mask[labels == i] = 255
        kept_blobs += 1

    dpi = get_dpi(img_path)

    if kept_blobs == 0:
        print(f"{img_path}: no small dark blobs found to inpaint (skipped_large={skipped_large}, skipped_small_noise={skipped_small}), saving unchanged")
        save_with_dpi(img, out_path, dpi)
        return True

    # dilate the mask slightly so the fill covers the blob's edge halo too
    safe_mask = cv2.dilate(safe_mask, np.ones((5, 5), np.uint8), iterations=1)

    result = img.copy()
    result[safe_mask == 255] = FILL_COLOR
    save_with_dpi(result, out_path, dpi)
    print(f"{img_path}: filled {kept_blobs} dark blob(s) white, skipped {skipped_large} large region(s) and {skipped_small} noise speck(s) -> {out_path}")
    return True


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 inpaint_damage.py <input_dir> <output_dir>")
        sys.exit(1)
    input_dir, output_dir = sys.argv[1], sys.argv[2]
    os.makedirs(output_dir, exist_ok=True)
    valid_ext = (".jpg", ".jpeg", ".png")
    files = sorted(f for f in os.listdir(input_dir) if f.lower().endswith(valid_ext))
    for f in files:
        inpaint_damage(os.path.join(input_dir, f), os.path.join(output_dir, f))