import threading

JOBS = {}
JOBS_LOCK = threading.Lock()

ACTIVE_STAGE_JOB = {
    "rotation": None,
    "crop": None,
    "autofill": None,
}

LAST_DB_CREDS = {
    "server": None,
    "driver": None,
    "database": None,
    "uid": None,
    "pwd": None,
}

STAGE_DB_CREDS = {'rotation': None, 'crop': None, 'autofill': None}