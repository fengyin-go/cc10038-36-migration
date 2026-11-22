"""版本化日志交付清单。

历史交付只按「视图名称 + 交付编号」落一条记录，文件路径之外没有可交换的来源结构。
本模块把这类记录迁移为版本化清单，并提供：

* 旧读取结构（``legacy-v1``）与版本化清单（``versioned-manifest-v2``）的并存读写；
* 存量回填：按交付编号逐个迁移，中断后可续做，已封存的清单不会被覆盖；
* 回滚：切回旧读取结构后仍可核实原始文件与迁移生成文件分别属于哪次交付；
* 快照读取：一次下载只对应一份完整清单，切换与读取并发时不会混用新旧内容。

状态保存在 ``store._tables["__delivery__"]`` 这个内部草稿表中，随内存仓库一起重置；
文件内容按摘要落到 ``DELIVERY_ROOT``，存储位置与业务状态分离。
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import settings
from app.store import store

SCRATCH_KEY = "__delivery__"
LEGACY_STRUCTURE = "legacy-v1"
MANIFEST_STRUCTURE = "versioned-manifest-v2"
MANIFEST_VERSION = 2
MAX_SOURCE_BYTES = 8 * 1024 * 1024

MIGRATION_STATUSES = ("待迁移", "迁移中", "已中断", "已暂停", "已完成", "已回滚")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _content_digest(content: str | bytes) -> str:
    data = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


def _deep_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _safe_text(content: str | bytes) -> str:
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content


def _new_state() -> dict[str, Any]:
    """交付域的内部状态。独立于业务模块表，避免影响 /api/overview 的模块清单。"""
    return {
        "legacy": [],
        "manifests": [],
        "sources": [],
        "bindings": [],
        "migrations": [],
        "read_modes": [],
        "files": [],
        "events": [],
        "paused": [],
        "legacy_frozen": {},
        "legacy_export_ids": [],
        "seq": {},
    }


class DeliveryManifestService:
    """版本化日志交付清单的读写与迁移服务。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 基础
    @property
    def root(self) -> Path:
        # 允许测试或部署时通过环境变量切换目录，不依赖模块导入时机。
        return Path(os.environ.get("DELIVERY_ROOT", settings.delivery_root))

    def _state(self) -> dict[str, Any]:
        raw = store._tables.get(SCRATCH_KEY)
        if not isinstance(raw, dict) or "manifests" not in raw:
            raw = _new_state()
            store._tables[SCRATCH_KEY] = raw
        return raw

    def _next(self, kind: str) -> int:
        state = self._state()
        state["seq"][kind] = int(state["seq"].get(kind, 0)) + 1
        return state["seq"][kind]

    def _event(self, action: str, **detail: Any) -> dict[str, Any]:
        state = self._state()
        entry = {
            "id": self._next("event"),
            "动作": action,
            "记录时间": _now_iso(),
            **detail,
        }
        state["events"].append(entry)
        return entry

    # ------------------------------------------------------------------ 文件仓
    def _blob_path(self, digest: str) -> Path:
        return self.root / "blobs" / digest[:2] / digest

    def _write_blob(self, content: str | bytes) -> dict[str, Any]:
        data = content.encode("utf-8") if isinstance(content, str) else content
        digest = hashlib.sha256(data).hexdigest()
        target = self._blob_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_size != len(data):
            target.write_bytes(data)
        return {
            "digest": digest,
            "size": len(data),
            "storage_path": str(target),
            "content": _safe_text(data),
        }

    def _read_blob(self, digest: str) -> str | None:
        target = self._blob_path(digest)
        if not target.is_file():
            return None
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            return None
        return _safe_text(data)

    def _register_file(
        self,
        *,
        delivery_no: str,
        role: str,
        path: str,
        blob: dict[str, Any],
        delivery_version: int,
        structure_version: str,
    ) -> dict[str, Any]:
        state = self._state()
        for row in state["files"]:
            if (
                row["交付编号"] == delivery_no
                and row["角色"] == role
                and row["文件摘要"] == blob["digest"]
            ):
                return row
        row = {
            "id": self._next("file"),
            "文件ID": f"file-{uuid.uuid4().hex[:16]}",
            "交付编号": delivery_no,
            "交付版本": delivery_version,
            "角色": role,
            "文件路径": path,
            "文件摘要": blob["digest"],
            "文件大小": blob["size"],
            "存储摘要": blob["digest"],
            "结构版本": structure_version,
            "记录时间": _now_iso(),
            "status": "已记录",
            "pending": False,
            "abnormal": False,
        }
        state["files"].append(row)
        return row

    # ------------------------------------------------------------------ 旧交付
    def _legacy_by_no(self, delivery_no: str) -> dict[str, Any] | None:
        for row in self._state()["legacy"]:
            if row["交付编号"] == delivery_no:
                return row
        return None

    def _legacy_versions(self, view_name: str) -> list[dict[str, Any]]:
        return sorted(
            [r for r in self._state()["legacy"] if r["视图名称"] == view_name],
            key=lambda r: (int(r["交付版本"]), int(r["id"])),
        )

    def _freeze_legacy_pointer(self, view_name: str, delivery_no: str) -> None:
        """记录旧客户端在部署期间应继续读取的那份同名视图交付。

        每次同名视图生成新交付，旧客户端就固定读取其上一份交付，保证
        「部署期间旧客户端读以前的交付」这一语义稳定可追溯。
        """
        self._state()["legacy_frozen"][view_name] = delivery_no

    def record_legacy(
        self,
        view_name: str,
        file_path: str,
        *,
        content: str | bytes | None = None,
        delivery_no: str | None = None,
        file_name: str | None = None,
        export_id: int | None = None,
        file_format: str = "csv",
    ) -> dict[str, Any]:
        """登记一份旧结构交付。

        内容在登记时落盘；后续旧客户端读取只使用这份内容，不会用新清单反推。
        """
        with self._lock:
            state = self._state()
            if delivery_no and self._legacy_by_no(delivery_no):
                return self._legacy_by_no(delivery_no)  # type: ignore[return-value]
            if delivery_no is None:
                delivery_no = f"LEG-{self._next('legacy_no'):04d}"
            versions = self._legacy_versions(view_name)
            version = (int(versions[-1]["交付版本"]) if versions else 0) + 1
            file_id = f"file-{uuid.uuid4().hex[:16]}"
            blob = None
            if content is not None:
                blob = self._write_blob(content)
            file_path = file_path or f"/exports/{delivery_no}.{file_format}"
            row = {
                "id": self._next("legacy"),
                "交付编号": delivery_no,
                "视图名称": view_name,
                "交付版本": version,
                "结构版本": LEGACY_STRUCTURE,
                "文件名称": file_name or Path(file_path).name,
                "文件路径": file_path,
                "文件路径引用": file_path,
                "文件ID": file_id if blob else None,
                "文件摘要": blob["digest"] if blob else None,
                "文件大小": blob["size"] if blob else 0,
                "导出任务ID": export_id,
                "记录时间": _now_iso(),
                "状态": "已记录",
                "status": "已记录",
                "pending": False,
                "abnormal": False,
                "来源清单回写": False,
                "迁移状态": "待迁移",
            }
            state["legacy"].append(row)
            if export_id is not None:
                state["legacy_export_ids"].append(int(export_id))
            if blob:
                self._register_file(
                    delivery_no=delivery_no,
                    role="legacy_artifact",
                    path=file_path,
                    blob=blob,
                    delivery_version=version,
                    structure_version=LEGACY_STRUCTURE,
                )
            # 同名视图出现新交付时，旧客户端在部署期继续读取上一份交付。
            if version > 1 and len(versions) >= 1:
                self._freeze_legacy_pointer(view_name, versions[-1]["交付编号"])
            self._event("登记旧交付", 交付编号=delivery_no, 视图名称=view_name,
                        交付版本=version)
            return row

    def ensure_legacy_from_exports(self) -> None:
        """把已存在的 data_export 记录补充为旧交付记录（不推断来源文件）。

        只导入看起来是真实文件引用的记录：绝对路径或以 ``/`` 开头的导出路径；
        示例数据里的占位文本不会被当成交付。
        """
        with self._lock:
            state = self._state()
            for row in store.rows("data_export"):
                export_id = int(row.get("id", 0) or 0)
                if export_id in state["legacy_export_ids"]:
                    continue
                path = str(row.get("文件路径") or "")
                if not path:
                    continue
                if not path.startswith("/") and not Path(path).is_file():
                    continue
                delivery_no = f"EXP-{export_id:04d}"
                if self._legacy_by_no(delivery_no) is None:
                    self.record_legacy(
                        str(row.get("导出范围") or "unknown"),
                        path,
                        delivery_no=delivery_no,
                        export_id=export_id,
                    )
                else:
                    state["legacy_export_ids"].append(export_id)

    # ------------------------------------------------------------------ 来源绑定
    def _binding_for(self, delivery_no: str) -> dict[str, Any] | None:
        for row in self._state()["bindings"]:
            if row["交付编号"] == delivery_no:
                return row
        return None

    def bind_source(
        self,
        delivery_no: str,
        *,
        content: str | bytes | None = None,
        path: str | None = None,
        source_file_id: str | None = None,
        grouping_basis: list[str] | None = None,
        field_order: list[str] | None = None,
        resolution: str = "explicit_reference",
    ) -> dict[str, Any]:
        """显式绑定来源文件。

        来源必须由文件内容（或已登记的文件 ID）确认。仅凭文件名不会建立绑定。
        """
        with self._lock:
            legacy = self._legacy_by_no(delivery_no)
            if legacy is None:
                return {"ok": False, "message": f"交付 {delivery_no} 不存在"}
            state = self._state()
            blob = None
            file_id = source_file_id
            existing_file = None
            if file_id:
                existing_file = next(
                    (f for f in state["files"] if f["文件ID"] == file_id), None
                )
                if existing_file is None:
                    return {"ok": False, "message": f"来源文件 {file_id} 未登记"}
                stored = self._read_blob(existing_file["存储摘要"])
                if stored is None:
                    return {"ok": False, "message": "来源文件内容缺失，无法绑定"}
                blob = {
                    "digest": existing_file["文件摘要"],
                    "size": existing_file["文件大小"],
                    "content": stored,
                }
            elif content is not None:
                blob = self._write_blob(content)
                file_id = f"file-{uuid.uuid4().hex[:16]}"
                self._register_file(
                    delivery_no=delivery_no,
                    role="source_original",
                    path=path or f"/sources/{delivery_no}.log",
                    blob=blob,
                    delivery_version=int(legacy["交付版本"]),
                    structure_version=MANIFEST_STRUCTURE,
                )
            else:
                return {
                    "ok": False,
                    "message": "绑定来源必须提供来源文件内容或已登记的文件 ID",
                }

            binding = {
                "id": self._next("binding"),
                "交付编号": delivery_no,
                "视图名称": legacy["视图名称"],
                "交付版本": int(legacy["交付版本"]),
                "来源文件ID": file_id,
                "来源文件路径": path or (existing_file or {}).get("文件路径"),
                "来源文件摘要": blob["digest"],
                "来源文件大小": blob["size"],
                "字段顺序": list(field_order or []),
                "分组依据": list(grouping_basis or []),
                "确认方式": resolution,
                "记录时间": _now_iso(),
                "status": "已确认",
                "pending": False,
                "abnormal": False,
            }
            state["bindings"] = [b for b in state["bindings"]
                                  if b["交付编号"] != delivery_no]
            state["bindings"].append(binding)
            # 绑定只登记来源；清单由迁移/双写按同一份绑定构建。已封存清单不受影响，
            # 待补来源的清单可在下次迁移时用新绑定重建。
            self._event("绑定来源", 交付编号=delivery_no,
                        来源文件ID=file_id, 确认方式=resolution)
            return {"ok": True, "message": "来源已绑定", "binding": binding}

    def _binding_from_saved_reference(self, legacy: dict[str, Any]) -> dict[str, Any] | None:
        """兼容旧入口保存的文件引用。

        只有文件真实存在且能读出内容时才建立绑定；文件名本身永远不足以回填来源。
        """
        reference = str(legacy.get("文件路径引用") or legacy.get("文件路径") or "")
        if not reference:
            return None
        candidate = Path(reference)
        if not candidate.is_file():
            return None
        try:
            if candidate.stat().st_size > MAX_SOURCE_BYTES:
                return None
            content = candidate.read_bytes()
        except OSError:
            return None
        result = self.bind_source(
            legacy["交付编号"],
            content=content,
            path=str(candidate),
            resolution="saved_reference",
        )
        if not result["ok"]:
            return None
        return result["binding"]

    # ------------------------------------------------------------------ 清单构建
    def _parse_source_records(
        self, content: str, field_order: list[str] | None
    ) -> list[dict[str, Any]]:
        """把来源文件解析为逐条记录，尽量保留原始记录 ID。"""
        text = content.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            records: list[dict[str, Any]] = []
            for idx, item in enumerate(parsed, start=1):
                if not isinstance(item, dict):
                    continue
                fields = item.get("fields")
                if not isinstance(fields, dict):
                    fields = {
                        key: value for key, value in item.items()
                        if key not in ("raw_record_id", "raw_line", "parsed_id")
                    }
                records.append({
                    "raw_record_id": str(
                        item.get("raw_record_id")
                        or item.get("原始记录ID")
                        or f"raw-{idx:04d}"
                    ),
                    "raw_line": int(item.get("raw_line") or item.get("原始行号") or idx),
                    "parsed_id": item.get("parsed_id"),
                    "fields": {str(k): str(v) for k, v in fields.items()},
                })
            return records

        order = list(field_order or [])
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return []
        # 未显式给出字段顺序时，只做保守的表头识别：
        # 首行由纯字母/下划线标识符组成，且第二行不是同形态（数据行通常含数字、
        # 空格或时间戳等）。不确定时保留 field_N，避免把数据行误当字段名。
        header: list[str] = []
        if not order and len(lines) > 1:
            delimiter = "|" if "|" in lines[0] else (
                "," if "," in lines[0] else "\t"
            )
            first = [p.strip() for p in lines[0].split(delimiter)]
            second = [p.strip() for p in lines[1].split(delimiter)]
            def name_like(token: str) -> bool:
                return bool(token) and all(ch.isalpha() or ch == "_" for ch in token) \
                    and any(ch.islower() for ch in token)
            if (
                len(first) == len(second)
                and all(name_like(token) for token in first)
                and not all(name_like(token) for token in second)
            ):
                header = first
                lines = lines[1:]

        records = []
        for idx, line in enumerate(lines, start=1):
            parts = line.split("|") if "|" in line else line.split(",")
            names = order or header or [f"field_{i}" for i in range(1, len(parts) + 1)]
            fields = {
                names[i]: parts[i].strip()
                for i in range(min(len(names), len(parts)))
            }
            records.append({
                "raw_record_id": f"raw-{idx:04d}",
                "raw_line": idx,
                "parsed_id": None,
                "fields": fields,
            })
        return records

    def _build_groups(
        self, records: list[dict[str, Any]], basis: list[str]
    ) -> dict[str, Any]:
        groups: dict[str, Any] = {}
        for record in records:
            key = "|".join(str(record["fields"].get(field, "")) for field in basis)
            key = key or "(empty)"
            group = groups.setdefault(key, {
                "count": 0,
                "raw_record_ids": [],
                "values": [],
                "fields": {},
            })
            group["count"] += 1
            group["raw_record_ids"].append(record["raw_record_id"])
            group["values"].append(record["raw_record_id"])
            for field, value in record["fields"].items():
                group["fields"].setdefault(field, value)
        return groups

    def _generated_content(self, groups: dict[str, Any], basis: list[str]) -> str:
        output = io.StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["group_key", *basis, "count", "raw_record_ids"])
        for key, group in groups.items():
            writer.writerow([
                key,
                *(group.get("fields", {}).get(field, "") for field in basis),
                group["count"],
                ",".join(group["raw_record_ids"]),
            ])
        return output.getvalue()

    def _field_rows(
        self,
        delivery_no: str,
        view_name: str,
        version: int,
        source_file_id: str | None,
        source_path: str | None,
        source_digest: str | None,
        records: list[dict[str, Any]],
        basis: list[str],
        migration_no: str,
        lineage_id: int | None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for record in records:
            group_key = "|".join(
                str(record["fields"].get(field, "")) for field in basis
            ) or "(empty)"
            for field, value in record["fields"].items():
                rows.append({
                    "id": self._next("source"),
                    "交付编号": delivery_no,
                    "交付版本": version,
                    "视图名称": view_name,
                    "来源文件ID": source_file_id,
                    "原始文件路径": source_path,
                    "原始文件摘要": source_digest,
                    "原始记录ID": record["raw_record_id"],
                    "原始行号": record["raw_line"],
                    "字段名": field,
                    "字段值": value,
                    "分组依据": ",".join(basis),
                    "分组键": group_key,
                    "迁移编号": migration_no,
                    "血缘ID": lineage_id,
                    "结构版本": MANIFEST_STRUCTURE,
                    "记录时间": _now_iso(),
                    "status": "已记录",
                    "pending": False,
                    "abnormal": False,
                })
        return rows

    def _manifest_for(self, delivery_no: str) -> dict[str, Any] | None:
        candidates = [
            row for row in self._state()["manifests"]
            if row["交付编号"] == delivery_no
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda row: (int(row["交付版本"]), int(row["id"])))

    def _upsert_manifest(self, payload: dict[str, Any], *, status: str,
                         sealed: bool, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        state = self._state()
        digest = _digest(payload)
        payload = _deep_copy(payload)
        payload["manifest_digest"] = digest
        row = {
            "id": self._next("manifest"),
            "交付编号": payload["delivery_no"],
            "视图名称": payload["view_name"],
            "交付版本": payload["delivery_version"],
            "清单版本": MANIFEST_VERSION,
            "结构版本": MANIFEST_STRUCTURE,
            "清单摘要": digest,
            "清单内容": payload,
            "状态": status,
            "已封存": sealed,
            "status": "已生效" if sealed else "待生效",
            "pending": not sealed,
            "abnormal": status == "待补来源",
            "记录时间": _now_iso(),
            "封存时间": _now_iso() if sealed else None,
            "回滚时间": None,
            **(extra or {}),
        }
        # 同一交付编号 + 交付版本只保留一份清单；已封存的清单绝不覆盖。
        for idx, existing in enumerate(state["manifests"]):
            if (
                existing["交付编号"] == row["交付编号"]
                and int(existing["交付版本"]) == int(row["交付版本"])
            ):
                if existing.get("已封存"):
                    return existing
                # 未封存（如待补来源）允许在补充绑定后重建，但沿用原 id 便于追溯。
                row["id"] = existing["id"]
                state["manifests"][idx] = row
                return row
        state["manifests"].append(row)
        return row

    def _build_manifest(
        self,
        legacy: dict[str, Any],
        binding: dict[str, Any] | None,
        *,
        migration_no: str,
        resolve_saved_refs: bool,
        grouping_basis: list[str] | None = None,
    ) -> dict[str, Any]:
        state = self._state()
        delivery_no = legacy["交付编号"]
        view_name = legacy["视图名称"]
        version = int(legacy["交付版本"])

        existing = self._manifest_for(delivery_no)
        if existing and existing.get("已封存"):
            return {"delivery_no": delivery_no, "status": "已封存", "skipped": True,
                    "manifest_id": existing["id"], "清单摘要": existing["清单摘要"]}

        if binding is None and resolve_saved_refs:
            binding = self._binding_from_saved_reference(legacy)
        if binding is None:
            binding = self._binding_for(delivery_no)

        if binding is None:
            # 只有文件名/路径字符串，没有可核实的来源内容：不生成来源结构，也不封存。
            payload = {
                "manifest_version": MANIFEST_VERSION,
                "structure": MANIFEST_STRUCTURE,
                "delivery_no": delivery_no,
                "view_name": view_name,
                "delivery_version": version,
                "source": {
                    "file_id": None,
                    "path": legacy.get("文件路径"),
                    "file_name": legacy.get("文件名称"),
                    "digest": None,
                    "size": 0,
                    "resolution": "filename_only",
                    "confirmed": False,
                    "record_count": 0,
                    "field_order": [],
                },
                "grouping": {"basis": [], "groups": {}},
                "fields": [],
                "file": None,
                "lineage": {"lineage_id": None},
            }
            row = self._upsert_manifest(
                payload,
                status="待补来源",
                sealed=False,
                extra={"来源未确认原因": "仅凭文件名称无法回填来源",
                        "迁移编号": migration_no},
            )
            legacy["迁移状态"] = "待补来源"
            self._event("迁移受阻", 交付编号=delivery_no, 原因="仅凭文件名无法确认来源")
            return {"delivery_no": delivery_no, "status": "待补来源", "skipped": False,
                    "manifest_id": row["id"], "清单摘要": row["清单摘要"]}

        source_content = self._read_blob(binding["来源文件摘要"])
        if source_content is None:
            return {"delivery_no": delivery_no, "status": "来源内容缺失", "skipped": True,
                    "manifest_id": None, "清单摘要": None}
        basis = list(
            grouping_basis
            or binding.get("分组依据")
            or legacy.get("分组依据")
            or []
        )
        field_order = list(binding.get("字段顺序") or [])
        records = self._parse_source_records(source_content, field_order)
        if not basis:
            basis = ["level"] if any("level" in r["fields"] for r in records) else (
                list(records[0]["fields"].keys())[:1] if records else []
            )
        groups = self._build_groups(records, basis) if basis else {}
        generated = self._generated_content(groups, basis)
        generated_blob = self._write_blob(generated)
        generated_file = self._register_file(
            delivery_no=delivery_no,
            role="generated",
            path=f"/deliveries/{view_name}.v{version}.{MANIFEST_STRUCTURE}.csv",
            blob=generated_blob,
            delivery_version=version,
            structure_version=MANIFEST_STRUCTURE,
        )

        # 采集血缘：从清单定位真实文件、分组依据与字段关联。
        lineage = None
        for candidate in store.rows("data_lineage"):
            if str(candidate.get("delivery_no") or "") == delivery_no:
                lineage = candidate
                break
        if lineage is None:
            lineage = {
                "id": max(
                    (int(r.get("id", 0)) for r in store.rows("data_lineage")),
                    default=0,
                ) + 1,
                "来源批次": view_name,
                "解析规则版本": binding.get("确认方式", "unknown"),
                "标准化版本": f"{MANIFEST_STRUCTURE}.v{version}",
                "下游消费标识": delivery_no,
                "记录时间": _now_iso(),
                "status": "已记录",
                "pending": False,
                "abnormal": False,
            }
            store.rows("data_lineage").append(lineage)
        lineage.update({
            "delivery_no": delivery_no,
            "delivery_version": version,
            "manifest_structure": MANIFEST_STRUCTURE,
            "source_file_id": binding["来源文件ID"],
            "source_file_digest": binding["来源文件摘要"],
            "source_file_path": binding["来源文件路径"],
            "grouping_basis": basis,
            "field_count": len({name for record in records for name in record["fields"]}),
            "record_count": len(records),
            "status": "已记录",
        })

        payload = {
            "manifest_version": MANIFEST_VERSION,
            "structure": MANIFEST_STRUCTURE,
            "delivery_no": delivery_no,
            "view_name": view_name,
            "delivery_version": version,
            "source": {
                "file_id": binding["来源文件ID"],
                "path": binding["来源文件路径"],
                "file_name": Path(str(binding["来源文件路径"] or "")).name,
                "digest": binding["来源文件摘要"],
                "size": binding["来源文件大小"],
                "resolution": binding["确认方式"],
                "confirmed": True,
                "record_count": len(records),
                "field_order": field_order,
            },
            "grouping": {"basis": basis, "groups": groups},
            "fields": [
                {
                    "raw_record_id": record["raw_record_id"],
                    "raw_line": record["raw_line"],
                    "values": record["fields"],
                    "group_key": "|".join(
                        str(record["fields"].get(field, "")) for field in basis
                    ) or "(empty)",
                }
                for record in records
            ],
            "file": {
                "file_id": generated_file["文件ID"],
                "path": generated_file["文件路径"],
                "digest": generated_blob["digest"],
                "size": generated_blob["size"],
                "format": "csv",
            },
            "lineage": {
                "lineage_id": lineage["id"],
                "source_batch": view_name,
                "downstream": delivery_no,
            },
        }
        row = self._upsert_manifest(
            payload,
            status="已封存",
            sealed=True,
            extra={"迁移编号": migration_no,
                    "来源文件ID": binding["来源文件ID"],
                    "生成文件ID": generated_file["文件ID"]},
        )
        digest = row["清单摘要"]

        # 迁移结果回写来源清单：关联原始文件和每个日志字段。
        state["sources"] = [s for s in state["sources"]
                            if s["交付编号"] != delivery_no]
        state["sources"].extend(self._field_rows(
            delivery_no, view_name, version,
            binding["来源文件ID"], binding["来源文件路径"],
            binding["来源文件摘要"], records, basis,
            migration_no, lineage["id"],
        ))
        lineage.update({
            "manifest_digest": digest,
            "manifest_id": row["id"],
            "source_manifest_rows": len(records) and sum(
                len(record["fields"]) for record in records
            ),
            "record_time": _now_iso(),
        })
        legacy["迁移状态"] = "已完成"
        legacy["来源清单回写"] = True
        legacy["manifest_digest"] = digest
        self._event("完成迁移", 交付编号=delivery_no, 清单摘要=digest,
                    来源文件ID=binding["来源文件ID"])
        return {"delivery_no": delivery_no, "status": "已封存", "skipped": False,
                "manifest_id": row["id"], "清单摘要": digest}

    # ------------------------------------------------------------------ 迁移编排
    def _target_delivery_nos(self, delivery_nos: list[str] | None) -> list[str]:
        self.ensure_legacy_from_exports()
        legacy_rows = sorted(self._state()["legacy"], key=lambda r: r["交付编号"])
        if delivery_nos:
            wanted = {str(no) for no in delivery_nos}
            return [r["交付编号"] for r in legacy_rows if r["交付编号"] in wanted]
        return [r["交付编号"] for r in legacy_rows]

    def migrate(
        self,
        delivery_nos: list[str] | None = None,
        *,
        resume: bool = True,
        fail_after: int | None = None,
        resolve_saved_refs: bool = True,
        migration_no: str | None = None,
    ) -> dict[str, Any]:
        """按交付编号迁移存量。可中断、可续做、已封存清单不会被覆盖。"""
        with self._lock:
            state = self._state()
            targets = self._target_delivery_nos(delivery_nos)
            batch = None
            if migration_no:
                batch = next(
                    (m for m in state["migrations"] if m["迁移编号"] == migration_no),
                    None,
                )
            if batch is None and resume and not delivery_nos:
                candidates = [
                    m for m in state["migrations"]
                    if m["状态"] in ("迁移中", "已中断") and m["方向"] == "正向"
                ]
                if candidates:
                    batch = max(candidates, key=lambda m: int(m["id"]))
            if batch is None:
                batch = {
                    "id": self._next("migration"),
                    "迁移编号": f"MG-{self._next('migration_no'):04d}",
                    "方向": "正向",
                    "状态": "迁移中",
                    "交付编号清单": targets,
                    "游标": 0,
                    "已处理": [],
                    "受阻": [],
                    "暂停项": [no for no in state["paused"]],
                    "错误": None,
                    "开始时间": _now_iso(),
                    "更新时间": _now_iso(),
                    "status": "迁移中",
                    "pending": True,
                    "abnormal": False,
                }
                state["migrations"].append(batch)
            elif targets and not batch.get("交付编号清单"):
                batch["交付编号清单"] = targets

            paused = set(state["paused"]) | set(batch.get("暂停项") or [])
            cursor = int(batch.get("游标", 0)) if resume else 0
            processed_this_run = 0
            batch_targets = list(batch.get("交付编号清单") or targets)
            # 兼容显式指定的交付编号：追加到批内但保持已有游标不倒退。
            for no in targets:
                if no not in batch_targets:
                    batch_targets.append(no)
            batch["交付编号清单"] = batch_targets

            for index in range(cursor, len(batch_targets)):
                delivery_no = batch_targets[index]
                if delivery_no in paused:
                    if delivery_no not in batch["暂停项"]:
                        batch["暂停项"].append(delivery_no)
                    # 暂停项保持原样，旧读取结构照常可用；同批其他交付继续迁移。
                    continue
                legacy = self._legacy_by_no(delivery_no)
                if legacy is None:
                    batch["受阻"].append({"delivery_no": delivery_no, "原因": "旧交付不存在"})
                    batch["游标"] = index + 1
                    continue
                existing = self._manifest_for(delivery_no)
                if existing and existing.get("已封存"):
                    if delivery_no not in batch["已处理"]:
                        batch["已处理"].append(delivery_no)
                    batch["游标"] = index + 1
                    continue

                result = self._build_manifest(
                    legacy,
                    self._binding_for(delivery_no),
                    migration_no=batch["迁移编号"],
                    resolve_saved_refs=resolve_saved_refs,
                )
                if result["status"] == "已封存":
                    if delivery_no not in batch["已处理"]:
                        batch["已处理"].append(delivery_no)
                    processed_this_run += 1
                else:
                    if not any(b.get("delivery_no") == delivery_no
                               for b in batch["受阻"]):
                        batch["受阻"].append({
                            "delivery_no": delivery_no,
                            "原因": result["status"],
                        })
                batch["游标"] = index + 1
                batch["更新时间"] = _now_iso()

                if fail_after is not None and processed_this_run >= fail_after:
                    batch["状态"] = "已中断"
                    batch["status"] = "已中断"
                    batch["pending"] = True
                    batch["abnormal"] = True
                    batch["错误"] = "部署中断：迁移在交付编号边界停止，可按交付编号续做"
                    batch["更新时间"] = _now_iso()
                    self._event("迁移中断", 迁移编号=batch["迁移编号"],
                                游标=batch["游标"])
                    return self._migration_result(batch, interrupted=True)

            batch["状态"] = "已完成"
            batch["status"] = "已完成"
            batch["pending"] = False
            batch["abnormal"] = False
            batch["错误"] = None
            batch["更新时间"] = _now_iso()
            if batch["暂停项"]:
                batch["错误"] = (
                    f"已跳过暂停项：{', '.join(batch['暂停项'])}"
                )
            self._event("迁移完成", 迁移编号=batch["迁移编号"],
                        已处理=len(batch["已处理"]),
                        暂停项=list(batch["暂停项"]))
            return self._migration_result(batch, interrupted=False)

    def _migration_result(self, batch: dict[str, Any], *, interrupted: bool) -> dict[str, Any]:
        return {
            "ok": True,
            "interrupted": interrupted,
            "migration_no": batch["迁移编号"],
            "status": batch["状态"],
            "cursor": batch["游标"],
            "processed": list(batch["已处理"]),
            "blocked": _deep_copy(batch["受阻"]),
            "paused": list(batch["暂停项"]),
            "total": len(batch["交付编号清单"]),
            "message": batch.get("错误") or f"迁移{batch['状态']}",
        }

    def pause_migration(self, delivery_no: str) -> dict[str, Any]:
        """暂停一份交付的迁移。旧读取结构不受影响。"""
        with self._lock:
            state = self._state()
            if delivery_no not in state["paused"]:
                state["paused"].append(delivery_no)
            for batch in state["migrations"]:
                if batch["状态"] in ("迁移中", "已中断", "已暂停"):
                    if delivery_no not in batch["暂停项"]:
                        batch["暂停项"].append(delivery_no)
                    batch["状态"] = "已暂停"
                    batch["status"] = "已暂停"
                    batch["pending"] = True
                    batch["更新时间"] = _now_iso()
            manifest = self._manifest_for(delivery_no)
            if manifest and not manifest.get("已封存"):
                manifest["状态"] = "已暂停"
                manifest["status"] = "待生效"
            legacy = self._legacy_by_no(delivery_no)
            if legacy:
                legacy["迁移状态"] = "已暂停"
            self._event("暂停迁移", 交付编号=delivery_no)
            return {"ok": True, "message": f"交付 {delivery_no} 的迁移已暂停",
                    "delivery_no": delivery_no, "paused": True}

    def resume_migration(self, delivery_no: str) -> dict[str, Any]:
        with self._lock:
            state = self._state()
            state["paused"] = [no for no in state["paused"] if no != delivery_no]
            for batch in state["migrations"]:
                batch["暂停项"] = [no for no in batch["暂停项"] if no != delivery_no]
                if batch["状态"] == "已暂停":
                    batch["状态"] = "已中断"
                    batch["status"] = "已中断"
            legacy = self._legacy_by_no(delivery_no)
            if legacy:
                legacy["迁移状态"] = "待迁移"
            self._event("恢复迁移", 交付编号=delivery_no)
            return {"ok": True, "message": f"交付 {delivery_no} 的迁移已恢复"}

    # ------------------------------------------------------------------ 读取切换
    def switch_read_mode(self, view_name: str, read_version: int, *,
                         client: str | None = None, note: str | None = None) -> dict[str, Any]:
        if int(read_version) not in (1, 2):
            return {"ok": False, "message": "读取版本只能是 1（旧结构）或 2（版本化清单）"}
        with self._lock:
            state = self._state()
            key = client or "__default__"
            row = next(
                (m for m in state["read_modes"]
                 if m["视图名称"] == view_name and m["客户端"] == key),
                None,
            )
            if row is None:
                row = {
                    "id": self._next("read_mode"),
                    "视图名称": view_name,
                    "客户端": key,
                    "读取版本": int(read_version),
                    "切换时间": _now_iso(),
                    "备注": note,
                    "status": "已生效",
                    "pending": False,
                    "abnormal": False,
                }
                state["read_modes"].append(row)
            else:
                row["读取版本"] = int(read_version)
                row["切换时间"] = _now_iso()
                row["备注"] = note
            self._event("切换读取版本", 视图名称=view_name, 客户端=key,
                        读取版本=int(read_version))
            return {"ok": True, "message": f"视图 {view_name} 读取版本已切换为 {read_version}",
                    "read_mode": row}

    def read_mode(self, view_name: str, client: str | None = None) -> dict[str, Any]:
        state = self._state()
        if client:
            row = next(
                (m for m in state["read_modes"]
                 if m["视图名称"] == view_name and m["客户端"] == client),
                None,
            )
            if row:
                return row
        row = next(
            (m for m in state["read_modes"]
             if m["视图名称"] == view_name and m["客户端"] == "__default__"),
            None,
        )
        if row:
            return row
        # 未显式切换时：
        # * 同名视图已经存在上一份交付（两个交付版本并存）→ 保持旧读取结构，
        #   旧客户端继续读取以前的交付；
        # * 只有一份新交付且已封存清单 → 默认读取版本化清单。
        legacy_rows = self._legacy_versions(view_name)
        has_sealed = any(
            m["视图名称"] == view_name and m.get("已封存")
            for m in state["manifests"]
        )
        if len(legacy_rows) >= 2 or not has_sealed:
            default_version = 1
        else:
            default_version = 2
        return {
            "视图名称": view_name,
            "客户端": client or "__default__",
            "读取版本": default_version if legacy_rows else 0,
            "切换时间": None,
            "status": "默认",
        }

    def _resolve_version(self, view_name: str, read_version: int | None,
                         client: str | None) -> int:
        if read_version in (1, 2):
            return int(read_version)
        return int(self.read_mode(view_name, client)["读取版本"])

    def _resolve_delivery_no(
        self, view_name: str, read_version: int
    ) -> str | None:
        state = self._state()
        if read_version == 1:
            # 部署期间旧客户端继续读取同名视图以前的交付。
            frozen = state["legacy_frozen"].get(view_name)
            if frozen and self._legacy_by_no(frozen):
                return frozen
            rows = self._legacy_versions(view_name)
            return rows[-1]["交付编号"] if rows else None
        manifests = [
            m for m in state["manifests"]
            if m["视图名称"] == view_name and m.get("已封存")
        ]
        if not manifests:
            return None
        return max(manifests, key=lambda m: (int(m["交付版本"]), int(m["id"])))["交付编号"]

    # ------------------------------------------------------------------ 快照读取
    def snapshot(self, delivery_no: str, read_version: int = 2) -> dict[str, Any]:
        """读取一份完整清单。

        整个过程持有同一把锁：要么拿到完整的旧交付内容，要么拿到完整的版本化
        清单；不会把新生成文件或新算出的分组拼进旧交付。
        """
        with self._lock:
            if int(read_version) == 1:
                return self._legacy_snapshot(delivery_no)
            return self._manifest_snapshot(delivery_no)

    def _legacy_snapshot(self, delivery_no: str) -> dict[str, Any]:
        legacy = self._legacy_by_no(delivery_no)
        if legacy is None:
            return {"ok": False, "code": "delivery_not_found",
                    "message": f"交付 {delivery_no} 不存在"}
        if not legacy.get("文件摘要"):
            return {
                "ok": False,
                "code": "legacy_file_missing",
                "message": "旧交付没有可核实的文件内容，不能用新生成的文件或分组代替",
                "delivery_no": delivery_no,
            }
        content = self._read_blob(legacy["文件摘要"])
        if content is None or _content_digest(content) != legacy["文件摘要"]:
            return {
                "ok": False,
                "code": "legacy_integrity_error",
                "message": "旧交付文件内容与登记摘要不一致，拒绝返回",
                "delivery_no": delivery_no,
            }
        payload = {
            "manifest_version": 1,
            "structure": LEGACY_STRUCTURE,
            "delivery_no": legacy["交付编号"],
            "view_name": legacy["视图名称"],
            "delivery_version": legacy["交付版本"],
            "file_name": legacy["文件名称"],
            "file_path": legacy["文件路径"],
            "file_digest": legacy["文件摘要"],
            "file_size": legacy["文件大小"],
            "content": content,
            "source_resolution": "legacy_record",
        }
        return {
            "ok": True,
            "read_version": 1,
            "structure": LEGACY_STRUCTURE,
            "delivery_no": delivery_no,
            "view_name": legacy["视图名称"],
            "delivery_version": legacy["交付版本"],
            "snapshot_digest": _digest(payload),
            "content": content,
            "payload": payload,
        }

    def _manifest_snapshot(self, delivery_no: str) -> dict[str, Any]:
        manifest = self._manifest_for(delivery_no)
        if manifest is None:
            return {"ok": False, "code": "manifest_not_found",
                    "message": f"交付 {delivery_no} 尚未迁移为版本化清单"}
        if not manifest.get("已封存"):
            return {
                "ok": False,
                "code": "manifest_not_sealed",
                "message": f"交付 {delivery_no} 的清单状态为 {manifest['状态']}，不能用于读取",
                "delivery_no": delivery_no,
                "status": manifest["状态"],
            }
        payload = _deep_copy(manifest["清单内容"])
        stored_digest = payload.pop("manifest_digest", None)
        actual_digest = _digest(payload)
        if stored_digest != actual_digest or manifest["清单摘要"] != actual_digest:
            return {
                "ok": False,
                "code": "manifest_integrity_error",
                "message": "清单内容与摘要不一致，拒绝返回",
                "delivery_no": delivery_no,
            }
        file_info = payload.get("file") or {}
        content = self._read_blob(file_info.get("digest", "")) if file_info else None
        if content is None:
            return {
                "ok": False,
                "code": "generated_file_missing",
                "message": "清单引用的生成文件缺失，拒绝用重新计算的结果代替",
                "delivery_no": delivery_no,
            }
        if _content_digest(content) != file_info.get("digest"):
            return {
                "ok": False,
                "code": "generated_file_integrity_error",
                "message": "生成文件内容与清单摘要不一致，拒绝返回",
                "delivery_no": delivery_no,
            }
        payload["manifest_digest"] = actual_digest
        return {
            "ok": True,
            "read_version": 2,
            "structure": MANIFEST_STRUCTURE,
            "delivery_no": delivery_no,
            "view_name": payload["view_name"],
            "delivery_version": payload["delivery_version"],
            "snapshot_digest": actual_digest,
            "content": content,
            "payload": payload,
        }

    def download(
        self,
        *,
        delivery_no: str | None = None,
        view_name: str | None = None,
        read_version: int | None = None,
        client: str | None = None,
    ) -> dict[str, Any]:
        """一次下载解析一份完整交付。"""
        with self._lock:
            if delivery_no:
                legacy = self._legacy_by_no(delivery_no)
                manifest = self._manifest_for(delivery_no)
                if view_name is None:
                    view_name = (legacy or manifest or {}).get("视图名称")
                if read_version not in (1, 2):
                    if legacy and not manifest:
                        version = 1
                    elif manifest and not legacy:
                        version = 2
                    elif view_name:
                        version = self._resolve_version(view_name, None, client)
                    else:
                        version = 1
                else:
                    version = int(read_version)
            else:
                if not view_name:
                    return {"ok": False, "message": "必须提供交付编号或视图名称"}
                version = self._resolve_version(view_name, read_version, client)
                delivery_no = self._resolve_delivery_no(view_name, version)
                if delivery_no is None:
                    return {
                        "ok": False,
                        "code": "delivery_not_found",
                        "message": f"视图 {view_name} 没有可读取的版本 {version} 交付",
                    }
            result = self.snapshot(delivery_no, version)
            if result.get("ok"):
                result["view_name"] = result.get("view_name") or view_name
            return result

    # ------------------------------------------------------------------ 回滚与核实
    def rollback(
        self,
        *,
        delivery_no: str | None = None,
        view_name: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """回滚到旧读取结构，但不删除迁移产物与文件归属。"""
        with self._lock:
            state = self._state()
            manifest = self._manifest_for(delivery_no) if delivery_no else None
            legacy = self._legacy_by_no(delivery_no) if delivery_no else None
            if view_name is None:
                view_name = (manifest or legacy or {}).get("视图名称")
            if not view_name:
                return {"ok": False, "message": "无法确定要回滚的视图"}
            if delivery_no is None:
                target = self._resolve_delivery_no(view_name, 2) or (
                    self._legacy_versions(view_name)[-1]["交付编号"]
                    if self._legacy_versions(view_name) else None
                )
                delivery_no = target
                manifest = self._manifest_for(delivery_no) if delivery_no else None
                legacy = self._legacy_by_no(delivery_no) if delivery_no else None
            if delivery_no is None:
                return {"ok": False, "message": f"视图 {view_name} 没有可回滚的交付"}

            if manifest:
                manifest["状态"] = "已回滚"
                manifest["status"] = "已停用"
                manifest["回滚时间"] = _now_iso()
                manifest["pending"] = False
                manifest["abnormal"] = False
            if legacy:
                legacy["迁移状态"] = "已回滚"
            for batch in state["migrations"]:
                if delivery_no in (batch.get("交付编号清单") or []):
                    batch["状态"] = "已回滚"
                    batch["status"] = "已回滚"
                    batch["pending"] = False
                    batch["更新时间"] = _now_iso()

            # 旧客户端与新入口都切回旧结构；已有清单保留用于核实。
            for row in state["read_modes"]:
                if row["视图名称"] == view_name:
                    row["读取版本"] = 1
                    row["切换时间"] = _now_iso()
                    row["备注"] = note or "回滚到旧读取结构"
            self.switch_read_mode(view_name, 1, note=note or "回滚到旧读取结构")
            self._event("回滚", 视图名称=view_name, 交付编号=delivery_no)
            result = self.verify(delivery_no)
            result.update({
                "ok": True,
                "message": f"视图 {view_name} 已回滚到旧读取结构",
                "read_version": 1,
            })
            return result

    def verify(self, delivery_no: str) -> dict[str, Any]:
        """核实某次交付的原始文件、生成文件、清单与迁移归属。"""
        with self._lock:
            state = self._state()
            legacy = self._legacy_by_no(delivery_no)
            manifest = self._manifest_for(delivery_no)
            if legacy is None and manifest is None:
                return {"ok": False, "message": f"交付 {delivery_no} 不存在"}
            view_name = (legacy or manifest or {}).get("视图名称")
            files = [
                {
                    "file_id": f["文件ID"],
                    "role": f["角色"],
                    "path": f["文件路径"],
                    "digest": f["文件摘要"],
                    "size": f["文件大小"],
                    "structure": f["结构版本"],
                    "delivery_no": f["交付编号"],
                    "delivery_version": f["交付版本"],
                    "ownership": "旧交付" if f["结构版本"] == LEGACY_STRUCTURE else "版本化清单",
                }
                for f in state["files"]
                if f["交付编号"] == delivery_no
            ]
            migrations = [
                {
                    "migration_no": m["迁移编号"],
                    "status": m["状态"],
                    "cursor": m["游标"],
                    "processed": list(m["已处理"]),
                    "paused": list(m["暂停项"]),
                }
                for m in state["migrations"]
                if delivery_no in (m.get("交付编号清单") or [])
            ]
            manifest_summary = None
            if manifest:
                payload = manifest["清单内容"]
                manifest_summary = {
                    "manifest_id": manifest["id"],
                    "status": manifest["状态"],
                    "sealed": manifest.get("已封存", False),
                    "digest": manifest["清单摘要"],
                    "structure": manifest["结构版本"],
                    "delivery_version": manifest["交付版本"],
                    "source_file_id": (payload.get("source") or {}).get("file_id"),
                    "source_digest": (payload.get("source") or {}).get("digest"),
                    "source_resolution": (payload.get("source") or {}).get("resolution"),
                    "grouping_basis": (payload.get("grouping") or {}).get("basis", []),
                    "group_count": len((payload.get("grouping") or {}).get("groups", {})),
                    "field_count": len({f for row in payload.get("fields", [])
                                        for f in row.get("values", {})}),
                    "generated_file_id": (payload.get("file") or {}).get("file_id"),
                    "generated_digest": (payload.get("file") or {}).get("digest"),
                    "lineage_id": (payload.get("lineage") or {}).get("lineage_id"),
                }
            source_rows = [s for s in state["sources"] if s["交付编号"] == delivery_no]
            return {
                "ok": True,
                "delivery_no": delivery_no,
                "view_name": view_name,
                "delivery_version": (legacy or manifest or {}).get("交付版本"),
                "read_mode": self.read_mode(view_name) if view_name else None,
                "legacy": {
                    "structure": LEGACY_STRUCTURE,
                    "file_name": legacy.get("文件名称") if legacy else None,
                    "file_path": legacy.get("文件路径") if legacy else None,
                    "digest": legacy.get("文件摘要") if legacy else None,
                    "restorable": bool(legacy and legacy.get("文件摘要")),
                    "migration_status": legacy.get("迁移状态") if legacy else None,
                },
                "manifest": manifest_summary,
                "files": files,
                "file_ownership": {
                    "original": [f for f in files if f["role"] in ("source_original",)],
                    "legacy": [f for f in files if f["role"] == "legacy_artifact"],
                    "generated": [f for f in files if f["role"] == "generated"],
                },
                "source_manifest_rows": len(source_rows),
                "source_manifest_fields": sorted({s["字段名"] for s in source_rows}),
                "migrations": migrations,
            }

    def restore_file(self, delivery_no: str, *,
                     role: str | None = None,
                     read_version: int | None = None) -> dict[str, Any]:
        """按原交付编号还原文件。"""
        with self._lock:
            state = self._state()
            files = [f for f in state["files"] if f["交付编号"] == delivery_no]
            if not files:
                return {"ok": False, "message": f"交付 {delivery_no} 没有登记文件"}
            if role:
                selected = next((f for f in files if f["角色"] == role), None)
            elif read_version == 1:
                selected = next((f for f in files if f["角色"] == "legacy_artifact"), None)
            elif read_version == 2:
                selected = next((f for f in files if f["角色"] == "generated"), None)
            else:
                selected = files[0]
            if selected is None:
                return {"ok": False, "message": f"交付 {delivery_no} 没有角色为 {role} 的文件"}
            content = self._read_blob(selected["存储摘要"])
            if content is None:
                return {"ok": False, "message": "文件内容缺失，无法按原编号还原"}
            return {
                "ok": True,
                "delivery_no": delivery_no,
                "role": selected["角色"],
                "structure": selected["结构版本"],
                "file_id": selected["文件ID"],
                "path": selected["文件路径"],
                "digest": selected["文件摘要"],
                "content": content,
            }

    # ------------------------------------------------------------------ 查询
    def source_manifest(self, delivery_no: str | None = None) -> dict[str, Any]:
        with self._lock:
            rows = [
                _deep_copy(row) for row in self._state()["sources"]
                if delivery_no is None or row["交付编号"] == delivery_no
            ]
            return {"ok": True, "total": len(rows), "items": rows}

    def list_deliveries(self, view_name: str | None = None) -> dict[str, Any]:
        with self._lock:
            self.ensure_legacy_from_exports()
            state = self._state()
            items: list[dict[str, Any]] = []
            for legacy in state["legacy"]:
                if view_name and legacy["视图名称"] != view_name:
                    continue
                # 只有真正可读的旧交付才对外展示：有内容，或来自导出任务。
                if not legacy.get("文件摘要") and legacy.get("导出任务ID") is None:
                    continue
                manifest = self._manifest_for(legacy["交付编号"])
                legacy_preview = {
                    "delivery_no": legacy["交付编号"],
                    "view_name": legacy["视图名称"],
                    "delivery_version": legacy["交付版本"],
                    "structure": LEGACY_STRUCTURE,
                    "file_name": legacy["文件名称"],
                    "file_path": legacy["文件路径"],
                    "digest": legacy["文件摘要"],
                    "has_content": bool(legacy.get("文件摘要")),
                }
                manifest_preview = None
                if manifest:
                    payload = manifest["清单内容"]
                    manifest_preview = {
                        "delivery_no": manifest["交付编号"],
                        "view_name": manifest["视图名称"],
                        "delivery_version": manifest["交付版本"],
                        "structure": manifest["结构版本"],
                        "status": manifest["状态"],
                        "sealed": manifest.get("已封存", False),
                        "digest": manifest["清单摘要"],
                        "source_file_id": (payload.get("source") or {}).get("file_id"),
                        "grouping_basis": (payload.get("grouping") or {}).get("basis", []),
                        "field_count": len({f for row in payload.get("fields", [])
                                            for f in row.get("values", {})}),
                    }
                items.append({
                    "delivery_no": legacy["交付编号"],
                    "view_name": legacy["视图名称"],
                    "delivery_version": legacy["交付版本"],
                    "legacy": legacy_preview,
                    "manifest": manifest_preview,
                    "migration_status": legacy.get("迁移状态"),
                    "read_mode": self.read_mode(legacy["视图名称"])["读取版本"],
                })
            return {"ok": True, "total": len(items), "items": items}

    def migrations(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "items": [_deep_copy(row) for row in self._state()["migrations"]],
            }

    def events(self) -> dict[str, Any]:
        with self._lock:
            return {"ok": True, "items": [_deep_copy(row) for row in self._state()["events"]]}

    # ------------------------------------------------------------------ 新交付双写
    def register_versioned_delivery(
        self,
        view_name: str,
        *,
        legacy_content: str,
        legacy_path: str,
        legacy_file_name: str | None = None,
        export_id: int | None = None,
        export_format: str = "csv",
        source_records: list[dict[str, Any]] | None = None,
        grouping_basis: list[str] | None = None,
        groups: dict[str, Any] | None = None,
        origin: str = "pipeline",
    ) -> dict[str, Any]:
        """部署期双写：同一交付同时保留旧结构与版本化清单。"""
        with self._lock:
            state = self._state()
            delivery_no = f"EXP-{export_id:04d}" if export_id else None
            existing = self._manifest_for(delivery_no) if delivery_no else None
            if existing and existing.get("已封存"):
                return {
                    "ok": True,
                    "delivery_no": delivery_no,
                    "delivery_version": existing["交付版本"],
                    "manifest": existing["清单内容"],
                    "manifest_digest": existing["清单摘要"],
                    "legacy": self._legacy_by_no(delivery_no),
                    "duplicate": True,
                }
            legacy = self.record_legacy(
                view_name,
                legacy_path,
                content=legacy_content,
                delivery_no=delivery_no,
                file_name=legacy_file_name,
                export_id=export_id,
                file_format=export_format,
            )
            # record_legacy 在未指定编号时会自行生成编号，后续一律以它为准。
            delivery_no = legacy["交付编号"]
            version = int(legacy["交付版本"])

            records = [dict(record) for record in (source_records or [])]
            basis = list(grouping_basis or [])
            if not basis and records:
                basis = ["level"] if any("level" in r["fields"] for r in records) else (
                    list(records[0]["fields"].keys())[:1]
                )
            source_content = json.dumps(records, ensure_ascii=False, indent=2)
            source_blob = self._write_blob(source_content)
            source_file_row = self._register_file(
                delivery_no=delivery_no,
                role="source_original",
                path=f"/sources/{delivery_no}.json",
                blob=source_blob,
                delivery_version=version,
                structure_version=MANIFEST_STRUCTURE,
            )
            source_file_id = source_file_row["文件ID"]
            binding = {
                "id": self._next("binding"),
                "交付编号": delivery_no,
                "视图名称": view_name,
                "交付版本": version,
                "来源文件ID": source_file_id,
                "来源文件路径": f"/sources/{delivery_no}.json",
                "来源文件摘要": source_blob["digest"],
                "来源文件大小": source_blob["size"],
                "字段顺序": sorted({f for r in records for f in r["fields"]}),
                "分组依据": basis,
                "确认方式": origin,
                "记录时间": _now_iso(),
                "status": "已确认",
                "pending": False,
                "abnormal": False,
            }
            state["bindings"] = [b for b in state["bindings"]
                                  if b["交付编号"] != delivery_no]
            state["bindings"].append(binding)
            # 复用迁移构建逻辑，保证新旧两条链路产出的清单结构一致。
            legacy["分组依据"] = basis
            result = self._build_manifest(
                legacy,
                binding,
                migration_no=f"DUAL-{delivery_no}",
                resolve_saved_refs=False,
                grouping_basis=basis,
            )
            manifest = self._manifest_for(delivery_no)
            if manifest and groups:
                # 用聚合视图已经算出的分组补齐清单中的分组结果，保持与导出可见结果一致。
                payload = manifest["清单内容"]
                for key, group in groups.items():
                    group_key = str(key)
                    if group_key in payload["grouping"]["groups"]:
                        continue
                    payload["grouping"]["groups"][group_key] = {
                        "count": int(group.get("count", 0)),
                        "values": [str(v) for v in group.get("values", [])],
                        "raw_record_ids": [
                            r["raw_record_id"] for r in records
                            if "|".join(str(r["fields"].get(f, "")) for f in basis)
                            == group_key
                        ],
                        "fields": {f: group_key if len(basis) == 1 else ""
                                   for f in basis},
                    }
                payload["grouping"]["basis"] = basis
                payload.pop("manifest_digest", None)
                manifest["清单内容"] = payload
                manifest["清单摘要"] = _digest(payload)
                payload["manifest_digest"] = manifest["清单摘要"]
            return {
                "ok": True,
                "delivery_no": delivery_no,
                "delivery_version": version,
                "legacy": legacy,
                "manifest": manifest["清单内容"] if manifest else None,
                "manifest_digest": manifest["清单摘要"] if manifest else None,
                "migration": result,
            }


delivery = DeliveryManifestService()
