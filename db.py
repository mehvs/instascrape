"""SQLite connection handling and schema migrations.

Plain sqlite3, no ORM. Schema uses only syntax that Postgres also understands
(`ON CONFLICT ... DO UPDATE`, no AUTOINCREMENT keyword outside the PK) so the
move to a hosted database later is a new backend class, not a rewrite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id          INTEGER PRIMARY KEY,
        kind        TEXT    NOT NULL,
        payload     TEXT    NOT NULL DEFAULT '{}',
        status      TEXT    NOT NULL DEFAULT 'pending',
        attempts    INTEGER NOT NULL DEFAULT 0,
        run_after   INTEGER NOT NULL DEFAULT 0,
        locked_by   TEXT,
        locked_at   INTEGER,
        last_error  TEXT,
        dedupe_key  TEXT UNIQUE,
        created_at  INTEGER NOT NULL DEFAULT 0,
        updated_at  INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs (status, run_after)",
    """
    CREATE TABLE IF NOT EXISTS profiles (
        username    TEXT PRIMARY KEY,
        user_id     TEXT,
        full_name   TEXT,
        followers   INTEGER,
        post_count  INTEGER,
        is_private  INTEGER,
        scraped_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS posts (
        shortcode       TEXT PRIMARY KEY,
        media_pk        TEXT,
        owner_username  TEXT,
        caption         TEXT,
        taken_at        TEXT,
        media_type      TEXT,
        like_count      INTEGER,
        comment_count   INTEGER,
        view_count      INTEGER,
        repost_count    INTEGER,
        share_count     INTEGER,
        source          TEXT,
        scraped_at      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_posts_owner ON posts (owner_username)",
    """
    CREATE TABLE IF NOT EXISTS comments (
        id          TEXT PRIMARY KEY,
        shortcode   TEXT NOT NULL,
        parent_id   TEXT,
        author      TEXT,
        text        TEXT,
        like_count  INTEGER,
        created_at  TEXT,
        media_kind  TEXT,
        scraped_at  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_comments_shortcode ON comments (shortcode)",
    """
    CREATE TABLE IF NOT EXISTS raw_payloads (
        id          INTEGER PRIMARY KEY,
        shortcode   TEXT,
        url         TEXT,
        captured_at TEXT,
        path        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_raw_shortcode ON raw_payloads (shortcode)",
    """
    CREATE TABLE IF NOT EXISTS counters (
        name    TEXT PRIMARY KEY,
        day     TEXT NOT NULL,
        value   INTEGER NOT NULL DEFAULT 0
    )
    """,
]


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with the pragmas this project depends on.

    WAL lets you read the database (sqlite3 CLI, a viewer, the export command)
    while the worker is writing to it. Without it, readers block writers.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


# Columns added after the first release. Existing databases - notably the one
# on the Mac - need these bolted on, since CREATE TABLE IF NOT EXISTS is a
# no-op once the table exists.
ADDED_COLUMNS = {
    "posts": [
        ("media_pk", "TEXT"),
        ("repost_count", "INTEGER"),
    ],
    "comments": [
        ("media_kind", "TEXT"),
    ],
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        for name, coltype in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")


def migrate(conn: sqlite3.Connection) -> None:
    """Idempotent. Safe to run at every startup."""
    for statement in SCHEMA:
        conn.execute(statement)
    _add_missing_columns(conn)


def init(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn
