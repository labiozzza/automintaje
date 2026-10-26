"""SQLite persistence for the personal development queue."""

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


DB_PATH = Path(__file__).with_name("development_queue.sqlite3")
MAX_NOTE_CHARS = 10000


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB_PATH), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS development_notes (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL CHECK(length(trim(text)) BETWEEN 1 AND 10000),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validated_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Текст заметки должен быть строкой")
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Введите текст заметки")
    if len(cleaned) > MAX_NOTE_CHARS:
        raise ValueError("Заметка слишком длинная")
    return cleaned


def _as_note(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "text": row["text"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_notes() -> list:
    with closing(_connect()) as connection:
        rows = connection.execute(
            "SELECT id, text, created_at, updated_at "
            "FROM development_notes ORDER BY updated_at DESC, id DESC"
        ).fetchall()
        return [_as_note(row) for row in rows]


def create_note(text: str) -> dict:
    cleaned = _validated_text(text)
    note = {
        "id": uuid.uuid4().hex,
        "text": cleaned,
        "created_at": _timestamp(),
    }
    note["updated_at"] = note["created_at"]
    with closing(_connect()) as connection:
        with connection:
            connection.execute(
                "INSERT INTO development_notes (id, text, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (note["id"], note["text"], note["created_at"], note["updated_at"]),
            )
    return note


def update_note(note_id: str, text: str) -> Optional[dict]:
    cleaned = _validated_text(text)
    updated_at = _timestamp()
    with closing(_connect()) as connection:
        with connection:
            cursor = connection.execute(
                "UPDATE development_notes SET text = ?, updated_at = ? WHERE id = ?",
                (cleaned, updated_at, note_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT id, text, created_at, updated_at FROM development_notes WHERE id = ?",
                (note_id,),
            ).fetchone()
            return _as_note(row)


def delete_note(note_id: str) -> bool:
    with closing(_connect()) as connection:
        with connection:
            cursor = connection.execute(
                "DELETE FROM development_notes WHERE id = ?", (note_id,)
            )
            return cursor.rowcount > 0
