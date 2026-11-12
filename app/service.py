"""Domain logic: retirement, export registration, and reference-safe cleanup.

Safety rules implemented here:

* A data file may only be deleted when it is referenced by no retained
  (non-retired) snapshot and by no unfinished export registration.
* References are followed through symlinks.  After the lakehouse storage
  migration, retained snapshots and unfinished exports may still read their
  Parquet files through *old* paths — directory symlinks or multi-level
  file symlink chains.  A manifest item is protected whenever deleting it
  would remove the final target — or any intermediate link — of such a
  read path.  Aliases that no read path uses stay reclaimable, even when
  they point at a protected file: unlinking a symlink never touches its
  target.
* Only symlink chains that stay inside the data directory are followed,
  and each deletion re-checks that the chain did not change during the
  cleanup run; anything unresolvable is left untouched and reported.
* Retirement only stops *future* registrations.  Registrations that already
  committed keep protecting their snapshot's files until they are released,
  even if retirement happens concurrently.
* Cleanup computes the deletable set and performs unlinks while holding
  SQLite's write lock, so a registration cannot commit in between the
  decision and the deletion.
"""

from __future__ import annotations

import json
import os
import posixpath
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .config import Settings
from .db import begin_immediate, connect, ensure_db
from .manifest import Manifest, load_manifest


class SnapshotNotFound(Exception):
    pass


class SnapshotRetired(Exception):
    pass


class RegistrationNotFound(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _manifest(settings: Settings) -> Manifest:
    return load_manifest(settings.manifest_path)


def _retired_map(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute(
        "SELECT snapshot_id, retired_at FROM snapshot_retirements"
    ).fetchall()
    return {row["snapshot_id"]: row["retired_at"] for row in rows}


def _active_exports(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT registration_id, snapshot_id, export_id, files_json, registered_at
          FROM export_registrations
         WHERE released_at IS NULL
         ORDER BY registered_at, registration_id
        """
    ).fetchall()
    exports: list[dict[str, Any]] = []
    for row in rows:
        try:
            files = json.loads(row["files_json"])
            if not isinstance(files, list):
                files = []
        except (TypeError, ValueError):
            files = []
        exports.append(
            {
                "registration_id": row["registration_id"],
                "snapshot_id": row["snapshot_id"],
                "export_id": row["export_id"],
                "files": files,
                "registered_at": row["registered_at"],
            }
        )
    return exports


def list_snapshots(settings: Settings) -> list[dict[str, Any]]:
    manifest = _manifest(settings)
    ensure_db(settings.db_path)
    with connect(settings.db_path) as connection:
        retired = _retired_map(connection)
        active_counts = {
            row["snapshot_id"]: row["count"]
            for row in connection.execute(
                """
                SELECT snapshot_id, COUNT(*) AS count
                  FROM export_registrations
                 WHERE released_at IS NULL
                 GROUP BY snapshot_id
                """
            ).fetchall()
        }

    result = []
    for snapshot_id, snapshot in manifest.snapshots.items():
        result.append(
            {
                "snapshot_id": snapshot_id,
                "files": list(snapshot.files),
                "file_count": len(snapshot.files),
                "retired": snapshot_id in retired,
                "retired_at": retired.get(snapshot_id),
                "active_export_count": active_counts.get(snapshot_id, 0),
            }
        )
    return result


def get_snapshot(settings: Settings, snapshot_id: str) -> dict[str, Any]:
    for snapshot in list_snapshots(settings):
        if snapshot["snapshot_id"] == snapshot_id:
            return snapshot
    raise SnapshotNotFound(snapshot_id)


def retire_snapshot(settings: Settings, snapshot_id: str) -> dict[str, Any]:
    manifest = _manifest(settings)
    if snapshot_id not in manifest.snapshots:
        raise SnapshotNotFound(snapshot_id)

    ensure_db(settings.db_path)
    with connect(settings.db_path) as connection:
        begin_immediate(connection)
        try:
            row = connection.execute(
                "SELECT retired_at FROM snapshot_retirements WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is not None:
                connection.commit()
                return {
                    "snapshot_id": snapshot_id,
                    "retired": True,
                    "already_retired": True,
                    "retired_at": row["retired_at"],
                }

            retired_at = _now()
            connection.execute(
                "INSERT INTO snapshot_retirements (snapshot_id, retired_at) VALUES (?, ?)",
                (snapshot_id, retired_at),
            )
            connection.commit()
            return {
                "snapshot_id": snapshot_id,
                "retired": True,
                "already_retired": False,
                "retired_at": retired_at,
            }
        except Exception:
            connection.rollback()
            raise


def register_export(
    settings: Settings, snapshot_id: str, export_id: str | None = None
) -> dict[str, Any]:
    manifest = _manifest(settings)
    snapshot = manifest.snapshots.get(snapshot_id)
    if snapshot is None:
        raise SnapshotNotFound(snapshot_id)

    # Capture the file list at registration time.  From this moment the
    # registration itself is sufficient proof that these files must be
    # retained, independent of later manifest edits.
    files = list(snapshot.files)
    ensure_db(settings.db_path)

    with connect(settings.db_path) as connection:
        begin_immediate(connection)
        try:
            retired = connection.execute(
                "SELECT 1 FROM snapshot_retirements WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if retired is not None:
                connection.rollback()
                raise SnapshotRetired(snapshot_id)

            registration_id = str(uuid.uuid4())
            registered_at = _now()
            connection.execute(
                """
                INSERT INTO export_registrations
                    (registration_id, snapshot_id, export_id, files_json, registered_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    registration_id,
                    snapshot_id,
                    export_id,
                    json.dumps(files, ensure_ascii=False),
                    registered_at,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {
        "registration_id": registration_id,
        "snapshot_id": snapshot_id,
        "export_id": export_id,
        "files": files,
        "registered_at": registered_at,
        "released": False,
    }


def release_export(settings: Settings, registration_id: str) -> dict[str, Any]:
    ensure_db(settings.db_path)
    with connect(settings.db_path) as connection:
        begin_immediate(connection)
        try:
            row = connection.execute(
                """
                SELECT registration_id, snapshot_id, export_id, files_json,
                       registered_at, released_at
                  FROM export_registrations
                 WHERE registration_id = ?
                """,
                (registration_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RegistrationNotFound(registration_id)

            if row["released_at"] is not None:
                connection.commit()
                return {
                    "registration_id": row["registration_id"],
                    "snapshot_id": row["snapshot_id"],
                    "export_id": row["export_id"],
                    "files": json.loads(row["files_json"]),
                    "registered_at": row["registered_at"],
                    "released_at": row["released_at"],
                    "released": True,
                    "already_released": True,
                }

            released_at = _now()
            connection.execute(
                "UPDATE export_registrations SET released_at = ? WHERE registration_id = ?",
                (released_at, registration_id),
            )
            connection.commit()
            return {
                "registration_id": row["registration_id"],
                "snapshot_id": row["snapshot_id"],
                "export_id": row["export_id"],
                "files": json.loads(row["files_json"]),
                "registered_at": row["registered_at"],
                "released_at": released_at,
                "released": True,
                "already_released": False,
            }
        except Exception:
            connection.rollback()
            raise


def list_exports(settings: Settings, active_only: bool = True) -> list[dict[str, Any]]:
    ensure_db(settings.db_path)
    query = """
        SELECT registration_id, snapshot_id, export_id, files_json,
               registered_at, released_at
          FROM export_registrations
    """
    if active_only:
        query += " WHERE released_at IS NULL"
    query += " ORDER BY registered_at, registration_id"

    with connect(settings.db_path) as connection:
        rows = connection.execute(query).fetchall()

    exports = []
    for row in rows:
        try:
            files = json.loads(row["files_json"])
            if not isinstance(files, list):
                files = []
        except (TypeError, ValueError):
            files = []
        exports.append(
            {
                "registration_id": row["registration_id"],
                "snapshot_id": row["snapshot_id"],
                "export_id": row["export_id"],
                "files": files,
                "registered_at": row["registered_at"],
                "released_at": row["released_at"],
                "active": row["released_at"] is None,
            }
        )
    return exports


def _normalize_relative_parts(parts: Iterable[str]) -> list[str] | None:
    """Lexically resolve "." and ".." against the data-dir root.

    Returns None when ".." would climb above the root.
    """

    normalized: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not normalized:
                return None
            normalized.pop()
        else:
            normalized.append(part)
    return normalized


def _walk_symlinks(
    root: Path, parts: tuple[str, ...], follow_final: bool
) -> tuple[list[str], str] | None:
    """Resolve ``parts`` below ``root``, expanding symlinks that stay inside.

    Returns ``(links, final)``: the absolute path of every symlink in the
    chain (in resolution order) and the absolute path the chain ends at.
    The last component is expanded only when ``follow_final`` is true.
    Returns None when the chain loops, leaves the data directory, or cannot
    be read — cleanup must not reason about such chains.
    """

    root_str = str(root)
    links: list[str] = []
    seen: set[str] = set()
    current = _normalize_relative_parts(parts)
    if current is None:
        return None
    while current:
        restarted = False
        prefix: list[str] = []
        for index, part in enumerate(current):
            prefix.append(part)
            is_final = index == len(current) - 1
            if is_final and not follow_final:
                break
            candidate = root.joinpath(*prefix)
            if not candidate.is_symlink():
                continue
            link = str(candidate)
            if link in seen:
                return None  # symlink loop
            seen.add(link)
            links.append(link)
            try:
                raw_target = os.readlink(candidate)
            except OSError:
                return None
            remainder = current[index + 1 :]
            if os.path.isabs(raw_target):
                absolute = posixpath.normpath(raw_target)
                if absolute != root_str and not absolute.startswith(
                    root_str + os.sep
                ):
                    return None  # escapes the data directory
                relative = absolute[len(root_str) :].strip(os.sep)
                head = [piece for piece in relative.split(os.sep) if piece]
                current = head + remainder
            else:
                merged = (
                    prefix[:-1] + list(PurePosixPath(raw_target).parts) + remainder
                )
                normalized = _normalize_relative_parts(merged)
                if normalized is None:
                    return None  # ".." climbs above the data directory
                current = normalized
            restarted = True
            break
        if not restarted:
            return links, str(root.joinpath(*current))
    return links, root_str


def _read_dependencies(root: Path, relative_path: str) -> set[str]:
    """Absolute paths that must keep existing so ``relative_path`` stays
    readable: every symlink in its resolution chain plus the final target.

    Only chains that stay inside the data directory yield dependencies;
    anything else (escaping chains, loops, unreadable links) is out of
    scope for cleanup and contributes none.
    """

    result = _walk_symlinks(
        root, PurePosixPath(relative_path).parts, follow_final=True
    )
    if result is None:
        return set()
    links, final = result
    return {*links, final}


def _unlink_effect(root: Path, relative_path: str) -> str | None:
    """Absolute path of the directory entry that unlink() would remove.

    Parent directories are resolved through in-data-dir symlinks; the final
    component itself is never followed (unlinking a symlink removes the
    link, not its target).  Returns None when the parent chain cannot be
    resolved safely inside the data directory.
    """

    result = _walk_symlinks(
        root, PurePosixPath(relative_path).parts, follow_final=False
    )
    if result is None:
        return None
    _, final = result
    return final


def _collect_protected_effects(
    root: Path, read_paths: Iterable[str]
) -> set[str]:
    """Union of every read path's dependencies, as absolute paths."""

    protected: set[str] = set()
    for read_path in read_paths:
        protected.update(_read_dependencies(root, read_path))
    return protected


def _file_present(data_dir: Path, relative_path: str) -> bool:
    try:
        target = _resolve_data_path(data_dir, relative_path)
    except ValueError:
        # Unsafe symlink target: let the cleanup attempt report the failure.
        return True
    return os.path.lexists(target)


def _build_plan(
    connection: sqlite3.Connection, manifest: Manifest, data_dir: Path
) -> tuple[dict[str, Any], Path, tuple[str, ...], dict[str, str | None]]:
    retired = _retired_map(connection)
    active_exports = _active_exports(connection)

    retained_files: set[str] = set()
    for snapshot_id, snapshot in manifest.snapshots.items():
        if snapshot_id not in retired:
            retained_files.update(snapshot.files)

    export_files: set[str] = set()
    for export in active_exports:
        export_files.update(export["files"])

    # Paths that retained snapshots and unfinished exports can still read.
    # Deleting any directory entry their resolution chains depend on — the
    # final data file or an intermediate symlink — would break those reads.
    read_paths = retained_files | export_files
    root = data_dir.expanduser().resolve()
    protected_effects = _collect_protected_effects(root, read_paths)

    protected_by_retained = [
        file_path for file_path in manifest.all_files if file_path in retained_files
    ]
    protected_by_exports = [
        file_path for file_path in manifest.all_files if file_path in export_files
    ]

    # A manifest item whose unlink would remove an entry some read path
    # resolves through is protected as well, even though no snapshot or
    # export references the item's own path.  Aliases no read path uses
    # stay reclaimable, even when they point at a protected file.
    unlink_effects: dict[str, str | None] = {}
    protected_by_aliases: list[str] = []
    for file_path in manifest.all_files:
        effect = _unlink_effect(root, file_path)
        unlink_effects[file_path] = effect
        if file_path in retained_files or file_path in export_files:
            continue
        if effect is not None and effect in protected_effects:
            protected_by_aliases.append(file_path)

    protected_set = (
        set(protected_by_retained)
        | set(protected_by_exports)
        | set(protected_by_aliases)
    )
    protected = [
        file_path for file_path in manifest.all_files if file_path in protected_set
    ]
    eligible = [
        file_path for file_path in manifest.all_files if file_path not in protected_set
    ]
    deletable = [
        file_path for file_path in eligible if _file_present(data_dir, file_path)
    ]
    already_absent = [
        file_path for file_path in eligible if not _file_present(data_dir, file_path)
    ]

    plan = {
        "deletable": deletable,
        "already_absent": already_absent,
        "protected": protected,
        "protected_by_retained_snapshots": protected_by_retained,
        "protected_by_active_exports": protected_by_exports,
        "protected_by_symlink_aliases": protected_by_aliases,
        "active_exports": active_exports,
    }
    item_effects = {file_path: unlink_effects[file_path] for file_path in deletable}
    return plan, root, tuple(sorted(read_paths)), item_effects


def preview_cleanup(settings: Settings) -> dict[str, Any]:
    manifest = _manifest(settings)
    ensure_db(settings.db_path)
    with connect(settings.db_path) as connection:
        # A read transaction gives one consistent view of retirements +
        # registrations for the preview.
        connection.execute("BEGIN")
        try:
            plan, _, _, _ = _build_plan(connection, manifest, settings.data_dir)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return plan


def _resolve_data_path(data_dir: Path, relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"unsafe relative path: {relative_path!r}")

    root = data_dir.expanduser().resolve()
    target = root.joinpath(*pure.parts)
    try:
        resolved_target = target.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve path {relative_path!r}: {exc}") from exc
    if resolved_target != root and root not in resolved_target.parents:
        raise ValueError(f"path escapes the data directory: {relative_path!r}")
    return target


def _ensure_still_reclaimable(
    root: Path,
    relative_path: str,
    planned_effect: str | None,
    protected_effects: set[str],
) -> None:
    """Re-check, just before unlinking, that the symlink chain did not
    change while the cleanup was running.

    Only chains that stay unchanged for the whole maintenance window are
    acted on: if the item's own resolution moved, or it now lands on an
    entry some read path depends on, the item is skipped (the next cleanup
    re-plans it).
    """

    effect = _unlink_effect(root, relative_path)
    if effect is None:
        raise ValueError(
            f"cannot resolve {relative_path!r} inside the data directory; skipped"
        )
    if effect != planned_effect:
        raise ValueError(
            f"symlink chain of {relative_path!r} changed during cleanup; skipped"
        )
    if effect in protected_effects:
        raise ValueError(
            f"{relative_path!r} became protected during cleanup; skipped"
        )


def _delete_file(data_dir: Path, relative_path: str) -> str:
    """Delete one file.  Returns 'deleted' or 'absent'.

    Absent files are treated as successfully reclaimed so a retried cleanup
    after a service restart is idempotent.
    """

    target = _resolve_data_path(data_dir, relative_path)
    # lexists() is true for a broken symlink as well; unlink() removes the
    # link itself, never the link target.
    if not os.path.lexists(target):
        return "absent"
    target.unlink()
    return "deleted"


def run_cleanup(settings: Settings, dry_run: bool = False) -> dict[str, Any]:
    manifest = _manifest(settings)
    ensure_db(settings.db_path)

    with connect(settings.db_path) as connection:
        # Hold the write lock across planning + unlink.  A registration that
        # arrives meanwhile waits and then sees either protected files (still
        # present) or a retired snapshot (and is rejected).
        begin_immediate(connection)
        try:
            plan, root, read_paths, item_effects = _build_plan(
                connection, manifest, settings.data_dir
            )
            if dry_run:
                connection.commit()
                return {
                    "dry_run": True,
                    "deleted": [],
                    "already_absent": plan["already_absent"],
                    "failed": [],
                    "failures": [],
                    "deletable": plan["deletable"],
                    "protected": plan["protected"],
                    "protected_by_retained_snapshots": plan[
                        "protected_by_retained_snapshots"
                    ],
                    "protected_by_active_exports": plan[
                        "protected_by_active_exports"
                    ],
                    "protected_by_symlink_aliases": plan[
                        "protected_by_symlink_aliases"
                    ],
                    "active_exports": plan["active_exports"],
                }

            deleted: list[str] = []
            already_absent: list[str] = list(plan["already_absent"])
            failed: list[str] = []
            failures: list[dict[str, str]] = []

            # Re-resolve the read paths just before unlinking: symlinks are
            # only honored while they stay unchanged for the whole
            # maintenance window, and every candidate is re-checked against
            # this fresh set before its unlink.
            protected_effects = _collect_protected_effects(root, read_paths)

            for relative_path in plan["deletable"]:
                try:
                    _ensure_still_reclaimable(
                        root,
                        relative_path,
                        item_effects.get(relative_path),
                        protected_effects,
                    )
                    status = _delete_file(settings.data_dir, relative_path)
                except OSError as exc:
                    failed.append(relative_path)
                    failures.append(
                        {"path": relative_path, "error": str(exc)}
                    )
                except ValueError as exc:
                    failed.append(relative_path)
                    failures.append(
                        {"path": relative_path, "error": str(exc)}
                    )
                else:
                    if status == "deleted":
                        deleted.append(relative_path)
                    else:
                        already_absent.append(relative_path)

            # Cleanup never changes retirement or registration state, so a
            # partial failure simply leaves the remaining files for the next
            # cleanup call (including after a restart).
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {
        "dry_run": False,
        "deleted": deleted,
        "already_absent": already_absent,
        "failed": failed,
        "failures": failures,
        "deletable": plan["deletable"],
        "protected": plan["protected"],
        "protected_by_retained_snapshots": plan[
            "protected_by_retained_snapshots"
        ],
        "protected_by_active_exports": plan["protected_by_active_exports"],
        "protected_by_symlink_aliases": plan["protected_by_symlink_aliases"],
        "active_exports": plan["active_exports"],
    }
