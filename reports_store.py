"""
reports_store.py
-----------------
Persists finished job runs to disk (JSON-lines) so the Reports tab shows
every run -- manual, chained, or scheduler-triggered -- and survives an app
restart. The existing per-job in-memory JOBS dict in app.py is unaffected;
this is purely additive.

One line per *batch result* (not per job), so filtering/CSV export is a
flat table. File rolls over to a new file past MAX_ROWS_PER_FILE to keep
any single file small.
"""
import csv
import io
import json
import threading
import time
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
REPORTS_DIR = DATA_DIR / "reports"
MAX_ROWS_PER_FILE = 20000

_LOCK = threading.Lock()

FIELDS = [
    'recorded_at', 'job_id', 'kind', 'trigger', 'batch_name', 'batch_directory',
    'status', 'error', 'total_images', 'rotated_success', 'rotated_unchanged',
    'rotated_failed', 'ollama_assisted', 'cropped_success', 'cropped_unchanged',
    'cropped_failed', 'filled_success', 'filled_unchanged', 'filled_failed',
    'moved_to_backup', 'already_backed_up', 'elapsed_seconds',
]


def _current_file():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(REPORTS_DIR.glob("reports-*.jsonl"))
    if files:
        last = files[-1]
        try:
            if sum(1 for _ in open(last, 'r', encoding='utf-8')) < MAX_ROWS_PER_FILE:
                return last
        except Exception:
            pass
    return REPORTS_DIR / f"reports-{int(time.time())}.jsonl"


def record_job(job):
    """Call once, after a job finishes. Writes one row per batch result."""
    rows = []
    for r in job.get('results', []):
        row = {k: r.get(k) for k in FIELDS if k not in ('recorded_at', 'job_id', 'kind', 'trigger')}
        row['recorded_at'] = time.time()
        row['job_id'] = job['id']
        row['kind'] = job['kind']
        row['trigger'] = job.get('trigger', 'manual')
        rows.append(row)
    if not rows:
        return
    with _LOCK:
        path = _current_file()
        with open(path, 'a', encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")


def list_reports(stage=None, limit=1000):
    """Most-recent-first, optionally filtered by stage ('rotation'/'crop'/'autofill')."""
    with _LOCK:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(REPORTS_DIR.glob("reports-*.jsonl"), reverse=True)
    out = []
    for path in files:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if stage and row.get('kind') != stage:
                continue
            out.append(row)
            if len(out) >= limit:
                return out
    return out


def to_csv(rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction='ignore')
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return output.getvalue()