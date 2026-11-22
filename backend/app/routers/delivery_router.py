"""版本化日志交付清单接口：迁移、切换、快照下载、回滚与核实。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.services.delivery import delivery

router = APIRouter(prefix="/api/delivery", tags=["版本化日志交付"])


class MigratePayload(BaseModel):
    delivery_nos: list[str] | None = None
    resume: bool = True
    fail_after: int | None = Field(default=None, ge=1)
    resolve_saved_refs: bool = True
    migration_no: str | None = None


class DeliveryNoPayload(BaseModel):
    delivery_no: str = Field(..., min_length=1)


class ReadModePayload(BaseModel):
    view_name: str = Field(..., min_length=1)
    read_version: int = Field(..., ge=1, le=2)
    client: str | None = None
    note: str | None = None


class RollbackPayload(BaseModel):
    delivery_no: str | None = None
    view_name: str | None = None
    note: str | None = None


class BindSourcePayload(BaseModel):
    delivery_no: str = Field(..., min_length=1)
    content: str | None = None
    path: str | None = None
    source_file_id: str | None = None
    grouping_basis: list[str] | None = None
    field_order: list[str] | None = None


@router.get("/deliveries")
def list_deliveries(view_name: str | None = Query(default=None)) -> dict[str, Any]:
    return delivery.list_deliveries(view_name)


@router.get("/manifest/{delivery_no}")
def get_manifest(delivery_no: str, read_version: int = Query(default=2, ge=1, le=2)) -> dict[str, Any]:
    return delivery.snapshot(delivery_no, read_version)


@router.post("/migrate")
def migrate(payload: MigratePayload) -> dict[str, Any]:
    return delivery.migrate(
        payload.delivery_nos,
        resume=payload.resume,
        fail_after=payload.fail_after,
        resolve_saved_refs=payload.resolve_saved_refs,
        migration_no=payload.migration_no,
    )


@router.post("/migrate/pause")
def pause_migration(payload: DeliveryNoPayload) -> dict[str, Any]:
    return delivery.pause_migration(payload.delivery_no)


@router.post("/migrate/resume")
def resume_migration(payload: DeliveryNoPayload) -> dict[str, Any]:
    return delivery.resume_migration(payload.delivery_no)


@router.post("/read-mode")
def switch_read_mode(payload: ReadModePayload) -> dict[str, Any]:
    return delivery.switch_read_mode(
        payload.view_name, payload.read_version, client=payload.client, note=payload.note
    )


@router.get("/download")
def download(
    delivery_no: str | None = Query(default=None),
    view_name: str | None = Query(default=None),
    read_version: int | None = Query(default=None, ge=1, le=2),
    client: str | None = Query(default=None),
) -> dict[str, Any]:
    return delivery.download(
        delivery_no=delivery_no,
        view_name=view_name,
        read_version=read_version,
        client=client,
    )


@router.post("/rollback")
def rollback(payload: RollbackPayload) -> dict[str, Any]:
    return delivery.rollback(
        delivery_no=payload.delivery_no,
        view_name=payload.view_name,
        note=payload.note,
    )


@router.get("/verify/{delivery_no}")
def verify(delivery_no: str) -> dict[str, Any]:
    return delivery.verify(delivery_no)


@router.get("/restore/{delivery_no}")
def restore(
    delivery_no: str,
    role: str | None = Query(default=None),
    read_version: int | None = Query(default=None, ge=1, le=2),
) -> dict[str, Any]:
    return delivery.restore_file(delivery_no, role=role, read_version=read_version)


@router.post("/bind-source")
def bind_source(payload: BindSourcePayload) -> dict[str, Any]:
    return delivery.bind_source(
        payload.delivery_no,
        content=payload.content,
        path=payload.path,
        source_file_id=payload.source_file_id,
        grouping_basis=payload.grouping_basis,
        field_order=payload.field_order,
    )


@router.get("/source-manifest")
def source_manifest(delivery_no: str | None = Query(default=None)) -> dict[str, Any]:
    return delivery.source_manifest(delivery_no)


@router.get("/migrations")
def migrations() -> dict[str, Any]:
    return delivery.migrations()


@router.get("/events")
def events() -> dict[str, Any]:
    return delivery.events()
