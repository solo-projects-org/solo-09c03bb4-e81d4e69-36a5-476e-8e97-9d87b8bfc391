"""Service-layer tests (stdlib unittest, no FastAPI needed)."""

from __future__ import annotations

import json
import os
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


class SymlinkCleanupTests(unittest.TestCase):
    """Cleanup must follow read paths through symlinks inside the data dir."""

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

    def symlink(self, target: str, link: str) -> None:
        link_path = self.data_dir / link
        link_path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, link_path)

    def exists(self, relative: str) -> bool:
        return (self.data_dir / relative).exists()

    def lexists(self, relative: str) -> bool:
        return os.path.lexists(self.data_dir / relative)

    def test_directory_symlink_read_path_protects_real_file(self) -> None:
        self.write_manifest(
            [
                {"snapshot_id": "new", "files": ["hydro/a.parquet"]},
                {"snapshot_id": "legacy", "files": ["legacy/hydro/a.parquet"]},
            ]
        )
        self.touch("hydro/a.parquet")
        self.symlink("../hydro", "legacy/hydro")

        # The retained snapshot reads through the directory symlink: the
        # retired snapshot's manifest item resolves to the same real file
        # and must be protected even though its path is unreferenced.
        service.retire_snapshot(self.settings(), "new")
        preview = service.preview_cleanup(self.settings())
        self.assertEqual(preview["deletable"], [])
        self.assertEqual(
            preview["protected_by_symlink_aliases"], ["hydro/a.parquet"]
        )
        result = service.run_cleanup(self.settings())
        self.assertEqual(result["deleted"], [])
        self.assertTrue(self.exists("hydro/a.parquet"))

        # Once the legacy reader is gone too, the file is reclaimed.
        service.retire_snapshot(self.settings(), "legacy")
        result = service.run_cleanup(self.settings())
        self.assertEqual(result["deleted"], ["hydro/a.parquet"])
        self.assertFalse(self.exists("hydro/a.parquet"))

    def test_multi_level_chain_protects_intermediate_links(self) -> None:
        self.write_manifest(
            [
                {"snapshot_id": "reader", "files": ["chain/l1.parquet"]},
                {"snapshot_id": "old", "files": ["chain/l2.parquet", "real.parquet"]},
            ]
        )
        self.touch("real.parquet")
        self.symlink("../real.parquet", "chain/l2.parquet")
        self.symlink("l2.parquet", "chain/l1.parquet")

        # The retained snapshot reads l1 -> l2 -> real.parquet: both the
        # intermediate link and the final target are protected.
        service.retire_snapshot(self.settings(), "old")
        preview = service.preview_cleanup(self.settings())
        self.assertEqual(preview["deletable"], [])
        self.assertEqual(
            sorted(preview["protected_by_symlink_aliases"]),
            ["chain/l2.parquet", "real.parquet"],
        )
        result = service.run_cleanup(self.settings())
        self.assertEqual(result["deleted"], [])
        self.assertTrue(self.lexists("chain/l2.parquet"))
        self.assertTrue(self.exists("real.parquet"))

        # After the reader retires, the whole chain is reclaimable.
        service.retire_snapshot(self.settings(), "reader")
        result = service.run_cleanup(self.settings())
        self.assertEqual(
            sorted(result["deleted"]),
            ["chain/l1.parquet", "chain/l2.parquet", "real.parquet"],
        )

    def test_unused_alias_reclaimed_but_target_protected(self) -> None:
        self.write_manifest(
            [
                {"snapshot_id": "live", "files": ["real.parquet"]},
                {"snapshot_id": "old", "files": ["alias.parquet"]},
            ]
        )
        self.touch("real.parquet")
        self.symlink("real.parquet", "alias.parquet")

        # The alias points at a protected file, but no read path uses the
        # alias itself: unlinking it only removes the link.
        service.retire_snapshot(self.settings(), "old")
        preview = service.preview_cleanup(self.settings())
        self.assertEqual(preview["deletable"], ["alias.parquet"])
        self.assertEqual(preview["protected"], ["real.parquet"])
        result = service.run_cleanup(self.settings())
        self.assertEqual(result["deleted"], ["alias.parquet"])
        self.assertFalse(self.lexists("alias.parquet"))
        self.assertTrue(self.exists("real.parquet"))

    def test_active_export_reading_old_paths_protects_targets(self) -> None:
        self.write_manifest(
            [
                {"snapshot_id": "new", "files": ["hydro/a.parquet", "hydro/b.parquet"]},
                {
                    "snapshot_id": "legacy",
                    "files": ["legacy/hydro/a.parquet", "links/b.parquet"],
                },
            ]
        )
        self.touch("hydro/a.parquet", "hydro/b.parquet")
        self.symlink("../hydro", "legacy/hydro")
        self.symlink("../hydro/b.parquet", "links/b.parquet")

        registration = service.register_export(self.settings(), "legacy", "old-paths")
        service.retire_snapshot(self.settings(), "new")
        service.retire_snapshot(self.settings(), "legacy")

        # The unfinished export reads via old paths: the real files behind
        # the directory symlink and the file symlink stay protected.
        preview = service.preview_cleanup(self.settings())
        self.assertEqual(preview["deletable"], [])
        self.assertEqual(
            sorted(preview["protected_by_symlink_aliases"]),
            ["hydro/a.parquet", "hydro/b.parquet"],
        )
        result = service.run_cleanup(self.settings())
        self.assertEqual(result["deleted"], [])
        self.assertTrue(self.exists("hydro/a.parquet"))
        self.assertTrue(self.exists("hydro/b.parquet"))

        # After release everything is reclaimable.  The legacy path resolves
        # to the already-deleted real file and is reported absent.
        service.release_export(self.settings(), registration["registration_id"])
        result = service.run_cleanup(self.settings())
        self.assertEqual(
            result["deleted"], ["hydro/a.parquet", "hydro/b.parquet", "links/b.parquet"]
        )
        self.assertEqual(result["already_absent"], ["legacy/hydro/a.parquet"])

    def test_escaping_symlink_chain_is_out_of_scope(self) -> None:
        self.write_manifest(
            [
                {"snapshot_id": "reader", "files": ["esc.parquet"]},
                {"snapshot_id": "old", "files": ["real.parquet", "out.parquet"]},
            ]
        )
        self.touch("real.parquet")
        self.symlink("/etc/hostname", "esc.parquet")
        self.symlink("/etc/hostname", "out.parquet")
        service.retire_snapshot(self.settings(), "old")

        result = service.run_cleanup(self.settings())
        # A read path whose chain leaves the data dir protects nothing, and
        # path validation refuses to delete an item resolving outside.
        self.assertEqual(result["deleted"], ["real.parquet"])
        self.assertEqual(result["failed"], ["out.parquet"])
        self.assertTrue(self.lexists("out.parquet"))

    def test_delete_time_symlink_change_is_skipped(self) -> None:
        self.touch("d1/a.parquet", "d2/a.parquet")
        self.symlink("d1", "current")
        root = self.data_dir.resolve()
        planned = service._unlink_effect(root, "current/a.parquet")
        self.assertEqual(planned, str(root / "d1" / "a.parquet"))

        # The directory symlink is repointed mid-window: the item must not
        # be unlinked at its new location.
        os.remove(self.data_dir / "current")
        self.symlink("d2", "current")
        with self.assertRaises(ValueError):
            service._ensure_still_reclaimable(
                root, "current/a.parquet", planned, set()
            )
        self.assertTrue(self.exists("d2/a.parquet"))

        # And an item that newly lands on a protected entry is skipped too.
        with self.assertRaises(ValueError):
            service._ensure_still_reclaimable(
                root,
                "current/a.parquet",
                str(root / "d2" / "a.parquet"),
                {str(root / "d2" / "a.parquet")},
            )


class SeedTests(unittest.TestCase):
    def test_seed_recreates_symlinks_as_symlinks(self) -> None:
        from app.seed import seed_sample_data

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sample = root / "sample"
            (sample / "hydro").mkdir(parents=True)
            (sample / "hydro" / "a.parquet").write_bytes(b"PAR1-PAR1")
            (sample / "legacy").mkdir()
            os.symlink("../hydro", sample / "legacy" / "hydro")
            os.symlink("../hydro/a.parquet", sample / "legacy" / "a_link.parquet")

            data = root / "data"
            copied = seed_sample_data(sample, data)
            self.assertEqual(copied, 3)
            self.assertTrue((data / "hydro" / "a.parquet").is_file())
            self.assertFalse((data / "hydro" / "a.parquet").is_symlink())
            self.assertTrue((data / "legacy" / "hydro").is_symlink())
            self.assertEqual(os.readlink(data / "legacy" / "hydro"), "../hydro")
            self.assertTrue((data / "legacy" / "a_link.parquet").is_symlink())
            # The recreated chain resolves to the seeded real file.
            self.assertTrue((data / "legacy" / "a_link.parquet").exists())

            # Seeding is a one-shot: the marker prevents re-copying.
            self.assertEqual(seed_sample_data(sample, data), 0)


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
