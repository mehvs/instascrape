"""Runtime configuration.

Every value can be overridden with an INSTASCRAPE_* environment variable or a
.env file sitting next to the project. Defaults are deliberately conservative:
this tool is slow on purpose.
"""

from __future__ import annotations

import socket
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INSTASCRAPE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Storage -------------------------------------------------------------
    data_dir: Path = Field(default=Path("./data"))

    # --- Browser -------------------------------------------------------------
    headless: bool = True
    locale: str = "en-US"
    timezone: str = "Europe/Berlin"
    viewport_width: int = 1440
    viewport_height: int = 900
    nav_timeout_ms: int = 45_000

    # --- Politeness ----------------------------------------------------------
    min_delay: float = 20.0
    max_delay: float = 45.0
    daily_cap: int = 150

    # --- Scope ---------------------------------------------------------------
    max_comments: int = 100
    fetch_replies: bool = False
    # Safety net on the profile grid scroll. The real terminator is the stall
    # check (three scrolls with no new posts); this just stops a runaway loop.
    # ~12 posts arrive per scroll, so 200 covers roughly 2,400 posts.
    max_scrolls: int = 200

    # --- Worker --------------------------------------------------------------
    poll_interval: float = 10.0
    lease_seconds: int = 900
    max_attempts: int = 5
    worker_id: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")

    # --- Derived paths -------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "instascrape.db"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def storage_state_path(self) -> Path:
        return self.state_dir / "storage_state.json"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.state_dir, self.raw_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
