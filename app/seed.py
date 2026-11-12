"""Seed the writable data directory with the bundled sample files.

In Docker Compose the data directory is a named volume.  Seeding happens once
(marker file); afterwards deletions performed through the API survive service
restarts.  `docker compose down -v` gets a fresh demo volume back.

The sample tree contains symlinks (legacy read paths left over from the
storage migration).  They are recreated as symlinks — copying their content
instead would turn the alias demo into real duplicate files.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


MARKER_NAME = ".sample-data-seeded"


def _seed_symlink(source: Path, target: Path) -> bool:
    """Recreate ``source``'s symlink at ``target``; False if already there."""

    if os.path.lexists(target):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(os.readlink(source), target)
    return True


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
    # followlinks=False: symlinked directories are reported in dirnames but
    # not descended into, so every symlink is recreated exactly once.
    for dirpath, dirnames, filenames in os.walk(sample_dir, followlinks=False):
        target_dir = data_dir / Path(dirpath).relative_to(sample_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in list(dirnames):
            source = Path(dirpath) / name
            if source.is_symlink():
                dirnames.remove(name)
                if _seed_symlink(source, target_dir / name):
                    copied += 1
        for name in filenames:
            if name == MARKER_NAME:
                continue
            source = Path(dirpath) / name
            target = target_dir / name
            if source.is_symlink():
                if _seed_symlink(source, target):
                    copied += 1
            elif not os.path.lexists(target):
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
