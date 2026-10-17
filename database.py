import json
import os
import sqlite3
from threading import Lock
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from dotenv import load_dotenv

from models import STATUS_TRANSITIONS, ValidationError, utc_now, validate_task_fields, validate_telegram_chat_id


load_dotenv()

SCHEMA_VERSION = 5
_postgres_schema_lock = Lock()
_initialized_postgres_dsns: set[str] = set()
_sqlite_schema_lock = Lock()
_initialized_sqlite_paths: set[str] = set()


class PostgresConnection:
    is_postgres = True

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError(
                "Supabase database support requires psycopg. Install requirements.txt."
            ) from exc
        self._connection = psycopg.connect(
            dsn, row_factory=dict_row, connect_timeout=10
        )

    def execute(self, query: str, parameters: Any = ()) -> Any:
        if query.lstrip().upper().startswith("PRAGMA"):
            return None
        query = query.replace("?", "%s")
        return self._connection.execute(query, parameters)

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        try:
            if exc_type is not None:
                self.rollback()
        finally:
            self.close()


class ClosingConnection(sqlite3.Connection):
    is_postgres = False

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect(db_path: str | Path = "coordinator.db") -> Any:
    backend = os.getenv("DATABASE_BACKEND", "supabase").strip().lower()
    if backend == "supabase":
        dsn = os.getenv("SUPABASE_DB_URL")
        if not dsn:
            raise RuntimeError(
                "Supabase database is not configured. Set SUPABASE_DB_URL in .env."
            )
        connection = PostgresConnection(dsn)
        try:
            with _postgres_schema_lock:
                if dsn not in _initialized_postgres_dsns:
                    initialize_schema(connection)
                    connection.commit()
                    _initialized_postgres_dsns.add(dsn)
        except Exception:
            connection.close()
            raise
        return connection
    if backend != "sqlite":
        raise RuntimeError("DATABASE_BACKEND must be 'supabase' or 'sqlite'.")
    path = str(db_path)
    if path != ":memory:":
        resolved_path = Path(path).expanduser().resolve()
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        path = str(resolved_path)
    connection = sqlite3.connect(
        path, timeout=10, isolation_level=None, factory=ClosingConnection
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    if path == ":memory:":
        initialize_schema(connection)
    else:
        try:
            with _sqlite_schema_lock:
                if path not in _initialized_sqlite_paths:
                    connection.execute("PRAGMA journal_mode = WAL")
                    initialize_schema(connection)
                    _initialized_sqlite_paths.add(path)
        except Exception:
            connection.close()
            raise
    return connection


def check_database_connection(dsn: str) -> str | None:
    try:
        import psycopg
    except ImportError:
        return "PostgreSQL support is missing. Install the project requirements."
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            connection.execute("SELECT 1")
    except ValueError:
        return "The Supabase database URI could not be parsed. Copy the full Session Pooler URI again."
    except psycopg.Error as exc:
        details = str(exc).lower()
        if "resolve host" in details or "translate host" in details or "getaddrinfo" in details:
            return (
                "The configured database host could not be resolved. In Supabase, copy the "
                "Session Pooler URI from Project Settings → Database → Connection string. "
                "Check that the project is active and that its URI password is URL-encoded."
            )
        if "password authentication failed" in details:
            return "Supabase rejected the database password. Update the Session Pooler URI with the correct database password."
        if "timeout" in details or "timed out" in details:
            return "The database connection timed out. Check the Session Pooler URI and network access."
        return f"Supabase database connection failed ({type(exc).__name__}). Check the Session Pooler URI, password, and SSL setting."
    return None


def initialize_schema(connection: Any) -> None:
    if getattr(connection, "is_postgres", False):
        _initialize_postgres_schema(connection)
        return
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            project_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            goals TEXT NOT NULL DEFAULT '',
            start_date TEXT,
            deadline TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS participants (
            participant_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            telegram_chat_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(project_id),
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            owner_participant_id TEXT NOT NULL REFERENCES participants(participant_id),
            deadline TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('Pending', 'In Progress', 'Blocked', 'Complete')),
            priority TEXT NOT NULL CHECK(priority IN ('Low', 'Medium', 'High')),
            blocker_reason TEXT NOT NULL DEFAULT '',
            next_action TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_follow_up_at TEXT,
            last_reply_at TEXT,
            archived_at TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            telegram_update_id TEXT UNIQUE,
            telegram_message_id TEXT,
            task_id TEXT REFERENCES tasks(task_id),
            participant_id TEXT REFERENCES participants(participant_id),
            parent_message_id TEXT REFERENCES messages(message_id),
            sender_chat_id TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('incoming', 'outgoing')),
            body TEXT NOT NULL,
            received_at TEXT NOT NULL,
            ai_interpretation TEXT,
            confidence REAL,
            resulting_action TEXT,
            analysis_error TEXT,
            source TEXT NOT NULL DEFAULT 'telegram',
            sender_user_id TEXT,
            sender_name TEXT,
            edited_at TEXT,
            deleted_at TEXT
        );
        CREATE TABLE IF NOT EXISTS message_reactions (
            message_id TEXT NOT NULL REFERENCES messages(message_id),
            actor_id TEXT NOT NULL,
            emoji TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(message_id, actor_id, emoji)
        );
        CREATE TABLE IF NOT EXISTS message_reads (
            message_id TEXT NOT NULL REFERENCES messages(message_id),
            reader_id TEXT NOT NULL,
            read_at TEXT NOT NULL,
            PRIMARY KEY(message_id, reader_id)
        );
        CREATE TABLE IF NOT EXISTS task_activity (
            activity_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            field_name TEXT,
            old_value TEXT,
            new_value TEXT,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_dependencies (
            prerequisite_task_id TEXT NOT NULL REFERENCES tasks(task_id),
            dependent_task_id TEXT NOT NULL REFERENCES tasks(task_id),
            created_at TEXT NOT NULL,
            PRIMARY KEY(prerequisite_task_id, dependent_task_id),
            CHECK(prerequisite_task_id != dependent_task_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_participant_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline);
        CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);
        CREATE INDEX IF NOT EXISTS idx_activity_task_time ON task_activity(task_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_dependencies_dependent ON task_dependencies(dependent_task_id);
        """
    )
    project_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(projects)").fetchall()
    }
    project_additions = {
        "description": "TEXT NOT NULL DEFAULT ''",
        "goals": "TEXT NOT NULL DEFAULT ''",
        "start_date": "TEXT",
        "deadline": "TEXT",
    }
    for column, definition in project_additions.items():
        if column not in project_columns:
            connection.execute(f"ALTER TABLE projects ADD COLUMN {column} {definition}")
    connection.execute(
        "INSERT OR IGNORE INTO projects(project_id, name, created_at) VALUES(1, ?, ?)",
        ("Student Team Project", utc_now()),
    )
    message_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(messages)").fetchall()
    }
    if "task_id" not in message_columns:
        connection.execute("ALTER TABLE messages ADD COLUMN task_id TEXT REFERENCES tasks(task_id)")
    message_additions = {
        "parent_message_id": "TEXT REFERENCES messages(message_id)",
        "source": "TEXT NOT NULL DEFAULT 'telegram'",
        "sender_user_id": "TEXT",
        "sender_name": "TEXT",
        "edited_at": "TEXT",
        "deleted_at": "TEXT",
    }
    for column, definition in message_additions.items():
        if column not in message_columns:
            connection.execute(f"ALTER TABLE messages ADD COLUMN {column} {definition}")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_task_time ON messages(task_id, received_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_message_id)"
    )
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if current < SCHEMA_VERSION:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _initialize_postgres_schema(connection: PostgresConnection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            project_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            goals TEXT NOT NULL DEFAULT '',
            start_date TEXT,
            deadline TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS participants (
            participant_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            telegram_chat_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(project_id),
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            owner_participant_id TEXT NOT NULL REFERENCES participants(participant_id),
            deadline TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('Pending', 'In Progress', 'Blocked', 'Complete')),
            priority TEXT NOT NULL CHECK(priority IN ('Low', 'Medium', 'High')),
            blocker_reason TEXT NOT NULL DEFAULT '',
            next_action TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_follow_up_at TEXT,
            last_reply_at TEXT,
            archived_at TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            telegram_update_id TEXT UNIQUE,
            telegram_message_id TEXT,
            task_id TEXT REFERENCES tasks(task_id),
            participant_id TEXT REFERENCES participants(participant_id),
            parent_message_id TEXT REFERENCES messages(message_id),
            sender_chat_id TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('incoming', 'outgoing')),
            body TEXT NOT NULL,
            received_at TEXT NOT NULL,
            ai_interpretation TEXT,
            confidence REAL,
            resulting_action TEXT,
            analysis_error TEXT,
            source TEXT NOT NULL DEFAULT 'telegram',
            sender_user_id TEXT,
            sender_name TEXT,
            edited_at TEXT,
            deleted_at TEXT
        );
        CREATE TABLE IF NOT EXISTS message_reactions (
            message_id TEXT NOT NULL REFERENCES messages(message_id),
            actor_id TEXT NOT NULL,
            emoji TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(message_id, actor_id, emoji)
        );
        CREATE TABLE IF NOT EXISTS message_reads (
            message_id TEXT NOT NULL REFERENCES messages(message_id),
            reader_id TEXT NOT NULL,
            read_at TEXT NOT NULL,
            PRIMARY KEY(message_id, reader_id)
        );
        CREATE TABLE IF NOT EXISTS task_activity (
            activity_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            field_name TEXT,
            old_value TEXT,
            new_value TEXT,
            details TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_dependencies (
            prerequisite_task_id TEXT NOT NULL REFERENCES tasks(task_id),
            dependent_task_id TEXT NOT NULL REFERENCES tasks(task_id),
            created_at TEXT NOT NULL,
            PRIMARY KEY(prerequisite_task_id, dependent_task_id),
            CHECK(prerequisite_task_id != dependent_task_id)
        );
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_participant_id);
        CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline);
        CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at);
        CREATE INDEX IF NOT EXISTS idx_activity_task_time ON task_activity(task_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_dependencies_dependent ON task_dependencies(dependent_task_id);
        """
    )
    connection.execute(
        "INSERT INTO projects(project_id, name, created_at) VALUES(1, ?, ?) ON CONFLICT(project_id) DO NOTHING",
        ("Student Team Project", utc_now()),
    )
    for column, definition in (
        ("description", "TEXT NOT NULL DEFAULT ''"),
        ("goals", "TEXT NOT NULL DEFAULT ''"),
        ("start_date", "TEXT"),
        ("deadline", "TEXT"),
    ):
        connection.execute(
            f"ALTER TABLE projects ADD COLUMN IF NOT EXISTS {column} {definition}"
        )
    column = connection.execute(
        """SELECT 1 FROM information_schema.columns
           WHERE table_schema = current_schema() AND table_name = 'messages'
             AND column_name = 'task_id'"""
    ).fetchone()
    if not column:
        connection.execute(
            "ALTER TABLE messages ADD COLUMN task_id TEXT REFERENCES tasks(task_id)"
        )
    for column, definition in (
        ("parent_message_id", "TEXT REFERENCES messages(message_id)"),
        ("source", "TEXT NOT NULL DEFAULT 'telegram'"),
        ("sender_user_id", "TEXT"),
        ("sender_name", "TEXT"),
        ("edited_at", "TEXT"),
        ("deleted_at", "TEXT"),
    ):
        connection.execute(
            f"ALTER TABLE messages ADD COLUMN IF NOT EXISTS {column} {definition}"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_task_time ON messages(task_id, received_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_message_id)"
    )


@contextmanager
def transaction(connection: Any) -> Iterator[Any]:
    connection.execute("BEGIN" if getattr(connection, "is_postgres", False) else "BEGIN IMMEDIATE")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def get_project(db_path: str) -> dict[str, Any]:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM projects WHERE project_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("The workspace project record is missing.")
        return dict(row)


def update_project(db_path: str, fields: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name", "description", "goals", "start_date", "deadline"}
    if not fields or set(fields) - allowed:
        raise ValidationError("Unsupported or empty project update")
    changes: dict[str, str | None] = {}
    for field, value in fields.items():
        if field in {"name", "description", "goals"}:
            if not isinstance(value, str):
                raise ValidationError(f"Project {field} must be text")
            value = value.strip()
            if field == "name" and not value:
                raise ValidationError("Project name is required")
            changes[field] = value
        else:
            if value in (None, ""):
                changes[field] = None
                continue
            if not isinstance(value, str):
                raise ValidationError(f"Project {field} must be a YYYY-MM-DD date")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValidationError(
                    f"Project {field} must be a valid YYYY-MM-DD date"
                ) from exc
            changes[field] = value

    with connect(db_path) as connection, transaction(connection):
        current = connection.execute(
            "SELECT * FROM projects WHERE project_id = 1"
        ).fetchone()
        if current is None:
            raise RuntimeError("The workspace project record is missing.")
        merged = {**dict(current), **changes}
        if merged["start_date"] and merged["deadline"]:
            if date.fromisoformat(merged["start_date"]) > date.fromisoformat(merged["deadline"]):
                raise ValidationError("Project start date must be on or before its deadline")
        assignments = ", ".join(f"{field} = ?" for field in changes)
        connection.execute(
            f"UPDATE projects SET {assignments} WHERE project_id = 1",
            tuple(changes.values()),
        )
        updated = connection.execute(
            "SELECT * FROM projects WHERE project_id = 1"
        ).fetchone()
        return dict(updated)


def create_participant(db_path: str, name: str, chat_id: str) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise ValidationError("Participant name is required")
    clean_chat_id = validate_telegram_chat_id(chat_id)
    participant_id = str(uuid4())
    with connect(db_path) as connection, transaction(connection):
        connection.execute(
            "INSERT INTO participants(participant_id, name, telegram_chat_id, created_at) VALUES(?, ?, ?, ?)",
            (participant_id, clean_name, clean_chat_id, utc_now()),
        )
        return dict(connection.execute(
            "SELECT * FROM participants WHERE participant_id = ?", (participant_id,)
        ).fetchone())


def list_participants(db_path: str) -> list[dict[str, Any]]:
    with connect(db_path) as connection:
        rows = connection.execute("SELECT * FROM participants ORDER BY name").fetchall()
        return [dict(row) for row in rows]


def get_participant_by_chat_id(db_path: str, chat_id: str) -> dict[str, Any] | None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM participants WHERE telegram_chat_id = ?",
            (validate_telegram_chat_id(chat_id),),
        ).fetchone()
        return dict(row) if row else None


def _task_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if not row:
        return None
    task = dict(row)
    task["owner_name"] = task.get("owner_name") or "Unassigned"
    return task


TASK_SELECT = """
    SELECT t.*, p.name AS owner_name, p.telegram_chat_id AS owner_telegram_chat_id
    FROM tasks t LEFT JOIN participants p ON p.participant_id = t.owner_participant_id
"""


def create_task(db_path: str, fields: dict[str, Any]) -> dict[str, Any]:
    validate_task_fields(fields)
    task_id = str(uuid4())
    now = utc_now()
    with connect(db_path) as connection, transaction(connection):
        if not connection.execute(
            "SELECT 1 FROM participants WHERE participant_id = ?",
            (fields["owner_participant_id"],),
        ).fetchone():
            raise ValidationError("Assigned participant does not exist")
        connection.execute(
            """INSERT INTO tasks(task_id, project_id, title, description, owner_participant_id,
                deadline, status, priority, blocker_reason, next_action, created_at, updated_at)
                VALUES(?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, fields["title"].strip(), fields.get("description", "").strip(),
                fields["owner_participant_id"], fields["deadline"], fields["status"],
                fields["priority"], fields.get("blocker_reason", "").strip(),
                fields.get("next_action", "").strip(), now, now,
            ),
        )
        connection.execute(
            """INSERT INTO task_activity(activity_id, task_id, actor, action, field_name,
                old_value, new_value, details, created_at)
                VALUES(?, ?, 'manager', 'task_created', 'status', NULL, ?, '', ?)""",
            (str(uuid4()), task_id, fields["status"], now),
        )
        return _task_row(connection.execute(TASK_SELECT + " WHERE t.task_id = ?", (task_id,)).fetchone())


def get_task(db_path: str, task_id: str, *, include_archived: bool = False) -> dict[str, Any] | None:
    query = TASK_SELECT + " WHERE t.task_id = ?"
    if not include_archived:
        query += " AND t.archived_at IS NULL"
    with connect(db_path) as connection:
        return _task_row(connection.execute(query, (task_id,)).fetchone())


def list_tasks(
    db_path: str,
    *,
    status: str | None = None,
    owner_participant_id: str | None = None,
    search: str = "",
    deadline_on_or_before: str | None = None,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if not include_archived:
        clauses.append("t.archived_at IS NULL")
    if status:
        clauses.append("t.status = ?")
        params.append(status)
    if owner_participant_id:
        clauses.append("t.owner_participant_id = ?")
        params.append(owner_participant_id)
    if deadline_on_or_before:
        clauses.append("t.deadline <= ?")
        params.append(deadline_on_or_before)
    if search.strip():
        match = f"%{search.strip()}%"
        clauses.append("(t.title LIKE ? OR t.description LIKE ? OR p.name LIKE ?)")
        params.extend((match, match, match))
    query = TASK_SELECT
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY t.deadline, t.created_at"
    with connect(db_path) as connection:
        return [dict(row) for row in connection.execute(query, params).fetchall()]


def update_task(
    db_path: str, task_id: str, changes: dict[str, Any], *, actor: str = "manager"
) -> dict[str, Any]:
    return _update_task(db_path, task_id, changes, actor=actor, connection_factory=connect)


def _update_task(
    db_path: str,
    task_id: str,
    changes: dict[str, Any],
    *,
    actor: str,
    connection_factory: Any = connect,
    connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    allowed = {
        "title", "description", "owner_participant_id", "deadline", "status",
        "priority", "blocker_reason", "next_action",
    }
    if not changes or set(changes) - allowed:
        raise ValidationError("No editable task fields supplied or unsupported fields included")
    validate_task_fields(changes, partial=True)

    def perform(conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            raise ValidationError("Task not found")
        if "status" in changes and changes["status"] not in STATUS_TRANSITIONS[row["status"]]:
            raise ValidationError(f"Cannot change task from {row['status']} to {changes['status']}")
        if "owner_participant_id" in changes and not conn.execute(
            "SELECT 1 FROM participants WHERE participant_id = ?",
            (changes["owner_participant_id"],),
        ).fetchone():
            raise ValidationError("Assigned participant does not exist")
        now = utc_now()
        updated: dict[str, Any] = {}
        for field, value in changes.items():
            if field in {"title", "description", "blocker_reason", "next_action"}:
                value = value.strip()
            if row[field] == value:
                continue
            conn.execute(
                f"UPDATE tasks SET {field} = ?, updated_at = ? WHERE task_id = ?",
                (value, now, task_id),
            )
            conn.execute(
                """INSERT INTO task_activity(activity_id, task_id, actor, action, field_name,
                    old_value, new_value, details, created_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, '', ?)""",
                (
                    str(uuid4()), task_id, actor,
                    "status_changed" if field == "status" else "task_updated",
                    field, row[field], value, now,
                ),
            )
            updated[field] = value
        if connection is not None:
            return _task_row(conn.execute(TASK_SELECT + " WHERE t.task_id = ?", (task_id,)).fetchone())
        return _task_row(conn.execute(TASK_SELECT + " WHERE t.task_id = ?", (task_id,)).fetchone())

    if connection is not None:
        return perform(connection)
    with connection_factory(db_path) as conn, transaction(conn):
        return perform(conn)


def archive_task(db_path: str, task_id: str) -> None:
    with connect(db_path) as connection, transaction(connection):
        row = connection.execute(
            "SELECT archived_at FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            raise ValidationError("Task not found")
        if row["archived_at"] is None:
            now = utc_now()
            connection.execute(
                "UPDATE tasks SET archived_at = ?, updated_at = ? WHERE task_id = ?",
                (now, now, task_id),
            )
            connection.execute(
                """INSERT INTO task_activity(activity_id, task_id, actor, action, details, created_at)
                    VALUES(?, ?, 'manager', 'task_archived', '', ?)""",
                (str(uuid4()), task_id, now),
            )


def get_task_activity(db_path: str, task_id: str, limit: int = 30) -> list[dict[str, Any]]:
    with connect(db_path) as connection:
        return [dict(row) for row in connection.execute(
            """SELECT * FROM task_activity WHERE task_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (task_id, limit),
        ).fetchall()]


def get_recent_task_activity(
    db_path: str, task_ids: list[str], per_task_limit: int = 5
) -> list[dict[str, Any]]:
    if not task_ids or per_task_limit <= 0:
        return []
    placeholders = ", ".join("?" for _ in task_ids)
    query = f"""SELECT * FROM (
                   SELECT task_activity.*,
                          ROW_NUMBER() OVER (
                              PARTITION BY task_id ORDER BY created_at DESC
                          ) AS activity_rank
                   FROM task_activity
                   WHERE task_id IN ({placeholders})
               ) recent
               WHERE activity_rank <= ?
               ORDER BY created_at DESC"""
    with connect(db_path) as connection:
        return [
            {key: row[key] for key in row.keys() if key != "activity_rank"}
            for row in connection.execute(query, (*task_ids, per_task_limit)).fetchall()
        ]


def list_task_dependencies(db_path: str) -> list[dict[str, Any]]:
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT d.prerequisite_task_id, d.dependent_task_id, d.created_at,
                      prerequisite.title AS prerequisite_title,
                      prerequisite.status AS prerequisite_status,
                      prerequisite.owner_participant_id AS prerequisite_owner_id,
                      prerequisite_owner.name AS prerequisite_owner_name,
                      dependent.title AS dependent_title,
                      dependent.status AS dependent_status,
                      dependent.owner_participant_id AS dependent_owner_id,
                      dependent_owner.name AS dependent_owner_name
               FROM task_dependencies d
               JOIN tasks prerequisite ON prerequisite.task_id = d.prerequisite_task_id
               JOIN participants prerequisite_owner
                    ON prerequisite_owner.participant_id = prerequisite.owner_participant_id
               JOIN tasks dependent ON dependent.task_id = d.dependent_task_id
               JOIN participants dependent_owner
                    ON dependent_owner.participant_id = dependent.owner_participant_id
               WHERE prerequisite.archived_at IS NULL AND dependent.archived_at IS NULL
               ORDER BY d.created_at"""
        ).fetchall()
        return [dict(row) for row in rows]


def add_task_dependency(db_path: str, prerequisite_task_id: str, dependent_task_id: str) -> None:
    if prerequisite_task_id == dependent_task_id:
        raise ValidationError("A task cannot depend on itself")
    now = utc_now()
    with connect(db_path) as connection, transaction(connection):
        tasks = {
            row["task_id"]
            for row in connection.execute(
                """SELECT task_id FROM tasks WHERE task_id IN (?, ?)
                   AND archived_at IS NULL""",
                (prerequisite_task_id, dependent_task_id),
            ).fetchall()
        }
        if len(tasks) != 2:
            raise ValidationError("Both dependency tasks must exist and be active")
        graph: dict[str, list[str]] = {}
        for row in connection.execute(
            "SELECT prerequisite_task_id, dependent_task_id FROM task_dependencies"
        ).fetchall():
            graph.setdefault(row["prerequisite_task_id"], []).append(row["dependent_task_id"])
        pending = [dependent_task_id]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == prerequisite_task_id:
                raise ValidationError("This dependency would create a cycle")
            if current not in seen:
                seen.add(current)
                pending.extend(graph.get(current, []))
        connection.execute(
            """INSERT INTO task_dependencies(prerequisite_task_id, dependent_task_id, created_at)
               VALUES(?, ?, ?)""",
            (prerequisite_task_id, dependent_task_id, now),
        )
        for task_id, details in (
            (prerequisite_task_id, "Prerequisite for a downstream task."),
            (dependent_task_id, "Depends on an upstream task."),
        ):
            connection.execute(
                """INSERT INTO task_activity(activity_id, task_id, actor, action,
                       details, created_at)
                   VALUES(?, ?, 'manager', 'dependency_added', ?, ?)""",
                (str(uuid4()), task_id, details, now),
            )


def remove_task_dependency(
    db_path: str, prerequisite_task_id: str, dependent_task_id: str
) -> None:
    with connect(db_path) as connection, transaction(connection):
        cursor = connection.execute(
            """DELETE FROM task_dependencies
               WHERE prerequisite_task_id = ? AND dependent_task_id = ?""",
            (prerequisite_task_id, dependent_task_id),
        )
        if not cursor.rowcount:
            raise ValidationError("Dependency not found")


def get_recent_messages(
    db_path: str, limit: int = 30, *, search: str = ""
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if search.strip():
        pattern = f"%{search.strip()}%"
        clauses.append(
            "m.deleted_at IS NULL AND "
            "(m.body LIKE ? OR t.title LIKE ? OR p.name LIKE ? OR m.sender_name LIKE ?)"
        )
        parameters.extend((pattern, pattern, pattern, pattern))
    query = """SELECT m.*, p.name AS participant_name, t.title AS task_title
               FROM messages m
               LEFT JOIN participants p ON p.participant_id = m.participant_id
               LEFT JOIN tasks t ON t.task_id = m.task_id"""
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY m.received_at DESC LIMIT ?"
    parameters.append(limit)
    with connect(db_path) as connection:
        return [dict(row) for row in connection.execute(
            query, parameters,
        ).fetchall()]


def create_internal_message(
    db_path: str,
    task_id: str,
    body: str,
    *,
    actor_id: str,
    actor_name: str,
    parent_message_id: str | None = None,
) -> dict[str, Any]:
    clean_body = body.strip()
    if not clean_body or len(clean_body) > 5000:
        raise ValidationError("Message must contain 1 to 5000 characters")
    if not actor_id.strip() or not actor_name.strip():
        raise ValidationError("A signed-in sender identity is required")
    message_id = str(uuid4())
    now = utc_now()
    with connect(db_path) as connection, transaction(connection):
        if not connection.execute(
            "SELECT 1 FROM tasks WHERE task_id = ? AND archived_at IS NULL",
            (task_id,),
        ).fetchone():
            raise ValidationError("Task not found or archived")
        if parent_message_id:
            parent = connection.execute(
                "SELECT task_id, deleted_at FROM messages WHERE message_id = ?",
                (parent_message_id,),
            ).fetchone()
            if not parent or parent["task_id"] != task_id or parent["deleted_at"]:
                raise ValidationError("Reply target must be an active message on this task")
        connection.execute(
            """INSERT INTO messages(message_id, task_id, parent_message_id,
                sender_chat_id, direction, body, received_at, resulting_action,
                source, sender_user_id, sender_name)
               VALUES(?, ?, ?, 'dashboard', 'outgoing', ?, ?, 'internal', 'dashboard', ?, ?)""",
            (
                message_id, task_id, parent_message_id, clean_body, now,
                actor_id, actor_name.strip(),
            ),
        )
        row = connection.execute(
            "SELECT * FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return dict(row)


def edit_internal_message(
    db_path: str, message_id: str, body: str, *, actor_id: str
) -> dict[str, Any]:
    clean_body = body.strip()
    if not clean_body or len(clean_body) > 5000:
        raise ValidationError("Message must contain 1 to 5000 characters")
    if not actor_id.strip():
        raise ValidationError("A signed-in sender identity is required")
    now = utc_now()
    with connect(db_path) as connection, transaction(connection):
        cursor = connection.execute(
            """UPDATE messages SET body = ?, edited_at = ?
               WHERE message_id = ? AND source = 'dashboard'
                 AND sender_user_id = ? AND deleted_at IS NULL""",
            (clean_body, now, message_id, actor_id),
        )
        if not cursor.rowcount:
            raise ValidationError("Only the author can edit an active internal message")
        row = connection.execute(
            "SELECT * FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return dict(row)


def delete_internal_message(db_path: str, message_id: str, *, actor_id: str) -> None:
    now = utc_now()
    with connect(db_path) as connection, transaction(connection):
        cursor = connection.execute(
            """UPDATE messages SET deleted_at = ?
               WHERE message_id = ? AND source = 'dashboard'
                 AND sender_user_id = ? AND deleted_at IS NULL""",
            (now, message_id, actor_id),
        )
        if not cursor.rowcount:
            raise ValidationError("Only the author can delete an active internal message")


MESSAGE_REACTIONS = ("👍", "👀", "✅", "❤️", "🎉", "🙏")


def toggle_message_reaction(
    db_path: str, message_id: str, emoji: str, *, actor_id: str
) -> bool:
    if emoji not in MESSAGE_REACTIONS:
        raise ValidationError("Unsupported reaction")
    if not actor_id.strip():
        raise ValidationError("A signed-in reaction identity is required")
    with connect(db_path) as connection, transaction(connection):
        if not connection.execute(
            "SELECT 1 FROM messages WHERE message_id = ? AND deleted_at IS NULL",
            (message_id,),
        ).fetchone():
            raise ValidationError("Message not found or deleted")
        existing = connection.execute(
            """SELECT 1 FROM message_reactions
               WHERE message_id = ? AND actor_id = ? AND emoji = ?""",
            (message_id, actor_id, emoji),
        ).fetchone()
        if existing:
            connection.execute(
                """DELETE FROM message_reactions
                   WHERE message_id = ? AND actor_id = ? AND emoji = ?""",
                (message_id, actor_id, emoji),
            )
            return False
        connection.execute(
            """INSERT INTO message_reactions(message_id, actor_id, emoji, created_at)
               VALUES(?, ?, ?, ?)""",
            (message_id, actor_id, emoji, utc_now()),
        )
        return True


def get_message_reactions(
    db_path: str, message_id: str, *, actor_id: str
) -> list[dict[str, Any]]:
    with connect(db_path) as connection:
        return [
            dict(row) for row in connection.execute(
                """SELECT emoji, COUNT(*) AS count,
                          MAX(CASE WHEN actor_id = ? THEN 1 ELSE 0 END) AS reacted
                   FROM message_reactions WHERE message_id = ? GROUP BY emoji
                   ORDER BY emoji""",
                (actor_id, message_id),
            ).fetchall()
        ]


def get_unread_message_ids(db_path: str, reader_id: str) -> set[str]:
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT m.message_id FROM messages m
               LEFT JOIN message_reads r
                 ON r.message_id = m.message_id AND r.reader_id = ?
               WHERE m.deleted_at IS NULL AND r.message_id IS NULL
                 AND (m.source = 'dashboard' OR m.direction = 'incoming')
                 AND (m.sender_user_id IS NULL OR m.sender_user_id != ?)""",
            (reader_id, reader_id),
        ).fetchall()
        return {row["message_id"] for row in rows}


def mark_messages_read(
    db_path: str, message_ids: list[str], *, reader_id: str
) -> None:
    if not message_ids or not reader_id.strip():
        return
    now = utc_now()
    with connect(db_path) as connection, transaction(connection):
        for message_id in message_ids:
            connection.execute(
                """INSERT INTO message_reads(message_id, reader_id, read_at)
                   SELECT ?, ?, ? WHERE EXISTS (
                       SELECT 1 FROM messages WHERE message_id = ? AND deleted_at IS NULL
                   ) ON CONFLICT(message_id, reader_id) DO NOTHING""",
                (message_id, reader_id, now, message_id),
            )


def link_message_to_task(db_path: str, message_id: str, task_id: str) -> None:
    with connect(db_path) as connection, transaction(connection):
        task = connection.execute(
            "SELECT 1 FROM tasks WHERE task_id = ? AND archived_at IS NULL",
            (task_id,),
        ).fetchone()
        message = connection.execute(
            "SELECT task_id FROM messages WHERE message_id = ? AND deleted_at IS NULL",
            (message_id,),
        ).fetchone()
        if not task or not message:
            raise ValidationError("Active task and message records are required")
        if message["task_id"] and message["task_id"] != task_id:
            raise ValidationError("Message is already linked to another task")
        connection.execute(
            "UPDATE messages SET task_id = ? WHERE message_id = ?",
            (task_id, message_id),
        )


def create_task_from_message(
    db_path: str,
    message_id: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    validate_task_fields(fields)
    now = utc_now()
    task_id = str(uuid4())
    with connect(db_path) as connection, transaction(connection):
        message = connection.execute(
            "SELECT task_id, body FROM messages WHERE message_id = ? AND deleted_at IS NULL",
            (message_id,),
        ).fetchone()
        if not message or message["task_id"]:
            raise ValidationError("Message is unavailable or already linked to a task")
        if not connection.execute(
            "SELECT 1 FROM participants WHERE participant_id = ?",
            (fields["owner_participant_id"],),
        ).fetchone():
            raise ValidationError("Assigned participant does not exist")
        connection.execute(
            """INSERT INTO tasks(task_id, project_id, title, description, owner_participant_id,
                deadline, status, priority, blocker_reason, next_action, created_at, updated_at)
               VALUES(?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, fields["title"].strip(),
                fields.get("description", "").strip() or message["body"],
                fields["owner_participant_id"], fields["deadline"], fields["status"],
                fields["priority"], fields.get("blocker_reason", "").strip(),
                fields.get("next_action", "").strip(), now, now,
            ),
        )
        connection.execute(
            """INSERT INTO task_activity(activity_id, task_id, actor, action, field_name,
                old_value, new_value, details, created_at)
               VALUES(?, ?, 'manager', 'task_created_from_message', 'status', NULL, ?, ?, ?)""",
            (str(uuid4()), task_id, fields["status"], message_id, now),
        )
        connection.execute(
            "UPDATE messages SET task_id = ? WHERE message_id = ?",
            (task_id, message_id),
        )
        row = connection.execute(
            TASK_SELECT + " WHERE t.task_id = ?", (task_id,)
        ).fetchone()
        return _task_row(row)


def get_message(db_path: str, message_id: str) -> dict[str, Any] | None:
    with connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return dict(row) if row else None


def insert_incoming_message(
    db_path: str,
    update_id: str,
    telegram_message_id: str,
    chat_id: str,
    body: str,
    *,
    reply_to_telegram_message_id: str | None = None,
) -> tuple[str, bool]:
    message_id = str(uuid4())
    clean_chat_id = validate_telegram_chat_id(chat_id)
    with connect(db_path) as connection, transaction(connection):
        participant = connection.execute(
            "SELECT participant_id FROM participants WHERE telegram_chat_id = ?",
            (clean_chat_id,),
        ).fetchone()
        parent = None
        if participant and reply_to_telegram_message_id:
            parent = connection.execute(
                """SELECT message_id, task_id FROM messages
                   WHERE telegram_message_id = ? AND direction = 'outgoing'
                     AND participant_id = ? AND task_id IS NOT NULL
                   ORDER BY received_at DESC LIMIT 1""",
                (str(reply_to_telegram_message_id), participant["participant_id"]),
            ).fetchone()
        cursor = connection.execute(
            """INSERT INTO messages(message_id, telegram_update_id,
                telegram_message_id, task_id, parent_message_id, participant_id,
                sender_chat_id, direction, body, received_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, 'incoming', ?, ?)
                ON CONFLICT(telegram_update_id) DO NOTHING""",
            (
                message_id, str(update_id), str(telegram_message_id),
                parent["task_id"] if parent else None,
                parent["message_id"] if parent else None,
                participant["participant_id"] if participant else None,
                clean_chat_id, body, utc_now(),
            ),
        )
        if cursor.rowcount:
            return message_id, True
        existing = connection.execute(
            "SELECT message_id FROM messages WHERE telegram_update_id = ?",
            (str(update_id),),
        ).fetchone()
        return existing["message_id"], False


def store_message_result(
    db_path: str,
    message_id: str,
    *,
    interpretation: dict[str, Any] | None,
    confidence: float | None,
    action: str,
    error: str | None = None,
) -> None:
    encoded = json.dumps(interpretation, ensure_ascii=True) if interpretation is not None else None
    with connect(db_path) as connection, transaction(connection):
        connection.execute(
            """UPDATE messages SET ai_interpretation = ?, confidence = ?,
                resulting_action = ?, analysis_error = ? WHERE message_id = ?""",
            (encoded, confidence, action, error, message_id),
        )


def apply_reply_update(
    db_path: str,
    message_id: str,
    task_id: str,
    changes: dict[str, Any],
    analysis: dict[str, Any],
) -> None:
    allowed = {"status", "blocker_reason", "next_action"}
    if not changes or set(changes) - allowed:
        raise ValidationError("Unsupported participant task update")
    validate_task_fields(changes, partial=True)
    with connect(db_path) as connection, transaction(connection):
        message = connection.execute(
            "SELECT participant_id, sender_chat_id FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        task = connection.execute(
            "SELECT owner_participant_id, status FROM tasks WHERE task_id = ? AND archived_at IS NULL",
            (task_id,),
        ).fetchone()
        if not message or not task or message["participant_id"] != task["owner_participant_id"]:
            raise ValidationError("Reply is not authorized to update this task")
        now = utc_now()
        changes = {**changes, "last_reply_at": now}
        # Apply the reply timestamp internally; it is intentionally not a manager-editable field.
        if "status" in changes and changes["status"] not in STATUS_TRANSITIONS[task["status"]]:
            raise ValidationError(f"Cannot change task from {task['status']} to {changes['status']}")
        for field, value in changes.items():
            if field == "last_reply_at":
                connection.execute(
                    "UPDATE tasks SET last_reply_at = ?, updated_at = ? WHERE task_id = ?",
                    (value, now, task_id),
                )
                continue
            if field in {"blocker_reason", "next_action"}:
                value = value.strip()
            old = connection.execute(
                f"SELECT {field} FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()[field]
            if old == value:
                continue
            connection.execute(
                f"UPDATE tasks SET {field} = ?, updated_at = ? WHERE task_id = ?",
                (value, now, task_id),
            )
            connection.execute(
                """INSERT INTO task_activity(activity_id, task_id, actor, action, field_name,
                    old_value, new_value, details, created_at)
                    VALUES(?, ?, 'participant', ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid4()), task_id,
                    "status_changed" if field == "status" else "participant_update",
                    field, old, value, analysis.get("progress_summary", ""), now,
                ),
            )
        connection.execute(
            """UPDATE messages SET task_id = ?,
                parent_message_id = CASE
                    WHEN (SELECT task_id FROM messages parent
                          WHERE parent.message_id = messages.parent_message_id) = ?
                    THEN parent_message_id ELSE NULL END,
                ai_interpretation = ?, confidence = ?,
                resulting_action = 'task_updated', analysis_error = NULL WHERE message_id = ?""",
            (
                task_id, task_id, json.dumps(analysis, ensure_ascii=True),
                analysis.get("confidence"), message_id,
            ),
        )


def get_task_statistics(db_path: str) -> dict[str, int]:
    with connect(db_path) as connection:
        rows = connection.execute(
            """SELECT status, COUNT(*) AS amount FROM tasks
               WHERE archived_at IS NULL GROUP BY status"""
        ).fetchall()
        counts = {status: 0 for status in ("Pending", "In Progress", "Blocked", "Complete")}
        counts.update({row["status"]: row["amount"] for row in rows})
        today = datetime.now(timezone.utc).date().isoformat()
        overdue_row = connection.execute(
            """SELECT COUNT(*) AS amount FROM tasks WHERE archived_at IS NULL AND status != 'Complete'
               AND deadline < ?""",
            (today,),
        ).fetchone()
        counts["Overdue"] = overdue_row["amount"]
        counts["Total"] = sum(counts[status] for status in ("Pending", "In Progress", "Blocked", "Complete"))
        return counts


def reserve_follow_up(db_path: str, task_id: str, cooldown_hours: float) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    reserved_at = now.isoformat(timespec="seconds")
    with connect(db_path) as connection, transaction(connection):
        task = connection.execute(
            TASK_SELECT + " WHERE t.task_id = ? AND t.archived_at IS NULL",
            (task_id,),
        ).fetchone()
        if not task:
            raise ValidationError("Task not found or archived")
        if task["status"] == "Complete":
            raise ValidationError("Cannot send a follow-up for a completed task")
        previous_at = task["last_follow_up_at"]
        if previous_at:
            try:
                previous_time = datetime.fromisoformat(previous_at)
            except ValueError as exc:
                raise ValidationError("Task has an invalid follow-up timestamp") from exc
            if now - previous_time < timedelta(hours=cooldown_hours):
                if cooldown_hours < 1:
                    mins = round(cooldown_hours * 60, 1)
                    duration_str = f"{mins:g} minute(s)" if mins != 1 else "1 minute"
                else:
                    duration_str = f"{cooldown_hours:g} hours"
                raise FollowUpCooldownError(
                    f"A follow-up was recently sent. Try again after {duration_str}."
                )
        connection.execute(
            "UPDATE tasks SET last_follow_up_at = ?, updated_at = ? WHERE task_id = ?",
            (reserved_at, reserved_at, task_id),
        )
        result = dict(task)
        result["previous_at"] = previous_at
        result["reserved_at"] = reserved_at
        result["last_follow_up_at"] = reserved_at
        return result


class FollowUpCooldownError(RuntimeError):
    pass


def release_follow_up(
    db_path: str, task_id: str, reserved_at: str, previous_at: str | None
) -> None:
    with connect(db_path) as connection, transaction(connection):
        connection.execute(
            """UPDATE tasks SET last_follow_up_at = ?, updated_at = ?
               WHERE task_id = ? AND last_follow_up_at = ?""",
            (previous_at, utc_now(), task_id, reserved_at),
        )


def record_follow_up_sent(
    db_path: str,
    task_id: str,
    reserved_at: str,
    body: str,
    telegram_message_id: str | None = None,
) -> None:
    with connect(db_path) as connection, transaction(connection):
        task = connection.execute(
            """SELECT t.owner_participant_id, p.telegram_chat_id FROM tasks t
               JOIN participants p ON p.participant_id = t.owner_participant_id
               WHERE t.task_id = ? AND t.last_follow_up_at = ?""",
            (task_id, reserved_at),
        ).fetchone()
        if not task:
            raise ValidationError("Follow-up reservation is no longer valid")
        connection.execute(
            """INSERT INTO messages(message_id, task_id, participant_id, sender_chat_id,
                telegram_message_id, direction, body, received_at, resulting_action)
                VALUES(?, ?, ?, ?, ?, 'outgoing', ?, ?, 'sent')""",
            (
                str(uuid4()), task_id, task["owner_participant_id"],
                task["telegram_chat_id"], str(telegram_message_id) if telegram_message_id else None,
                body, reserved_at,
            ),
        )
        connection.execute(
            """INSERT INTO task_activity(activity_id, task_id, actor, action, details, created_at)
               VALUES(?, ?, 'manager', 'follow_up_sent', ?, ?)""",
            (str(uuid4()), task_id, "Telegram follow-up sent.", reserved_at),
        )


def seed_demo_data(db_path: str) -> None:
    participants = list_participants(db_path)
    participant = next(
        (
            entry for entry in participants
            if entry["name"] == "Demo Participant (DEMO)"
        ),
        None,
    )
    if participant is None:
        participant = create_participant(db_path, "Demo Participant (DEMO)", "999000111")
    if any(
        task["title"] == "[DEMO] Prepare project outline"
        for task in list_tasks(db_path)
    ):
        return
    create_task(
        db_path,
        {
            "title": "[DEMO] Prepare project outline",
            "description": "Draft and share the team's initial project outline.",
            "owner_participant_id": participant["participant_id"],
            "deadline": (datetime.now(timezone.utc).date() + timedelta(days=3)).isoformat(),
            "status": "Pending",
            "priority": "Medium",
        },
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Initialize the coordinator database.")
    parser.add_argument("--db", default="coordinator.db")
    parser.add_argument("--seed-demo", action="store_true", help="Insert clearly labelled demo data")
    options = parser.parse_args()
    with connect(options.db):
        pass
    if options.seed_demo:
        seed_demo_data(options.db)
    print(f"Database initialized at {options.db}")
