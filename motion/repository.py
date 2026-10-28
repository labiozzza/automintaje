"""SQLite persistence for Motion projects, immutable revisions, and operations."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class MotionRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._init_lock:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS motion_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS motion_projects (
                    id TEXT PRIMARY KEY,
                    source_job_id TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_revision INTEGER NOT NULL DEFAULT 0,
                    approved_revision INTEGER,
                    model_id TEXT,
                    session_id TEXT,
                    output_job_id TEXT,
                    manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS motion_projects_source_idx
                    ON motion_projects(source_job_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS motion_revisions (
                    project_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    review_json TEXT NOT NULL,
                    output_job_id TEXT,
                    preview_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, revision),
                    FOREIGN KEY(project_id) REFERENCES motion_projects(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS motion_operations (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    progress REAL,
                    detail TEXT NOT NULL,
                    error TEXT,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id, request_id),
                    FOREIGN KEY(project_id) REFERENCES motion_projects(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS motion_operations_project_idx
                    ON motion_operations(project_id, created_at DESC);
                """
                )
                row = connection.execute("SELECT value FROM motion_meta WHERE key='schema_version'").fetchone()
                if row and int(row["value"]) > SCHEMA_VERSION:
                    raise RuntimeError("Motion database schema is newer than this application")
                connection.execute(
                    "INSERT OR REPLACE INTO motion_meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.commit()
            finally:
                connection.close()

    @staticmethod
    def _project_from_row(row: Optional[sqlite3.Row]) -> Optional[dict]:
        if row is None:
            return None
        project = json.loads(row["manifest_json"])
        project.update({
            "id": row["id"],
            "source_job_id": row["source_job_id"],
            "source_fingerprint": row["source_fingerprint"],
            "status": row["status"],
            "current_revision": int(row["current_revision"]),
            "approved_revision": row["approved_revision"],
            "model_id": row["model_id"] or "",
            "session_id": row["session_id"] or "",
            "output_job_id": row["output_job_id"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        })
        return project

    def create_project(self, project: dict) -> dict:
        now = utc_now()
        stored = dict(project)
        project_id = str(stored["id"])
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO motion_projects(
                       id,source_job_id,source_fingerprint,status,current_revision,
                       approved_revision,model_id,session_id,output_job_id,manifest_json,
                       created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    project_id, str(stored["source_job_id"]), str(stored["source_fingerprint"]),
                    str(stored.get("status") or "created"), int(stored.get("current_revision") or 0),
                    stored.get("approved_revision"), stored.get("model_id") or None,
                    stored.get("session_id") or None, stored.get("output_job_id") or None,
                    _json(stored), now, now,
                ),
            )
        stored["created_at"] = now
        stored["updated_at"] = now
        return stored

    def get_project(self, project_id: str) -> Optional[dict]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM motion_projects WHERE id=?", (str(project_id),)).fetchone()
        return self._project_from_row(row)

    def list_projects(self, source_job_id: Optional[str] = None, limit: int = 100) -> List[dict]:
        limit = max(1, min(500, int(limit)))
        with self._connect() as connection:
            if source_job_id:
                rows = connection.execute(
                    "SELECT * FROM motion_projects WHERE source_job_id=? ORDER BY updated_at DESC LIMIT ?",
                    (str(source_job_id), limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM motion_projects ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._project_from_row(row) for row in rows if row is not None]

    def update_project(self, project_id: str, **changes) -> Optional[dict]:
        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM motion_projects WHERE id=?", (str(project_id),)).fetchone()
            current = self._project_from_row(row)
            if current is None:
                return None
            current.update(changes)
            connection.execute(
                """UPDATE motion_projects SET source_job_id=?,source_fingerprint=?,status=?,
                   current_revision=?,approved_revision=?,model_id=?,session_id=?,output_job_id=?,
                   manifest_json=?,updated_at=? WHERE id=?""",
                (
                    current["source_job_id"], current["source_fingerprint"], current["status"],
                    int(current.get("current_revision") or 0), current.get("approved_revision"),
                    current.get("model_id") or None, current.get("session_id") or None,
                    current.get("output_job_id") or None, _json(current), now, str(project_id),
                ),
            )
        current["updated_at"] = now
        return current

    def create_revision(self, project_id: str, revision: int, plan: dict,
                        review: Optional[dict] = None, status: str = "review") -> dict:
        now = utc_now()
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO motion_revisions(
                   project_id,revision,status,plan_json,review_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (str(project_id), int(revision), str(status), _json(plan), _json(review or {}), now, now),
            )
        return self.get_revision(project_id, revision)

    def get_revision(self, project_id: str, revision: int) -> Optional[dict]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM motion_revisions WHERE project_id=? AND revision=?",
                (str(project_id), int(revision)),
            ).fetchone()
        if row is None:
            return None
        return {
            "project_id": row["project_id"], "revision": int(row["revision"]),
            "status": row["status"], "plan": json.loads(row["plan_json"]),
            "review": json.loads(row["review_json"]), "output_job_id": row["output_job_id"],
            "preview_path": row["preview_path"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def update_revision(self, project_id: str, revision: int, **changes) -> Optional[dict]:
        current = self.get_revision(project_id, revision)
        if current is None:
            return None
        current.update(changes)
        now = utc_now()
        with self._transaction() as connection:
            connection.execute(
                """UPDATE motion_revisions SET status=?,plan_json=?,review_json=?,output_job_id=?,
                   preview_path=?,updated_at=? WHERE project_id=? AND revision=?""",
                (
                    current["status"], _json(current["plan"]), _json(current.get("review") or {}),
                    current.get("output_job_id"), current.get("preview_path"), now,
                    str(project_id), int(revision),
                ),
            )
        current["updated_at"] = now
        return current

    def list_revisions(self, project_id: str) -> List[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM motion_revisions WHERE project_id=? ORDER BY revision DESC",
                (str(project_id),),
            ).fetchall()
        return [self.get_revision(row["project_id"], row["revision"]) for row in rows]

    def create_operation(self, project_id: str, kind: str, request_id: str,
                         stage: str = "queued", detail: str = "Задание принято") -> dict:
        operation_id = uuid.uuid4().hex
        now = utc_now()
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO motion_operations(
                   id,project_id,kind,status,stage,progress,detail,error,request_id,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (operation_id, str(project_id), str(kind), "queued", str(stage), 0.0,
                 str(detail), None, str(request_id), now, now),
            )
        return self.get_operation(operation_id)

    def get_operation(self, operation_id: str) -> Optional[dict]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM motion_operations WHERE id=?", (str(operation_id),)).fetchone()
        return dict(row) if row else None

    def find_operation_by_request(self, project_id: str, request_id: str) -> Optional[dict]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM motion_operations WHERE project_id=? AND request_id=?",
                (str(project_id), str(request_id)),
            ).fetchone()
        return dict(row) if row else None

    def update_operation(self, operation_id: str, **changes) -> Optional[dict]:
        current = self.get_operation(operation_id)
        if current is None:
            return None
        current.update(changes)
        with self._transaction() as connection:
            connection.execute(
                """UPDATE motion_operations SET status=?,stage=?,progress=?,detail=?,error=?,updated_at=?
                   WHERE id=?""",
                (
                    current["status"], current["stage"], current.get("progress"), current.get("detail") or "",
                    current.get("error"), utc_now(), str(operation_id),
                ),
            )
        return self.get_operation(operation_id)

    def active_operation(self, project_id: str) -> Optional[dict]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM motion_operations WHERE project_id=?
                   AND status IN ('queued','running','cancelling') ORDER BY created_at DESC LIMIT 1""",
                (str(project_id),),
            ).fetchone()
        return dict(row) if row else None

    def recover_interrupted(self) -> List[str]:
        interrupted = []
        with self._transaction() as connection:
            operations = connection.execute(
                "SELECT id,project_id FROM motion_operations WHERE status IN ('queued','running','cancelling')"
            ).fetchall()
            now = utc_now()
            for operation in operations:
                connection.execute(
                    "UPDATE motion_operations SET status='interrupted',stage='interrupted',"
                    "detail='Операция прервана перезапуском сервера',error='interrupted',updated_at=? WHERE id=?",
                    (now, operation["id"]),
                )
                connection.execute(
                    "UPDATE motion_projects SET status='interrupted',active_operation_id='',updated_at=? WHERE id=?",
                    (now, operation["project_id"]),
                )
                interrupted.append(str(operation["project_id"]))
        return interrupted
