"""
scheduler.py
------------
Background thread that checks each stage (rotation, crop, autofill) for
auto-run conditions.

CHANGE in this version:
  `get_db_creds` is now called PER STAGE: `self._get_db_creds(kind)` instead
  of `self._get_db_creds()`. Previously one shared connection was polled for
  all three stages, which meant connecting a database for one stage silently
  switched (or broke) the other two. app.py now supplies a per-stage lookup.

  Tick frequency is unchanged (CHECK_EVERY_SECONDS = 20) -- that's the
  "checks every few minutes" behaviour that was already correct; each
  stage's own `interval_minutes` / `batch_count_trigger` in Settings decides
  whether that tick actually does anything.
"""
import logging
import threading
import time

import config_store
import cropping_core as cc

logger = logging.getLogger("qcc_autocrop")
CHECK_EVERY_SECONDS = 20


class Scheduler:
    def __init__(self, get_db_creds, get_stage_busy, start_stage_run):
        self._get_db_creds = get_db_creds  # now: get_db_creds(kind) -> creds dict | None
        self._get_stage_busy = get_stage_busy
        self._start_stage_run = start_stage_run
        self._last_auto_run = {'rotation': 0.0, 'crop': 0.0, 'autofill': 0.0}
        self._last_check_error = {'rotation': None, 'crop': None, 'autofill': None}
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Scheduler thread started.")

    def status(self):
        return {
            'running': bool(self._thread and self._thread.is_alive()),
            'last_auto_run': dict(self._last_auto_run),
            'last_check_error': dict(self._last_check_error),
        }

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.exception(f"Scheduler tick failed: {e}")
            self._stop.wait(CHECK_EVERY_SECONDS)

    def _tick(self):
        settings = config_store.load()
        sched = settings['scheduler']

        for kind in ('rotation', 'crop', 'autofill'):
            cfg = sched.get(kind, {})
            self._last_check_error[kind] = None
            if not cfg.get('enabled'):
                continue
            if self._get_stage_busy(kind):
                continue

            creds = self._get_db_creds(kind)
            if not creds:
                self._last_check_error[kind] = f"no database connected for the {kind} stage"
                continue

            in_status = settings['pipeline'][kind]['in_status']
            status_column = settings['pipeline'][kind].get('status_column', 'StatusText')
            now = time.time()
            due_by_timer = False
            interval_minutes = cfg.get('interval_minutes') or 0
            if interval_minutes > 0:
                due_by_timer = (now - self._last_auto_run[kind]) >= interval_minutes * 60

            due_by_count = False
            count_trigger = cfg.get('batch_count_trigger') or 0
            batch_count = None
            if count_trigger > 0:
                try:
                    batch_count = cc.count_batches_from_db(
                        creds['server'], creds['driver'], creds['database'],
                        creds['uid'], creds['pwd'], in_status, status_column=status_column,
                    )
                    due_by_count = batch_count >= count_trigger
                except Exception as e:
                    self._last_check_error[kind] = str(e)
                    continue

            if not (due_by_timer or due_by_count):
                continue

            try:
                batches = cc.get_batches_from_db(
                    creds['server'], creds['driver'], creds['database'],
                    creds['uid'], creds['pwd'], status_filter=in_status,
                    status_column=status_column,
                )
            except Exception as e:
                self._last_check_error[kind] = str(e)
                continue

            self._last_auto_run[kind] = now
            if not batches:
                continue

            reason = f"timer ({interval_minutes}m)" if due_by_timer else f"batch count ({batch_count}/{count_trigger})"
            logger.info(f"Scheduler auto-triggering {kind} on {len(batches)} batch(es) -- {reason}")
            self._start_stage_run(kind, batches, reason, creds)