"""Domain logic: retirement, export registration, and reference-safe cleanup.

Safety rules implemented here:

* A data file may only be deleted when it is referenced by no retained
  (non-retired) snapshot and by no unfinished export registration.
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
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

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


def _file_present(data_dir: Path, relative_path: str) -> bool:
    try:
        target = _resolve_data_path(data_dir, relative_path)
    except ValueError:
        # Unsafe symlink target: let the cleanup attempt report the failure.
        return True
    return os.path.lexists(target)


def _build_plan(
    connection: sqlite3.Connection, manifest: Manifest, data_dir: Path
) -> dict[str, Any]:
    retired = _retired_map(connection)
    active_exports = _active_exports(connection)

    retained_files: set[str] = set()
    for snapshot_id, snapshot in manifest.snapshots.items():
        if snapshot_id not in retired:
            retained_files.update(snapshot.files)

    export_files: set[str] = set()
    for export in active_exports:
        export_files.update(export["files"])

    eligible = [
        file_path
        for file_path in manifest.all_files
        if file_path not in retained_files and file_path not in export_files
    ]
    deletable = [
        file_path for file_path in eligible if _file_present(data_dir, file_path)
    ]
    already_absent = [
        file_path for file_path in eligible if not _file_present(data_dir, file_path)
    ]
    protected_by_retained = [
        file_path for file_path in manifest.all_files if file_path in retained_files
    ]
    protected_by_exports = [
        file_path for file_path in manifest.all_files if file_path in export_files
    ]
    protected = [
        file_path
        for file_path in manifest.all_files
        if file_path in retained_files or file_path in export_files
    ]

    return {
        "deletable": deletable,
        "already_absent": already_absent,
        "protected": protected,
        "protected_by_retained_snapshots": protected_by_retained,
        "protected_by_active_exports": protected_by_exports,
        "active_exports": active_exports,
    }


def preview_cleanup(settings: Settings) -> dict[str, Any]:
    manifest = _manifest(settings)
    ensure_db(settings.db_path)
    with connect(settings.db_path) as connection:
        # A read transaction gives one consistent view of retirements +
        # registrations for the preview.
        connection.execute("BEGIN")
        try:
            plan = _build_plan(connection, manifest, settings.data_dir)
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
            plan = _build_plan(connection, manifest, settings.data_dir)
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
                    "active_exports": plan["active_exports"],
                }

            deleted: list[str] = []
            already_absent: list[str] = list(plan["already_absent"])
            failed: list[str] = []
            failures: list[dict[str, str]] = []

            for relative_path in plan["deletable"]:
                try:
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
        "active_exports": plan["active_exports"],
    }
