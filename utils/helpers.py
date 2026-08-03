from pathlib import Path
import time
from db import LAST_DB_CREDS
import config_store
from scheduler import Scheduler
import threading
from utils.log import _log
from utils.models import _resolve_model_paths
import cropping_core as cc
import autofill_core as afc
import orientation_core as oc
import uuid
from utils.state import JOBS_LOCK,JOBS
ACTIVE_STAGE_JOB = {'rotation': None, 'crop': None, 'autofill': None}


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def _start_job(kind, batches, threshold, update_status, db_creds, settings, trigger_label='manual', extra_params=None):
    job_id = _new_job(kind, batches)
    with JOBS_LOCK:
        job = JOBS[job_id]
    _log(job, f"Starting {kind} run on {len(batches)} batch(es) ({trigger_label}).")
    thread = threading.Thread(
        target=_run_job,
        args=(job_id, kind, batches, threshold, update_status, db_creds, settings, trigger_label, extra_params or {}),
        daemon=True,
    )
    thread.start()
    return job_id

def _scheduler_get_creds():
    if LAST_DB_CREDS.get('server') and LAST_DB_CREDS.get('database'):
        return dict(LAST_DB_CREDS)
    return None

def _scheduler_stage_busy(kind):
    return ACTIVE_STAGE_JOB.get(kind) is not None

def _scheduler_start_run(kind, batches, reason):
    settings = config_store.load()
    threshold = settings.get('_chain_crop_threshold', 100) if kind == 'crop' else \
                settings.get('_chain_fill_threshold', 60) if kind == 'autofill' else 100
    _start_job(kind, batches, threshold, update_status=True, db_creds=_scheduler_get_creds(),
               settings=settings, trigger_label=f"auto ({reason})", extra_params={})


def _run_job(job_id, kind, batches, threshold, update_status, db_creds, settings, trigger_label, extra_params):
    with JOBS_LOCK:
        job = JOBS[job_id]
        job['trigger'] = trigger_label
    ACTIVE_STAGE_JOB[kind] = job_id
    pipeline_cfg = settings['pipeline'][kind]
    ollama_cfg = settings.get('ollama')
    status_column = pipeline_cfg.get('status_column', 'StatusText')

    completed_batches = []

    for batch in batches:
        if job['cancel_requested']:
            _log(job, "Cancelled by user.")
            job['status'] = 'cancelled'
            break

        batch_name = batch.get('BatchName', 'unknown')
        batch_dir = batch.get('BatchDirectory')
        job['current_batch'] = batch_name
        job['files_done_current'] = 0
        job['files_total_current'] = 0

        batch_db_creds = batch.get('_server_creds', db_creds)

        if not batch_dir or not Path(batch_dir).exists():
            _log(job, f"[{batch_name}] SKIPPED -- directory missing: {batch_dir}")
            job['results'].append({'batch_name': batch_name, 'batch_directory': batch_dir,
                                   'status': 'error', 'error': 'directory missing'})
            job['batches_done'] += 1
            continue

        def progress_cb(done, total, filename):
            job['files_done_current'] = done
            job['files_total_current'] = total
            job['current_file'] = filename

        def cancel_check():
            return job['cancel_requested']

        start = time.time()
        try:
            if kind == 'crop':
                _log(job, f"[{batch_name}] Backing up, then cropping (threshold={threshold})...")
                stats = cc.process_batch_with_backup(
                    batch_dir, threshold=threshold, progress_cb=progress_cb, cancel_check=cancel_check,
                )
                summary = (f"cropped: {stats['cropped_success']}, unchanged: {stats['cropped_unchanged']}, "
                           f"failed: {stats['cropped_failed']}")
            elif kind == 'rotation':
                model_paths, profile_name = _resolve_model_paths(settings, batch.get('DocumentType'))
                if not model_paths:
                    raise RuntimeError(
                        f"no orientation model checkpoint configured for document type "
                        f"'{batch.get('DocumentType') or '(none)'}' -- add one in Settings"
                    )
                deskew = extra_params.get('deskew', False)
                _log(job, f"[{batch_name}] Backing up, then rotating (model: {profile_name}, deskew={deskew})...")
                stats = oc.process_batch_with_backup(
                    batch_dir, model_paths=model_paths, ollama_cfg=ollama_cfg,
                    progress_cb=progress_cb, cancel_check=cancel_check, deskew=deskew,
                )
                summary = (f"rotated: {stats['rotated_success']}, unchanged: {stats['rotated_unchanged']}, "
                           f"failed: {stats['rotated_failed']}, ollama-assisted: {stats['ollama_assisted']}")
            elif kind == 'autofill':
                _log(job, f"[{batch_name}] Backing up, then auto-filling (threshold={threshold})...")
                stats = afc.process_batch_with_backup(
                    batch_dir, threshold=threshold, progress_cb=progress_cb, cancel_check=cancel_check,
                )
                summary = (f"filled: {stats['filled_success']}, unchanged: {stats['filled_unchanged']}, "
                           f"failed: {stats['filled_failed']}")
            else:
                raise ValueError(f"Unknown kind: {kind}")

            elapsed = time.time() - start
            stats['elapsed_seconds'] = round(elapsed, 1)
            stats['batch_name'] = batch_name
            stats['batch_directory'] = batch_dir
            stats['status'] = 'done'

            _log(job, f"[{batch_name}] Done in {cc.format_time(elapsed)} -- {summary}, "
                       f"moved to backup: {stats['moved_to_backup']}, already backed up: {stats['already_backed_up']}")
            for err in stats.get('errors', [])[:20]:
                _log(job, f"[{batch_name}]   ERROR: {err}")

            job['results'].append(stats)
            completed_batches.append(batch)

            if update_status and batch.get('BatchID') is not None and batch_db_creds:
                try:
                    cc.update_batch_status(
                        server=batch_db_creds['server'],
                        driver=batch_db_creds['driver'],
                        database=batch_db_creds['database'],
                        uid=batch_db_creds['uid'],
                        pwd=batch_db_creds['pwd'],
                        batch_id=batch['BatchID'],
                        status_text=pipeline_cfg['out_status'],
                        status_code=pipeline_cfg['out_code'],
                        status_column=status_column,
                    )
                    _log(job, f"[{batch_name}] Database status updated to '{pipeline_cfg['out_status']}'.")
                except Exception as e:
                    _log(job, f"[{batch_name}] Failed to update DB status: {e}")

        except Exception as e:
            _log(job, f"[{batch_name}] FAILED: {e}")
            job['results'].append({'batch_name': batch_name, 'batch_directory': batch_dir,
                                   'status': 'error', 'error': str(e)})

        job['batches_done'] += 1

    if job['status'] == 'running':
        job['status'] = 'done'
    job['finished_at'] = time.time()
    job['current_batch'] = None
    _log(job, f"Job finished with status: {job['status']}")
    ACTIVE_STAGE_JOB[kind] = None

    # -------------------------------------------------------------------------
    # ORDER CHANGE: Chain Rotation → Auto-Fill (skipping Crop)
    # -------------------------------------------------------------------------
    chain_map = settings.get('scheduler', {})
    if kind == 'rotation' and job['status'] == 'done' and completed_batches and chain_map.get('chain_rotation_to_crop'):
        _log(job, f"Chaining {len(completed_batches)} batch(es) into Auto-Fill (skipping Crop)...")
        fill_threshold = settings.get('_chain_fill_threshold', 60)
        _start_job('autofill', completed_batches, fill_threshold, update_status, db_creds,
                   config_store.load(), trigger_label=f"chained from rotation job {job_id}", extra_params={})
    # "Crop -> Auto-Fill" chain is REMOVED per your request

def _new_job(kind, batches):
    job_id = uuid.uuid4().hex[:12]
    job = {
        'id': job_id,
        'kind': kind,
        'status': 'running',
        'started_at': time.time(),
        'finished_at': None,
        'batches_total': len(batches),
        'batches_done': 0,
        'current_batch': None,
        'current_file': None,
        'files_done_current': 0,
        'files_total_current': 0,
        'log': [],
        'results': [],
        'cancel_requested': False,
        'trigger': 'manual',
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
    return job_id


scheduler = Scheduler(_scheduler_get_creds, _scheduler_stage_busy, _scheduler_start_run)

