"""Dump scraped data to CSV or JSONL.

Posts are joined against `profiles` so each row carries the owner's follower
count, which is what you usually want for engagement-rate maths.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

POSTS_QUERY = """
SELECT p.shortcode,
       p.media_pk,
       p.owner_username,
       pr.followers        AS owner_followers,
       p.taken_at,
       p.media_type,
       p.like_count,
       p.comment_count,
       p.view_count,
       p.repost_count,
       p.share_count,
       p.caption,
       p.source,
       p.scraped_at
FROM posts p
LEFT JOIN profiles pr ON pr.username = p.owner_username
ORDER BY p.taken_at DESC
"""

COMMENTS_QUERY = """
SELECT c.id,
       c.shortcode,
       p.owner_username,
       c.parent_id,
       c.author,
       c.text,
       c.like_count,
       c.media_kind,
       c.created_at
FROM comments c
LEFT JOIN posts p ON p.shortcode = c.shortcode
ORDER BY c.shortcode, c.created_at
"""

TABLES = {"posts": POSTS_QUERY, "comments": COMMENTS_QUERY}


def _write_csv(rows: list[sqlite3.Row], dest: Path) -> int:
    if not rows:
        dest.write_text("", encoding="utf-8")
        return 0
    with dest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    return len(rows)


def _write_jsonl(rows: list[sqlite3.Row], dest: Path) -> int:
    with dest.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
    return len(rows)


def export(conn: sqlite3.Connection, out_dir: Path, fmt: str = "csv") -> dict[str, int]:
    """Write one file per table into `out_dir`. Returns row counts."""
    if fmt not in ("csv", "jsonl"):
        raise ValueError(f"unsupported format: {fmt}")

    out_dir.mkdir(parents=True, exist_ok=True)
    write = _write_csv if fmt == "csv" else _write_jsonl
    counts: dict[str, int] = {}

    for name, query in TABLES.items():
        rows = conn.execute(query).fetchall()
        counts[name] = write(rows, out_dir / f"{name}.{fmt}")

    return counts
