"""Thin persistence layer for scraped entities.

Handlers call these functions and never write SQL themselves. That is the one
rule that keeps a future move to hosted Postgres to a single new module.

Upserts use `ON CONFLICT ... DO UPDATE`, which SQLite and Postgres both speak.
`COALESCE(excluded.x, table.x)` means a re-scrape that failed to read one field
keeps the value we already had rather than nulling it out.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

from .models import Comment, Post, Profile


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_profile(conn: sqlite3.Connection, profile: Profile) -> None:
    conn.execute(
        """
        INSERT INTO profiles (username, user_id, full_name, followers,
                              post_count, is_private, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (username) DO UPDATE SET
            user_id    = COALESCE(excluded.user_id,    profiles.user_id),
            full_name  = COALESCE(excluded.full_name,  profiles.full_name),
            followers  = COALESCE(excluded.followers,  profiles.followers),
            post_count = COALESCE(excluded.post_count, profiles.post_count),
            is_private = COALESCE(excluded.is_private, profiles.is_private),
            scraped_at = excluded.scraped_at
        """,
        (
            profile.username,
            profile.user_id,
            profile.full_name,
            profile.followers,
            profile.post_count,
            None if profile.is_private is None else int(profile.is_private),
            _now_iso(),
        ),
    )


def upsert_post(conn: sqlite3.Connection, post: Post, source: str = "api") -> None:
    """Store a post.

    Which side wins a conflict depends on where the numbers came from:

    * API-derived values overwrite whatever is there. They are authoritative,
      and a re-scrape must be able to correct a bad earlier row.
    * DOM-scraped values only fill gaps (`COALESCE(posts.x, excluded.x)`), so a
      low-confidence guess can never clobber a good API reading.

    Either way a NULL never overwrites a real value.
    """
    trusted = not source.startswith("dom")
    # COALESCE(excluded.x, posts.x) -> incoming wins when it has a value.
    # COALESCE(posts.x, excluded.x) -> incoming only fills a gap.
    def merge(col: str) -> str:
        return (
            f"{col} = COALESCE(excluded.{col}, posts.{col})"
            if trusted
            else f"{col} = COALESCE(posts.{col}, excluded.{col})"
        )

    columns = (
        "media_pk",
        "owner_username",
        "caption",
        "taken_at",
        "media_type",
        "like_count",
        "comment_count",
        "view_count",
        "repost_count",
        "share_count",
    )

    conn.execute(
        f"""
        INSERT INTO posts (shortcode, media_pk, owner_username, caption, taken_at,
                           media_type, like_count, comment_count, view_count,
                           repost_count, share_count, source, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (shortcode) DO UPDATE SET
            {", ".join(merge(c) for c in columns)},
            source     = excluded.source,
            scraped_at = excluded.scraped_at
        """,
        (
            post.shortcode,
            post.media_pk,
            post.owner_username,
            post.caption,
            post.taken_at,
            post.media_type,
            post.like_count,
            post.comment_count,
            post.view_count,
            post.repost_count,
            post.share_count,
            source,
            _now_iso(),
        ),
    )


def upsert_comments(conn: sqlite3.Connection, comments: Iterable[Comment]) -> int:
    rows = [
        (
            c.id,
            c.shortcode,
            c.parent_id,
            c.author,
            c.text,
            c.like_count,
            c.created_at,
            c.media_kind,
            _now_iso(),
        )
        for c in comments
    ]
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO comments (id, shortcode, parent_id, author, text,
                              like_count, created_at, media_kind, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            text       = COALESCE(excluded.text,       comments.text),
            like_count = COALESCE(excluded.like_count, comments.like_count),
            media_kind = COALESCE(excluded.media_kind, comments.media_kind),
            scraped_at = excluded.scraped_at
        """,
        rows,
    )
    return len(rows)


def record_raw_payload(
    conn: sqlite3.Connection,
    shortcode: Optional[str],
    url: str,
    path: str,
) -> None:
    conn.execute(
        "INSERT INTO raw_payloads (shortcode, url, captured_at, path) VALUES (?, ?, ?, ?)",
        (shortcode, url, _now_iso(), path),
    )


def has_comments(conn: sqlite3.Connection, shortcode: str) -> bool:
    """Whether we already pulled the comment thread for this post.

    Post jobs now only fetch comments - the profile crawl already stored the
    metrics - so "already done" has to mean "has comments", not "has a
    like_count". Checking like_count here would skip every job, since the crawl
    writes the metrics before queueing.
    """
    row = conn.execute(
        "SELECT 1 FROM comments WHERE shortcode = ? LIMIT 1", (shortcode,)
    ).fetchone()
    return row is not None


def media_pk_for(conn: sqlite3.Connection, shortcode: str) -> Optional[str]:
    """The stored media id for a post, if the profile crawl already saw it.

    When this returns a value the post job can call the comments endpoint
    directly and skip loading the post page entirely.
    """
    row = conn.execute(
        "SELECT media_pk FROM posts WHERE shortcode = ?", (shortcode,)
    ).fetchone()
    return row["media_pk"] if row and row["media_pk"] else None
