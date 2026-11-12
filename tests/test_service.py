"""Service-layer tests (stdlib unittest, no FastAPI needed)."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from app import db, service
from app.config import Settings


class CleanupServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.manifest_path = self.root / "manifest.json"
        self.db_path = self.root / "state" / "cleanup.db"
        db.reset_initialization_cache()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def settings(self) -> Settings:
        return Settings(
            manifest_path=self.manifest_path,
            data_dir=self.data_dir,
            db_path=self.db_path,
            sample_data_dir=self.root / "samples",
            seed_on_start=False,
        )

    def write_manifest(self, snapshots: list[dict]) -> None:
        self.manifest_path.write_text(
            json.dumps({"snapshots": snapshots}), encoding="utf-8"
        )

    def touch(self, *paths: str) -> None:
        for relative in paths:
            target = self.data_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"PAR1-demo-PAR1")

    def exists(self, relative: str) -> bool:
        return (self.data_dir / relative).exists()

    def test_retirement_removes_references_but_shared_file_survives(self) -> None:
        self.write_manifest(
            [
                {"snapshot_id": "a", "files": ["a.parquet", "shared.parquet"]},
                {"snapshot_id": "b", "files": ["b.parquet", "shared.parquet"]},
            ]
        )
        self.touch("a.parquet", "b.parquet", "shared.parquet")

        # Nothing retired: nothing may be deleted.
        result = service.run_cleanup(self.settings())
        self.assertEqual(result["deleted"], [])
        self.assertTrue(self.exists("a.parquet"))

        # Retire a: only a.parquet becomes unreferenced; shared is still
        # referenced by retained snapshot b.
        service.retire_snapshot(self.settings(), "a")
        result = service.run_cleanup(self.settings())
        self.assertEqual(result["deleted"], ["a.parquet"])
        self.assertFalse(self.exists("a.parquet"))
        self.assertTrue(self.exists("b.parquet"))
        self.assertTrue(self.exists("shared.parquet"))

        # Retire b as well: all remaining files are reclaimable.  a.parquet
        # was already removed by the previous call and is reported as absent.
        service.retire_snapshot(self.settings(), "b")
        result = service.run_cleanup(self.settings())
        self.assertEqual(sorted(result["deleted"]), ["b.parquet", "shared.parquet"])
        self.assertEqual(result["already_absent"], ["a.parquet"])
        self.assertEqual(result["failed"], [])

    def test_active_export_protects_files_until_release(self) -> None:
        self.write_manifest(
            [
                {"snapshot_id": "a", "files": ["a.parquet", "shared.parquet"]},
                {"snapshot_id": "b", "files": ["b.parquet", "shared.parquet"]},
            ]
        )
        self.touch("a.parquet", "b.parquet", "shared.parquet")

        registration = service.register_export(self.settings(), "a", "export-1")
        service.retire_snapshot(self.settings(), "a")
        service.retire_snapshot(self.settings(), "b")

        # Before the export finishes: b.parquet (b is retired and no export
        # holds it) is free, but everything the active export registered is
        # still protected (including the shared file).
        result = service.run_cleanup(self.settings())
        self.assertEqual(result["deleted"], ["b.parquet"])
        self.assertTrue(self.exists("a.parquet"))
        self.assertTrue(self.exists("shared.parquet"))
        self.assertIn("shared.parquet", result["protected_by_active_exports"])

        # After release the export stops protecting its files.
        service.release_export(self.settings(), registration["registration_id"])
        result = service.run_cleanup(self.settings())
        self.assertEqual(sorted(result["deleted"]), ["a.parquet", "shared.parquet"])
        self.assertEqual(result["already_absent"], ["b.parquet"])
        self.assertEqual(result["failed"], [])

    def test_retired_snapshot_rejects_new_registration(self) -> None:
        self.write_manifest([{"snapshot_id": "a", "files": ["a.parquet"]}])
        self.touch("a.parquet")
        service.retire_snapshot(self.settings(), "a")
        with self.assertRaises(service.SnapshotRetired):
            service.register_export(self.settings(), "a", "late-export")

    def test_preview_is_read_only(self) -> None:
        self.write_manifest([{"snapshot_id": "a", "files": ["a.parquet"]}])
        self.touch("a.parquet")
        service.retire_snapshot(self.settings(), "a")
        preview = service.preview_cleanup(self.settings())
        self.assertEqual(preview["deletable"], ["a.parquet"])
        self.assertTrue(self.exists("a.parquet"))

    def test_deletion_failure_does_not_undo_retirement_and_can_retry(self) -> None:
        self.write_manifest([{"snapshot_id": "a", "files": ["a.parquet"]}])
        # A directory at the file path makes unlink() fail.
        target = self.data_dir / "a.parquet"
        target.mkdir(parents=True)
        service.retire_snapshot(self.settings(), "a")

        result = service.run_cleanup(self.settings())
        self.assertEqual(result["deleted"], [])
        self.assertEqual(result["failed"], ["a.parquet"])
        self.assertEqual(len(result["failures"]), 1)

        # Retirement is still persisted, and the next call (as after a
        # restart) retries the same file.
        snapshots = service.list_snapshots(self.settings())
        self.assertTrue(snapshots[0]["retired"])
        db.reset_initialization_cache()

        target.rmdir()
        target.write_bytes(b"PAR1-PAR1")
        result = service.run_cleanup(self.settings())
        self.assertEqual(result["deleted"], ["a.parquet"])
        self.assertEqual(result["failed"], [])
        self.assertFalse(self.exists("a.parquet"))

    def test_state_persists_across_service_restart(self) -> None:
        self.write_manifest(
            [
                {"snapshot_id": "a", "files": ["a.parquet"]},
                {"snapshot_id": "b", "files": ["b.parquet"]},
            ]
        )
        self.touch("a.parquet", "b.parquet")
        registration = service.register_export(self.settings(), "a", "export-1")
        service.retire_snapshot(self.settings(), "a")
        service.retire_snapshot(self.settings(), "b")

        # Simulate a fresh process: forget SQLite initialization state and
        # build a new Settings object; the DB file is the same.
        db.reset_initialization_cache()
        snapshots = {s["snapshot_id"]: s for s in service.list_snapshots(self.settings())}
        self.assertTrue(snapshots["b"]["retired"])
        self.assertEqual(snapshots["a"]["active_export_count"], 1)

        result = service.run_cleanup(self.settings())
        self.assertEqual(result["deleted"], ["b.parquet"])
        self.assertTrue(self.exists("a.parquet"))

        service.release_export(self.settings(), registration["registration_id"])
        result = service.run_cleanup(self.settings())
        self.assertEqual(result["deleted"], ["a.parquet"])
        self.assertEqual(result["already_absent"], ["b.parquet"])

    def test_concurrent_register_and_retire_are_serialized(self) -> None:
        self.write_manifest([{"snapshot_id": "a", "files": ["a.parquet"]}])

        for iteration in range(12):
            db.reset_initialization_cache()
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                data_dir = root / "data"
                data_dir.mkdir()
                (data_dir / "a.parquet").write_bytes(b"PAR1-PAR1")
                manifest = root / "manifest.json"
                manifest.write_text(
                    json.dumps({"snapshots": [{"snapshot_id": "a", "files": ["a.parquet"]}]}),
                    encoding="utf-8",
                )
                settings = Settings(
                    manifest_path=manifest,
                    data_dir=data_dir,
                    db_path=root / "state.db",
                    sample_data_dir=root,
                    seed_on_start=False,
                )

                barrier = threading.Barrier(2)
                outcome: dict[str, object] = {}

                def do_register() -> None:
                    barrier.wait()
                    try:
                        outcome["registration"] = service.register_export(
                            settings, "a", "export"
                        )
                    except service.SnapshotRetired:
                        outcome["retired_error"] = True

                def do_retire() -> None:
                    barrier.wait()
                    outcome["retire"] = service.retire_snapshot(settings, "a")

                threads = [
                    threading.Thread(target=do_register),
                    threading.Thread(target=do_retire),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

                # Whichever commits first, both operations complete.
                self.assertIn("retire", outcome)
                if "registration" in outcome:
                    # Registration won the race: its file must survive a
                    # cleanup, even though retirement also succeeded.
                    result = service.run_cleanup(settings)
                    self.assertEqual(result["deleted"], [])
                    self.assertTrue((data_dir / "a.parquet").exists())
                    service.release_export(
                        settings, outcome["registration"]["registration_id"]  # type: ignore[index]
                    )
                    result = service.run_cleanup(settings)
                    self.assertEqual(result["deleted"], ["a.parquet"])
                else:
                    # Retirement won: the registration was cleanly rejected.
                    self.assertTrue(outcome.get("retired_error"))
                    result = service.run_cleanup(settings)
                    self.assertEqual(result["deleted"], ["a.parquet"])

    def test_cleanup_is_idempotent(self) -> None:
        self.write_manifest([{"snapshot_id": "a", "files": ["a.parquet"]}])
        self.touch("a.parquet")
        service.retire_snapshot(self.settings(), "a")
        first = service.run_cleanup(self.settings())
        self.assertEqual(first["deleted"], ["a.parquet"])
        second = service.run_cleanup(self.settings())
        self.assertEqual(second["deleted"], [])
        self.assertEqual(second["already_absent"], ["a.parquet"])
        self.assertEqual(second["failed"], [])


class ManifestTests(unittest.TestCase):
    def test_flexible_manifest_shapes_merge_shared_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "snapshots": {
                            "snap-a": ["x.parquet", "shared.parquet"],
                            "snap-b": {"paths": [{"relative_path": "y.parquet"}, "shared.parquet"]},
                        }
                    }
                ),
                encoding="utf-8",
            )
            from app.manifest import load_manifest

            manifest = load_manifest(path)
            self.assertEqual(manifest.snapshots["snap-a"].files, ("x.parquet", "shared.parquet"))
            self.assertEqual(manifest.snapshots["snap-b"].files, ("y.parquet", "shared.parquet"))
            self.assertEqual(
                manifest.all_files, ("x.parquet", "shared.parquet", "y.parquet")
            )

    def test_absolute_and_parent_paths_are_rejected(self) -> None:
        from app.manifest import ManifestError, normalize_rel_path

        for bad in ("/etc/passwd", "../outside.parquet", "a/../../b.parquet"):
            with self.assertRaises(ManifestError):
                normalize_rel_path(bad)


if __name__ == "__main__":
    unittest.main()
