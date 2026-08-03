from utils.helpers import count_images

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