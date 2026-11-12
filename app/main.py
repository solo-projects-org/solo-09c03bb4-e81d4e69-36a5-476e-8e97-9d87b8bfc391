"""FastAPI application for reference-safe Parquet reclamation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse

from . import service
from .config import get_settings
from .db import ensure_db
from .manifest import ManifestError
from .models import (
    CleanupPreviewResponse,
    CleanupRequest,
    CleanupResponse,
    ExportListResponse,
    RegisterExportRequest,
    RegistrationResponse,
    ReleaseResponse,
    RetireResponse,
    SnapshotInfo,
    SnapshotListResponse,
)
from .seed import seed_sample_data


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    ensure_db(settings.db_path)
    if settings.seed_on_start:
        seed_sample_data(settings.sample_data_dir, settings.data_dir)
    yield


app = FastAPI(
    title="水文观测明细表 · 快照小文件回收服务",
    version="1.1.0",
    description=(
        "用于回收小文件合并后遗留的旧 Parquet 文件。\n\n"
        "**安全规则**：清单中的文件只有在以下三个条件同时满足时才会被删除：\n"
        "1. 没有任何未退役（保留）的快照引用它；\n"
        "2. 没有任何未完成的导出登记引用它；\n"
        "3. 删除它不会切断任何保留快照/未完成导出的读取链：存储目录迁移后，"
        "这些读取可能仍走旧路径（目录软链接、多级文件软链接），服务会解析"
        "整条软链接链，保护链上的最终文件和中间链接。只跟随全程留在数据"
        "目录内、维护期间不变的软链接；未被读取路径用到的别名仍可回收，"
        "即使它指向同一个文件。\n\n"
        "退役只阻止新的登记；已经成功登记的导出会一直保护其文件，"
        "直到导出调用 release 释放登记。清理失败不会改变退役状态，"
        "修复磁盘问题后可直接重试（服务重启后状态仍在 SQLite 中）。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(ManifestError)
async def manifest_error_handler(_, exc: ManifestError) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"], summary="健康检查")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(
    "/snapshots",
    response_model=SnapshotListResponse,
    tags=["快照"],
    summary="列出清单中的快照及其退役/登记状态",
)
def list_snapshots() -> SnapshotListResponse:
    return SnapshotListResponse(snapshots=service.list_snapshots(get_settings()))


@app.get(
    "/snapshots/{snapshot_id}",
    response_model=SnapshotInfo,
    tags=["快照"],
    summary="查看单个快照",
)
def get_snapshot(snapshot_id: str) -> SnapshotInfo:
    try:
        return SnapshotInfo(**service.get_snapshot(get_settings(), snapshot_id))
    except service.SnapshotNotFound:
        raise HTTPException(
            status_code=404, detail=f"快照不存在: {snapshot_id}"
        ) from None


@app.post(
    "/snapshots/{snapshot_id}/retire",
    response_model=RetireResponse,
    tags=["快照"],
    summary="退役快照（之后不再接受新的导出登记）",
)
def retire_snapshot(snapshot_id: str) -> RetireResponse:
    try:
        return RetireResponse(
            **service.retire_snapshot(get_settings(), snapshot_id)
        )
    except service.SnapshotNotFound:
        raise HTTPException(
            status_code=404, detail=f"快照不存在: {snapshot_id}"
        ) from None


@app.post(
    "/exports/register",
    status_code=201,
    response_model=RegistrationResponse,
    tags=["导出登记"],
    summary="导出程序按快照登记（登记后其文件受保护）",
)
def register_export(payload: RegisterExportRequest) -> RegistrationResponse:
    try:
        return RegistrationResponse(
            **service.register_export(
                get_settings(), payload.snapshot_id, payload.export_id
            )
        )
    except service.SnapshotNotFound:
        raise HTTPException(
            status_code=404, detail=f"快照不存在: {payload.snapshot_id}"
        ) from None
    except service.SnapshotRetired:
        raise HTTPException(
            status_code=409,
            detail=(
                f"快照 {payload.snapshot_id} 已退役，不再接受新的导出登记；"
                "已成功登记的导出不受影响"
            ),
        ) from None


@app.post(
    "/exports/{registration_id}/release",
    response_model=ReleaseResponse,
    tags=["导出登记"],
    summary="导出完成后释放自己的登记",
)
def release_export(registration_id: str) -> ReleaseResponse:
    try:
        return ReleaseResponse(
            **service.release_export(get_settings(), registration_id)
        )
    except service.RegistrationNotFound:
        raise HTTPException(
            status_code=404, detail=f"导出登记不存在: {registration_id}"
        ) from None


@app.get(
    "/exports",
    response_model=ExportListResponse,
    tags=["导出登记"],
    summary="列出导出登记（默认只列未完成的）",
)
def list_exports(
    active_only: bool = Query(
        default=True, description="true 只列未完成的导出，false 包含历史"
    ),
) -> ExportListResponse:
    return ExportListResponse(
        exports=service.list_exports(get_settings(), active_only=active_only)
    )


@app.get(
    "/cleanup/preview",
    response_model=CleanupPreviewResponse,
    tags=["清理"],
    summary="预览：当前哪些文件可删、哪些文件仍被保护",
)
def preview_cleanup() -> CleanupPreviewResponse:
    return CleanupPreviewResponse(**service.preview_cleanup(get_settings()))


@app.post(
    "/cleanup",
    response_model=CleanupResponse,
    tags=["清理"],
    summary="执行清理：删除不再被保留快照/未完成导出引用的文件",
)
def cleanup(
    payload: CleanupRequest | None = Body(default=None),
    dry_run: bool = Query(default=False, description="true 时只预览，不删除"),
) -> CleanupResponse:
    effective_dry_run = dry_run or bool(payload and payload.dry_run)
    return CleanupResponse(
        **service.run_cleanup(get_settings(), dry_run=effective_dry_run)
    )
