"""Persistent per-platform publication cadence and queue."""

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


DB_PATH = Path(__file__).with_name("publication_queue.sqlite3")
PLATFORMS = ("instagram", "youtube")
DEFAULT_INTERVAL_MINUTES = 5 * 60
MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 7 * 24 * 60
ACTIVE_STATUSES = ("queued", "preparing", "publishing")
TERMINAL_STATUSES = ("published", "failed", "unknown", "cancelled")
ALL_STATUSES = ACTIVE_STATUSES + TERMINAL_STATUSES


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: Optional[datetime] = None) -> str:
    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB_PATH), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS publication_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS publication_control (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
            pause_reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS publication_settings (
            platform TEXT PRIMARY KEY,
            interval_minutes INTEGER NOT NULL
                CHECK (interval_minutes BETWEEN 1 AND 10080),
            last_attempt_at TEXT
        );
        CREATE TABLE IF NOT EXISTS publication_queue (
            id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            platform TEXT NOT NULL,
            target_ids_json TEXT NOT NULL,
            display_title TEXT NOT NULL,
            youtube_title TEXT NOT NULL DEFAULT '',
            instagram_caption TEXT NOT NULL DEFAULT '',
            cover_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            progress_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            confirm_unknown INTEGER NOT NULL DEFAULT 0 CHECK (confirm_unknown IN (0, 1)),
            force_now INTEGER NOT NULL DEFAULT 0 CHECK (force_now IN (0, 1)),
            UNIQUE(request_id, platform)
        );
        CREATE INDEX IF NOT EXISTS idx_publication_queue_due
            ON publication_queue(status, scheduled_at, created_at);
        CREATE INDEX IF NOT EXISTS idx_publication_queue_platform
            ON publication_queue(platform, status, scheduled_at);
        """
    )
    connection.executemany(
        "INSERT OR IGNORE INTO publication_settings(platform, interval_minutes) VALUES (?, ?)",
        [(platform, DEFAULT_INTERVAL_MINUTES) for platform in PLATFORMS],
    )
    connection.execute(
        "INSERT OR IGNORE INTO publication_control(id, paused, pause_reason) VALUES (1, 0, '')"
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(publication_queue)")}
    if "confirm_unknown" not in columns:
        connection.execute(
            "ALTER TABLE publication_queue ADD COLUMN confirm_unknown INTEGER NOT NULL DEFAULT 0"
        )
    metadata = connection.execute(
        "SELECT value FROM publication_metadata WHERE key = 'target_rows_v1'"
    ).fetchone()
    if not metadata:
        queued_rows = connection.execute(
            "SELECT * FROM publication_queue WHERE status = 'queued' ORDER BY created_at, id"
        ).fetchall()
        had_pending_items = bool(queued_rows)
        with connection:
            for row in queued_rows:
                target_ids = list(dict.fromkeys(json.loads(row["target_ids_json"] or "[]")))
                if not target_ids:
                    continue
                base_request_id = str(row["request_id"])
                base_created_at = _parse_time(row["created_at"]) or utc_now()
                for index, target_id in enumerate(target_ids):
                    request_id = f"{base_request_id}:target:{target_id}"
                    created_at = iso_utc(base_created_at + timedelta(microseconds=index))
                    if index == 0:
                        connection.execute(
                            "UPDATE publication_queue SET request_id = ?, target_ids_json = ?, "
                            "created_at = ? WHERE id = ?",
                            (request_id, json.dumps([target_id]), created_at, row["id"]),
                        )
                        continue
                    connection.execute(
                        """INSERT OR IGNORE INTO publication_queue(
                               id, request_id, job_id, platform, target_ids_json, display_title,
                               youtube_title, instagram_caption, cover_id, status, created_at,
                               scheduled_at, started_at, finished_at, attempts, progress_json,
                               error, confirm_unknown, force_now
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            uuid.uuid4().hex, request_id, row["job_id"], row["platform"],
                            json.dumps([target_id]), row["display_title"], row["youtube_title"],
                            row["instagram_caption"], row["cover_id"], created_at,
                            row["scheduled_at"], row["started_at"], row["finished_at"],
                            row["attempts"], row["progress_json"], row["error"],
                            row["confirm_unknown"], row["force_now"],
                        ),
                    )
            connection.execute(
                "INSERT INTO publication_metadata(key, value) VALUES ('target_rows_v1', '1')"
            )
            if had_pending_items:
                connection.execute(
                    "UPDATE publication_control SET paused = 1, "
                    "pause_reason = ? WHERE id = 1",
                    ("Проверьте отдельные цели старых записей перед отправкой.",),
                )
            for platform in PLATFORMS:
                _reschedule_pending(connection, platform)
    order_migration = connection.execute(
        "SELECT value FROM publication_metadata WHERE key = 'target_order_v1'"
    ).fetchone()
    if order_migration is None:
        target_order = {
            "instagram_trial": 0,
            "instagram_trial_mirrored": 1,
            "instagram_feed": 2,
            "instagram_feed_mirrored": 3,
            "youtube": 4,
            "youtube_mirrored": 5,
        }
        pending = connection.execute(
            "SELECT id, request_id, platform, target_ids_json, created_at "
            "FROM publication_queue WHERE status = 'queued' ORDER BY created_at, id"
        ).fetchall()
        grouped = {}
        for row in pending:
            base_request_id = str(row["request_id"]).rsplit(":target:", 1)[0]
            grouped.setdefault((base_request_id, row["platform"]), []).append(row)
        with connection:
            for rows in grouped.values():
                if len(rows) < 2 or len({row["created_at"] for row in rows}) != 1:
                    continue
                base_created_at = _parse_time(rows[0]["created_at"]) or utc_now()
                rows.sort(key=lambda row: target_order.get(
                    (json.loads(row["target_ids_json"] or "[]") or [""])[0], 99
                ))
                for index, row in enumerate(rows):
                    connection.execute(
                        "UPDATE publication_queue SET created_at = ? WHERE id = ?",
                        (iso_utc(base_created_at + timedelta(microseconds=index)), row["id"]),
                    )
            connection.execute(
                "INSERT INTO publication_metadata(key, value) VALUES ('target_order_v1', '1')"
            )
            for platform in PLATFORMS:
                _reschedule_pending(connection, platform)
    cancelled_migration = connection.execute(
        "SELECT value FROM publication_metadata WHERE key = 'cancelled_rows_removed_v1'"
    ).fetchone()
    if cancelled_migration is None:
        with connection:
            connection.execute("DELETE FROM publication_queue WHERE status = 'cancelled'")
            connection.execute(
                "INSERT INTO publication_metadata(key, value) "
                "VALUES ('cancelled_rows_removed_v1', '1')"
            )
    connection.commit()
    return connection


def _as_item(row: sqlite3.Row) -> dict:
    return {
        "id": str(row["id"]),
        "request_id": str(row["request_id"]),
        "job_id": str(row["job_id"]),
        "platform": str(row["platform"]),
        "target_ids": json.loads(row["target_ids_json"]),
        "display_title": str(row["display_title"]),
        "youtube_title": str(row["youtube_title"] or ""),
        "instagram_caption": str(row["instagram_caption"] or ""),
        "cover_id": str(row["cover_id"] or ""),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
        "scheduled_at": str(row["scheduled_at"]),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "attempts": int(row["attempts"]),
        "progress": json.loads(row["progress_json"] or "{}"),
        "error": str(row["error"] or ""),
        "confirm_unknown": bool(row["confirm_unknown"]),
        "force_now": bool(row["force_now"]),
    }


def _ensure_platform(connection: sqlite3.Connection, platform: str) -> sqlite3.Row:
    if platform not in PLATFORMS:
        raise ValueError("Неизвестная социальная сеть")
    connection.execute(
        "INSERT OR IGNORE INTO publication_settings(platform, interval_minutes) VALUES (?, ?)",
        (platform, DEFAULT_INTERVAL_MINUTES),
    )
    return connection.execute(
        "SELECT platform, interval_minutes, last_attempt_at "
        "FROM publication_settings WHERE platform = ?", (platform,)
    ).fetchone()


def _reschedule_pending(
    connection: sqlite3.Connection, platform: str, now: Optional[datetime] = None
) -> None:
    settings = _ensure_platform(connection, platform)
    interval = timedelta(minutes=int(settings["interval_minutes"]))
    current = (now or utc_now()).astimezone(timezone.utc)
    last_attempt = _parse_time(settings["last_attempt_at"])
    next_allowed = last_attempt + interval if last_attempt else None
    rows = connection.execute(
        "SELECT id, created_at, scheduled_at, force_now FROM publication_queue "
        "WHERE platform = ? AND status = 'queued' "
        "ORDER BY force_now DESC, created_at ASC, id ASC", (platform,)
    ).fetchall()
    for row in rows:
        created_at = _parse_time(row["created_at"]) or current
        if bool(row["force_now"]):
            scheduled = _parse_time(row["scheduled_at"]) or current
            next_allowed = scheduled + interval
        else:
            scheduled = max(created_at, next_allowed) if next_allowed else created_at
            next_allowed = scheduled + interval
        connection.execute(
            "UPDATE publication_queue SET scheduled_at = ? WHERE id = ? AND status = 'queued'",
            (iso_utc(scheduled), row["id"]),
        )


def get_settings() -> list:
    with closing(_connect()) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT platform, interval_minutes, last_attempt_at "
            "FROM publication_settings ORDER BY platform"
        ).fetchall()]


def get_control() -> dict:
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT paused, pause_reason FROM publication_control WHERE id = 1"
        ).fetchone()
        return {"paused": bool(row["paused"]), "pause_reason": str(row["pause_reason"] or "")}


def is_paused() -> bool:
    return bool(get_control()["paused"])


def resume_queue() -> dict:
    with closing(_connect()) as connection:
        with connection:
            connection.execute(
                "UPDATE publication_control SET paused = 0, pause_reason = '' WHERE id = 1"
            )
        row = connection.execute(
            "SELECT paused, pause_reason FROM publication_control WHERE id = 1"
        ).fetchone()
        return {"paused": bool(row["paused"]), "pause_reason": str(row["pause_reason"] or "")}


def update_setting(platform: str, interval_minutes: int, now: Optional[datetime] = None) -> dict:
    settings = update_settings({platform: interval_minutes}, now=now)
    return next(item for item in settings if item["platform"] == platform)


def update_settings(updates: dict, now: Optional[datetime] = None) -> list:
    if not isinstance(updates, dict) or not updates:
        raise ValueError("Передайте частоту хотя бы для одной социальной сети")
    normalized = {}
    for platform, value in updates.items():
        if platform not in PLATFORMS:
            raise ValueError("Неизвестная социальная сеть")
        try:
            interval = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Интервал должен быть целым числом минут") from exc
        if not MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES:
            raise ValueError("Интервал должен быть от 1 минуты до 7 дней")
        normalized[platform] = interval
    with closing(_connect()) as connection:
        with connection:
            for platform, interval in normalized.items():
                connection.execute(
                    "INSERT INTO publication_settings(platform, interval_minutes) VALUES (?, ?) "
                    "ON CONFLICT(platform) DO UPDATE SET interval_minutes = excluded.interval_minutes",
                    (platform, interval),
                )
            for platform in normalized:
                _reschedule_pending(connection, platform, now=now)
        rows = connection.execute(
            "SELECT platform, interval_minutes, last_attempt_at "
            "FROM publication_settings ORDER BY platform"
        ).fetchall()
        return [dict(row) for row in rows]


def enqueue(
    *, request_id: str, job_id: str, platform: str, target_ids: list,
    display_title: str, youtube_title: str = "", instagram_caption: str = "",
    cover_id: str = "", confirm_unknown: bool = False,
    now: Optional[datetime] = None,
) -> dict:
    if platform not in PLATFORMS:
        raise ValueError("Неизвестная социальная сеть")
    if not request_id:
        request_id = uuid.uuid4().hex
    targets = list(dict.fromkeys(str(target_id) for target_id in target_ids if target_id))
    if not targets:
        raise ValueError("Не выбраны цели публикации")
    created_at = iso_utc(now)
    queue_id = uuid.uuid4().hex
    with closing(_connect()) as connection:
        with connection:
            connection.execute(
                """INSERT OR IGNORE INTO publication_queue(
                       id, request_id, job_id, platform, target_ids_json, display_title,
                       youtube_title, instagram_caption, cover_id, status,
                       created_at, scheduled_at, confirm_unknown
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)""",
                (queue_id, request_id, str(job_id), platform, json.dumps(targets),
                 str(display_title), str(youtube_title), str(instagram_caption),
                 str(cover_id), created_at, created_at, int(bool(confirm_unknown))),
            )
            row = connection.execute(
                "SELECT * FROM publication_queue WHERE request_id = ? AND platform = ?",
                (request_id, platform),
            ).fetchone()
            _reschedule_pending(connection, platform, now=now)
            row = connection.execute(
                "SELECT * FROM publication_queue WHERE id = ?", (row["id"],)
            ).fetchone()
        return _as_item(row)


def list_page(page: int = 1, page_size: int = 10) -> dict:
    page = max(1, int(page))
    page_size = max(1, min(50, int(page_size)))
    with closing(_connect()) as connection:
        total = int(connection.execute(
            "SELECT COUNT(*) FROM publication_queue"
        ).fetchone()[0])
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        offset = (page - 1) * page_size
        rows = connection.execute(
            "SELECT * FROM publication_queue "
            "ORDER BY CASE WHEN status IN ('queued','preparing','publishing') THEN 0 ELSE 1 END, "
            "scheduled_at ASC, created_at DESC, id DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()
    return {
        "items": [_as_item(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


def get_item(queue_id: str) -> Optional[dict]:
    with closing(_connect()) as connection:
        row = connection.execute(
            "SELECT * FROM publication_queue WHERE id = ?", (queue_id,)
        ).fetchone()
        return _as_item(row) if row else None


def claim_next_due(now: Optional[datetime] = None, force_only: bool = False) -> Optional[dict]:
    current = (now or utc_now()).astimezone(timezone.utc)
    current_text = iso_utc(current)
    force_filter = " AND force_now = 1" if force_only else ""
    with closing(_connect()) as connection:
        with connection:
            row = connection.execute(
                "SELECT * FROM publication_queue WHERE status = 'queued' "
                "AND scheduled_at <= ?" + force_filter + " "
                "ORDER BY force_now DESC, scheduled_at ASC, created_at ASC, id ASC LIMIT 1",
                (current_text,),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                "UPDATE publication_queue SET status = 'preparing', started_at = ?, "
                "attempts = attempts + 1, force_now = 0, error = '' "
                "WHERE id = ? AND status = 'queued'",
                (current_text, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM publication_queue WHERE id = ?", (row["id"],)
            ).fetchone()
            return _as_item(claimed)


def record_attempt(queue_id: str, now: Optional[datetime] = None) -> Optional[dict]:
    current = now or utc_now()
    current_text = iso_utc(current)
    with closing(_connect()) as connection:
        with connection:
            row = connection.execute(
                "SELECT platform FROM publication_queue WHERE id = ?", (queue_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE publication_settings SET last_attempt_at = ? WHERE platform = ?",
                (current_text, row["platform"]),
            )
            _reschedule_pending(connection, row["platform"], now=current)
            item = connection.execute(
                "SELECT * FROM publication_queue WHERE id = ?", (queue_id,)
            ).fetchone()
            return _as_item(item)


def set_progress(queue_id: str, progress: dict) -> Optional[dict]:
    with closing(_connect()) as connection:
        with connection:
            cursor = connection.execute(
                "UPDATE publication_queue SET progress_json = ? WHERE id = ?",
                (json.dumps(progress, ensure_ascii=False), queue_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM publication_queue WHERE id = ?", (queue_id,)
            ).fetchone()
            return _as_item(row)


def set_status(queue_id: str, status: str, error: str = "") -> Optional[dict]:
    if status not in ALL_STATUSES:
        raise ValueError("Неизвестный статус очереди")
    finished_at = iso_utc() if status in TERMINAL_STATUSES else None
    with closing(_connect()) as connection:
        with connection:
            cursor = connection.execute(
                "UPDATE publication_queue SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, str(error or ""), finished_at, queue_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM publication_queue WHERE id = ?", (queue_id,)
            ).fetchone()
            return _as_item(row)


def publish_now(
    queue_id: str, confirm_unknown: bool = False, now: Optional[datetime] = None
) -> Optional[dict]:
    now = now or utc_now()
    now_text = iso_utc(now)
    with closing(_connect()) as connection:
        with connection:
            row = connection.execute(
                "SELECT * FROM publication_queue WHERE id = ?", (queue_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] in {"preparing", "publishing"}:
                raise ValueError("Эта публикация уже выполняется")
            if row["status"] in {"published", "cancelled"}:
                raise ValueError("Завершённую запись нельзя запустить повторно; создайте новую публикацию")
            connection.execute(
                "UPDATE publication_queue SET status = 'queued', scheduled_at = ?, "
                "force_now = 1, confirm_unknown = ?, error = '', finished_at = NULL WHERE id = ?",
                (now_text, int(bool(confirm_unknown)), queue_id),
            )
            _reschedule_pending(connection, row["platform"], now=now)
            updated = connection.execute(
                "SELECT * FROM publication_queue WHERE id = ?", (queue_id,)
            ).fetchone()
            return _as_item(updated)


def cancel_queued_item(queue_id: str, now: Optional[datetime] = None) -> Optional[dict]:
    current = now or utc_now()
    with closing(_connect()) as connection:
        with connection:
            row = connection.execute(
                "SELECT * FROM publication_queue WHERE id = ?", (queue_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "queued":
                raise ValueError("Уже начатую или завершённую публикацию нельзя убрать из очереди")
            item = _as_item(row)
            connection.execute(
                "DELETE FROM publication_queue WHERE id = ? AND status = 'queued'", (queue_id,)
            )
            _reschedule_pending(connection, row["platform"], now=current)
            item["status"] = "cancelled"
            item["error"] = "Удалено пользователем"
            item["finished_at"] = iso_utc(current)
            return item


def recover_inflight() -> list:
    """Mark interrupted publications as unknown; never blindly send them twice."""
    recovered = []
    now = iso_utc()
    with closing(_connect()) as connection:
        with connection:
            rows = connection.execute(
                "SELECT id FROM publication_queue WHERE status IN ('preparing','publishing')"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE publication_queue SET status = 'unknown', "
                    "error = 'Сервер перезапустился во время публикации; проверьте площадку вручную', "
                    "finished_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
                recovered.append(str(row["id"]))
    return recovered
