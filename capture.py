"""Network payload capture.

Rather than hardcoding Instagram's internal endpoint URLs - which change often
and without notice - we let the page make its own requests and archive every
JSON response that looks relevant. Nothing is parsed at capture time.

The payoff: when parsing breaks six months from now, the raw payloads are still
on disk and re-parsing costs nothing. No re-scraping, no extra requests against
the account.

Response bodies are read in `flush()` rather than inside the event callback, to
avoid re-entrancy problems with Playwright's sync API.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import Page, Response

# Anything whose URL contains one of these is worth keeping.
URL_MARKERS = (
    "/graphql/query",
    "/api/v1/",
    "PolarisPost",
    "PolarisProfile",
    "web_profile_info",
)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(url: str, limit: int = 60) -> str:
    path = url.split("?", 1)[0]
    return _SAFE.sub("-", path).strip("-")[-limit:] or "payload"


@dataclass
class CapturedPayload:
    url: str
    data: Any
    path: Optional[Path] = None


class PayloadCapture:
    """Attaches to a Page and buffers interesting JSON responses."""

    def __init__(self, page: Page, raw_dir: Path, label: str) -> None:
        self.page = page
        self.raw_dir = raw_dir
        self.label = label
        self._pending: list[Response] = []
        self.payloads: list[CapturedPayload] = []
        page.on("response", self._on_response)

    def _on_response(self, response: Response) -> None:
        url = response.url or ""
        if any(marker in url for marker in URL_MARKERS):
            self._pending.append(response)

    def flush(self, persist: bool = True) -> list[CapturedPayload]:
        """Read buffered bodies and, optionally, write them to disk.

        Must be called before navigating away - Playwright discards bodies once
        the page moves on. Failures are swallowed on purpose: a body we cannot
        read is not a reason to fail the job.
        """
        out_dir = self.raw_dir / self.label
        pending, self._pending = self._pending, []

        for index, response in enumerate(pending):
            try:
                if response.status >= 400:
                    continue
                data = response.json()
            except Exception:
                continue  # not JSON, redirect, or body already gone

            payload = CapturedPayload(url=response.url, data=data)

            if persist:
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                    dest = out_dir / f"{len(self.payloads):03d}-{_slug(response.url)}.json"
                    dest.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    payload.path = dest
                except Exception:
                    pass  # archiving is best-effort

            self.payloads.append(payload)

        return self.payloads

    def detach(self) -> None:
        try:
            self.page.remove_listener("response", self._on_response)
        except Exception:
            pass


def load_archived(raw_dir: Path, label: str) -> list[CapturedPayload]:
    """Re-read payloads archived by an earlier run, for offline re-parsing."""
    directory = raw_dir / label
    if not directory.is_dir():
        return []
    out: list[CapturedPayload] = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(
                CapturedPayload(
                    url=path.name,
                    data=json.loads(path.read_text(encoding="utf-8")),
                    path=path,
                )
            )
        except Exception:
            continue
    return out
