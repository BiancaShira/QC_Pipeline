"""
config_store.py
----------------
Small JSON-file-backed settings store.

CHANGES in this version:
  * Added `db_connections` -- one saved connection (server/driver/uid/database,
    NEVER the password) per stage: rotation / crop / autofill. This is what
    lets the scheduler poll a different database per stage instead of the
    single shared `db_last` that used to get overwritten by whichever stage
    connected last.
  * Replaced the two confusing chain booleans (`chain_rotation_to_crop`,
    `chain_crop_to_autofill`) with a single explicit dropdown value:
    `scheduler.rotation_next` = "none" | "crop" | "autofill".
    `_migrate()` reads old settings.json files that still have the booleans
    and converts them once, so existing installs don't lose their setting.
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
        # Single either/or choice. Replaces chain_rotation_to_crop /
        # chain_crop_to_autofill. One of: "none", "crop", "autofill".
        "rotation_next": "autofill",
    },
    "ollama": {
        "enabled": False,
        "base_url": "http://localhost:11434",
        "model": "llava",
        "trigger": "low_confidence",
        "confidence_threshold": 0.55,
        "timeout_seconds": 45,
        # Independent toggle for the new selective/prompt-driven rotation
        # fallback (see ollama_selective_rotate.py). Kept separate from the
        # per-page orientation fallback above so turning one on doesn't
        # silently turn on the other.
        "selective_enabled": False,
        "selective_model": "qwen2.5vl",
    },
    "theme": "dark",
    # Legacy single global connection. Kept only so older settings.json
    # files still parse; no longer written to by the app. Prefer
    # `db_connections` below.
    "db_last": {
        "server": "", "driver": "ODBC Driver 17 for SQL Server", "uid": "", "database": "",
    },
    # Per-stage saved connection info (server/driver/uid/database only --
    # never the password). This is what the scheduler now reads per stage.
    "db_connections": {
        "rotation": {"server": "", "driver": "ODBC Driver 17 for SQL Server", "uid": "", "database": ""},
        "crop": {"server": "", "driver": "ODBC Driver 17 for SQL Server", "uid": "", "database": ""},
        "autofill": {"server": "", "driver": "ODBC Driver 17 for SQL Server", "uid": "", "database": ""},
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


def _migrate(settings):
    """One-way migration for settings.json files saved by the old version."""
    sched = settings.get("scheduler", {})
    if "rotation_next" not in sched:
        if sched.get("chain_rotation_to_crop"):
            sched["rotation_next"] = "autofill"  # old code actually chained to autofill, not crop
        elif sched.get("chain_crop_to_autofill"):
            sched["rotation_next"] = "autofill"
        else:
            sched["rotation_next"] = "none"
    # Drop the old keys once migrated so they don't linger in the UI/state.
    sched.pop("chain_rotation_to_crop", None)
    sched.pop("chain_crop_to_autofill", None)
    settings["scheduler"] = sched
    return settings


def load():
    with _LOCK:
        if not SETTINGS_PATH.exists():
            return json.loads(json.dumps(DEFAULTS))
        try:
            with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
                saved = json.load(f)
        except Exception:
            return json.loads(json.dumps(DEFAULTS))
        merged = _deep_merge(DEFAULTS, saved)
        return _migrate(merged)


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


def set_stage_connection(stage, server, driver, uid, database):
    """Persist the last-used (non-password) connection info for one stage."""
    if stage not in ("rotation", "crop", "autofill"):
        raise ValueError(f"Unknown stage: {stage}")
    return update({"db_connections": {stage: {
        "server": server, "driver": driver, "uid": uid, "database": database,
    }}})