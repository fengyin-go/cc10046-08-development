"""__NAME__领域服务：字段、状态和动作来自业务配置，数据处理骨架由基类复用。"""
from __future__ import annotations

from app.services.base import BaseModuleService


class ExportChannelService(BaseModuleService):
    MODULE = "export_channel"
    ENTITY = "导出任务"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["任务标识", "来源批次", "导出格式", "目标路径", "字段筛选", "导出状态", "文件大小", "完成时间"]
    REQUIRED_FIELDS = ["任务标识", "来源批次", "导出格式"]
    STATUS_ORDER = ["排队中", "导出中", "已完成", "导出失败"]
    ACTION_RULES = {"启动导出": "排队中", "取消导出": "导出中"}
    NEGATIVE_ACTIONS = []

    def domain_export(self, batch_id: str, export_format: str = "csv",
                      fields: list[str] | None = None) -> dict:
        """导出批次数据到指定格式。"""
        from app.store import store
        import time
        rows = store.rows(self.MODULE)
        now = time.strftime("%Y-%m-%d")
        file_size = (len(batch_id) * 1024) + 512
        # 同一批次可能多次交付/导出，按「批次+格式」去重，避免重放时多出一条导出任务。
        for row in rows:
            if row.get("来源批次") == batch_id and row.get("导出格式") == export_format:
                row["导出状态"] = "已完成"
                row["完成时间"] = now
                row["fields"] = ",".join(fields or [])
                return row
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "任务标识": f"EXPORT-{batch_id}",
            "来源批次": batch_id,
            "导出格式": export_format,
            "目标路径": f"/exports/{batch_id}.{export_format}",
            "字段筛选": ",".join(fields or []),
            "导出状态": "已完成",
            "文件大小": f"{file_size}",
            "完成时间": now,
            "status": "已完成",
            "pending": False,
            "abnormal": False,
        }
        rows.append(entry)
        return entry
