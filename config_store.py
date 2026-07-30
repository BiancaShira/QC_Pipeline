"""
config_store.py
----------------
Small JSON-file-backed settings store.
"""
import json
import threading
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"

_LOCK = threading.Lock()

DEFAULTS = {
    "pipeline": {
        "rotation": {
            "in_status": "Blank Page Removal",
            "out_status": "Ready For Auto Crop",
            "out_code": 15,
            "status_column": "StatusText",
        },
        "crop": {
            "in_status": "Ready For Auto Crop",
            "out_status": "Cropped",
            "out_code": 20,
            "status_column": "StatusText",
        },
        "autofill": {
            "in_status": "Auto Rotate-complete",
            "out_status": "Ready For Quality Control",
            "out_code": 22,
            "status_column": "StatusText",
        },
    },
    "orientation_models": [
        {"name": "Default", "match": "", "model_paths": []}
    ],
    "scheduler": {
        "rotation": {
            "enabled": False,
            "interval_minutes": 30,
            "batch_count_trigger": 0,
        },
        "crop": {
            "enabled": False,
            "interval_minutes": 30,
            "batch_count_trigger": 0,
        },
        "autofill": {
            "enabled": False,
            "interval_minutes": 30,
            "batch_count_trigger": 0,
        },
        "chain_rotation_to_crop": True,
        "chain_crop_to_autofill": False,
    },
    "ollama": {
        "enabled": False,
        "base_url": "http://localhost:11434",
        "model": "llava",
        "trigger": "low_confidence",
        "confidence_threshold": 0.55,
        "timeout_seconds": 45,
    },
    "theme": "dark",
    "db_last": {
        "server": "", "driver": "ODBC Driver 17 for SQL Server", "uid": "", "database": "",
    },
}

def _deep_merge(base, overrides):
    out = dict(base)
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out

def load():
    with _LOCK:
        if not SETTINGS_PATH.exists():
            return json.loads(json.dumps(DEFAULTS))
        try:
            with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
                saved = json.load(f)
        except Exception:
            return json.loads(json.dumps(DEFAULTS))
        return _deep_merge(DEFAULTS, saved)

def save(settings):
    with _LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_PATH.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
        tmp.replace(SETTINGS_PATH)

def update(patch):
    current = load()
    merged = _deep_merge(current, patch)
    save(merged)
    return merged