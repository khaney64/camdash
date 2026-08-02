from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  camera_id TEXT NOT NULL,
  camera_name TEXT NOT NULL,
  source TEXT NOT NULL,
  source_key TEXT,
  triggered_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  profile TEXT NOT NULL,
  status TEXT NOT NULL,
  trigger_count INTEGER NOT NULL DEFAULT 1,
  primary_media_id TEXT,
  analysis_json TEXT,
  alert_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(camera_id, source_key)
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(triggered_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_camera ON events(camera_id, triggered_at DESC);
CREATE TABLE IF NOT EXISTS media (
  id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  sequence INTEGER NOT NULL DEFAULT 0,
  captured_at TEXT NOT NULL,
  path TEXT,
  thumb_path TEXT,
  mime_type TEXT,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  duration_seconds REAL,
  analyzed INTEGER NOT NULL DEFAULT 0,
  analysis_json TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_event ON media(event_id, sequence);
CREATE TABLE IF NOT EXISTS saved_media (
  id TEXT PRIMARY KEY,
  source_media_id TEXT,
  source_event_id TEXT,
  camera_id TEXT NOT NULL,
  camera_name TEXT NOT NULL,
  kind TEXT NOT NULL,
  saved_at TEXT NOT NULL,
  path TEXT NOT NULL,
  thumb_path TEXT,
  mime_type TEXT,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  analysis_json TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def create_event(self, row: dict[str, Any]) -> bool:
        with self._lock:
            try:
                self.conn.execute(
                    """INSERT INTO events
                    (id,camera_id,camera_name,source,source_key,triggered_at,received_at,profile,status,
                     trigger_count,created_at) VALUES (?,?,?,?,?,?,?,?,?,1,?)""",
                    (row["id"], row["camera_id"], row["camera_name"], row["source"], row.get("source_key"),
                     row["triggered_at"], row["received_at"], row["profile"], row["status"], row["created_at"]),
                )
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def increment_trigger(self, event_id: str) -> None:
        self.execute("UPDATE events SET trigger_count=trigger_count+1 WHERE id=?", (event_id,))

    def update_event(self, event_id: str, **values: Any) -> None:
        allowed = {"status", "primary_media_id", "analysis_json", "alert_json", "error", "trigger_count"}
        values = {k: json.dumps(v) if k.endswith("_json") and not isinstance(v, str) else v for k, v in values.items() if k in allowed}
        if not values:
            return
        self.execute("UPDATE events SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?", (*values.values(), event_id))

    def add_media(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO media
                (id,event_id,kind,sequence,captured_at,path,thumb_path,mime_type,size_bytes,duration_seconds,
                 analyzed,analysis_json,error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], row["event_id"], row["kind"], row.get("sequence", 0), row["captured_at"],
                 row.get("path"), row.get("thumb_path"), row.get("mime_type"), row.get("size_bytes", 0),
                 row.get("duration_seconds"), int(row.get("analyzed", False)),
                 json.dumps(row.get("analysis")) if row.get("analysis") is not None else None, row.get("error")),
            )
            self.conn.commit()

    def update_media(self, media_id: str, **values: Any) -> None:
        allowed = {"path", "thumb_path", "mime_type", "size_bytes", "duration_seconds", "analyzed", "analysis_json", "error"}
        values = {k: json.dumps(v) if k == "analysis_json" and not isinstance(v, str) else v for k, v in values.items() if k in allowed}
        if values:
            self.execute("UPDATE media SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?", (*values.values(), media_id))

    def list_events(self, *, camera_id: str | None = None, status: str | None = None, query: str | None = None,
                    limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        where, args = [], []
        if camera_id:
            where.append("camera_id=?"); args.append(camera_id)
        if status:
            where.append("status=?"); args.append(status)
        if query:
            where.append("LOWER(COALESCE(analysis_json,'')) LIKE ?"); args.append(f"%{query.lower()}%")
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY triggered_at DESC LIMIT ? OFFSET ?"
        args.extend((max(1, min(limit, 200)), max(0, offset)))
        return [self._event_dict(r) for r in self.query(sql, tuple(args))]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM events WHERE id=?", (event_id,))
        if not rows:
            return None
        event = self._event_dict(rows[0])
        event["media"] = [self._media_dict(r) for r in self.query("SELECT * FROM media WHERE event_id=? ORDER BY sequence", (event_id,))]
        return event

    def get_media(self, media_id: str) -> dict[str, Any] | None:
        rows = self.query("""SELECT m.*,e.camera_id,e.camera_name FROM media m JOIN events e ON e.id=m.event_id WHERE m.id=?""", (media_id,))
        return self._media_dict(rows[0]) if rows else None

    def delete_event(self, event_id: str) -> dict[str, Any] | None:
        event = self.get_event(event_id)
        if event:
            self.execute("DELETE FROM events WHERE id=?", (event_id,))
        return event

    def save_media(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO saved_media
                (id,source_media_id,source_event_id,camera_id,camera_name,kind,saved_at,path,thumb_path,mime_type,size_bytes,analysis_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["id"], row.get("source_media_id"), row.get("source_event_id"), row["camera_id"], row["camera_name"],
                 row["kind"], row["saved_at"], row["path"], row.get("thumb_path"), row.get("mime_type"), row.get("size_bytes", 0),
                 json.dumps(row.get("analysis")) if row.get("analysis") is not None else None),
            )
            self.conn.commit()

    def list_saved(self) -> list[dict[str, Any]]:
        return [self._saved_dict(r) for r in self.query("SELECT * FROM saved_media ORDER BY saved_at DESC")]

    def get_saved(self, saved_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM saved_media WHERE id=?", (saved_id,))
        return self._saved_dict(rows[0]) if rows else None

    def delete_saved(self, saved_id: str) -> dict[str, Any] | None:
        item = self.get_saved(saved_id)
        if item:
            self.execute("DELETE FROM saved_media WHERE id=?", (saved_id,))
        return item

    def retention_candidates(self, cutoff: datetime, max_bytes: int) -> list[dict[str, Any]]:
        rows = self.query("""SELECT e.id,e.triggered_at,COALESCE(SUM(m.size_bytes),0) bytes
            FROM events e LEFT JOIN media m ON m.event_id=e.id
            GROUP BY e.id ORDER BY e.triggered_at DESC""")
        keep, delete, total = [], [], 0
        for row in rows:
            total += row["bytes"]
            timestamp = datetime.fromisoformat(row["triggered_at"])
            (delete if timestamp < cutoff or total > max_bytes else keep).append(dict(row))
        return delete

    def execute(self, sql: str, args: tuple = ()) -> None:
        with self._lock:
            self.conn.execute(sql, args)
            self.conn.commit()

    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(sql, args))

    @staticmethod
    def _json(value: str | None) -> Any:
        try:
            return json.loads(value) if value else None
        except json.JSONDecodeError:
            return None

    def _event_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["analysis"] = self._json(result.pop("analysis_json", None))
        result["alert"] = self._json(result.pop("alert_json", None))
        return result

    def _media_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["analysis"] = self._json(result.pop("analysis_json", None))
        result["analyzed"] = bool(result["analyzed"])
        return result

    def _saved_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["analysis"] = self._json(result.pop("analysis_json", None))
        return result


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
