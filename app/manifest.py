"""Reading the local snapshot/file manifest.

The manifest is the source of truth for *which files exist* and *which
snapshots reference them*.  Several snapshots may reference the same file,
which is why reference counting is done over sets of paths.

Example (the parser is deliberately a little tolerant so existing exports can
point at their current manifest shape)::

    {
      "snapshots": [
        {"snapshot_id": "snap-a", "files": ["data/a.parquet"]},
        {"snapshot_id": "snap-b", "files": ["data/b.parquet"]}
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


class ManifestError(Exception):
    """The manifest is missing, malformed, or contains an unsafe path."""


@dataclass(frozen=True)
class SnapshotDef:
    snapshot_id: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    snapshots: dict[str, SnapshotDef]
    # Every file referenced by at least one snapshot, in manifest order.
    all_files: tuple[str, ...]


_ID_KEYS = ("snapshot_id", "id", "name", "snapshot")
_FILE_LIST_KEYS = (
    "files",
    "paths",
    "data_files",
    "file_paths",
    "relative_paths",
    "data_file_paths",
)
_FILE_ITEM_KEYS = ("path", "relative_path", "file", "file_path", "name")


def normalize_rel_path(raw: str) -> str:
    """Normalize a manifest path and reject anything that escapes the root."""

    if not isinstance(raw, str) or not raw.strip():
        raise ManifestError(f"invalid data file path: {raw!r}")
    candidate = raw.strip().replace("\\", "/")
    # PurePosixPath collapses duplicate slashes and "." components.
    pure = PurePosixPath(candidate)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ManifestError(f"data file path must be relative and stay inside the data dir: {raw!r}")
    return pure.as_posix()


def _snapshot_id(entry: dict[str, Any], index: int) -> str:
    for key in _ID_KEYS:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ManifestError(f"snapshot #{index} has no snapshot_id/id/name field")


def _file_entries(entry: dict[str, Any], index: int) -> Iterable[Any]:
    for key in _FILE_LIST_KEYS:
        if key in entry:
            value = entry[key]
            break
    else:
        return []

    if isinstance(value, dict):
        # A mapping is accepted as a convenience: {"path.parquet": {...}}.
        return value.keys()
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return value
    raise ManifestError(f"snapshot #{index} has a non-list files field")


def _file_path(item: Any, snapshot_id: str) -> str:
    if isinstance(item, str):
        return normalize_rel_path(item)
    if isinstance(item, dict):
        for key in _FILE_ITEM_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return normalize_rel_path(value)
    raise ManifestError(f"snapshot {snapshot_id!r} contains an invalid file entry: {item!r}")


def _top_level_entries(document: Any) -> list[Any]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        snapshots = document.get("snapshots", document.get("snapshot", None))
        if isinstance(snapshots, list):
            return snapshots
        if isinstance(snapshots, dict):
            entries: list[Any] = []
            for snapshot_id, value in snapshots.items():
                if isinstance(value, list):
                    entries.append({"snapshot_id": snapshot_id, "files": value})
                elif isinstance(value, dict):
                    merged = dict(value)
                    merged.setdefault("snapshot_id", snapshot_id)
                    entries.append(merged)
                elif isinstance(value, str):
                    entries.append({"snapshot_id": snapshot_id, "files": [value]})
                else:
                    raise ManifestError(
                        f"snapshot {snapshot_id!r} has an invalid value"
                    )
            return entries
        if snapshots is None and all(isinstance(v, list) for v in document.values()):
            return [
                {"snapshot_id": key, "files": value}
                for key, value in document.items()
            ]
        if snapshots is not None:
            raise ManifestError("'snapshots' must be a list or a mapping")
    raise ManifestError("manifest must contain a 'snapshots' list/mapping")


def load_manifest(path: Path) -> Manifest:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {path} is not valid JSON: {exc}") from exc

    entries = _top_level_entries(document)
    snapshots: dict[str, SnapshotDef] = {}
    ordered_files: list[str] = []
    seen_files: set[str] = set()

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ManifestError(f"snapshot #{index} must be an object")
        snapshot_id = _snapshot_id(entry, index)

        files: list[str] = []
        seen_in_snapshot: set[str] = set()
        for item in _file_entries(entry, index):
            file_path = _file_path(item, snapshot_id)
            if file_path in seen_in_snapshot:
                continue
            seen_in_snapshot.add(file_path)
            files.append(file_path)
            if file_path not in seen_files:
                seen_files.add(file_path)
                ordered_files.append(file_path)

        if snapshot_id in snapshots:
            # Duplicate snapshot declaration: merge, keep first-seen order.
            existing = list(snapshots[snapshot_id].files)
            for file_path in files:
                if file_path not in existing:
                    existing.append(file_path)
            files = existing

        snapshots[snapshot_id] = SnapshotDef(
            snapshot_id=snapshot_id, files=tuple(files)
        )

    return Manifest(snapshots=snapshots, all_files=tuple(ordered_files))
