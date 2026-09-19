"""MediaDock persistence boundary (Stage-005).

SQLite storage for Task records and state events, with versioned migrations,
a pre-migration backup and a graceful degraded mode: if the database cannot
be opened, migrated or written, the server keeps working from memory and
reports `degraded` through `/health`.

Responsibilities (Stage-005.md 5.5):

    Store          -> connection, migrations, backup/restore, SQL
    TaskPersister  -> what TaskManager calls; swallows and logs SQL errors

TaskManager stays the only writer of Task state; this module only stores
snapshots. stdlib only (`sqlite3`, `shutil`, `os`).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

DB_ENV_VAR = "MEDIADOCK_DB"
DEFAULT_DB_NAME = "tasks.db"
PURGE_KEEP_DEFAULT = 200

# Column order used for both INSERT and SELECT; one column per Task field.
TASK_COLUMNS: Tuple[str, ...] = (
    "task_id", "type", "status", "url", "platform", "title", "percent",
    "speed", "eta", "file_path", "error_code", "error_message",
    "created_at", "started_at", "updated_at", "completed_at",
    "completion_order",
)

MIGRATIONS: List[Tuple[int, Sequence[str]]] = [
    (1, (
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
        """CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            type TEXT NOT NULL DEFAULT 'download',
            status TEXT NOT NULL DEFAULT 'pending',
            url TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            percent REAL NOT NULL DEFAULT 0,
            speed TEXT NOT NULL DEFAULT '',
            eta TEXT NOT NULL DEFAULT '',
            file_path TEXT NOT NULL DEFAULT '',
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            completion_order INTEGER
        )""",
        "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
        "CREATE INDEX IF NOT EXISTS idx_tasks_completed_at "
        "ON tasks(completed_at)",
        """CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id)",
    )),
    (2, (
        "CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at)",
    )),
]


class StoreError(Exception):
    """Schema/migration failure that must not be silently ignored."""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def resolve_db_path(explicit: Optional[str] = None) -> str:
    """`explicit` > `MEDIADOCK_DB` > `<repo>/tasks.db`. `:memory:` allowed."""
    if explicit:
        return explicit
    env = os.environ.get(DB_ENV_VAR)
    if env:
        return env
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        DEFAULT_DB_NAME)


def _is_memory_path(path: str) -> bool:
    return str(path) in (":memory:", "")


class Store:
    """Thin, thread-safe wrapper over one SQLite database file."""

    def __init__(self, db_path: str, logger: Optional[Callable[..., None]] = None):
        self.db_path = str(db_path)
        self._log = logger or (lambda *a: None)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self.version = 0
        self.degraded = False
        self.reason = ""

    # -- connection --------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(self.db_path, timeout=5.0,
                                             isolation_level=None,
                                             check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    @property
    def is_memory(self) -> bool:
        return _is_memory_path(self.db_path)

    def _degrade(self, reason: str) -> None:
        self.degraded = True
        if not self.reason:
            self.reason = reason
        self._log(f"storage degraded: {reason}")

    def info(self) -> Dict[str, Any]:
        return {"kind": "sqlite", "db": self.db_path,
                "schema_version": self.version, "degraded": self.degraded,
                "reason": self.reason}

    # -- migrations --------------------------------------------------
    def _current_version(self) -> int:
        try:
            row = self.connect().execute(
                "SELECT MAX(version) FROM schema_version").fetchone()
        except sqlite3.Error:
            return 0
        if row is None or row[0] is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return 0

    def _write_version(self, version: int) -> None:
        conn = self.connect()
        row = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
        if row and int(row[0]) > 0:
            conn.execute("UPDATE schema_version SET version=?", (version,))
        else:
            conn.execute("INSERT INTO schema_version (version) VALUES (?)",
                         (version,))

    def target_version(self, migrations: Optional[Sequence] = None) -> int:
        items = migrations if migrations is not None else MIGRATIONS
        return max((int(v) for v, _ in items), default=0)

    def backup(self) -> Optional[str]:
        """Copy the database file to `<db>.bak`; None for in-memory DBs."""
        if self.is_memory:
            return None
        target = self.db_path + ".bak"
        try:
            shutil.copyfile(self.db_path, target)
        except OSError as exc:
            raise StoreError(f"backup failed: {exc}") from exc
        self._log(f"storage backup written: {target}")
        return target

    def initialize(self, migrations: Optional[Sequence] = None) -> int:
        """Apply pending migrations in one transaction; returns the version."""
        items = sorted(migrations if migrations is not None else MIGRATIONS,
                       key=lambda item: int(item[0]))
        target = self.target_version(items)
        with self._lock:
            conn = self.connect()
            current = self._current_version()
            if current >= target:
                self.version = current
                return current
            if current > 0:
                self.backup()
            try:
                conn.execute("BEGIN")
                for version, statements in items:
                    if int(version) <= current:
                        continue
                    for sql in statements:
                        conn.execute(sql)
                self._write_version(target)
                conn.execute("COMMIT")
            except (sqlite3.Error, StoreError) as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                self.version = current
                self._degrade(f"migration to v{target} failed: {exc}")
                raise StoreError(f"migration failed: {exc}") from exc
            self.version = target
            self._log(f"storage migrated to schema v{target}")
            return target

    # -- task records ------------------------------------------------
    def _row_values(self, data: Dict[str, Any]) -> Tuple:
        values: List[Any] = []
        for column in TASK_COLUMNS:
            value = data.get(column)
            if column == "percent":
                try:
                    values.append(float(value or 0.0))
                except (TypeError, ValueError):
                    values.append(0.0)
            elif column == "completion_order":
                if value is None or value == "":
                    values.append(None)
                else:
                    try:
                        values.append(int(value))
                    except (TypeError, ValueError):
                        values.append(None)
            else:
                values.append("" if value is None else str(value))
        return tuple(values)

    def save_task(self, data: Dict[str, Any]) -> bool:
        """INSERT OR REPLACE one Task snapshot; never raises."""
        if not isinstance(data, dict) or not data.get("task_id"):
            return False
        sql = ("INSERT OR REPLACE INTO tasks ({columns}) "
               "VALUES ({marks})").format(
            columns=", ".join(TASK_COLUMNS),
            marks=", ".join("?" for _ in TASK_COLUMNS))
        with self._lock:
            try:
                self.connect().execute(sql, self._row_values(data))
            except (sqlite3.Error, AttributeError) as exc:
                self._degrade(f"write failed for {data.get('task_id')}: {exc}")
                return False
        return True

    def save_tasks(self, items: Sequence[Dict[str, Any]]) -> int:
        return sum(1 for item in items or () if self.save_task(item))

    def load_tasks(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            try:
                rows = self.connect().execute(
                    "SELECT * FROM tasks").fetchall()
            except sqlite3.Error as exc:
                self._degrade(f"read failed: {exc}")
                return out
        for row in rows:
            data = {column: row[column] for column in TASK_COLUMNS}
            task_id = data.get("task_id")
            if task_id:
                out[str(task_id)] = data
        return out

    def count_tasks(self) -> int:
        with self._lock:
            try:
                row = self.connect().execute(
                    "SELECT COUNT(*) FROM tasks").fetchone()
            except sqlite3.Error:
                return 0
        return int(row[0]) if row else 0

    # -- events ------------------------------------------------------
    def record_event(self, task_id: str, kind: str,
                     detail: str = "") -> bool:
        with self._lock:
            try:
                self.connect().execute(
                    "INSERT INTO task_events (task_id, kind, detail, "
                    "created_at) VALUES (?, ?, ?, ?)",
                    (str(task_id or ""), str(kind or ""), str(detail or ""),
                     _now_iso()))
            except sqlite3.Error as exc:
                self._degrade(f"event write failed for {task_id}: {exc}")
                return False
        return True

    def events(self, task_id: Optional[str] = None,
               limit: int = 50) -> List[Dict[str, Any]]:
        """Latest `limit` events for a task (or all), returned oldest first."""
        try:
            window = max(1, int(limit))
        except (TypeError, ValueError):
            window = 50
        sql = ("SELECT id, task_id, kind, detail, created_at FROM ("
               "SELECT id, task_id, kind, detail, created_at FROM task_events")
        params: List[Any] = []
        if task_id:
            sql += " WHERE task_id = ?"
            params.append(str(task_id))
        sql += " ORDER BY id DESC LIMIT ?) ORDER BY id ASC"
        params.append(window)
        with self._lock:
            try:
                rows = self.connect().execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                self._degrade(f"event read failed: {exc}")
                return []
        return [dict(row) for row in rows]

    def has_events(self, task_id: str) -> bool:
        with self._lock:
            try:
                row = self.connect().execute(
                    "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
                    (str(task_id),)).fetchone()
            except sqlite3.Error:
                return False
        return bool(row and int(row[0]) > 0)

    # -- deletion / cleanup ------------------------------------------
    def delete_task(self, task_id: str, record: bool = True) -> bool:
        """Delete a task row + its events. `record=False` skips the audit row."""
        with self._lock:
            try:
                conn = self.connect()
                cursor = conn.execute("DELETE FROM tasks WHERE task_id = ?",
                                      (str(task_id),))
                removed = cursor.rowcount > 0
                conn.execute("DELETE FROM task_events WHERE task_id = ?",
                             (str(task_id),))
            except sqlite3.Error as exc:
                self._degrade(f"delete failed for {task_id}: {exc}")
                return False
        if removed and record:
            self.record_event(task_id, "deleted", "")
        return bool(removed)

    def purge_terminal(self, keep: int = PURGE_KEEP_DEFAULT) -> int:
        """Keep the newest `keep` terminal tasks; delete older records."""
        try:
            window = max(0, int(keep))
        except (TypeError, ValueError):
            window = PURGE_KEEP_DEFAULT
        with self._lock:
            try:
                rows = self.connect().execute(
                    "SELECT task_id FROM tasks WHERE status IN "
                    "('completed','error','cancelled') "
                    "ORDER BY completed_at DESC, completion_order DESC, "
                    "task_id ASC").fetchall()
            except sqlite3.Error as exc:
                self._degrade(f"purge scan failed: {exc}")
                return 0
        victims = [str(row[0]) for row in rows[window:]]
        removed = 0
        for task_id in victims:
            # 容量清理不写审计事件，否则事件表会随清理一起长大
            if self.delete_task(task_id, record=False):
                removed += 1
        if removed:
            self._log(f"storage purged {removed} terminal task(s)")
        return removed


def restore_backup(db_path: Optional[str] = None,
                   logger: Optional[Callable[..., None]] = None) -> bool:
    """Roll back a database file from `<db>.bak`; returns True when copied."""
    log = logger or (lambda *a: None)
    path = resolve_db_path(db_path)
    backup = path + ".bak"
    if _is_memory_path(path) or not os.path.isfile(backup):
        log(f"restore skipped: no backup at {backup}")
        return False
    shutil.copyfile(backup, path)
    log(f"restored {path} from {backup}")
    return True


class TaskPersister:
    """Adapter handed to `TaskManager`; all failures end up in the log."""

    def __init__(self, store: Optional[Store],
                 logger: Optional[Callable[..., None]] = None):
        self.store = store
        self._log = logger or (lambda *a: None)

    def save_task(self, data: Dict[str, Any]) -> bool:
        if self.store is None:
            return False
        try:
            return self.store.save_task(data)
        except Exception as exc:  # noqa: BLE001 - persistence must never raise
            self._log(f"persist save failed: {exc}")
            return False

    def delete_task(self, task_id: str) -> bool:
        if self.store is None:
            return False
        try:
            return self.store.delete_task(task_id)
        except Exception as exc:  # noqa: BLE001 - persistence must never raise
            self._log(f"persist delete failed: {exc}")
            return False

    def record_event(self, task_id: str, kind: str, detail: str = "") -> bool:
        if self.store is None:
            return False
        try:
            return self.store.record_event(task_id, kind, detail)
        except Exception as exc:  # noqa: BLE001
            self._log(f"persist event failed: {exc}")
            return False


def open_store(db_path: Optional[str] = None,
               logger: Optional[Callable[..., None]] = None
               ) -> Tuple[Optional[Store], str]:
    """Open + migrate; returns `(store, reason)`. `store=None` means memory."""
    log = logger or (lambda *a: None)
    path = resolve_db_path(db_path)
    store = Store(path, log)
    try:
        store.connect()
        store.initialize()
        return store, ""
    except (StoreError, sqlite3.Error, OSError, ValueError) as exc:
        log(f"storage unavailable ({path}): {exc}; running without persistence")
        try:
            store.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
        return None, str(exc)
