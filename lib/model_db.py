import sqlite3
import json
import os

DB_PATH = os.path.join(os.path.dirname(__file__), 'qc_pipeline.sqlite3')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS orientation_models (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            match TEXT DEFAULT '',
            model_paths TEXT NOT NULL,   -- JSON array, stored as text
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def row_to_dict(row):
    try:
        paths = json.loads(row['model_paths']) if row['model_paths'] else []
    except (json.JSONDecodeError, TypeError):
        paths = []

    return {
        'id': row['id'],
        'name': row['name'],
        'match': row['match'] or '',
        'model_paths': paths,
    }

def list_db_models():
    """Renamed from list_models to explicitly distinguish from filesystem model checks."""
    conn = get_db()
    rows = conn.execute('SELECT * FROM orientation_models ORDER BY id').fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]

def add_model(name, match, model_paths):
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO orientation_models (name, match, model_paths) VALUES (?, ?, ?)',
        (name, match or '', json.dumps(model_paths))
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id

def update_model(model_id, name, match, model_paths):
    conn = get_db()
    conn.execute(
        'UPDATE orientation_models SET name=?, match=?, model_paths=? WHERE id=?',
        (name, match or '', json.dumps(model_paths), model_id)
    )
    conn.commit()
    conn.close()

def delete_model(model_id):
    conn = get_db()
    conn.execute('DELETE FROM orientation_models WHERE id=?', (model_id,))
    conn.commit()
    conn.close()

def replace_all_models(models):
    """Used by the Settings 'Save model profiles' bulk-save button."""
    conn = get_db()
    conn.execute('DELETE FROM orientation_models')
    for m in models:
        conn.execute(
            'INSERT INTO orientation_models (name, match, model_paths) VALUES (?, ?, ?)',
            (m.get('name', ''), m.get('match', ''), json.dumps(m.get('model_paths', [])))
        )
    conn.commit()
    conn.close()