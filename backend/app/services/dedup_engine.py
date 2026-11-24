"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class DedupEngineService(BaseModuleService):
    MODULE = "dedup_engine"
    ENTITY = "去重策略"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["策略标识", "业务键列", "排序字段", "保留规则", "时间窗口", "相似阈值", "哈希算法", "输出目标"]
    REQUIRED_FIELDS = ["策略标识", "业务键列", "排序字段"]
    STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "启用策略": "已启用", "停用策略": "已停用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    def domain_dedup(self, items: list[dict], key_fields: list[str]) -> dict:
        """对单次输入做无状态去重（保留首次出现的行）。"""
        from app.compute import hash_dedup
        deduped, removed = hash_dedup(items, key_fields)

        return {
            "original_count": len(items),
            "deduped_count": len(deduped),
            "removed_count": removed,
            "deduped_items": deduped,
        }

    def domain_dedup_across_batches(self) -> dict:
        """跨批去重：按业务键汇总所有批次，保留最早入库且来源仍有效的报价。

        与 ``domain_dedup`` 的区别是这里保留来源：每个选中行都能回查到它出自
        哪一次采集，同一商品的其它有效报价作为备选来源一并保留。
        """
        from app.services.dedup_registry import dedup_registry
        return dedup_registry.dataset()
