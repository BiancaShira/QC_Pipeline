"""
QCC Pipeline -- Flask UI around cropping_core.py + orientation_core.py + autofill_core.py
"""
import os
import csv
import io
import logging
import threading
import time
import uuid
from pathlib import Path
from utils.log import _log

from flask import Flask, jsonify, render_template, request, Response

import config_store
import cropping_core as cc
import orientation_core as oc
import autofill_core as afc
from scheduler import Scheduler

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("qcc_autocrop")

app = Flask(__name__)

JOBS = {}
JOBS_LOCK = threading.Lock()
ACTIVE_STAGE_JOB = {'rotation': None, 'crop': None, 'autofill': None}
LAST_DB_CREDS = {'server': None, 'driver': None, 'database': None, 'uid': None, 'pwd': None}


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





def _resolve_model_paths(settings, document_type):
    profiles = settings.get('orientation_models') or []
    profile = oc.pick_profile(profiles, document_type)
    if not profile or not profile.get('model_paths'):
        return None, (profile or {}).get('name')
    return profile['model_paths'], profile['name']


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


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------

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

scheduler = Scheduler(_scheduler_get_creds, _scheduler_stage_busy, _scheduler_start_run)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html')


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    return jsonify({'ok': True, 'settings': config_store.load()})

@app.route('/api/settings', methods=['POST'])
def api_settings_patch():
    patch = request.get_json(force=True) or {}
    merged = config_store.update(patch)
    return jsonify({'ok': True, 'settings': merged})

# Theme toggle
@app.route('/api/theme', methods=['POST'])
def api_theme():
    data = request.get_json(force=True) or {}
    theme = data.get('theme', 'dark')
    if theme not in ('dark', 'light'):
        return jsonify({'ok': False, 'error': 'Invalid theme'}), 400
    config_store.update({'theme': theme})
    return jsonify({'ok': True, 'theme': theme})


# ---------------------------------------------------------------------------
# Ollama test
# ---------------------------------------------------------------------------

@app.route('/api/ollama/test', methods=['POST'])
def api_ollama_test():
    import requests
    data = request.get_json(force=True) or {}
    base_url = (data.get('base_url') or 'http://localhost:11434').rstrip('/')
    model = data.get('model') or 'llava'
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=6)
        resp.raise_for_status()
        names = [m.get('name', '') for m in resp.json().get('models', [])]
        has_model = any(model in n for n in names)
        return jsonify({'ok': True, 'reachable': True, 'has_model': has_model, 'models': names})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Database source – with multiple servers support
# ---------------------------------------------------------------------------

def _parse_servers(server_str):
    """Split comma-separated server list, strip whitespace."""
    return [s.strip() for s in server_str.split(',') if s.strip()]

@app.route('/api/db/test', methods=['POST'])
def api_db_test():
    data = request.get_json(force=True) or {}
    server_str = data.get('server', '').strip()
    driver = data.get('driver', '').strip()
    uid = data.get('uid', '').strip()
    pwd = data.get('pwd', '')
    if not server_str or not driver or not uid:
        return jsonify({'ok': False, 'error': "Server, driver and username are required."}), 400

    servers = _parse_servers(server_str)
    if not servers:
        return jsonify({'ok': False, 'error': "No valid server names."}), 400

    # Try first server to list databases (assume all servers have same DBs, or we just need one)
    try:
        dbs = cc.list_server_databases(servers[0], driver, uid, pwd)
    except Exception as e:
        return jsonify({'ok': False, 'error': f"Connection to {servers[0]} failed: {e}"}), 500

    LAST_DB_CREDS.update({'server': server_str, 'driver': driver, 'uid': uid, 'pwd': pwd})
    config_store.update({'db_last': {'server': server_str, 'driver': driver, 'uid': uid}})
    return jsonify({'ok': True, 'databases': dbs})


@app.route('/api/db/distinct', methods=['POST'])
def api_db_distinct():
    data = request.get_json(force=True) or {}
    required = ['server', 'driver', 'database', 'uid', 'pwd']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'ok': False, 'error': f"Missing: {', '.join(missing)}"}), 400
    status_column = data.get('status_column', 'StatusText')
    try:
        values = cc.distinct_statuses(
            data['server'], data['driver'], data['database'],
            data['uid'], data['pwd'], status_column=status_column
        )
        return jsonify({'ok': True, 'values': values})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/db/batches', methods=['POST'])
def api_db_batches():
    data = request.get_json(force=True) or {}
    required = ['server', 'driver', 'database', 'uid', 'pwd']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'ok': False, 'error': f"Missing: {', '.join(missing)}"}), 400

    server_str = data['server']
    servers = _parse_servers(server_str)
    status_filter = data.get('status_filter', 'Ready For Quality Control')
    status_column = data.get('status_column', 'StatusText')
    all_batches = []
    for server in servers:
        try:
            batches = cc.get_batches_from_db(
                server, data['driver'], data['database'],
                data['uid'], data['pwd'],
                status_filter=status_filter,
                status_column=status_column,
            )
            # Tag each batch with its server credentials for later updates
            for b in batches:
                b['_server_creds'] = {
                    'server': server,
                    'driver': data['driver'],
                    'database': data['database'],
                    'uid': data['uid'],
                    'pwd': data['pwd'],
                }
            all_batches.extend(batches)
        except Exception as e:
            # Log error but continue with other servers
            logger.warning(f"Failed to fetch from server {server}: {e}")
    # Store the first server's creds as default for LAST_DB_CREDS
    if servers:
        LAST_DB_CREDS.update({'server': servers[0], 'driver': data['driver'],
                              'database': data['database'], 'uid': data['uid'], 'pwd': data['pwd']})
        config_store.update({'db_last': {'server': server_str, 'driver': data['driver'],
                                         'uid': data['uid'], 'database': data['database']}})
    return jsonify({'ok': True, 'batches': all_batches})


# ---------------------------------------------------------------------------
# Folder source (unchanged)
# ---------------------------------------------------------------------------

@app.route('/api/folder/discover', methods=['POST'])
def api_folder_discover():
    data = request.get_json(force=True) or {}
    parent_folder = (data.get('parent_folder') or '').strip()
    if not parent_folder:
        return jsonify({'ok': False, 'error': 'parent_folder is required'}), 400
    try:
        batches = cc.discover_batches_from_folder(parent_folder)
        return jsonify({'ok': True, 'batches': batches})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/folder/prepare', methods=['POST'])
def api_folder_prepare():
    data = request.get_json(force=True) or {}
    batches = data.get('batches') or []
    if not batches:
        return jsonify({'ok': False, 'error': 'No batches supplied'}), 400
    results = cc.prepare_backup_folders(batches)
    return jsonify({'ok': True, 'results': results})


# ---------------------------------------------------------------------------
# Preview (supports rotation with deskew, crop, autofill)
# ---------------------------------------------------------------------------

@app.route('/api/preview', methods=['POST'])
def api_preview():
    data = request.get_json(force=True) or {}
    kind = data.get('kind', 'crop')
    batch_dir = data.get('batch_directory')
    sample_size = int(data.get('sample_size', 6))
    if not batch_dir:
        return jsonify({'ok': False, 'error': 'batch_directory is required'}), 400
    if not Path(batch_dir).exists():
        return jsonify({'ok': False, 'error': f'Directory does not exist: {batch_dir}'}), 400
    try:
        if kind == 'crop':
            threshold = int(data.get('threshold', 100))
            result = cc.generate_preview(batch_dir, threshold=threshold, sample_size=sample_size)
        elif kind == 'rotation':
            settings = config_store.load()
            model_paths, profile_name = _resolve_model_paths(settings, data.get('document_type'))
            if not model_paths:
                return jsonify({'ok': False, 'error': 'No orientation model configured for this document type.'}), 400
            deskew = data.get('deskew', False)
            result = oc.generate_preview(batch_dir, model_paths=model_paths, sample_size=sample_size,
                                         ollama_cfg=settings.get('ollama'), deskew=deskew)
            result['model_profile'] = profile_name
        elif kind == 'autofill':
            threshold = int(data.get('threshold', 60))
            result = afc.generate_preview(batch_dir, threshold=threshold, sample_size=sample_size)
        else:
            return jsonify({'ok': False, 'error': f'Unknown kind: {kind}'}), 400
        return jsonify({'ok': True, **result})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Live run (start)
# ---------------------------------------------------------------------------

@app.route('/api/run/start', methods=['POST'])
def api_run_start():
    data = request.get_json(force=True) or {}
    kind = data.get('kind')
    if kind not in ('rotation', 'crop', 'autofill'):
        return jsonify({'ok': False, 'error': "kind must be 'rotation', 'crop', or 'autofill'"}), 400
    batches = data.get('batches') or []
    threshold = int(data.get('threshold', 100))
    update_status = bool(data.get('update_status', False))
    db_creds = data.get('db_creds')
    extra_params = {}
    if kind == 'rotation':
        extra_params['deskew'] = data.get('deskew', False)

    if not batches:
        return jsonify({'ok': False, 'error': 'No batches supplied'}), 400
    if ACTIVE_STAGE_JOB.get(kind):
        return jsonify({'ok': False, 'error': f'A {kind} job is already running.'}), 409

    settings = config_store.load()
    if kind == 'crop':
        settings['_chain_crop_threshold'] = threshold
    elif kind == 'autofill':
        settings['_chain_fill_threshold'] = threshold
    job_id = _start_job(kind, batches, threshold, update_status, db_creds, settings, extra_params=extra_params)
    return jsonify({'ok': True, 'job_id': job_id})


@app.route('/api/run/status/<job_id>')
def api_run_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'Unknown job id'}), 404
    return jsonify({'ok': True, **job})

@app.route('/api/run/cancel/<job_id>', methods=['POST'])
def api_run_cancel(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'Unknown job id'}), 404
    job['cancel_requested'] = True
    return jsonify({'ok': True})

@app.route('/api/run/active')
def api_run_active():
    return jsonify({'ok': True, 'active': dict(ACTIVE_STAGE_JOB)})


# ---------------------------------------------------------------------------
# CSV report download
# ---------------------------------------------------------------------------

@app.route('/api/run/csv/<job_id>')
def api_run_csv(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({'ok': False, 'error': 'Unknown job'}), 404
    if job['status'] not in ('done', 'cancelled'):
        return jsonify({'ok': False, 'error': 'Job not finished yet'}), 400

    results = job.get('results', [])
    if not results:
        return Response("No results", mimetype='text/csv')

    # Determine columns based on kind
    kind = job['kind']
    if kind == 'rotation':
        headers = ['batch_name', 'total_images', 'rotated_success', 'rotated_unchanged',
                   'rotated_failed', 'ollama_assisted', 'moved_to_backup',
                   'already_backed_up', 'elapsed_seconds', 'status']
    elif kind == 'crop':
        headers = ['batch_name', 'total_images', 'cropped_success', 'cropped_unchanged',
                   'cropped_failed', 'moved_to_backup', 'already_backed_up',
                   'elapsed_seconds', 'status']
    elif kind == 'autofill':
        headers = ['batch_name', 'total_images', 'filled_success', 'filled_unchanged',
                   'filled_failed', 'moved_to_backup', 'already_backed_up',
                   'elapsed_seconds', 'status']
    else:
        headers = ['batch_name', 'total_images', 'status']

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, extrasaction='ignore')
    writer.writeheader()
    for r in results:
        writer.writerow(r)
    csv_data = output.getvalue()
    return Response(csv_data, mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename=report_{job_id}.csv'})


# ---------------------------------------------------------------------------
# Scheduler check endpoint (for manual trigger)
# ---------------------------------------------------------------------------

@app.route('/api/scheduler/check/<kind>', methods=['POST'])
def api_scheduler_check(kind):
    if kind not in ('rotation', 'crop', 'autofill'):
        return jsonify({'ok': False, 'error': "kind must be 'rotation', 'crop', or 'autofill'"}), 400
    creds = _scheduler_get_creds()
    if not creds:
        return jsonify({'ok': False, 'error': 'Connect to a database first.'}), 400
    if ACTIVE_STAGE_JOB.get(kind):
        return jsonify({'ok': False, 'error': f'A {kind} job is already running.'}), 409

    settings = config_store.load()
    pipeline = settings['pipeline'][kind]
    in_status = pipeline['in_status']
    status_column = pipeline.get('status_column', 'StatusText')

    # Parse multiple servers
    server_str = creds['server']
    servers = _parse_servers(server_str)
    all_batches = []
    for server in servers:
        try:
            batches = cc.get_batches_from_db(
                server, creds['driver'], creds['database'],
                creds['uid'], creds['pwd'],
                status_filter=in_status,
                status_column=status_column,
            )
            for b in batches:
                b['_server_creds'] = {
                    'server': server,
                    'driver': creds['driver'],
                    'database': creds['database'],
                    'uid': creds['uid'],
                    'pwd': creds['pwd'],
                }
            all_batches.extend(batches)
        except Exception as e:
            logger.warning(f"Scheduler check failed for server {server}: {e}")

    if not all_batches:
        return jsonify({'ok': True, 'found': 0, 'message': f"No batches with status '{in_status}'."})

    threshold = settings.get('_chain_crop_threshold', 100) if kind == 'crop' else \
                settings.get('_chain_fill_threshold', 60) if kind == 'autofill' else 100
    extra_params = {'deskew': False} if kind == 'rotation' else {}
    job_id = _start_job(kind, all_batches, threshold, update_status=True,
                        db_creds=creds, settings=settings,
                        trigger_label='manual check-now', extra_params=extra_params)
    return jsonify({'ok': True, 'found': len(all_batches), 'job_id': job_id})


@app.route('/api/scheduler/status')
def api_scheduler_status():
    return jsonify({'ok': True, **scheduler.status(), 'active_jobs': dict(ACTIVE_STAGE_JOB),
                    'has_db_creds': _scheduler_get_creds() is not None})


if __name__ == '__main__':
    scheduler.start()
    debug = os.environ.get('QCC_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=8000, debug=debug, use_reloader=False)