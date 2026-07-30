# QCC Pipeline

Flask console for a two-stage document pipeline:

1. **Auto-Rotation** -- classifies each page's orientation (EfficientNet-B0
   ensemble + 3-way TTA, same model as `auto_orientation_v3.py`) and rotates
   it upright. Supports multiple checkpoints routed by document type, plus an
   optional Ollama vision-model fallback for pages the classifier isn't sure
   about.
2. **Auto-Crop** -- removes black borders (unchanged detection logic from the
   original tool).

Both stages share the same shape: pick batches from a **Database** or
**Folder structure** source, **Preview** in memory, then **Run** to back
originals up into `QCCBackups` and write the processed result back in place.
Both can also run **automatically** on a schedule and hand batches to each
other by database status.

## Install

```bash
pip install -r requirements.txt
```

`pyodbc` needs a working ODBC driver (e.g. "ODBC Driver 17 for SQL Server")
for the Database source. Folder mode, preview, and both processing stages
work without it. `torch`/`torchvision` are only needed for Auto-Rotation.

## Run

```bash
python app.py
```

Open `http://localhost:5000`.

## How the pipeline flows

Each stage watches a `StatusText` in `batchtable` and writes a new one on
success -- configurable under **Automation & Settings \u2192 Status
pipeline**:

| Stage         | Default input status     | Default output status |
|---------------|---------------------------|------------------------|
| Auto-Rotation | `Blank Page Removal`      | `Ready For Auto Crop`  |
| Auto-Crop     | `Ready For Auto Crop`     | `Cropped`              |

Crop's input defaults to matching Rotation's output so batches flow straight
through, but the two fields are independent -- editing one never silently
changes the other.

**Chaining vs. scheduling** -- there are two ways a batch can move from
Rotation into Crop:
- **Chain** (Settings \u2192 Status pipeline): as soon as a rotation *run*
  finishes, its successfully-processed batches are hand-carried straight
  into a Crop run in the same breath, without waiting on Crop's own
  schedule. On by default.
- **Crop's own scheduler**: independent of chaining, Crop can also poll for
  batches sitting in its input status on its own timer/count trigger, which
  will pick up anything chaining missed (folder-mode batches, batches that
  arrived from elsewhere, etc).

They can be used together, or you can turn chaining off and run both stages
purely on their own schedules.

## Scheduler

Under **Automation & Settings \u2192 Scheduler**, each stage can be:
- left fully manual, with a **"Check batches now"** button that polls its
  input status immediately and runs on whatever it finds;
- run automatically every *N* minutes;
- run automatically once *N* batches accumulate in its input status;
- or both (whichever fires first).

The scheduler is a single background thread that ticks every 20 seconds. It
only has something to poll once a Database connection has been made *once*
in the running process (via either stage's "Connect & list databases")
-- the password is kept in server memory for the life of the process, never
written to disk. Folder-mode has no status column, so it stays manual.

## Orientation models (multiple document types)

Under **Settings \u2192 Orientation models**, register one or more trained
checkpoints as "profiles". Each profile has:
- a **match** string -- matched case-insensitively as a substring against a
  batch's `DocumentType` (pulled from `batchtable` if that column exists,
  otherwise blank). Leave blank for the catch-all default profile.
- one or more **checkpoint paths** -- multiple paths are ensembled together
  exactly like `MODEL_PATHS` in the original script.

A batch with no `DocumentType` match falls back to the default profile.

## Ollama fallback

Under **Settings \u2192 Ollama**, you can point the rotation stage at a local
Ollama vision model (e.g. `llava`) as a second opinion:
- **Trigger: low confidence** -- only pages where the classifier's top
  softmax score is below the configured threshold are also sent to Ollama,
  which is asked how many degrees clockwise the page needs to rotate. Its
  answer replaces the classifier's when it responds with a parseable number.
- **Trigger: always** -- every page is checked by Ollama.

This runs per-page over HTTP to Ollama's `/api/generate`, so it's slower --
"low confidence" is the practical default. Use **Test connection** to check
Ollama is reachable and the model is pulled before enabling it live.

## Files

- `app.py` -- Flask routes, a generalized background job runner shared by
  both stages, scheduler wiring, and DB-credential retention for automation.
- `cropping_core.py` -- black-border detection/cropping engine, plus the
  SQL Server and folder-discovery helpers shared by both stages.
- `orientation_core.py` -- rotation classifier (EfficientNet ensemble + TTA),
  multi-profile model routing, Ollama fallback, backup-then-rotate run/preview.
- `scheduler.py` -- the timer/count-trigger background thread.
- `config_store.py` -- JSON-file-backed settings (`data/settings.json`):
  pipeline statuses, model profiles, scheduler config, Ollama config.
- `templates/index.html`, `static/css/style.css`, `static/js/app.js` -- the
  UI: a sidebar with Auto-Rotation / Auto-Crop / Automation & Settings, each
  stage a self-contained source \u2192 batches \u2192 preview \u2192 run
  workflow.
