"""Command line interface.

    instascrape login                      sign in once, by hand
    instascrape add profile <username>     queue a profile crawl
    instascrape add post <shortcode>       queue a single post
    instascrape worker                     run the loop
    instascrape status                     queue depth, session, daily budget
    instascrape show posts                 print rows in the terminal
    instascrape export                     dump to CSV or JSONL
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

import typer

from . import browser as browser_mod
from . import worker as worker_mod
from .config import Settings, get_settings
from .db import init as db_init
from .export import export as export_data
from .models import KIND_POST, KIND_PROFILE
from .queue import SQLiteQueue
from .ratelimit import RateLimiter

app = typer.Typer(
    add_completion=False,
    help="Scrape likes, comment counts and comment threads from Instagram posts.",
)
add_app = typer.Typer(help="Queue work for the worker to pick up.")
app.add_typer(add_app, name="add")


# Matches /p/, /reel/, /reels/ and /tv/ permalinks. Anchoring on the path
# segment rather than splitting on "/" avoids picking up a trailing query
# string, which is its own segment in URLs that end with "/?utm_source=...".
_PERMALINK_RE = re.compile(r"/(?:p|reels?|tv)/([A-Za-z0-9_-]+)")
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,30}$")


def parse_shortcode(value: str) -> str:
    """Accept a bare shortcode or any Instagram permalink; return the shortcode."""
    text = (value or "").strip()
    if not text:
        raise typer.BadParameter("empty post reference")

    if "instagram.com" in text or "/" in text:
        match = _PERMALINK_RE.search(text)
        if not match:
            raise typer.BadParameter(f"could not find a post shortcode in {value!r}")
        text = match.group(1)

    text = text.split("?", 1)[0].strip("/")
    if not _SHORTCODE_RE.match(text):
        raise typer.BadParameter(f"{text!r} does not look like a post shortcode")
    return text


def _open(settings: Settings):
    settings.ensure_dirs()
    conn = db_init(settings.db_path)
    return conn, SQLiteQueue(conn, max_attempts=settings.max_attempts)


# --- login -------------------------------------------------------------------


@app.command()
def login(
    timeout: int = typer.Option(
        600, help="Seconds to wait for you to sign in (default 10 minutes)."
    ),
) -> None:
    """Open a browser and wait while you sign in yourself.

    Your password is typed into Instagram's own page and never passes through
    this tool - only the resulting session cookie is saved.
    """
    settings = get_settings()
    ok = browser_mod.interactive_login(settings, timeout_seconds=timeout)
    raise typer.Exit(0 if ok else 1)


# --- queueing ----------------------------------------------------------------


@add_app.command("profile")
def add_profile(
    username: str = typer.Argument(..., help="Instagram username, with or without @."),
    limit: int = typer.Option(
        30, help="How many recent posts to cover. Use 0 for every post on the profile."
    ),
    comments: bool = typer.Option(
        True,
        "--comments/--no-comments",
        help="Also queue a comment-fetch job per post. Metrics are stored either way.",
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Re-scrape posts already in the database."
    ),
) -> None:
    """Queue a profile crawl.

    The crawl stores full metrics (likes, comment counts, reposts, caption) for
    every post it sees - roughly 12 per request - and then optionally queues one
    lightweight comment-fetch job per post.
    """
    settings = get_settings()
    conn, queue = _open(settings)
    clean = username.strip().lstrip("@")
    unlimited = limit <= 0

    if unlimited and comments:
        # Metrics are cheap; comment jobs are not. Say so before the user finds
        # out three days in.
        typer.echo(
            "Note: unlimited crawl with comments. Metrics are cheap (~12 posts "
            f"per request), but comment jobs cost one slot each and you have "
            f"{settings.daily_cap}/day - a 1,000-post profile would take about "
            f"{1000 // max(1, settings.daily_cap)} days.\n"
            "      Use --no-comments for metrics only (minutes, not days).\n"
        )

    job_id = queue.put(
        KIND_PROFILE,
        {"username": clean, "limit": limit, "refresh": refresh, "comments": comments},
        dedupe_key=None if refresh else f"profile:{clean}:{limit}:{int(comments)}",
    )
    scope = "all posts" if unlimited else f"limit {limit}"
    if job_id is None:
        typer.echo(f"@{clean} is already queued for {scope} (use --refresh to force).")
    else:
        typer.echo(
            f"Queued profile crawl for @{clean} (job {job_id}, {scope}, "
            f"comments {'on' if comments else 'off'})."
        )
    conn.close()


@add_app.command("post")
def add_post(
    shortcode: str = typer.Argument(..., help="Post shortcode or full URL."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-scrape if already stored."),
) -> None:
    """Queue a single post by shortcode or URL."""
    settings = get_settings()
    conn, queue = _open(settings)

    clean = parse_shortcode(shortcode)

    job_id = queue.put(
        KIND_POST,
        {"shortcode": clean},
        dedupe_key=None if refresh else f"post:{clean}",
    )
    if job_id is None:
        typer.echo(f"{clean} is already queued (use --refresh to force).")
    else:
        typer.echo(f"Queued post {clean} (job {job_id}).")
    conn.close()


# --- running -----------------------------------------------------------------


@app.command()
def worker(
    once: bool = typer.Option(
        False, "--once", help="Process one job, then exit. Good for testing."
    ),
    max_jobs: Optional[int] = typer.Option(
        None, "--max-jobs", help="Stop after this many jobs."
    ),
    headless: Optional[bool] = typer.Option(
        None, "--headless/--headed", help="Override the configured browser mode."
    ),
) -> None:
    """Run the worker loop: claim a job, do it, repeat."""
    settings = get_settings()
    if headless is not None:
        settings = settings.model_copy(update={"headless": headless})
    raise typer.Exit(worker_mod.run(settings, once=once, max_jobs=max_jobs))


# --- inspection --------------------------------------------------------------


@app.command()
def status(
    check_session: bool = typer.Option(
        False, "--check-session", help="Open a browser to verify the login still works."
    ),
) -> None:
    """Show queue depth, stored rows, today's budget and session state."""
    settings = get_settings()
    conn, queue = _open(settings)

    typer.echo(f"database   {settings.db_path}")

    stats = queue.stats()
    if stats:
        typer.echo("queue      " + "  ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    else:
        typer.echo("queue      empty")

    soonest = queue.next_retry_at()
    if soonest is not None:
        wait = max(0, soonest - int(time.time()))
        typer.echo(
            f"backoff    next retry in {wait}s "
            f"(at {time.strftime('%H:%M:%S', time.localtime(soonest))}) "
            "- `instascrape retry` clears it"
        )

    for table in ("profiles", "posts", "comments"):
        count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        typer.echo(f"{table:<10} {count}")

    limiter = RateLimiter(
        conn, settings.min_delay, settings.max_delay, settings.daily_cap
    )
    typer.echo(
        f"budget     {limiter.used_today()}/{settings.daily_cap} used today "
        f"({limiter.remaining_today()} left)"
    )

    dead = conn.execute(
        "SELECT id, kind, last_error FROM jobs WHERE status = 'dead' ORDER BY id DESC LIMIT 5"
    ).fetchall()
    if dead:
        typer.echo("\nrecent dead jobs:")
        for row in dead:
            typer.echo(f"  {row['id']} [{row['kind']}] {row['last_error']}")

    if not settings.storage_state_path.exists():
        typer.echo("\nsession    MISSING - run `instascrape login`")
    elif check_session:
        ok = browser_mod.check_session(settings)
        typer.echo(f"\nsession    {'valid' if ok else 'EXPIRED - run `instascrape login`'}")
    else:
        typer.echo(
            f"\nsession    saved at {settings.storage_state_path} "
            "(use --check-session to verify)"
        )

    conn.close()


def _print_table(rows: list, max_width: int = 40) -> None:
    """Render rows as an aligned text table. Long values are truncated."""
    if not rows:
        typer.echo("(no rows)")
        return

    columns = list(rows[0].keys())

    def cell(value) -> str:
        if value is None:
            return "-"
        text = str(value).replace("\n", " ").replace("\r", " ")
        # ASCII "..." rather than the ellipsis character: the Windows console
        # uses cp1252 and renders U+2026 as a replacement glyph.
        return text[: max_width - 3] + "..." if len(text) > max_width else text

    table = [columns] + [[cell(row[c]) for c in columns] for row in rows]
    widths = [max(len(r[i]) for r in table) for i in range(len(columns))]

    typer.echo("  ".join(h.ljust(w) for h, w in zip(table[0], widths)).rstrip())
    typer.echo("  ".join("-" * w for w in widths))
    for row in table[1:]:
        typer.echo("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip())
    typer.echo(f"\n({len(rows)} row{'s' if len(rows) != 1 else ''})")


@app.command()
def show(
    table: str = typer.Argument(
        "posts", help="posts | comments | profiles | jobs | raw"
    ),
    limit: int = typer.Option(20, help="Rows to show."),
    post: Optional[str] = typer.Option(None, help="Filter comments to one shortcode."),
    owner: Optional[str] = typer.Option(None, help="Filter posts to one username."),
) -> None:
    """Print scraped rows straight to the terminal."""
    settings = get_settings()
    conn, _ = _open(settings)
    params: list = []

    if table == "posts":
        sql = (
            "SELECT shortcode, owner_username AS owner, like_count AS likes, "
            "comment_count AS comments, view_count AS views, media_type, "
            "taken_at, source FROM posts"
        )
        if owner:
            sql += " WHERE owner_username = ?"
            params.append(owner.lstrip("@"))
        sql += " ORDER BY taken_at DESC LIMIT ?"

    elif table == "comments":
        sql = (
            "SELECT shortcode, author, text, like_count AS likes, created_at "
            "FROM comments"
        )
        if post:
            sql += " WHERE shortcode = ?"
            params.append(post)
        sql += " ORDER BY shortcode, created_at LIMIT ?"

    elif table == "profiles":
        sql = (
            "SELECT username, user_id, full_name, followers, post_count, "
            "is_private, scraped_at FROM profiles ORDER BY username LIMIT ?"
        )

    elif table == "jobs":
        sql = (
            "SELECT id, kind, status, attempts, payload, last_error "
            "FROM jobs ORDER BY id DESC LIMIT ?"
        )

    elif table == "raw":
        sql = (
            "SELECT id, shortcode, captured_at, path FROM raw_payloads "
            "ORDER BY id DESC LIMIT ?"
        )

    else:
        conn.close()
        raise typer.BadParameter(
            f"unknown table {table!r} - use posts, comments, profiles, jobs or raw"
        )

    params.append(limit)
    _print_table(conn.execute(sql, params).fetchall())
    conn.close()


@app.command("sql")
def sql_cmd(
    query: str = typer.Argument(..., help="A SELECT statement."),
    limit: int = typer.Option(50, help="Safety cap if the query has no LIMIT."),
) -> None:
    """Run a read-only SELECT against the database.

    Writes are rejected - use the normal commands to change anything.
    """
    settings = get_settings()
    conn, _ = _open(settings)

    cleaned = query.strip().rstrip(";")
    if not cleaned.lower().startswith(("select", "with")):
        conn.close()
        raise typer.BadParameter("only SELECT (or WITH ... SELECT) queries are allowed")

    if " limit " not in cleaned.lower():
        cleaned += f" LIMIT {limit}"

    try:
        _print_table(conn.execute(cleaned).fetchall())
    except Exception as exc:
        typer.echo(f"query failed: {exc}")
        raise typer.Exit(1)
    finally:
        conn.close()


@app.command()
def show(
    table: str = typer.Argument("posts", help="posts | comments | profiles"),
    limit: int = typer.Option(15, help="Rows to print."),
    username: Optional[str] = typer.Option(None, help="Filter to one profile."),
) -> None:
    """Print scraped rows in the terminal, newest/biggest first."""
    settings = get_settings()
    conn, _ = _open(settings)

    if table == "posts":
        sql = """
            SELECT shortcode, media_type AS type, like_count AS likes,
                   comment_count AS comments, repost_count AS reposts,
                   owner_username AS owner, substr(caption, 1, 38) AS caption
            FROM posts
            {where}
            ORDER BY like_count DESC LIMIT ?
        """
    elif table == "comments":
        sql = """
            SELECT c.shortcode, c.author, c.like_count AS likes,
                   COALESCE(c.media_kind, 'text') AS kind,
                   substr(c.text, 1, 46) AS text
            FROM comments c LEFT JOIN posts p ON p.shortcode = c.shortcode
            {where}
            ORDER BY c.like_count DESC LIMIT ?
        """
    elif table == "profiles":
        sql = "SELECT * FROM profiles {where} LIMIT ?"
    else:
        raise typer.BadParameter("table must be posts, comments or profiles")

    column = {
        "posts": "owner_username",
        "comments": "p.owner_username",
        "profiles": "username",
    }[table]
    params: list = []
    where = ""
    if username:
        where = f"WHERE {column} = ?"
        params.append(username.strip().lstrip("@"))
    params.append(limit)

    rows = conn.execute(sql.format(where=where), params).fetchall()
    if not rows:
        typer.echo(f"No rows in {table}.")
        conn.close()
        return

    headers = list(rows[0].keys())
    widths = [
        max(len(h), max(len(str(r[h] if r[h] is not None else "-")) for r in rows))
        for h in headers
    ]
    widths = [min(w, 46) for w in widths]

    def line(values):
        return "  ".join(
            str(v if v is not None else "-").replace(chr(10), " ")[:w].ljust(w)
            for v, w in zip(values, widths)
        )

    typer.echo(line(headers))
    typer.echo("  ".join("-" * w for w in widths))
    for row in rows:
        typer.echo(line([row[h] for h in headers]))

    total = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    typer.echo(f"{chr(10)}showing {len(rows)} of {total} row(s) in {table}")
    conn.close()


@app.command("export")
def export_cmd(
    out: Path = typer.Option(Path("./out"), help="Directory to write into."),
    fmt: str = typer.Option("csv", "--format", help="csv or jsonl."),
) -> None:
    """Write posts and comments to files."""
    settings = get_settings()
    conn, _ = _open(settings)
    counts = export_data(conn, out, fmt)
    for name, count in counts.items():
        typer.echo(f"{count:>7} rows -> {out / f'{name}.{fmt}'}")
    conn.close()


@app.command()
def retry(
    dead: bool = typer.Option(False, "--dead", help="Also requeue jobs marked dead."),
) -> None:
    """Put failed jobs back on the queue immediately."""
    settings = get_settings()
    conn, _ = _open(settings)
    statuses = "('failed', 'dead')" if dead else "('failed')"
    cur = conn.execute(
        f"""
        UPDATE jobs
        SET status = 'pending', attempts = 0, run_after = 0, last_error = NULL
        WHERE status IN {statuses}
        """
    )
    typer.echo(f"Requeued {cur.rowcount} job(s).")
    conn.close()


@app.command()
def cancel(
    job_ids: Optional[list[int]] = typer.Argument(None, help="Job ids to delete."),
    username: Optional[str] = typer.Option(
        None, "--username", "-u", help="Cancel queued jobs for this profile instead."
    ),
    all_pending: bool = typer.Option(
        False, "--all-pending", help="Cancel every job that hasn't run yet."
    ),
    force: bool = typer.Option(
        False, "--force", help="Also cancel jobs a worker is currently running."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Delete queued jobs.

    Only affects the queue - scraped posts, comments and profiles are untouched.
    Jobs already marked `done` are left alone; cancelling one would just make it
    look like the work never happened.
    """
    settings = get_settings()
    conn, _ = _open(settings)

    # A 'running' job belongs to a live worker. Deleting the row underneath it
    # does not stop the work, so skip those unless explicitly forced.
    statuses = ["pending", "failed", "dead"]
    if force:
        statuses.append("running")
    placeholders = ",".join("?" for _ in statuses)

    if job_ids:
        ids = ",".join("?" for _ in job_ids)
        where = f"id IN ({ids}) AND status IN ({placeholders})"
        params: list = [*job_ids, *statuses]
    elif username:
        clean = username.strip().lstrip("@")
        where = f"payload LIKE ? AND status IN ({placeholders})"
        params = [f'%"{clean}"%', *statuses]
    elif all_pending:
        where = f"status IN ({placeholders})"
        params = list(statuses)
    else:
        raise typer.BadParameter(
            "give job ids, --username, or --all-pending "
            "(see `instascrape status` for what is queued)"
        )

    rows = conn.execute(
        f"SELECT id, kind, status, payload FROM jobs WHERE {where} ORDER BY id", params
    ).fetchall()

    if not rows:
        typer.echo("Nothing matched. `instascrape status` shows what is queued.")
        conn.close()
        return

    typer.echo(f"About to cancel {len(rows)} job(s):")
    for row in rows:
        typer.echo(f"  {row['id']:>4} [{row['kind']}] {row['status']}  {row['payload']}")

    if not yes and not typer.confirm("\nDelete these?", default=False):
        typer.echo("Left alone.")
        conn.close()
        return

    cur = conn.execute(f"DELETE FROM jobs WHERE {where}", params)
    typer.echo(f"Cancelled {cur.rowcount} job(s). Scraped data was not touched.")
    conn.close()


if __name__ == "__main__":
    app()
