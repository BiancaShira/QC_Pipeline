import time
import logging

logger = logging.getLogger("qcc_autocrop")

def _log(job, line):
    stamped = f"{time.strftime('%H:%M:%S')}  {line}"
    job['log'].append(stamped)
    if len(job['log']) > 500:
        job['log'] = job['log'][-500:]
    logger.info(f"[{job['kind']}] {line}")