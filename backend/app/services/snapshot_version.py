"""快照版本领域服务：管理湖表数据的快照版本和基于时间点的数据恢复。

上游接收 schema 演进和回填任务的事件，下游为治理报告提供版本追溯数据。
每个快照带版本号和时间戳，支持废弃管理和版本对比。

生成快照时必须一次性读出湖表登记信息并整体冻结：列表里显示“可用”的快照，
其登记内容必须已经落库齐全，否则只是空壳快照，不能进入 Schema 审批引用。
生成中断（仍是生成中，或可用但内容缺失）时可重新执行生成动作恢复，
恢复后这份版本才能继续被审批核对。
"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 冻结内容字段：取自湖表登记信息，供 Schema 审批复查前置结构
SNAPSHOT_CONTENT_FIELDS = ["表名", "所属业务域", "存储格式", "分区键", "当前分片数"]


class SnapshotVersionService(BaseModuleService):
    MODULE = "snapshot_version"
    ENTITY = "快照版本"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["湖表名", "快照时间戳", "版本号", "记录数", "快照类型"]
    REQUIRED_FIELDS = ["湖表名", "快照时间戳", "版本号"]
    STATUS_ORDER = ["生成中", "可用", "已废弃"]
    ACTION_RULES = {"生成快照": "可用", "废弃快照": "已废弃"}
    NEGATIVE_ACTIONS = []

    # ---- 快照内容定位与核对 ----

    @staticmethod
    def content_key(table_name: str, version_no: str) -> str:
        """快照内容标识：同湖表同版本固定同一个 key，恢复重生成时整体覆盖。"""
        return f"{table_name}@{version_no}"

    def find_snapshots(self, table_name: str, version_no: str) -> list[dict[str, Any]]:
        """按湖表名 + 版本号定位快照登记，避免跨表串用同号快照。"""
        return [
            sv for sv in store.rows(self.MODULE)
            if sv.get("湖表名") == table_name
            and str(sv.get("版本号", "")) == str(version_no)
        ]

    def snapshot_content(self, entry: dict[str, Any]) -> list[dict[str, Any]] | None:
        """读取快照冻结的登记内容；从未冻结过时返回 None。"""
        key = entry.get("快照内容键")
        if not key:
            return None
        return store.get_snapshot(str(key))

    def is_snapshot_complete(self, entry: dict[str, Any]) -> bool:
        """快照数据是否落库齐全：状态可用且冻结内容确实存在。"""
        if entry.get("status") != "可用":
            return False
        return bool(self.snapshot_content(entry))

    def generate_snapshot(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """生成快照版本：读出湖表登记信息并整体冻结，交付可用湖表快照。

        生成中断后恢复：状态仍是生成中，或可用但内容未落库齐全时，
        允许重新执行本动作补齐冻结内容；内容齐全的可用快照不可重复生成。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "快照版本不存在"
        status = entry.get("status")
        if status == "已废弃":
            return None, "快照版本已废弃，不可再生成"
        if status == "可用" and self.is_snapshot_complete(entry):
            return (
                None,
                f"湖表 {entry.get('湖表名')} 版本 {entry.get('版本号')} "
                "快照已生成且内容齐全，不可重复生成",
            )

        table_name = entry.get("湖表名", "")
        version_no = entry.get("版本号", "")

        # 检查同湖表是否有同名版本快照
        existing = [
            sv for sv in store.rows(self.MODULE)
            if sv.get("湖表名") == table_name
            and str(sv.get("版本号", "")) == str(version_no)
            and sv.get("id") != entry_id
        ]
        if existing:
            return None, f"湖表 {table_name} 已存在版本 {version_no} 的快照，不可重复生成"

        # 一次性读出湖表登记信息；读不到实际登记数据就只能交付空壳，直接拒绝
        registrations = store.find_by_field("lake_table", "表名", table_name)
        if not registrations:
            return None, f"湖表 {table_name} 尚未登记，无法生成可用快照"

        # 整体深拷贝冻结：之后湖表登记再修改，也不会改变这份已冻结版本
        frozen_rows = [
            {field: row.get(field, "") for field in SNAPSHOT_CONTENT_FIELDS}
            for row in registrations
        ]
        snapshot_key = store.freeze_snapshot(
            self.content_key(table_name, version_no), frozen_rows)

        entry["status"] = "可用"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["快照类型"] = "全量快照"
        entry["记录数"] = str(len(frozen_rows))
        entry["快照时间戳"] = datetime.datetime.now().isoformat(timespec="seconds")
        entry["快照内容键"] = snapshot_key

        store.log_event("snapshot_created", "snapshot_version", "lake_table", {
            "snapshot_id": entry_id,
            "table_name": table_name,
            "version_no": version_no,
            "snapshot_key": snapshot_key,
            "frozen_rows": len(frozen_rows),
        })

        return entry, (
            f"湖表 {table_name} 版本 {version_no} 快照已生成，"
            f"冻结 {len(frozen_rows)} 条登记信息，key={snapshot_key}"
        )

    def discard_snapshot(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """废弃快照版本：检查是否有后续快照依赖。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "快照版本不存在"
        if entry.get("status") != "可用":
            return None, f"快照版本状态为{entry.get('status')}，不可废弃"

        table_name = entry.get("湖表名", "")
        version_no = entry.get("版本号", "")

        # 检查 schema 演进是否引用了此版本（同湖表 + 同版本号才算引用）
        schema_changes = [
            sc for sc in store.rows("schema_evolution")
            if str(sc.get("变更前快照", "")) == str(version_no)
            and sc.get("湖表名") == table_name
        ]
        pending_changes = [sc for sc in schema_changes if sc.get("status") in ("草稿", "待审批")]
        if pending_changes:
            return None, f"版本 {version_no} 仍被 {len(pending_changes)} 个待处理 Schema 变更引用，不可废弃"

        entry["status"] = "已废弃"
        entry["pending"] = False
        entry["abnormal"] = False

        store.log_event("snapshot_discarded", "snapshot_version", "schema_evolution", {
            "snapshot_id": entry_id,
            "table_name": table_name,
            "version_no": version_no,
        })

        return entry, f"快照版本 {version_no} 已废弃"

    def available_snapshots_for_table(self, table_name: str) -> list[dict[str, Any]]:
        """查询指定湖表的可用快照列表（内容落库齐全的才算可用）。"""
        return [
            sv for sv in store.rows(self.MODULE)
            if sv.get("湖表名") == table_name and self.is_snapshot_complete(sv)
        ]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "生成快照":
            return self.generate_snapshot(entry_id)
        if action == "废弃快照":
            return self.discard_snapshot(entry_id)
        return super().run_action(entry_id, action)
