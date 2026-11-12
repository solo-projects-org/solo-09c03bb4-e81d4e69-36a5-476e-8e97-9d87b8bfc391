"""Application configuration.

All paths can be overridden with environment variables so the same image can
run locally, in Docker Compose, or in tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return Path(raw).expanduser()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    manifest_path: Path
    data_dir: Path
    db_path: Path
    sample_data_dir: Path
    seed_on_start: bool


def get_settings() -> Settings:
    """Read settings from the environment.

    This is intentionally not cached: tests (and operators) can point the
    service at another manifest/database without re-importing the module.
    """

    return Settings(
        manifest_path=_env_path("MANIFEST_PATH", BASE_DIR / "manifest.json"),
        # Local default: use the bundled sample files directly, so
        # `uvicorn app.main:app` works without seeding.  Docker sets
        # DATA_DIR=/app/data and seeds it from the read-only sample tree.
        data_dir=_env_path("DATA_DIR", BASE_DIR / "sample_data"),
        db_path=_env_path(
            "CLEANUP_DB_PATH", BASE_DIR / "local_state" / "cleanup.db"
        ),
        sample_data_dir=_env_path("SAMPLE_DATA_DIR", BASE_DIR / "sample_data"),
        seed_on_start=_env_bool("SEED_SAMPLE_DATA_ON_START", False),
    )
