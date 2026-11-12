"""HTTP API tests.  Run with: pytest -q (requires requirements-dev.txt)."""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from app import db


def _write_manifest(path, snapshots):
    path.write_text(json.dumps({"snapshots": snapshots}), encoding="utf-8")


@pytest.fixture()
def api(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(
        manifest_path,
        [
            {"snapshot_id": "a", "files": ["a.parquet", "shared.parquet"]},
            {"snapshot_id": "b", "files": ["b.parquet", "shared.parquet"]},
        ],
    )
    for name in ("a.parquet", "b.parquet", "shared.parquet"):
        (data_dir / name).write_bytes(b"PAR1-PAR1")

    monkeypatch.setenv("MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("CLEANUP_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("SEED_SAMPLE_DATA_ON_START", "0")
    db.reset_initialization_cache()

    from app.main import app

    with TestClient(app) as client:
        yield client, data_dir


def test_demo_before_and_after_export_completion(api):
    client, data_dir = api

    registration = client.post(
        "/exports/register", json={"snapshot_id": "a", "export_id": "export-1"}
    )
    assert registration.status_code == 201, registration.text
    registration_id = registration.json()["registration_id"]

    for snapshot_id in ("a", "b"):
        response = client.post(f"/snapshots/{snapshot_id}/retire")
        assert response.status_code == 200, response.text

    # Before the export completes: b.parquet is free, but a's files and the
    # shared file are still protected by the unfinished export.
    preview = client.get("/cleanup/preview")
    assert preview.status_code == 200
    body = preview.json()
    assert body["deletable"] == ["b.parquet"]
    assert body["protected_by_active_exports"] == [
        "a.parquet",
        "shared.parquet",
    ]

    cleanup = client.post("/cleanup")
    assert cleanup.status_code == 200, cleanup.text
    assert cleanup.json()["deleted"] == ["b.parquet"]
    assert cleanup.json()["failed"] == []
    assert (data_dir / "a.parquet").exists()
    assert (data_dir / "shared.parquet").exists()

    # Release (export finished) then clean up the rest.
    released = client.post(f"/exports/{registration_id}/release")
    assert released.status_code == 200
    assert released.json()["released"] is True
    assert released.json()["already_released"] is False

    preview = client.get("/cleanup/preview").json()
    assert preview["deletable"] == ["a.parquet", "shared.parquet"]
    assert preview["active_exports"] == []

    cleanup = client.post("/cleanup").json()
    assert cleanup["deleted"] == ["a.parquet", "shared.parquet"]
    assert cleanup["failed"] == []

    assert client.get("/cleanup/preview").json()["deletable"] == []


def test_retirement_rejects_new_registration(api):
    client, _ = api
    assert client.post("/snapshots/a/retire").status_code == 200
    response = client.post("/exports/register", json={"snapshot_id": "a"})
    assert response.status_code == 409
    assert "已退役" in response.json()["detail"]


def test_dry_run_does_not_delete(api):
    client, data_dir = api
    for snapshot_id in ("a", "b"):
        client.post(f"/snapshots/{snapshot_id}/retire")
    response = client.post("/cleanup?dry_run=true")
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["deleted"] == []
    assert body["deletable"] == ["a.parquet", "shared.parquet", "b.parquet"]
    assert body["already_absent"] == []
    assert (data_dir / "a.parquet").exists()


def test_unknown_ids_and_idempotent_release(api):
    client, _ = api
    assert client.post("/snapshots/nope/retire").status_code == 404
    assert client.post("/exports/register", json={"snapshot_id": "nope"}).status_code == 404
    assert client.post("/exports/nope/release").status_code == 404

    registration_id = client.post(
        "/exports/register", json={"snapshot_id": "a"}
    ).json()["registration_id"]
    first = client.post(f"/exports/{registration_id}/release")
    second = client.post(f"/exports/{registration_id}/release")
    assert first.status_code == second.status_code == 200
    assert second.json()["already_released"] is True


def test_snapshot_listing(api):
    client, _ = api
    client.post("/snapshots/a/retire")
    client.post("/exports/register", json={"snapshot_id": "b"})

    snapshots = {
        item["snapshot_id"]: item
        for item in client.get("/snapshots").json()["snapshots"]
    }
    assert snapshots["a"]["retired"] is True
    assert snapshots["a"]["active_export_count"] == 0
    assert snapshots["b"]["retired"] is False
    assert snapshots["b"]["active_export_count"] == 1
    assert snapshots["b"]["files"] == ["b.parquet", "shared.parquet"]


@pytest.fixture()
def api_with_symlinks(tmp_path, monkeypatch):
    """Post-migration layout: exports still read through old symlink paths."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    manifest_path = tmp_path / "manifest.json"
    _write_manifest(
        manifest_path,
        [
            {"snapshot_id": "new", "files": ["hydro/a.parquet", "hydro/b.parquet"]},
            {
                "snapshot_id": "legacy",
                "files": ["legacy/hydro/a.parquet", "chain/l1.parquet"],
            },
            {"snapshot_id": "aliases", "files": ["alias_a.parquet", "chain/l2.parquet"]},
        ],
    )
    (data_dir / "hydro").mkdir()
    (data_dir / "hydro" / "a.parquet").write_bytes(b"PAR1-a")
    (data_dir / "hydro" / "b.parquet").write_bytes(b"PAR1-b")
    (data_dir / "legacy").mkdir()
    os.symlink("../hydro", data_dir / "legacy" / "hydro")
    os.symlink("hydro/a.parquet", data_dir / "alias_a.parquet")
    (data_dir / "chain").mkdir()
    os.symlink("l2.parquet", data_dir / "chain" / "l1.parquet")
    os.symlink("../hydro/b.parquet", data_dir / "chain" / "l2.parquet")

    monkeypatch.setenv("MANIFEST_PATH", str(manifest_path))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("CLEANUP_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("SEED_SAMPLE_DATA_ON_START", "0")
    db.reset_initialization_cache()

    from app.main import app

    with TestClient(app) as client:
        yield client, data_dir


def test_cleanup_follows_symlink_read_paths(api_with_symlinks):
    client, data_dir = api_with_symlinks

    # A downstream export registers its old (pre-migration) read paths.
    registration = client.post(
        "/exports/register", json={"snapshot_id": "legacy", "export_id": "old-paths"}
    )
    assert registration.status_code == 201, registration.text
    registration_id = registration.json()["registration_id"]

    for snapshot_id in ("new", "legacy", "aliases"):
        response = client.post(f"/snapshots/{snapshot_id}/retire")
        assert response.status_code == 200, response.text

    # Preview: the export's own paths are protected directly; the real
    # files and the intermediate link its reads resolve through are
    # protected via symlink resolution; the unused alias is reclaimable.
    preview = client.get("/cleanup/preview")
    assert preview.status_code == 200
    body = preview.json()
    assert body["protected_by_active_exports"] == [
        "legacy/hydro/a.parquet",
        "chain/l1.parquet",
    ]
    assert body["protected_by_symlink_aliases"] == [
        "hydro/a.parquet",
        "hydro/b.parquet",
        "chain/l2.parquet",
    ]
    assert body["deletable"] == ["alias_a.parquet"]

    cleanup = client.post("/cleanup")
    assert cleanup.status_code == 200, cleanup.text
    result = cleanup.json()
    assert result["deleted"] == ["alias_a.parquet"]
    assert result["failed"] == []
    # The alias is gone but its (protected) target survives, as do the
    # files and intermediate links the export reads through.
    assert not os.path.lexists(data_dir / "alias_a.parquet")
    assert (data_dir / "hydro" / "a.parquet").exists()
    assert (data_dir / "hydro" / "b.parquet").exists()
    assert os.path.lexists(data_dir / "chain" / "l2.parquet")

    # Export finishes: everything becomes reclaimable.  The legacy path
    # resolves to the already-deleted real file and, like the alias removed
    # by the previous cleanup, is reported absent.
    released = client.post(f"/exports/{registration_id}/release")
    assert released.status_code == 200
    cleanup = client.post("/cleanup").json()
    assert cleanup["deleted"] == [
        "hydro/a.parquet",
        "hydro/b.parquet",
        "chain/l1.parquet",
        "chain/l2.parquet",
    ]
    assert cleanup["already_absent"] == [
        "alias_a.parquet",
        "legacy/hydro/a.parquet",
    ]
    assert not (data_dir / "hydro" / "a.parquet").exists()
    assert not os.path.lexists(data_dir / "chain" / "l1.parquet")
