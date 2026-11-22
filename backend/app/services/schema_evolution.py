"""Schema 演进领域服务：管理湖表 Schema 的可控演进、兼容性检查和回滚。

上游依赖快照版本模块提供变更前快照，下游联动回填任务进行存量数据迁移。
兼容性检查是核心：变更类型与兼容级别必须匹配，不兼容变更需要回填。

提交审批时引用的变更前快照，必须是本湖表已生成完成、内容落库齐全的
可用快照；只登记了版本号、内容还没确认的占位快照不能进入审批引用。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.snapshot_version import SnapshotVersionService
from app.store import store


# 合法兼容级别及其允许的变更类型
COMPATIBILITY_RULES: dict[str, set[str]] = {
    "向后兼容": {"添加字段", "修改默认值", "添加索引"},
    "向前兼容": {"删除字段", "放宽约束"},
    "不兼容": {"修改字段类型", "重命名字段", "删除字段"},
}


class SchemaEvolutionService(BaseModuleService):
    MODULE = "schema_evolution"
    ENTITY = "Schema变更"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["湖表名", "变更类型", "变更字段", "兼容级别", "变更前快照"]
    REQUIRED_FIELDS = ["湖表名", "变更类型", "变更字段"]
    STATUS_ORDER = ["草稿", "待审批", "已执行", "已回滚"]
    ACTION_RULES = {"提交审批": "待审批", "执行变更": "已执行"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self._snapshots = SnapshotVersionService()

    def submit_approval(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """提交审批：兼容性检查 + 变更前快照可用性核对。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "Schema 变更不存在"
        if entry.get("status") != "草稿":
            return None, f"Schema 变更状态为{entry.get('status')}，不可提交审批"

        change_type = entry.get("变更类型", "")
        compat_level = entry.get("兼容级别", "")

        # 兼容性检查
        allowed = COMPATIBILITY_RULES.get(compat_level, set())
        if change_type not in allowed:
            return (
                None,
                f"兼容性检查失败：{compat_level} 不允许 {change_type}，"
                f"允许的操作：{'、'.join(allowed) if allowed else '无'}",
            )

        # 核对变更前快照：必须引用本湖表已生成完成、内容落库齐全的版本，
        # 同号但属于其他湖表的快照、或本表仍在生成中的占位快照都不能用
        snapshot_ref = entry.get("变更前快照", "")
        if snapshot_ref:
            table_name = entry.get("湖表名", "")
            matches = self._snapshots.find_snapshots(table_name, snapshot_ref)
            if not matches:
                return None, f"湖表 {table_name} 不存在版本 {snapshot_ref} 的快照，不可提交审批"
            available = [s for s in matches if s.get("status") == "可用"]
            if not available:
                return None, f"变更前快照 {snapshot_ref} 仍在生成中，内容未确认，不可提交审批"
            if not any(self._snapshots.is_snapshot_complete(s) for s in available):
                return None, f"变更前快照 {snapshot_ref} 数据未落库齐全，不可提交审批"

        entry["status"] = "待审批"
        entry["pending"] = True
        entry["abnormal"] = False

        store.log_event("schema_submitted", "schema_evolution", "snapshot_version", {
            "entry_id": entry_id,
            "table_name": entry.get("湖表名", ""),
            "change_type": change_type,
            "compat_level": compat_level,
        })

        return entry, f"Schema 变更 {entry.get('湖表名')} - {change_type} 已提交审批，兼容级别={compat_level}"

    def snapshot_context(self, entry_id: int) -> dict[str, Any] | None:
        """审批详情：从变更前快照的引用读出当时冻结的湖表结构。

        已提交的变更可以据此复查前置结构：关联表、快照版本、
        当时的分区键和存储格式都来自冻结内容，不受后续登记修改影响。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None

        table_name = entry.get("湖表名", "")
        snapshot_ref = entry.get("变更前快照", "")
        context: dict[str, Any] = {
            "entry_id": entry_id,
            "湖表名": table_name,
            "变更前快照": snapshot_ref,
            "快照状态": "未引用",
            "内容完整": False,
            "分区键": "",
            "存储格式": "",
            "冻结内容": [],
        }
        if not snapshot_ref:
            return context

        matches = self._snapshots.find_snapshots(table_name, snapshot_ref)
        if not matches:
            context["快照状态"] = "未找到"
            return context

        # 优先取内容落库齐全的可用版本
        snapshot = next(
            (s for s in matches if self._snapshots.is_snapshot_complete(s)),
            matches[0],
        )
        content = self._snapshots.snapshot_content(snapshot) or []
        context["快照状态"] = snapshot.get("status", "")
        context["内容完整"] = self._snapshots.is_snapshot_complete(snapshot)
        context["冻结内容"] = content
        if content:
            first = content[0]
            context["分区键"] = first.get("分区键", "")
            context["存储格式"] = first.get("存储格式", "")
        return context

    def execute_change(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """执行 Schema 变更：联动回填任务进行存量数据迁移。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "Schema 变更不存在"
        if entry.get("status") != "待审批":
            return None, f"Schema 变更状态为{entry.get('status')}，不可执行"

        table_name = entry.get("湖表名", "")
        change_type = entry.get("变更类型", "")
        compat_level = entry.get("兼容级别", "")

        # 自身状态流转
        entry["status"] = "已执行"
        entry["pending"] = False
        entry["abnormal"] = False

        # 如果是不兼容变更，触发回填任务
        if compat_level == "不兼容":
            backfill_jobs = store.find_by_field("backfill_job", "目标湖表", table_name)
            if backfill_jobs:
                for bj in backfill_jobs:
                    if bj.get("status") == "待调度":
                        bj["pending"] = True

            store.log_event("schema_to_backfill", "schema_evolution", "backfill_job", {
                "entry_id": entry_id,
                "table_name": table_name,
                "change_type": change_type,
                "compat_level": compat_level,
                "affected_jobs": len(backfill_jobs),
            })

        store.log_event("schema_executed", "schema_evolution", "lake_table", {
            "entry_id": entry_id,
            "table_name": table_name,
            "change_type": change_type,
        })

        return entry, f"Schema 变更 {table_name} - {change_type} 已执行"

    def rollback_change(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """回滚 Schema 变更：恢复到变更前快照。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "Schema 变更不存在"
        if entry.get("status") != "已执行":
            return None, f"Schema 变更状态为{entry.get('status')}，不可回滚"

        snapshot_ref = entry.get("变更前快照", "")
        table_name = entry.get("湖表名", "")

        # 尝试恢复快照：按本湖表 + 版本号定位已冻结的快照内容
        restored = False
        if snapshot_ref:
            for snapshot in self._snapshots.find_snapshots(table_name, snapshot_ref):
                if self._snapshots.snapshot_content(snapshot):
                    restored = True
                    break

        if restored:
            entry["status"] = "已回滚"
            entry["pending"] = False
            entry["abnormal"] = False
            store.log_event("schema_rolled_back", "schema_evolution", "snapshot_version", {
                "entry_id": entry_id,
                "table_name": table_name,
                "snapshot_ref": snapshot_ref,
            })
            return entry, f"Schema 变更回滚到快照 {snapshot_ref}"

        entry["status"] = "已回滚"
        entry["pending"] = False
        entry["abnormal"] = True
        return entry, f"Schema 变更已回滚（快照 {snapshot_ref} 无法精确恢复）"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "提交审批":
            return self.submit_approval(entry_id)
        if action == "执行变更":
            return self.execute_change(entry_id)
        return super().run_action(entry_id, action)
