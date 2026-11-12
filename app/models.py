"""Pydantic request/response models used by FastAPI/Swagger."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RegisterExportRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "snapshot_id": "snap-20261101-night",
                    "export_id": "nightly-export-001",
                }
            ]
        }
    )

    snapshot_id: str = Field(..., description="清单中的快照 ID")
    export_id: str | None = Field(
        default=None, description="导出程序自己的任务 ID（可选，仅用于排障）"
    )


class CleanupRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"dry_run": False}]}
    )

    dry_run: bool = Field(
        default=False, description="true 时只预览可删除文件，不真正删除"
    )


class SnapshotInfo(BaseModel):
    snapshot_id: str
    files: list[str]
    file_count: int
    retired: bool
    retired_at: str | None = None
    active_export_count: int


class SnapshotListResponse(BaseModel):
    snapshots: list[SnapshotInfo]


class RetireResponse(BaseModel):
    snapshot_id: str
    retired: bool
    already_retired: bool
    retired_at: str


class RegistrationResponse(BaseModel):
    registration_id: str
    snapshot_id: str
    export_id: str | None = None
    files: list[str]
    registered_at: str
    released: bool


class ReleaseResponse(BaseModel):
    registration_id: str
    snapshot_id: str
    export_id: str | None = None
    files: list[str]
    registered_at: str
    released_at: str
    released: bool
    already_released: bool


class ExportInfo(BaseModel):
    registration_id: str
    snapshot_id: str
    export_id: str | None = None
    files: list[str]
    registered_at: str
    released_at: str | None = None
    active: bool


class ExportListResponse(BaseModel):
    exports: list[ExportInfo]


class ActiveExport(BaseModel):
    registration_id: str
    snapshot_id: str
    export_id: str | None = None
    files: list[str]
    registered_at: str


class CleanupFailure(BaseModel):
    path: str
    error: str


class CleanupPreviewResponse(BaseModel):
    deletable: list[str]
    already_absent: list[str]
    protected: list[str]
    protected_by_retained_snapshots: list[str]
    protected_by_active_exports: list[str]
    protected_by_symlink_aliases: list[str] = Field(
        ...,
        description=(
            "自身路径未被引用、但删除它会切断保留快照/未完成导出读取链"
            "（目录软链接、多级文件软链接）的清单项"
        ),
    )
    active_exports: list[ActiveExport]


class CleanupResponse(BaseModel):
    dry_run: bool
    deleted: list[str]
    already_absent: list[str]
    failed: list[str]
    failures: list[CleanupFailure]
    deletable: list[str]
    protected: list[str]
    protected_by_retained_snapshots: list[str]
    protected_by_active_exports: list[str]
    protected_by_symlink_aliases: list[str] = Field(
        ...,
        description=(
            "自身路径未被引用、但删除它会切断保留快照/未完成导出读取链"
            "（目录软链接、多级文件软链接）的清单项"
        ),
    )
    active_exports: list[ActiveExport]
