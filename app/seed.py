"""Seed the writable data directory with the bundled sample files.

In Docker Compose the data directory is a named volume.  Seeding happens once
(marker file); afterwards deletions performed through the API survive service
restarts.  `docker compose down -v` gets a fresh demo volume back.
"""

from __future__ import annotations

import shutil
from pathlib import Path


MARKER_NAME = ".sample-data-seeded"


def seed_sample_data(sample_dir: Path, data_dir: Path) -> int:
    sample_dir = sample_dir.expanduser().resolve()
    data_dir = data_dir.expanduser().resolve()

    # Local default points DATA_DIR at the sample tree itself; never copy onto
    # itself and never place a marker in the read-only image sample directory.
    if sample_dir == data_dir:
        return 0
    if not sample_dir.is_dir():
        return 0

    marker = data_dir / MARKER_NAME
    if marker.exists():
        return 0

    data_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for source in sorted(sample_dir.rglob("*")):
        if not source.is_file():
            continue
        if source.name == MARKER_NAME:
            continue
        relative = source.relative_to(sample_dir)
        target = data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(source, target)
            copied += 1

    marker.write_text(
        "Sample data was copied into this volume once. "
        "Run `docker compose down -v` to reset the demo.\n",
        encoding="utf-8",
    )
    return copied


def main() -> None:
    from .config import get_settings

    settings = get_settings()
    if not settings.seed_on_start:
        return
    copied = seed_sample_data(settings.sample_data_dir, settings.data_dir)
    print(f"sample data seeded: {copied} file(s)")


if __name__ == "__main__":
    main()
