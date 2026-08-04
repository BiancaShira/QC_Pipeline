# QCC Pipeline — changes in this pass

I read your actual zip (`app.py`, `scheduler.py`, `db.py`, `config_store.py`,
`orientation_core.py`, `templates/index.html`, `static/js/app.js`) rather than
guessing. Below is what's actually there, what was breaking, and exactly what
I changed — file by file, so you can review/commit each one separately.

## 1. Root cause of the crash / "two places to fill" / scheduler confusion

Your frontend already does the right thing: **Rotation, Crop, and Auto-Fill
each have their own "Connect to database" panel**, and `app.js` keeps a
separate `state.dbCreds` per stage (`rotStage`, `cropStage`, `fillStage`).

The backend undid that. `app.py` had exactly **one** global variable:

```python
LAST_DB_CREDS = {'server': None, 'driver': None, 'database': None, 'uid': None, 'pwd': None}
```

Both `/api/db/test` and `/api/db/batches` overwrote this *same* dict no
matter which stage's panel called them. `scheduler.py` then polled
`get_db_creds()` — one shared set of credentials — for all three stages'
scheduled checks. So in practice:

- Connecting a DB for Auto-Crop would silently change what Rotation's
  scheduler was polling against (or point it at a server it doesn't
  expect).
- If the "wrong" stage's DB got disconnected/changed mid-session, the
  next scheduler tick for another stage would try to query a status
  column that didn't match, or a server no longer valid — this is almost
  certainly your "status could not fetch from the database" crash.
- The `warn-note` in Settings even says *"Automation only works while **a**
  database connection has been made"* (singular) — confirming this was a
  known limitation baked into the UI copy, not just a bug.

I also found the smoking gun for the changes you made and had to undo:
`templates/index.html` line 485 has a leftover comment `<!-- MODIFIED
LABEL HERE -->` above the rotation-chain checkbox — someone (you) already
tried to hand-patch the wording without fixing the underlying either/or
logic, which is exactly the "Crop → Auto-Fill chain is REMOVED per your
request" hardcoding admitted in a comment in the old `app.py`.

**Fix:** per-stage credentials end-to-end — `STAGE_DB_CREDS` (backend) /
`db_connections` (settings.json) / `scheduler.py` now asks for creds
*per kind* instead of once. See file-by-file notes below.

## 2. Files changed

| File | What changed |
|---|---|
| `config_store.py` | Added `db_connections` (per-stage saved server/driver/uid/database — **never password**). Replaced the two chain booleans (`chain_rotation_to_crop`, `chain_crop_to_autofill`) with one field: `scheduler.rotation_next` = `"none" \| "crop" \| "autofill"`. Includes a one-time migration so your existing `data/settings.json` doesn't need to be hand-edited — it converts on first load. |
| `db.py` | Added `search_batches()` — the schema-safe multi-column filter. Only two extra columns are ever allowed into SQL: `BatchID` (your "JobID") and `BatchName` (your "JobName"), via a fixed `ALLOWED_FILTER_COLUMNS` map. Nothing else the client sends can reach a query. `distinct_statuses()` (the function your backend already had but the frontend never called) is unchanged. |
| `scheduler.py` | `get_db_creds` is now called as `get_db_creds(kind)` per stage inside the tick loop, instead of once for all three. Tick frequency is unchanged — it still checks every 20 seconds in the background; that part was already working the way you want ("runtime after every few minutes"), it just didn't have per-stage credentials to check *with*. |
| `app.py` | `STAGE_DB_CREDS` replaces `LAST_DB_CREDS`. `/api/db/batches` takes an optional `stage` field so it knows which slot to remember the connection in, plus optional `batch_id` / `batch_name` filters. `/api/scheduler/check/<kind>` and the scheduler wiring now resolve creds per stage. Rotation chaining reads the new `rotation_next` dropdown value instead of the two booleans. Every finished job is persisted via `reports_store.record_job()`. New `/api/reports` and `/api/reports/csv` endpoints. New `/api/ollama/selective/preview` and `/api/ollama/selective/apply` endpoints (see §4). |
| `reports_store.py` | **New file.** Appends one row per batch result to `data/reports/reports-*.jsonl` whenever a job finishes (manual, chained, or scheduler-triggered). `list_reports(stage=None, limit=1000)` and `to_csv(rows)` back the new endpoints. This survives an app restart — the old per-job CSV button only worked while that job's entry was still in the in-memory `JOBS` dict. |
| `ollama_selective_rotate.py` | **New file.** The prompt-driven selective rotation module — see §4. |

None of the three processing engines (`cropping_core.py`, `orientation_core.py`,
`autofill_core.py`) needed to change — the bug was entirely in credential
plumbing and settings shape, not in the image processing itself. I left
them untouched on purpose so nothing about "as functional as it is right
now" regresses.

## 3. Frontend — `index.html` and `app.js` are included, fully edited

These are your actual uploaded files with the changes applied directly (not
a rewrite from scratch), so the rest of the UI — layout, CSS classes,
existing behavior — is untouched. What changed:

- Every `/api/db/batches` call now sends `stage: kind`, so the backend
  remembers each stage's connection separately (this is the actual fix for
  the cross-talk bug, wired all the way through).
- Each stage's "List batches" panel got two new inputs — **JobID** and
  **JobName contains** — sent as `batch_id` / `batch_name` alongside the
  existing status filter.
- The status filter input on each stage is now backed by a `<datalist>`
  autocomplete, refreshed from `/api/db/distinct` (always unfiltered by
  other criteria) right before every "List batches" click.
- The `set-chain-rot-crop` / `set-chain-crop-fill` checkbox pair is gone,
  replaced with one `<select id="set-rotation-next">` — "Nothing" / "Auto-Crop" /
  "Auto-Fill" — a real either/or choice.
- Settings → Scheduler now shows a live per-stage "connected / not
  connected" line for Rotation / Crop / Auto-Fill, instead of the old
  single "a database connection has been made" note.
- New **Reports** nav item and view: stage filter, refresh, CSV download,
  backed by the new `/api/reports` and `/api/reports/csv` endpoints.
- New **Ollama fallback for selective/prompt-driven rotation** settings
  panel (independent enable switch + model name), and a new panel on the
  Rotation view — prompt box, "Preview matches" (shows thumbnails +
  suggested rotation), "Rotate matched pages" — wired to
  `/api/ollama/selective/preview` and `/api/ollama/selective/apply`.

Both files were syntax-checked (`node --check` for `app.js`, a tag-balance
pass for `index.html`), and every static `$('id')` reference in the new
JS was cross-checked against an actual element ID in the HTML — none were
left dangling. I still can't run the live app against a real SQL Server or
Ollama instance from here, so give it a real run before trusting it on
production batches, same as any other change to a system that's been
crashing.

## 4. The Ollama "identify & rotate specific pages by prompt" feature

This is a genuinely new capability (nothing like it existed before — your
current Ollama fallback in `orientation_core.py` only judges *rotation
angle* for a page, it never decides *whether a page qualifies* for
anything). I built it as its own module, `ollama_selective_rotate.py`,
completely separate from the existing per-page fallback so turning one on
never affects the other (separate `ollama.selective_enabled` /
`ollama.selective_model` settings).

How it works:

1. **`select_batch(batch_dir, prompt, base_url, model)`** — read-only. Sends
   every page in the batch to your Ollama vision model (default
   `qwen2.5vl`, matching what you mentioned) along with your free-text
   prompt (e.g. *"identify all pages with a table"*, *"find pages with a
   mostly green image"*). The model is asked to return strict JSON:
   `{"matches": true/false, "rotation_degrees_cw": 0/90/180/270, "reason": "..."}`.
   Matched pages come back with a thumbnail for preview. **Nothing is
   written to disk at this step.**
2. **`apply_selection(batch_dir, matched)`** — takes the matched list you
   just reviewed and rotates *only* those files. Every touched file is
   copied to `.selective_backup/` first, so a run can be undone by
   restoring from there — same safety principle as your existing
   `QCCBackups` folder for the main rotation stage.

This is intentionally an **optional, on-demand step you trigger with a
prompt**, not something wired into the automatic Rotation stage or the
scheduler — exactly as you described it ("an optional step we can run
after rotation is done"). The two new endpoints
(`/api/ollama/selective/preview`, `/api/ollama/selective/apply`) are ready
to call; `FRONTEND_PATCHES.md` has a minimal prompt-box UI for them.

**One thing I could not verify for you:** I don't have your Ollama server
to test against, so I can't confirm `qwen2.5vl` is the exact model tag
your install uses (some Ollama installs pull it as `qwen2.5vl:7b` or
similar) or that the model reliably returns clean JSON for your specific
page images. Test `select_batch()` against one real batch before trusting
`apply_selection()` on anything you care about — the backup-before-write
step protects you either way.

## 5. What I deliberately left alone

- Your "Chain Rotation → Auto-Fill (skipping Crop)" *behavior* as a
  possible choice is preserved — it's now just one explicit option in
  `rotation_next` (`"autofill"`) instead of a checkbox whose label had
  drifted from what it actually did. `"crop"` and `"none"` are the other
  two options, giving you the real either/or choice you asked for.
- The 20-second scheduler tick interval — that's not your "runtime after
  every few minutes," that's just how often the background thread *checks
  whether* a stage is due; each stage's own `interval_minutes` /
  `batch_count_trigger` in Settings is what actually paces how often a
  stage runs, and that logic was already correct.
- I did not touch `orientation_core.py`, `cropping_core.py`, or
  `autofill_core.py` — the processing engines you said are "working as it
  is right now" are unchanged.

## 6. Suggested commit order

1. `config_store.py` (adds new fields, migrates old ones, nothing else depends on it breaking)
2. `db.py` (additive — new `search_batches`, nothing removed)
3. `scheduler.py` + `app.py` together (they depend on each other's new signatures)
4. `reports_store.py` (new, additive)
5. `ollama_selective_rotate.py` (new, additive, unused until wired in step 6)
6. Frontend patches from `FRONTEND_PATCHES.md`, one snippet at a time

Steps 1–2 and 4–5 are safe to deploy with zero frontend changes — they add
capability without removing anything the current UI calls. Step 3 is the
one that actually fixes the scheduler bug, and it's a matched pair
(`scheduler.py`'s `get_db_creds(kind)` signature must match `app.py`'s
`_scheduler_get_creds(kind)`), so commit those two together.