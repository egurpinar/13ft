"""Personal archive: every article you read is saved to a local SQLite file.

This is the part of archive.ph that 13ft did not have: a copy that stays
available after the publisher changes or removes the page. Nothing leaves
your machine; the database is a single file under ``DATA_DIR``.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY,
    url            TEXT NOT NULL,
    url_key        TEXT NOT NULL,
    source_url     TEXT,
    title          TEXT NOT NULL DEFAULT '',
    byline         TEXT NOT NULL DEFAULT '',
    published      TEXT NOT NULL DEFAULT '',
    site_name      TEXT NOT NULL DEFAULT '',
    method         TEXT NOT NULL,
    content_source TEXT NOT NULL DEFAULT '',
    word_count     INTEGER NOT NULL DEFAULT 0,
    complete       INTEGER NOT NULL DEFAULT 0,
    warnings       TEXT NOT NULL DEFAULT '[]',
    attempts       TEXT NOT NULL DEFAULT '[]',
    content_html   TEXT NOT NULL,
    content_text   TEXT NOT NULL,
    raw_html       BLOB,
    fetched_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_key ON snapshots (url_key, fetched_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots (fetched_at);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS snapshots_fts USING fts5(
    title, site_name, content_text, content='snapshots', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS snapshots_fts_insert AFTER INSERT ON snapshots BEGIN
    INSERT INTO snapshots_fts (rowid, title, site_name, content_text)
    VALUES (new.id, new.title, new.site_name, new.content_text);
END;
CREATE TRIGGER IF NOT EXISTS snapshots_fts_delete AFTER DELETE ON snapshots BEGIN
    INSERT INTO snapshots_fts (snapshots_fts, rowid, title, site_name, content_text)
    VALUES ('delete', old.id, old.title, old.site_name, old.content_text);
END;
"""

# Columns for list views: everything except the large ones.
_SUMMARY_COLUMNS = (
    "id, url, url_key, source_url, title, byline, published, site_name, method, "
    "content_source, word_count, complete, fetched_at"
)


def default_data_dir():
    configured = os.environ.get("DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / "data"


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fts_query(text):
    """Turn free text into a safe FTS5 query: every word must match (prefix match)."""
    words = [w for w in text.split() if w]
    return " ".join('"' + w.replace('"', '""') + '"*' for w in words)


class ArchiveStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fts = False
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)
            try:
                db.executescript(_FTS_SCHEMA)
                self.fts = True
            except sqlite3.OperationalError:
                self.fts = False  # SQLite built without FTS5: fall back to LIKE search
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def _connect(self):
        """One short-lived connection per operation: commit on success, always close."""
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, *, url, url_key, method, content_html, content_text, source_url="",
             title="", byline="", published="", site_name="", content_source="",
             word_count=0, complete=False, warnings=(), attempts=(), raw_html=None,
             fetched_at=None):
        raw = zlib.compress(raw_html.encode("utf-8"), 6) if raw_html else None
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO snapshots (url, url_key, source_url, title, byline, published,
                       site_name, method, content_source, word_count, complete, warnings,
                       attempts, content_html, content_text, raw_html, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (url, url_key, source_url, title, byline, published, site_name, method,
                 content_source, int(word_count), int(bool(complete)),
                 json.dumps(list(warnings)), json.dumps(list(attempts)),
                 content_html, content_text, raw, fetched_at or _now()),
            )
            return cursor.lastrowid

    def get(self, snapshot_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        return _decode(row)

    def raw_html(self, snapshot_id):
        with self._connect() as db:
            row = db.execute("SELECT raw_html FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if not row or row["raw_html"] is None:
            return None
        return zlib.decompress(row["raw_html"]).decode("utf-8")

    def latest_for(self, url_key):
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM snapshots WHERE url_key = ? ORDER BY fetched_at DESC, id DESC LIMIT 1",
                (url_key,),
            ).fetchone()
        return _decode(row)

    def versions(self, url_key):
        with self._connect() as db:
            rows = db.execute(
                f"SELECT {_SUMMARY_COLUMNS} FROM snapshots WHERE url_key = ? ORDER BY fetched_at DESC, id DESC",
                (url_key,),
            ).fetchall()
        return [dict(r) for r in rows]

    def search(self, query="", limit=50, offset=0):
        """Newest first. Returns (rows, total)."""
        query = (query or "").strip()
        with self._connect() as db:
            if not query:
                total = db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
                rows = db.execute(
                    f"SELECT {_SUMMARY_COLUMNS} FROM snapshots ORDER BY fetched_at DESC, id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
            elif self.fts:
                match = _fts_query(query)
                where = "id IN (SELECT rowid FROM snapshots_fts WHERE snapshots_fts MATCH ?)"
                total = db.execute(f"SELECT COUNT(*) FROM snapshots WHERE {where}", (match,)).fetchone()[0]
                rows = db.execute(
                    f"SELECT {_SUMMARY_COLUMNS} FROM snapshots WHERE {where} "
                    "ORDER BY fetched_at DESC, id DESC LIMIT ? OFFSET ?",
                    (match, limit, offset),
                ).fetchall()
            else:
                like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                where = ("(title LIKE ? ESCAPE '\\' OR site_name LIKE ? ESCAPE '\\' "
                         "OR content_text LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\')")
                params = (like, like, like, like)
                total = db.execute(f"SELECT COUNT(*) FROM snapshots WHERE {where}", params).fetchone()[0]
                rows = db.execute(
                    f"SELECT {_SUMMARY_COLUMNS} FROM snapshots WHERE {where} "
                    "ORDER BY fetched_at DESC, id DESC LIMIT ? OFFSET ?",
                    params + (limit, offset),
                ).fetchall()
        return [dict(r) for r in rows], total

    def delete(self, snapshot_id):
        with self._connect() as db:
            return db.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,)).rowcount > 0


def _decode(row):
    if row is None:
        return None
    record = dict(row)
    record.pop("raw_html", None)
    record["has_raw_html"] = row["raw_html"] is not None
    record["warnings"] = json.loads(record["warnings"] or "[]")
    record["attempts"] = json.loads(record["attempts"] or "[]")
    record["complete"] = bool(record["complete"])
    return record


_store = None
_store_lock = threading.Lock()


def get_store():
    """Process-wide store at ``DATA_DIR/archive.sqlite3`` (created on first use)."""
    global _store
    with _store_lock:
        path = default_data_dir() / "archive.sqlite3"
        if _store is None or _store.path != path:
            _store = ArchiveStore(path)
        return _store
