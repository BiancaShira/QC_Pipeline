from utils.image_utils import count_images

LAST_DB_CREDS = {'server': None, 'driver': None, 'database': None, 'uid': None, 'pwd': None}

# Only these logical filter keys are ever allowed to reach SQL. Anything else
# in a request body is silently ignored -- this is what makes the new
# multi-column filter endpoint schema-safe.
ALLOWED_FILTER_COLUMNS = {
    'batch_id': 'BatchID',
    'batch_name': 'BatchName',
}


def _connect(server, driver, uid, pwd, database="master", timeout=8):
    import pyodbc
    conn_str = (
    f"DRIVER={{{driver}}};"
    f"SERVER={server};"
    f"DATABASE={database};"
    f"UID={uid};"
    f"PWD={pwd};"
    "Encrypt=yes;"
    "TrustServerCertificate=yes;"
)
    return pyodbc.connect(conn_str, timeout=timeout)


def list_server_databases(server, driver, uid, pwd):
    conn = _connect(server, driver, uid, pwd, database="master")
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sys.databases WHERE database_id > 4 ORDER BY name")
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def get_batches_from_db(server, driver, database, uid, pwd,
                        status_filter="Ready For Quality Control",
                        status_column="StatusText"):
    """
    Fetch batches with given status_filter on the specified status_column.
    Also tries to get DocumentType; falls back gracefully if column missing.
    """
    conn = _connect(server, driver, uid, pwd, database=database)
    try:
        cursor = conn.cursor()
        has_doc_type = True
        try:
            cursor.execute(
                f"""
                SELECT BatchID, BatchName, BatchDirectory, DocumentType
                FROM batchtable
                WHERE {status_column} = ?
                """,
                status_filter,
            )
            rows = cursor.fetchall()
        except Exception:
            has_doc_type = False
            cursor.execute(
                f"""
                SELECT BatchID, BatchName, BatchDirectory
                FROM batchtable
                WHERE {status_column} = ?
                """,
                status_filter,
            )
            rows = cursor.fetchall()

        out = []
        for r in rows:
            batch_dir = r.BatchDirectory
            out.append({
                'BatchID': r.BatchID,
                'BatchName': r.BatchName,
                'BatchDirectory': batch_dir,
                'DocumentType': (getattr(r, 'DocumentType', None) if has_doc_type else None),
                'ImageCount': count_images(batch_dir) if batch_dir else 0,
            })
        return out
    finally:
        conn.close()


def search_batches(server, driver, database, uid, pwd,
                   status_filter=None, status_column="StatusText",
                   batch_id=None, batch_name=None):
    """
    Same shape of result as get_batches_from_db, but supports the extra
    JobID/JobName (BatchID/BatchName) filter boxes on top of status --
    all three combine with AND, any of them may be blank/None.

    Column names are never taken from the caller directly -- only the
    fixed ALLOWED_FILTER_COLUMNS keys are ever substituted into SQL, and
    status_column is compared against a short allow-list before use.
    """
    if status_column not in ("StatusText",):
        # widen this tuple if you add more legitimate status columns later
        status_column = "StatusText"

    conditions = []
    params = []
    if status_filter:
        conditions.append(f"{status_column} = ?")
        params.append(status_filter)
    if batch_id:
        conditions.append(f"{ALLOWED_FILTER_COLUMNS['batch_id']} = ?")
        params.append(batch_id)
    if batch_name:
        conditions.append(f"{ALLOWED_FILTER_COLUMNS['batch_name']} LIKE ?")
        params.append(f"%{batch_name}%")

    where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = _connect(server, driver, uid, pwd, database=database)
    try:
        cursor = conn.cursor()
        has_doc_type = True
        try:
            cursor.execute(
                f"SELECT BatchID, BatchName, BatchDirectory, DocumentType FROM batchtable {where_sql}",
                params,
            )
            rows = cursor.fetchall()
        except Exception:
            has_doc_type = False
            cursor.execute(
                f"SELECT BatchID, BatchName, BatchDirectory FROM batchtable {where_sql}",
                params,
            )
            rows = cursor.fetchall()

        out = []
        for r in rows:
            batch_dir = r.BatchDirectory
            out.append({
                'BatchID': r.BatchID,
                'BatchName': r.BatchName,
                'BatchDirectory': batch_dir,
                'DocumentType': (getattr(r, 'DocumentType', None) if has_doc_type else None),
                'ImageCount': count_images(batch_dir) if batch_dir else 0,
            })
        return out
    finally:
        conn.close()


def count_batches_from_db(server, driver, database, uid, pwd,
                          status_filter, status_column="StatusText"):
    conn = _connect(server, driver, uid, pwd, database=database)
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM batchtable WHERE {status_column} = ?", status_filter)
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def update_batch_status(server, driver, database, uid, pwd, batch_id,
                        status_text="Cropped", status_code=20,
                        status_column="StatusText"):
    conn = _connect(server, driver, uid, pwd, database=database)
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            UPDATE batchtable
            SET {status_column} = ?, Status = ?, UserNameLock = '', ProcessNameLock = ''
            WHERE BatchID = ?
            """,
            status_text, status_code, batch_id,
        )
        conn.commit()
        return True
    finally:
        conn.close()


def distinct_statuses(server, driver, database, uid, pwd, status_column="StatusText"):
    """Fetch distinct values from the given status column."""
    conn = _connect(server, driver, uid, pwd, database=database)
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT DISTINCT {status_column} FROM batchtable ORDER BY {status_column}")
        return [row[0] for row in cursor.fetchall()]
    finally:
        conn.close()


def _parse_servers(server_str):
    """Split comma-separated server list, strip whitespace."""
    return [s.strip() for s in server_str.split(',') if s.strip()]