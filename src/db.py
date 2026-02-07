import sqlite3
import os
import re
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from contextlib import contextmanager

DB_PATH = "data/app.db"
SQL_DIR = "sql"
INIT_SQL_PATH = os.path.join(SQL_DIR, "init.sql")
BASE_SCHEMA_VERSION = "1.0"

INIT_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT UNIQUE NOT NULL,
    prompt TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL,
    prompt TEXT NOT NULL,
    n INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    results TEXT,
    error TEXT,
    config_name TEXT
);

CREATE TABLE IF NOT EXISTS schema_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

INSERT INTO schema_versions (version, applied_at)
VALUES ('1.0', datetime('now'));
"""


@contextmanager
def get_db():
    """Context manager for database connections"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """Initialize database and apply migrations"""
    os.makedirs("data", exist_ok=True)
    _write_init_sql()

    if not os.path.exists(DB_PATH):
        _init_db_from_sql()

    with get_db() as conn:
        _ensure_schema_versions(conn)
        current_version = _get_current_version(conn)

    _apply_migrations(current_version)


def _write_init_sql():
    os.makedirs(SQL_DIR, exist_ok=True)
    content = INIT_SCHEMA_SQL.strip() + "\n"
    if not os.path.exists(INIT_SQL_PATH):
        with open(INIT_SQL_PATH, "w", encoding="utf-8") as file:
            file.write(content)
        return

    with open(INIT_SQL_PATH, "r", encoding="utf-8") as file:
        existing = file.read()
    if existing != content:
        with open(INIT_SQL_PATH, "w", encoding="utf-8") as file:
            file.write(content)


def _init_db_from_sql():
    with open(INIT_SQL_PATH, "r", encoding="utf-8") as file:
        script = file.read()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(script)
        conn.commit()
    finally:
        conn.close()


def _ensure_schema_versions(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
    """)
    row = conn.execute("SELECT COUNT(*) as count FROM schema_versions").fetchone()
    if row["count"] == 0:
        conn.execute(
            "INSERT INTO schema_versions (version, applied_at) VALUES (?, ?)",
            (BASE_SCHEMA_VERSION, datetime.now().isoformat())
        )
    conn.commit()


def _get_current_version(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT version FROM schema_versions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["version"] if row else BASE_SCHEMA_VERSION


def _apply_migrations(current_version: str):
    scripts = _list_migration_scripts()
    current_key = _parse_version(current_version)
    if current_key is None:
        current_key = _parse_version(BASE_SCHEMA_VERSION)

    for version, path in scripts:
        version_key = _parse_version(version)
        if version_key is None or current_key is None:
            continue
        if version_key <= current_key:
            continue
        _execute_migration(path, version)
        current_key = version_key


def _execute_migration(path: str, version: str):
    with open(path, "r", encoding="utf-8") as file:
        script = file.read()

    with get_db() as conn:
        conn.executescript(script)
        conn.execute(
            "INSERT INTO schema_versions (version, applied_at) VALUES (?, ?)",
            (version, datetime.now().isoformat())
        )
        conn.commit()


def _list_migration_scripts() -> List[Tuple[str, str]]:
    if not os.path.exists(SQL_DIR):
        return []

    scripts = []
    for filename in os.listdir(SQL_DIR):
        if filename == "init.sql":
            continue
        match = re.match(r"app_(\d+\.\d+)\.sql$", filename)
        if not match:
            continue
        version = match.group(1)
        scripts.append((version, os.path.join(SQL_DIR, filename)))

    scripts.sort(key=lambda item: _parse_version(item[0]) or (0, 0))
    return scripts


def _parse_version(version: str) -> Optional[Tuple[int, ...]]:
    if not version:
        return None
    parts = version.split(".")
    numbers = []
    for part in parts:
        if not part.isdigit():
            return None
        numbers.append(int(part))
    return tuple(numbers)

def insert_image(filename: str, prompt: str) -> int:
    """Insert a new image record"""
    created_at = datetime.now().isoformat()

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO images (filename, prompt, created_at) VALUES (?, ?, ?)",
            (filename, prompt, created_at)
        )
        conn.commit()
        return cursor.lastrowid


def list_images(page: int = 1, page_size: int = 16) -> Tuple[List[Dict], int]:
    """
    List images with pagination
    Returns: (list of images, total count)
    """
    offset = (page - 1) * page_size

    with get_db() as conn:
        # Get total count
        total = conn.execute("SELECT COUNT(*) as count FROM images").fetchone()["count"]

        # Get paginated results
        cursor = conn.execute(
            """
            SELECT id, filename, created_at
            FROM images
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset)
        )

        images = [dict(row) for row in cursor.fetchall()]
        return images, total


def get_image_by_id(image_id: int) -> Optional[Dict]:
    """Get image record by ID"""
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT id, filename, prompt, created_at FROM images WHERE id = ?",
            (image_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_prompt(image_id: int) -> Optional[str]:
    """Get prompt for an image"""
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT prompt FROM images WHERE id = ?",
            (image_id,)
        )
        row = cursor.fetchone()
        return row["prompt"] if row else None


def delete_image(image_id: int) -> Optional[str]:
    """
    Delete image record and return filename
    Returns filename if deleted, None if not found
    """
    with get_db() as conn:
        # Get filename first
        cursor = conn.execute(
            "SELECT filename FROM images WHERE id = ?",
            (image_id,)
        )
        row = cursor.fetchone()

        if not row:
            return None

        filename = row["filename"]

        # Delete record
        conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
        conn.commit()

        return filename


def cleanup_orphaned_records(output_dir: str = "output") -> int:
    """
    Clean up database records for images that no longer exist on disk
    Returns the number of records deleted
    """
    with get_db() as conn:
        # Get all image records
        cursor = conn.execute("SELECT id, filename FROM images")
        records = cursor.fetchall()
        
        deleted_count = 0
        for record in records:
            image_id = record["id"]
            filename = record["filename"]
            filepath = os.path.join(output_dir, filename)
            
            # Check if file exists
            if not os.path.exists(filepath):
                # File doesn't exist, delete the database record
                conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
                deleted_count += 1
        
        if deleted_count > 0:
            conn.commit()
        
        return deleted_count


# Initialize database on module import
init_db()


# Task database operations
def insert_task(task_id: str, status: str, prompt: str, n: int, config_name: Optional[str] = None) -> int:
    """Insert a new task record"""
    created_at = datetime.now().isoformat()

    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO tasks (task_id, status, prompt, n, created_at, config_name) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, status, prompt, n, created_at, config_name)
        )
        conn.commit()
        return cursor.lastrowid


def update_task_status(task_id: str, status: str, started_at: Optional[str] = None,
                       finished_at: Optional[str] = None, results: Optional[str] = None,
                       error: Optional[str] = None):
    """Update task status and related fields"""
    with get_db() as conn:
        updates = ["status = ?"]
        params = [status]

        if started_at:
            updates.append("started_at = ?")
            params.append(started_at)

        if finished_at:
            updates.append("finished_at = ?")
            params.append(finished_at)

        if results:
            updates.append("results = ?")
            params.append(results)

        if error:
            updates.append("error = ?")
            params.append(error)

        params.append(task_id)

        conn.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?",
            params
        )
        conn.commit()


def reset_running_tasks_to_queued(reason: Optional[str] = None) -> int:
    """Reset running tasks to queued after restart"""
    with get_db() as conn:
        if reason is None:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, started_at = NULL, finished_at = NULL, error = NULL
                WHERE status = ?
                """,
                ("queued", "running")
            )
        else:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, started_at = NULL, finished_at = NULL, error = ?
                WHERE status = ?
                """,
                ("queued", reason, "running")
            )
        conn.commit()
        return cursor.rowcount


def get_task_by_id(task_id: str) -> Optional[Dict]:
    """Get task by task_id"""
    with get_db() as conn:
        cursor = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?",
            (task_id,)
        )
        row = cursor.fetchone()
        if row:
            task = dict(row)
            # Parse results JSON string back to list
            if task.get("results"):
                import json
                task["results"] = json.loads(task["results"])
            else:
                task["results"] = []
            return task
        return None


def list_tasks(limit: int = 10) -> List[Dict]:
    """List recent tasks, ordered by status (running first) then created_at"""
    with get_db() as conn:
        cursor = conn.execute(
            """
            SELECT * FROM tasks
            ORDER BY
                CASE status
                    WHEN 'running' THEN 1
                    WHEN 'queued' THEN 2
                    WHEN 'succeeded' THEN 3
                    WHEN 'failed' THEN 4
                    ELSE 5
                END,
                created_at DESC
            LIMIT ?
            """,
            (limit,)
        )

        tasks = []
        for row in cursor.fetchall():
            task = dict(row)
            # Parse results JSON string back to list
            if task.get("results"):
                import json
                task["results"] = json.loads(task["results"])
            else:
                task["results"] = []
            tasks.append(task)

        return tasks


def list_tasks_by_status(status: str) -> List[Dict]:
    """List tasks by status"""
    with get_db() as conn:
        cursor = conn.execute(
            """
            SELECT * FROM tasks
            WHERE status = ?
            ORDER BY created_at ASC
            """,
            (status,)
        )

        tasks = []
        for row in cursor.fetchall():
            task = dict(row)
            if task.get("results"):
                import json
                task["results"] = json.loads(task["results"])
            else:
                task["results"] = []
            tasks.append(task)

        return tasks
