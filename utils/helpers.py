from pathlib import Path
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp')
BACKUP_DIR_NAME = "QCCBackups"

def count_images(batch_dir):
    try:
        return len(list_images(batch_dir))
    except Exception:
        return 0


def list_images(batch_dir):
    """All images under batch_dir, excluding anything inside QCCBackups."""
    batch_dir = Path(batch_dir)
    backup_dir = (batch_dir / BACKUP_DIR_NAME).resolve()
    out = []
    if not batch_dir.exists():
        return out
    for p in sorted(batch_dir.rglob('*')):
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            continue
        try:
            if backup_dir in p.resolve().parents:
                continue
        except OSError:
            continue
        out.append(p)
    return out