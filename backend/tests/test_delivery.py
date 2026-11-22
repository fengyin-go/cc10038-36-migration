"""版本化日志交付清单测试：存量迁移、读写切换、并发快照、回滚与来源核实。"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
from pathlib import Path

import pytest

from app.store import store


@pytest.fixture(autouse=True)
def delivery_root(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="delivery-test-")
    monkeypatch.setenv("DELIVERY_ROOT", tmp)
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


def _ingest(pipeline, source_id: str, logs: list[dict[str, str]]) -> dict:
    pipeline.register_source(source_id, "http", "default")
    pipeline.validate_source(source_id)
    batch = pipeline.start_ingest_batch(source_id, "2026-01-15", logs)
    batch_no = batch["batch"]["批次编号"]
    pipeline.parse_batch(batch_no)
    pipeline.validate_entities(batch_no)
    return batch_no


def _make_delivery(pipeline, source_id: str, logs: list[dict[str, str]],
                   view_name: str | None = None) -> dict:
    batch_no = _ingest(pipeline, source_id, logs)
    view = view_name or f"view-{batch_no}"
    pipeline.refresh_aggregation(view, "level", "time", "count")
    return pipeline.build_export(view, "csv")


class TestDualWrite:
    def test_export_writes_legacy_and_manifest(self, pipeline, sample_raw_logs):
        result = _make_delivery(pipeline, "dual-src", sample_raw_logs)
        assert result["ok"] is True
        delivery_no = result["delivery"]["delivery_no"]
        assert delivery_no
        assert result["export"]["交付编号"] == delivery_no
        assert result["export"]["清单摘要"]

        listed = delivery_service().list_deliveries()
        item = next(i for i in listed["items"] if i["delivery_no"] == delivery_no)
        assert item["legacy"]["digest"]
        assert item["manifest"]["sealed"] is True
        assert item["manifest"]["grouping_basis"] == ["level"]

    def test_manifest_links_source_file_and_every_field(self, pipeline, sample_raw_logs):
        result = _make_delivery(pipeline, "fields-src", sample_raw_logs)
        delivery_no = result["delivery"]["delivery_no"]
        snap = delivery_service().snapshot(delivery_no, 2)
        payload = snap["payload"]
        assert payload["source"]["confirmed"] is True
        assert payload["source"]["file_id"]
        assert payload["source"]["digest"]
        assert len(payload["fields"]) == len(sample_raw_logs)
        for row in payload["fields"]:
            assert row["raw_record_id"]
            assert set(row["values"]) >= {"time", "level", "message"}
            assert row["group_key"] in payload["grouping"]["groups"]

        verify = delivery_service().verify(delivery_no)
        assert verify["manifest"]["source_file_id"] == payload["source"]["file_id"]
        assert verify["manifest"]["source_digest"] == payload["source"]["digest"]
        assert verify["source_manifest_rows"] > 0
        assert {"time", "level", "message"} <= set(verify["source_manifest_fields"])
        assert verify["file_ownership"]["original"]
        assert verify["file_ownership"]["generated"]

    def test_lineage_points_at_manifest_file_and_grouping(self, pipeline, sample_raw_logs):
        result = _make_delivery(pipeline, "lineage-src", sample_raw_logs)
        lineage = result["lineage"]
        assert lineage["delivery_no"] == result["delivery"]["delivery_no"]
        assert lineage["source_file_id"]
        assert lineage["grouping_basis"] == ["level"]
        assert lineage["field_count"] == 3
        assert lineage["record_count"] == len(sample_raw_logs)


class TestReadSwitch:
    def test_two_versions_of_same_view(self, pipeline, sample_raw_logs):
        first = _make_delivery(pipeline, "two-src", sample_raw_logs[:2], "shared-view")
        second = _make_delivery(pipeline, "two-src", sample_raw_logs[2:], "shared-view")
        view = first["export"]["导出范围"]
        assert second["export"]["导出范围"] == view
        assert first["delivery"]["delivery_version"] == 1
        assert second["delivery"]["delivery_version"] == 2

        delivery_no = second["delivery"]["delivery_no"]
        # 部署期旧客户端仍读同名视图以前的交付（v1），新入口读 v2 清单。
        old = delivery_service().download(view_name=view, read_version=1)
        new = delivery_service().download(view_name=view, read_version=2)
        assert old["ok"] is True and old["read_version"] == 1
        assert new["ok"] is True and new["read_version"] == 2
        assert old["delivery_no"] != new["delivery_no"]
        assert new["delivery_no"] == delivery_no

    def test_switch_changes_target_for_new_clients(self, pipeline, sample_raw_logs):
        first = _make_delivery(pipeline, "switch-src", sample_raw_logs[:2], "switch-view")
        second = _make_delivery(pipeline, "switch-src", sample_raw_logs[2:], "switch-view")
        view = first["export"]["导出范围"]
        svc = delivery_service()
        svc.switch_read_mode(view, 1)
        assert svc.download(view_name=view)["delivery_no"] == first["delivery"]["delivery_no"]
        svc.switch_read_mode(view, 2)
        assert svc.download(view_name=view)["delivery_no"] == second["delivery"]["delivery_no"]

    def test_legacy_snapshot_never_uses_regenerated_file(self, pipeline, sample_raw_logs):
        result = _make_delivery(pipeline, "noguess-src", sample_raw_logs)
        delivery_no = result["delivery"]["delivery_no"]
        snap = delivery_service().snapshot(delivery_no, 1)
        assert snap["ok"] is True
        assert snap["structure"] == "legacy-v1"
        # 旧交付内容与版本化清单生成文件不同源，不能冒充。
        manifest_snap = delivery_service().snapshot(delivery_no, 2)
        assert snap["content"] != manifest_snap["content"]

    def test_concurrent_reads_distinct_snapshots(self, pipeline, sample_raw_logs):
        first = _make_delivery(pipeline, "race-src", sample_raw_logs[:2], "race-view")
        second = _make_delivery(pipeline, "race-src", sample_raw_logs[2:], "race-view")
        view = first["export"]["导出范围"]
        svc = delivery_service()
        observed: dict[str, str] = {}

        def read_old():
            observed["old"] = svc.download(view_name=view, read_version=1)["delivery_no"]

        def read_new():
            observed["new"] = svc.download(view_name=view, read_version=2)["delivery_no"]

        def toggle():
            for _ in range(50):
                svc.switch_read_mode(view, 2)
                svc.switch_read_mode(view, 1)

        threads = [
            threading.Thread(target=read_old),
            threading.Thread(target=read_new),
            threading.Thread(target=toggle),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert observed["old"] == first["delivery"]["delivery_no"]
        assert observed["new"] == second["delivery"]["delivery_no"]
        # 每份下载结果本身摘要自洽，完整且可独立校验。
        for version, delivery_no in ((1, observed["old"]), (2, observed["new"])):
            snap = svc.snapshot(delivery_no, version)
            assert snap["ok"] is True
            assert snap["snapshot_digest"]


class TestMigration:
    def _seed_legacy(self, tmp_path: Path, count: int = 3) -> list[str]:
        """构造只有旧记录、没有版本化清单的存量交付。"""
        svc = delivery_service()
        nos = []
        for idx in range(1, count + 1):
            view = f"legacy-view-{idx}"
            path = tmp_path / f"{view}.csv"
            path.write_text(
                f"group,count\nA,{idx}\nB,{idx + 1}\n", encoding="utf-8"
            )
            row = svc.record_legacy(
                view, str(path), content=path.read_text(encoding="utf-8"),
                delivery_no=f"OLD-{idx:04d}",
            )
            # 显式提供来源内容，模拟迁移时已核实来源的存量交付。
            svc.bind_source(
                row["交付编号"],
                path=str(path),
                content=path.read_text(encoding="utf-8"),
            )
            nos.append(row["交付编号"])
        return nos

    def test_filename_only_does_not_backfill_source(self, pipeline, tmp_path):
        svc = delivery_service()
        path = tmp_path / "named-only.csv"
        path.write_text("A,1\nB,2\n", encoding="utf-8")
        svc.record_legacy(
            "name-view", "/data/reports/named-only.csv", content=path.read_text(),
            delivery_no="OLD-NAME",
        )
        result = svc.migrate(["OLD-NAME"])
        assert result["ok"] is True
        manifest = svc._manifest_for("OLD-NAME")
        assert manifest["状态"] == "待补来源"
        assert manifest["已封存"] is False
        assert manifest["清单内容"]["source"]["resolution"] == "filename_only"
        assert manifest["清单内容"]["source"]["file_id"] is None
        # 待补来源的清单不能用于读取。
        assert svc.snapshot("OLD-NAME", 2)["ok"] is False

    def test_saved_reference_backfills_when_file_exists(self, pipeline, tmp_path):
        svc = delivery_service()
        path = tmp_path / "reference.csv"
        path.write_text("time|level|message\n"
                        "2026-01-01T00:00:00|INFO|a\n"
                        "2026-01-02T00:00:00|INFO|b\n",
                        encoding="utf-8")
        svc.record_legacy("ref-view", str(path), content="group,count\nINFO,2\n",
                          delivery_no="OLD-REF")
        result = svc.migrate(["OLD-REF"], resolve_saved_refs=True)
        assert result["processed"] == ["OLD-REF"]
        manifest = svc._manifest_for("OLD-REF")
        assert manifest["已封存"] is True
        assert manifest["清单内容"]["source"]["resolution"] == "saved_reference"
        assert manifest["清单内容"]["source"]["file_id"]

    def test_interrupted_migration_resumes_by_delivery_no(self, pipeline, tmp_path):
        nos = self._seed_legacy(tmp_path, 3)
        svc = delivery_service()
        interrupted = svc.migrate(nos, resolve_saved_refs=False, fail_after=1)
        assert interrupted["interrupted"] is True
        assert interrupted["status"] == "已中断"
        assert len(interrupted["processed"]) == 1
        sealed_after_interrupt = svc._manifest_for(nos[0])
        assert sealed_after_interrupt["已封存"] is True

        resumed = svc.migrate(nos, resume=True, resolve_saved_refs=False,
                              migration_no=interrupted["migration_no"])
        assert resumed["status"] == "已完成"
        assert set(resumed["processed"]) == set(nos)

    def test_tampered_legacy_content_is_refused(self, pipeline, tmp_path):
        svc = delivery_service()
        path = tmp_path / "tamper.csv"
        path.write_text("time|level|message\nt1|INFO|a\n", encoding="utf-8")
        row = svc.record_legacy("tamper-view", str(path),
                                content="group,count\nINFO,1\n", delivery_no="OLD-TAMP")
        # 直接篡改落盘内容，模拟部署期文件被替换。
        blob = svc._blob_path(row["文件摘要"])
        blob.write_text("group,count\nFAKE,99\n", encoding="utf-8")
        snap = svc.snapshot("OLD-TAMP", 1)
        assert snap["ok"] is False
        assert snap["code"] == "legacy_integrity_error"

    def test_tampered_manifest_is_refused(self, pipeline, sample_raw_logs):
        result = _make_delivery(pipeline, "mtamper-src", sample_raw_logs)
        delivery_no = result["delivery"]["delivery_no"]
        svc = delivery_service()
        manifest = svc._manifest_for(delivery_no)
        manifest["清单内容"]["grouping"]["groups"]["INJECTED"] = {"count": 1}
        snap = svc.snapshot(delivery_no, 2)
        assert snap["ok"] is False
        assert snap["code"] == "manifest_integrity_error"

    def test_generated_file_missing_is_not_regenerated(self, pipeline, sample_raw_logs):
        result = _make_delivery(pipeline, "missing-src", sample_raw_logs)
        delivery_no = result["delivery"]["delivery_no"]
        svc = delivery_service()
        manifest = svc._manifest_for(delivery_no)
        digest = manifest["清单内容"]["file"]["digest"]
        svc._blob_path(digest).unlink()
        snap = svc.snapshot(delivery_no, 2)
        assert snap["ok"] is False
        assert snap["code"] == "generated_file_missing"

    def test_unknown_delivery_reports_not_found(self):
        svc = delivery_service()
        assert svc.snapshot("NO-SUCH", 1)["code"] == "delivery_not_found"
        assert svc.snapshot("NO-SUCH", 2)["code"] == "manifest_not_found"

    def test_migration_is_idempotent_and_sealed_manifests_untouched(self, pipeline, tmp_path):
        nos = self._seed_legacy(tmp_path, 2)
        svc = delivery_service()
        svc.migrate(nos, resolve_saved_refs=False)
        before = {no: svc._manifest_for(no)["清单摘要"] for no in nos}

        svc.migrate(nos, resolve_saved_refs=False)
        after = {no: svc._manifest_for(no)["清单摘要"] for no in nos}
        assert before == after

    def test_rollback_keeps_file_ownership_verifiable(self, pipeline, tmp_path):
        svc = delivery_service()
        path = tmp_path / "verify.csv"
        path.write_text("x|INFO|m1\ny|WARN|m2\n", encoding="utf-8")
        svc.record_legacy("roll-view", str(path), content="group,count\nINFO,1\n",
                          delivery_no="OLD-ROLL")
        svc.bind_source("OLD-ROLL", path=str(path), content=path.read_text(encoding="utf-8"))
        svc.migrate(["OLD-ROLL"], resolve_saved_refs=True)
        svc.switch_read_mode("roll-view", 2)

        result = svc.rollback(delivery_no="OLD-ROLL")
        assert result["ok"] is True
        assert result["read_version"] == 1
        assert svc.read_mode("roll-view")["读取版本"] == 1

        verify = svc.verify("OLD-ROLL")
        roles = {(f["role"], f["delivery_no"]) for f in verify["files"]}
        assert ("legacy_artifact", "OLD-ROLL") in roles
        assert ("source_original", "OLD-ROLL") in roles
        assert ("generated", "OLD-ROLL") in roles
        # 回滚后旧读取结构仍能按原编号还原旧文件。
        old = svc.restore_file("OLD-ROLL", read_version=1)
        assert old["ok"] is True
        assert old["content"] == "group,count\nINFO,1\n"
        # 回滚不删除清单，可按原编号核实新生成文件。
        new = svc.restore_file("OLD-ROLL", read_version=2)
        assert new["ok"] is True
        assert new["digest"]


class TestScenarioPauseOneOfTwo:
    def test_two_generations_pause_one_and_verify_each(self, pipeline, sample_raw_logs):
        first = _make_delivery(pipeline, "pause-src", sample_raw_logs[:2], "pause-view")
        second = _make_delivery(pipeline, "pause-src", sample_raw_logs[2:], "pause-view")
        view = first["export"]["导出范围"]
        first_no = first["delivery"]["delivery_no"]
        second_no = second["delivery"]["delivery_no"]

        # 另起一份同名视图的存量交付，单独暂停其迁移。
        svc = delivery_service()
        row = svc.record_legacy(
            view, "/exports/hold.csv", content="group,count\nINFO,9\n",
            delivery_no="OLD-HOLD",
        )
        svc.bind_source("OLD-HOLD", path="/exports/hold.csv",
                        content="2026-01-01T00:00:00|INFO|held\n")
        svc._freeze_legacy_pointer(view, first_no)
        svc.pause_migration("OLD-HOLD")
        migrated = svc.migrate(["OLD-HOLD"], resolve_saved_refs=False)
        assert "OLD-HOLD" in migrated["paused"]
        assert svc._manifest_for("OLD-HOLD") is None or not svc._manifest_for("OLD-HOLD")["已封存"]

        # 旧客户端读 v1 旧交付，新入口读 v2 清单。
        old = svc.download(view_name=view, read_version=1)
        new = svc.download(view_name=view, read_version=2)
        assert old["delivery_no"] == first_no
        assert new["delivery_no"] == second_no

        # 每份文件、聚合结果、血缘都可分别核实。
        for delivery_no, version in ((first_no, 1), (second_no, 2)):
            verify = svc.verify(delivery_no)
            assert verify["ok"] is True
            assert verify["legacy"]["restorable"] or verify["manifest"]

        # 回滚后旧客户端内容不变。
        rollback = svc.rollback(view_name=view)
        assert rollback["ok"] is True
        after = svc.download(view_name=view, read_version=1)
        assert after["delivery_no"] == first_no
        assert after["content"] == old["content"]

    def test_paused_delivery_blocks_new_read_version(self, pipeline, sample_raw_logs):
        result = _make_delivery(pipeline, "block-src", sample_raw_logs)
        view = result["export"]["导出范围"]
        svc = delivery_service()
        row = svc.record_legacy(view, "/exports/blocked.csv",
                                content="group,count\nINFO,1\n", delivery_no="OLD-BLOCK")
        svc.bind_source("OLD-BLOCK", path="/exports/blocked.csv",
                        content="2026-01-01T00:00:00|INFO|blocked\n")
        svc.pause_migration("OLD-BLOCK")
        svc.migrate(["OLD-BLOCK"], resolve_saved_refs=False)
        # 暂停项没有封存清单，不能冒充可读的 v2 交付。
        assert svc.snapshot("OLD-BLOCK", 2)["ok"] is False
        # 但旧结构照常可读。
        old = svc.snapshot("OLD-BLOCK", 1)
        assert old["ok"] is True
        assert old["content"] == "group,count\nINFO,1\n"

    def test_blocked_delivery_can_backfill_then_migrate(self, pipeline, tmp_path):
        svc = delivery_service()
        svc.record_legacy("backfill-view", "/exports/unknown.csv",
                          content="group,count\nINFO,1\n", delivery_no="OLD-BF")
        first = svc.migrate(["OLD-BF"], resolve_saved_refs=False)
        assert "OLD-BF" in [b["delivery_no"] for b in first["blocked"]]
        assert svc.snapshot("OLD-BF", 2)["ok"] is False

        # 补上真实来源内容后，重跑同一交付编号的迁移即可封存。
        source = tmp_path / "bf.log"
        source.write_text("time|level|message\nt1|INFO|a\nt2|WARN|b\n",
                          encoding="utf-8")
        svc.bind_source("OLD-BF", path=str(source),
                        content=source.read_text(encoding="utf-8"))
        svc.migrate(["OLD-BF"], resolve_saved_refs=False)
        manifest = svc._manifest_for("OLD-BF")
        assert manifest["已封存"] is True
        assert manifest["清单内容"]["source"]["resolution"] == "explicit_reference"
        snap = svc.snapshot("OLD-BF", 2)
        assert snap["ok"] is True
        assert snap["payload"]["grouping"]["basis"] == ["level"]

    def test_manifest_groups_match_aggregation_results(self, pipeline, sample_raw_logs):
        result = _make_delivery(pipeline, "consistent-src", sample_raw_logs)
        delivery_no = result["delivery"]["delivery_no"]
        payload = delivery_service().snapshot(delivery_no, 2)["payload"]
        aggregation_results = result["export"]["results"]
        assert set(payload["grouping"]["groups"]) == set(aggregation_results)
        for key, group in payload["grouping"]["groups"].items():
            assert group["count"] == aggregation_results[key]["count"]

    def test_migration_does_not_touch_other_views(self, pipeline, sample_raw_logs):
        keep = _make_delivery(pipeline, "keep-src", sample_raw_logs[:2], "keep-view")
        svc = delivery_service()
        svc.record_legacy("other-view", "/exports/other.csv",
                          content="group,count\nINFO,1\n", delivery_no="OLD-OTHER")
        before = svc.snapshot(keep["delivery"]["delivery_no"], 2)["snapshot_digest"]
        svc.migrate(["OLD-OTHER"], resolve_saved_refs=False)
        after = svc.snapshot(keep["delivery"]["delivery_no"], 2)["snapshot_digest"]
        assert before == after


def delivery_service():
    from app.services.delivery import delivery
    # 每轮测试重置内部草稿状态但保留落盘目录（目录由 fixture 每次新建）。
    return delivery
