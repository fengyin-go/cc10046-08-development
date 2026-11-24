"""跨批去重领域仓库：持久保存带来源的去重数据集、交付记录与历史回执。

与 ``app/store.py`` 一样，这里用一个进程内单例保存状态；与临时去重计算不同的是，
写入这里的来源行、交付记录和回执不会在每次请求后消失：

* 每一行原始数据都有 ``provenance`` 记录，能回答「这条报价出自哪个采集批次」；
* 同一批次重发只做幂等合并，不会多记一份来源；
* 撤回来源按批次生成可续跑的任务，逐行推进，完成后不会因为重放而复活；
* 每次交付生成不可变回执，回执里冻结当时发出的行和来源，后续撤回/重放都改不到它。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import time
from typing import Any

STATE_VERSION = 1

# 行数据的字段别名：不同采集批次可能用不同列名，落库前统一成业务键和报价。
PRODUCT_KEY_ALIASES = (
    "product_key", "product_id", "product", "sku", "goods_id", "item_id",
    "商品", "商品标识", "商品编码", "商品名称",
)
QUOTE_KEY_ALIASES = ("quote", "price", "报价", "价格", "amount", "value")


class DedupError(Exception):
    """跨批去重的业务错误，路由层转换成对应的 HTTP 状态码。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _pick(row: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for key in aliases:
        if key not in row:
            continue
        value = row[key]
        if value is not None and str(value).strip() != "":
            return value
    return None


def _fingerprint(row: dict[str, Any]) -> str:
    """行内容指纹：同一批次重发时用它做幂等判断。"""
    payload = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_row(raw: dict[str, Any]) -> tuple[str, Any, dict[str, Any]]:
    product = _pick(raw, PRODUCT_KEY_ALIASES)
    quote = _pick(raw, QUOTE_KEY_ALIASES)
    if product is None:
        raise ValueError("缺少商品业务键（product_key / product_id / sku）")
    if quote is None:
        raise ValueError("缺少报价（quote / price）")
    fields = copy.deepcopy(raw)
    fields.setdefault("product_key", product)
    fields.setdefault("quote", quote)
    return _text(product), quote, fields


def _public_provenance(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "provenance_id": entry["provenance_id"],
        "batch_id": entry["batch_id"],
        "row_key": entry["row_key"],
        "row_no": entry["row_no"],
        "product_key": entry["product_key"],
        "quote": entry["quote"],
        "fields": copy.deepcopy(entry["fields"]),
        "ingested_at": entry["ingested_at"],
        "withdrawn": entry["withdrawn"],
        "withdrawn_at": entry["withdrawn_at"],
        "withdrawal_job": entry["withdrawal_job"],
    }


class DedupRegistry:
    """跨批去重与有来源交付的单一状态仓库。"""

    def __init__(self, state_path: str = "") -> None:
        self._lock = threading.RLock()
        self._state_path = state_path
        self.reset(persist=False)

    # ------------------------------------------------------------------ 持久化
    def _to_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "provenance": self._provenance,
            "provenance_order": self._provenance_order,
            "batch_fingerprints": {
                batch_id: sorted(items)
                for batch_id, items in self._batch_fingerprints.items()
            },
            "batches": self._batches,
            "batch_order": self._batch_order,
            "withdrawals": {
                job_id: {**job, "products": sorted(job.get("products", set()))}
                for job_id, job in self._withdrawals.items()
            },
            "withdrawal_order": self._withdrawal_order,
            "deliveries": self._deliveries,
            "delivery_order": self._delivery_order,
            "receipts": self._receipts,
            "receipt_order": self._receipt_order,
            "seq": self._seq,
            "counter": self._counter,
            "dataset_version": self._dataset_version,
        }

    def _load_state(self, state: dict[str, Any]) -> None:
        self._provenance = state.get("provenance", {})
        self._provenance_order = state.get("provenance_order", [])
        self._batch_fingerprints = {
            batch_id: set(items)
            for batch_id, items in state.get("batch_fingerprints", {}).items()
        }
        self._batches = state.get("batches", {})
        self._batch_order = state.get("batch_order", [])
        self._withdrawals = {
            job_id: {**job, "products": set(job.get("products", []))}
            for job_id, job in state.get("withdrawals", {}).items()
        }
        self._withdrawal_order = state.get("withdrawal_order", [])
        self._deliveries = state.get("deliveries", {})
        self._delivery_order = state.get("delivery_order", [])
        self._receipts = state.get("receipts", {})
        self._receipt_order = state.get("receipt_order", [])
        self._seq = int(state.get("seq", 0))
        self._counter = {**{
            "provenance": 0, "withdrawal": 0, "delivery": 0, "receipt": 0, "round": 0,
        }, **state.get("counter", {})}
        self._dataset_version = int(state.get("dataset_version", 0))

    def _persist(self) -> None:
        """有状态文件时原子写入；回执、来源和撤回进度因此能跨进程重启保留。"""
        if not self._state_path:
            return
        directory = os.path.dirname(os.path.abspath(self._state_path))
        if not directory:
            return
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(self._to_state(), ensure_ascii=False, default=str)
        handle, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fp:
                fp.write(payload)
            os.replace(tmp_path, self._state_path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def load(self) -> bool:
        """从状态文件恢复；文件不存在或损坏时保持当前内存状态。"""
        if not self._state_path or not os.path.exists(self._state_path):
            return False
        try:
            with open(self._state_path, "r", encoding="utf-8") as fp:
                state = json.load(fp)
        except (OSError, ValueError):
            return False
        with self._lock:
            self._load_state(state)
        return True

    # ------------------------------------------------------------------ 基础状态
    def reset(self, *, persist: bool = True) -> None:
        """清空仓库；测试和演示重新开始时使用。

        构造实例时不落盘（``persist=False``），否则会在 ``load()`` 之前把已有的
        状态文件覆盖成空仓库。
        """
        with self._lock:
            self._provenance: dict[str, dict[str, Any]] = {}
            self._provenance_order: list[str] = []
            self._batch_fingerprints: dict[str, set[str]] = {}
            self._batches: dict[str, dict[str, Any]] = {}
            self._batch_order: list[str] = []
            self._withdrawals: dict[str, dict[str, Any]] = {}
            self._withdrawal_order: list[str] = []
            self._deliveries: dict[str, dict[str, Any]] = {}
            self._delivery_order: list[str] = []
            self._receipts: dict[str, dict[str, Any]] = {}
            self._receipt_order: list[str] = []
            self._seq = 0
            self._counter = {
                "provenance": 0,
                "withdrawal": 0,
                "delivery": 0,
                "receipt": 0,
                "round": 0,
            }
            self._dataset_version = 0
            if persist:
                self._persist()

    def _next_id(self, kind: str, prefix: str, width: int = 6) -> str:
        self._counter[kind] += 1
        return f"{prefix}-{self._counter[kind]:0{width}d}"

    def _touch_dataset(self) -> None:
        self._dataset_version += 1

    # ------------------------------------------------------------------ 批次与来源
    def ingest_batch(
        self,
        batch_id: str,
        rows: list[dict[str, Any]],
        *,
        source_config: str = "",
        replay: bool = False,
    ) -> dict[str, Any]:
        """登记一次采集批次，把每一行的来源写入去重数据集。

        幂等规则：同一 ``batch_id`` 下内容相同的行只记一份来源；重发/重放既不会
        多出来源，也不会把已经撤回的来源恢复成有效来源。
        """
        batch_id = _text(batch_id)
        if not batch_id:
            raise DedupError("批次标识不能为空")
        if not isinstance(rows, list):
            raise DedupError("rows 必须是数组")
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                batch = {
                    "batch_id": batch_id,
                    "source_config": _text(source_config),
                    "first_ingested_at": _now(),
                    "last_ingested_at": _now(),
                    "ingest_count": 0,
                    "raw_rows": [],
                }
                self._batches[batch_id] = batch
                self._batch_order.append(batch_id)
            elif source_config:
                batch["source_config"] = _text(source_config)

            fingerprints = self._batch_fingerprints.setdefault(batch_id, set())
            batch["ingest_count"] += 1
            batch["last_ingested_at"] = _now()

            # 整批撤回过的批次，后续新进来的行也直接按撤回处理，
            # 从而保证「后续重放不得恢复已撤回的记录」。
            withdrawn_job = self._latest_withdrawal_job(batch_id)
            if withdrawn_job is not None and not withdrawn_job.get("full_batch", True):
                withdrawn_job = None
            ingested_ids: list[str] = []
            skipped = 0
            rejected: list[dict[str, Any]] = []
            for row_no, raw in enumerate(rows):
                if not isinstance(raw, dict):
                    rejected.append({"row_no": row_no, "reason": "行数据必须是对象"})
                    continue
                try:
                    product_key, quote, fields = _normalize_row(raw)
                except ValueError as exc:
                    rejected.append({"row_no": row_no, "reason": str(exc)})
                    continue
                fingerprint = _fingerprint(raw)
                if fingerprint in fingerprints:
                    skipped += 1
                    continue
                fingerprints.add(fingerprint)
                batch["raw_rows"].append(copy.deepcopy(raw))
                self._seq += 1
                provenance_id = self._next_id("provenance", "PV")
                entry = {
                    "provenance_id": provenance_id,
                    "batch_id": batch_id,
                    "row_key": fingerprint[:12],
                    "row_no": row_no,
                    "product_key": product_key,
                    "quote": quote,
                    "fields": fields,
                    "seq": self._seq,
                    "ingested_at": _now(),
                    "withdrawn": withdrawn_job is not None,
                    "withdrawn_at": _now() if withdrawn_job is not None else None,
                    "withdrawal_job": withdrawn_job["job_id"] if withdrawn_job else None,
                }
                self._provenance[provenance_id] = entry
                self._provenance_order.append(provenance_id)
                ingested_ids.append(provenance_id)
                if withdrawn_job is not None and withdrawn_job["status"] == "撤回中":
                    withdrawn_job.setdefault("products", set()).add(product_key)
            if ingested_ids:
                self._touch_dataset()
            self._persist()
            summary = self.batch_summary(batch_id)
            return {
                "batch": summary,
                "ingested": len(ingested_ids),
                "skipped": skipped,
                "rejected": rejected,
                "provenance_ids": ingested_ids,
                "source_count": summary["source_count"],
                "dataset_version": self._dataset_version,
                "replay": replay,
            }

    def list_batches(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.batch_summary(batch_id) for batch_id in self._batch_order]

    def batch_summary(self, batch_id: str) -> dict[str, Any]:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise DedupError(f"采集批次 {batch_id} 不存在", 404)
            entries = [
                self._provenance[pid]
                for pid in self._provenance_order
                if self._provenance[pid]["batch_id"] == batch_id
            ]
            withdrawn = sum(1 for entry in entries if entry["withdrawn"])
            total = len(entries)
            if total == 0:
                status = "空批次"
            elif withdrawn == 0:
                status = "已入库"
            elif withdrawn < total:
                status = "部分撤回"
            else:
                status = "已撤回"
            return {
                "batch_id": batch_id,
                "source_config": batch["source_config"],
                "status": status,
                "source_count": total,
                "valid_count": total - withdrawn,
                "withdrawn_count": withdrawn,
                "ingest_count": batch["ingest_count"],
                "replay_count": max(batch["ingest_count"] - 1, 0),
                "first_ingested_at": batch["first_ingested_at"],
                "last_ingested_at": batch["last_ingested_at"],
            }

    def batch_detail(self, batch_id: str) -> dict[str, Any]:
        """批次回查：来源行、撤回任务和该批次参与过的交付/回执。"""
        with self._lock:
            summary = self.batch_summary(batch_id)
            entries = [
                _public_provenance(self._provenance[pid])
                for pid in self._provenance_order
                if self._provenance[pid]["batch_id"] == batch_id
            ]
            jobs = [
                self._public_withdrawal(job)
                for job in self._withdrawals.values()
                if job["batch_id"] == batch_id
            ]
            deliveries = [
                self._public_delivery(delivery)
                for delivery in self._deliveries.values()
                if any(
                    self._provenance.get(pid, {}).get("batch_id") == batch_id
                    for pid in delivery["provenance_ids"]
                )
            ]
            receipts = [
                self.get_receipt(receipt_id)
                for receipt_id in self._receipt_order
                if any(
                    item.get("batch_id") == batch_id
                    for item in self._receipts[receipt_id].get("provenance", [])
                )
            ]
            return {
                "batch": summary,
                "provenance": entries,
                "withdrawals": jobs,
                "deliveries": deliveries,
                "receipts": [receipt for receipt in receipts if receipt],
            }

    def replay_batch(self, batch_id: str) -> dict[str, Any]:
        """重放批次：使用第一次登记时保存的原始行重新走幂等登记。"""
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                raise DedupError(f"采集批次 {batch_id} 不存在", 404)
            rows = copy.deepcopy(batch["raw_rows"])
        return self.ingest_batch(
            batch_id, rows, source_config=batch.get("source_config", ""), replay=True
        )

    def list_provenance(
        self,
        # 来源明细按业务键筛选，回答「同一商品的两个报价各出自哪次采集」。
        *,
        batch_id: str | None = None,
        product_key: str | None = None,
        withdrawn: bool | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            items: list[dict[str, Any]] = []
            for pid in self._provenance_order:
                entry = self._provenance[pid]
                if batch_id and entry["batch_id"] != batch_id:
                    continue
                if product_key and entry["product_key"] != product_key:
                    continue
                if withdrawn is not None and entry["withdrawn"] != withdrawn:
                    continue
                items.append(_public_provenance(entry))
            return items

    # ------------------------------------------------------------------ 数据集/看板
    def _valid_candidates(self, product_key: str) -> list[dict[str, Any]]:
        return [
            self._provenance[pid]
            for pid in self._provenance_order
            if self._provenance[pid]["product_key"] == product_key
            and not self._provenance[pid]["withdrawn"]
        ]

    def _select_candidate(self, product_key: str) -> dict[str, Any] | None:
        """去重保留规则：同一业务键取最早入库且来源仍有效的报价。"""
        candidates = self._valid_candidates(product_key)
        if not candidates:
            return None
        return min(candidates, key=lambda entry: (entry["seq"], entry["provenance_id"]))

    def _product_keys(self) -> list[str]:
        seen: set[str] = set()
        for pid in self._provenance_order:
            seen.add(self._provenance[pid]["product_key"])
        return sorted(seen)

    def _active_delivery(self, product_key: str) -> dict[str, Any] | None:
        for delivery_id in reversed(self._delivery_order):
            delivery = self._deliveries[delivery_id]
            if delivery["product_key"] == product_key and delivery["active"]:
                return delivery
        return None

    def _pending_withdrawal(self, product_key: str) -> dict[str, Any] | None:
        for job_id in reversed(self._withdrawal_order):
            job = self._withdrawals[job_id]
            if job["status"] == "撤回中" and product_key in job.get("products", set()):
                return job
        return None

    def _source_batches(self, product_key: str) -> list[str]:
        return sorted({
            self._provenance[pid]["batch_id"]
            for pid in self._provenance_order
            if self._provenance[pid]["product_key"] == product_key
        })

    def _board_item(self, product_key: str) -> dict[str, Any]:
        active = self._active_delivery(product_key)
        pending_job = self._pending_withdrawal(product_key)
        candidate = self._select_candidate(product_key)
        all_entries = [
            self._provenance[pid]
            for pid in self._provenance_order
            if self._provenance[pid]["product_key"] == product_key
        ]
        valid_entries = [entry for entry in all_entries if not entry["withdrawn"]]

        if pending_job is not None:
            state = "核对中"
            reason = f"来源批次 {pending_job['batch_id']} 的撤回任务尚未完成"
        elif active is not None:
            state = "已交付"
            reason = f"已交付报价 {active['quote']}（来源批次 {active['batch_id']}）"
        elif candidate is not None:
            state = "可领取"
            reason = (
                f"可领取报价 {candidate['quote']}（来源批次 {candidate['batch_id']}）"
            )
        else:
            state = "无可领取"
            reason = "该商品没有仍有效的来源，可能已全部撤回"

        claimable = None
        if candidate is not None and active is None and pending_job is None:
            claimable = {
                "quote": candidate["quote"],
                "batch_id": candidate["batch_id"],
                "provenance_id": candidate["provenance_id"],
                "row_key": candidate["row_key"],
            }
        delivered = None
        if active is not None:
            delivered = {
                "quote": active["quote"],
                "batch_id": active["batch_id"],
                "delivery_id": active["delivery_id"],
                "receipt_id": active["receipt_id"],
            }
        return {
            "product_key": product_key,
            "state": state,
            "reason": reason,
            "claimable_quote": claimable["quote"] if claimable else None,
            "claimable_source": claimable,
            "delivered_quote": delivered["quote"] if delivered else None,
            "delivered_source": delivered,
            "active_delivery_id": active["delivery_id"] if active else None,
            "active_receipt_id": active["receipt_id"] if active else None,
            "pending_withdrawal_id": pending_job["job_id"] if pending_job else None,
            "pending_withdrawal_batch": pending_job["batch_id"] if pending_job else None,
            "source_batches": self._source_batches(product_key),
            "source_count": len(all_entries),
            "valid_source_count": len(valid_entries),
            "withdrawn_source_count": len(all_entries) - len(valid_entries),
        }

    def board(self) -> dict[str, Any]:
        with self._lock:
            items = [self._board_item(product_key) for product_key in self._product_keys()]
            counts = {
                "claimable": sum(1 for item in items if item["state"] == "可领取"),
                "delivered": sum(1 for item in items if item["state"] == "已交付"),
                "reviewing": sum(1 for item in items if item["state"] == "核对中"),
                "unavailable": sum(1 for item in items if item["state"] == "无可领取"),
            }
            return {
                "items": items,
                "total": len(items),
                "counts": counts,
                "dataset_version": self._dataset_version,
                "generated_at": _now(),
            }

    def dataset(self) -> dict[str, Any]:
        """有来源的去重数据集：每个业务键一条选中记录，并附带全部来源。"""
        with self._lock:
            products: list[dict[str, Any]] = []
            for product_key in self._product_keys():
                candidate = self._select_candidate(product_key)
                active = self._active_delivery(product_key)
                valid_entries = sorted(
                    self._valid_candidates(product_key),
                    key=lambda entry: entry["seq"],
                )
                alternatives = [
                    _public_provenance(entry)
                    for entry in valid_entries
                    if candidate is None or entry["provenance_id"] != candidate["provenance_id"]
                ]
                products.append({
                    "product_key": product_key,
                    "selected": _public_provenance(candidate) if candidate else None,
                    "alternatives": alternatives,
                    "active_delivery_id": active["delivery_id"] if active else None,
                    "state": self._board_item(product_key)["state"],
                })
            return {
                "version": self._dataset_version,
                "products": products,
                "total": len(products),
                "provenance": [
                    _public_provenance(self._provenance[pid])
                    for pid in self._provenance_order
                ],
            }

    # ------------------------------------------------------------------ 撤回
    def _latest_withdrawal_job(self, batch_id: str) -> dict[str, Any] | None:
        for job_id in reversed(self._withdrawal_order):
            job = self._withdrawals[job_id]
            if job["batch_id"] == batch_id:
                return job
        return None

    def _withdrawal_products(self, batch_id: str) -> set[str]:
        return {
            self._provenance[pid]["product_key"]
            for pid in self._provenance_order
            if self._provenance[pid]["batch_id"] == batch_id
        }

    def _public_withdrawal(self, job: dict[str, Any]) -> dict[str, Any]:
        total = len(job["scope"])
        processed = min(job["cursor"], total)
        return {
            "job_id": job["job_id"],
            "batch_id": job["batch_id"],
            "status": job["status"],
            "step_size": job["step_size"],
            "total": total,
            "processed": processed,
            "remaining": max(total - processed, 0),
            "products": sorted(job.get("products", set())),
            "full_batch": job.get("full_batch", True),
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "completed_at": job["completed_at"],
        }

    def start_withdrawal(
        self,
        batch_id: str,
        *,
        step_size: int = 1,
        provenance_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """创建撤回来源任务。可指定 step_size 让任务分多次执行，便于续跑。"""
        batch_id = _text(batch_id)
        if not batch_id:
            raise DedupError("批次标识不能为空")
        with self._lock:
            if batch_id not in self._batches:
                raise DedupError(f"采集批次 {batch_id} 不存在", 404)
            existing = self._latest_withdrawal_job(batch_id)
            if existing is not None and existing["status"] == "撤回中":
                return {
                    "job": self._public_withdrawal(existing),
                    "created": False,
                    "message": "该批次已有进行中的撤回任务，可继续处理",
                }
            if provenance_ids:
                scope: list[str] = []
                for pid in provenance_ids:
                    entry = self._provenance.get(pid)
                    if entry is None:
                        raise DedupError(f"来源记录 {pid} 不存在", 404)
                    if entry["batch_id"] != batch_id:
                        raise DedupError(f"来源记录 {pid} 不属于批次 {batch_id}")
                    scope.append(pid)
            else:
                scope = [
                    pid for pid in self._provenance_order
                    if self._provenance[pid]["batch_id"] == batch_id
                ]
            job = {
                "job_id": self._next_id("withdrawal", "WDR", 4),
                "batch_id": batch_id,
                "status": "撤回中",
                "scope": scope,
                "cursor": 0,
                "step_size": max(1, int(step_size or 1)),
                "products": self._withdrawal_products(batch_id),
                "full_batch": not provenance_ids,
                "created_at": _now(),
                "updated_at": _now(),
                "completed_at": None,
            }
            self._withdrawals[job["job_id"]] = job
            self._withdrawal_order.append(job["job_id"])
            self._advance_withdrawal(job, steps=1)
            self._persist()
            return {
                "job": self._public_withdrawal(job),
                "created": True,
                "message": "撤回来源任务已创建"
                + ("并完成" if job["status"] == "已完成" else "，可继续处理"),
            }

    def _withdraw_entry(self, entry: dict[str, Any], job: dict[str, Any]) -> None:
        if entry["withdrawn"]:
            if not entry.get("withdrawal_job"):
                entry["withdrawal_job"] = job["job_id"]
            return
        entry["withdrawn"] = True
        entry["withdrawn_at"] = _now()
        entry["withdrawal_job"] = job["job_id"]

    def _advance_withdrawal(self, job: dict[str, Any], *, steps: int = 1) -> None:
        if job["status"] == "已完成":
            return
        step_size = max(1, int(job["step_size"]))
        for _ in range(max(1, int(steps))):
            if job["status"] == "已完成":
                break
            consumed = 0
            while consumed < step_size and job["cursor"] < len(job["scope"]):
                provenance_id = job["scope"][job["cursor"]]
                job["cursor"] += 1
                consumed += 1
                entry = self._provenance.get(provenance_id)
                if entry is not None:
                    self._withdraw_entry(entry, job)
            job["updated_at"] = _now()
            self._touch_dataset()
            if job["cursor"] >= len(job["scope"]):
                job["status"] = "已完成"
                job["completed_at"] = _now()
                job["updated_at"] = job["completed_at"]
            self._reconcile_deliveries()
            if job["status"] == "已完成":
                break

    def _reconcile_deliveries(self) -> None:
        """根据来源撤回进度更新交付状态，并保证一个商品最多一条有效交付。

        一条交付只有在它引用的来源行被撤回、并且处理这批来源的撤回任务已经
        完成之后才会失效；撤回任务还在跑的时候，商品在领取页显示为「核对中」，
        既不会让旧交付半途消失，也不会让下一轮交付提前发出。
        """
        for delivery_id in self._delivery_order:
            delivery = self._deliveries[delivery_id]
            if not delivery["active"]:
                continue
            withdrawn_jobs = [
                self._provenance.get(pid, {}).get("withdrawal_job")
                for pid in delivery["provenance_ids"]
                if self._provenance.get(pid, {}).get("withdrawn")
            ]
            if not withdrawn_jobs:
                delivery["status"] = "已交付"
                continue
            pending = any(
                self._withdrawals.get(job_id, {}).get("status") == "撤回中"
                for job_id in withdrawn_jobs
                if job_id
            )
            if pending:
                delivery["status"] = "核对中"
            else:
                delivery["active"] = False
                delivery["status"] = "已撤回"
                delivery["invalidated_at"] = _now()

        # 旧回执里引用的来源即使后来被撤回也只影响回执之外的当前交付；
        # 回执本身是冻结快照，这里不碰 self._receipts。

        seen: set[str] = set()
        for delivery_id in self._delivery_order:
            delivery = self._deliveries[delivery_id]
            if not delivery["active"]:
                continue
            if delivery["product_key"] in seen:
                delivery["active"] = False
                delivery["status"] = "已替代"
                delivery["invalidated_at"] = _now()
            else:
                seen.add(delivery["product_key"])

    def resume_withdrawal(self, job_id: str, *, steps: int = 1) -> dict[str, Any]:
        with self._lock:
            job = self._withdrawals.get(job_id)
            if job is None:
                raise DedupError(f"撤回任务 {job_id} 不存在", 404)
            self._advance_withdrawal(job, steps=steps)
            self._persist()
            return {
                "job": self._public_withdrawal(job),
                "message": "撤回任务已完成"
                if job["status"] == "已完成"
                else "已继续处理撤回任务",
            }

    def get_withdrawal(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._withdrawals.get(job_id)
            if job is None:
                raise DedupError(f"撤回任务 {job_id} 不存在", 404)
            return self._public_withdrawal(job)

    def list_withdrawals(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public_withdrawal(self._withdrawals[job_id])
                    for job_id in self._withdrawal_order]

    # ------------------------------------------------------------------ 交付与回执
    def _public_delivery(self, delivery: dict[str, Any]) -> dict[str, Any]:
        return {
            "delivery_id": delivery["delivery_id"],
            "product_key": delivery["product_key"],
            "quote": delivery["quote"],
            "batch_id": delivery["batch_id"],
            "provenance_ids": list(delivery["provenance_ids"]),
            "receipt_id": delivery["receipt_id"],
            "round_id": delivery["round_id"],
            "status": delivery["status"],
            "active": delivery["active"],
            "delivered_at": delivery["delivered_at"],
            "invalidated_at": delivery["invalidated_at"],
        }

    def _create_delivery(
        self,
        product_key: str,
        candidate: dict[str, Any],
        *,
        round_id: str,
    ) -> dict[str, Any]:
        delivery_id = self._next_id("delivery", "DLV", 4)
        receipt_id = self._next_id("receipt", "RCPT", 4)
        issued_at = _now()
        receipt = {
            "receipt_id": receipt_id,
            "delivery_id": delivery_id,
            "round_id": round_id,
            "product_key": product_key,
            "quote": candidate["quote"],
            "row": copy.deepcopy(candidate["fields"]),
            "provenance": [{
                "provenance_id": candidate["provenance_id"],
                "batch_id": candidate["batch_id"],
                "row_key": candidate["row_key"],
                "row_no": candidate["row_no"],
            }],
            "dataset_version": self._dataset_version,
            "issued_at": issued_at,
        }
        delivery = {
            "delivery_id": delivery_id,
            "product_key": product_key,
            "quote": candidate["quote"],
            "batch_id": candidate["batch_id"],
            "provenance_ids": [candidate["provenance_id"]],
            "receipt_id": receipt_id,
            "round_id": round_id,
            "status": "已交付",
            "active": True,
            "delivered_at": issued_at,
            "invalidated_at": None,
        }
        # 回执是冻结快照：单独深拷贝保存，之后任何撤回/重放都不会改动它。
        self._receipts[receipt_id] = copy.deepcopy(receipt)
        self._receipt_order.append(receipt_id)
        self._deliveries[delivery_id] = delivery
        self._delivery_order.append(delivery_id)
        return {"delivery": delivery, "receipt": receipt}

    def claim(self, product_key: str, *, quote: Any = None) -> dict[str, Any]:
        """领取单个商品：每个商品同时只能有一条有效交付。"""
        product_key = _text(product_key)
        if not product_key:
            raise DedupError("商品业务键不能为空")
        with self._lock:
            pending_job = self._pending_withdrawal(product_key)
            if pending_job is not None:
                raise DedupError(
                    f"商品 {product_key} 尚在核对（来源批次 {pending_job['batch_id']} "
                    "撤回处理中），暂不能领取",
                    409,
                )
            active = self._active_delivery(product_key)
            if active is not None:
                raise DedupError(
                    f"商品 {product_key} 已交付报价 {active['quote']}，"
                    "同一商品不能同时交付两条",
                    409,
                )
            candidate = self._select_candidate(product_key)
            if candidate is None:
                raise DedupError(f"商品 {product_key} 没有仍有效的来源，无法交付", 409)
            if quote is not None and _text(quote) != _text(candidate["quote"]):
                withdrawn = [
                    entry["quote"]
                    for entry in self._provenance.values()
                    if entry["product_key"] == product_key and entry["withdrawn"]
                ]
                hint = f"，报价 {quote} 的来源已撤回" if _text(quote) in {
                    _text(item) for item in withdrawn
                } else ""
                raise DedupError(
                    f"商品 {product_key} 当前只能交付报价 {candidate['quote']}"
                    f"（来源批次 {candidate['batch_id']}）{hint}",
                    409,
                )
            round_id = f"ROUND-CLAIM-{self._counter['delivery'] + 1:04d}"
            created = self._create_delivery(product_key, candidate, round_id=round_id)
            self._persist()
            return {
                "delivery": self._public_delivery(created["delivery"]),
                "receipt": copy.deepcopy(created["receipt"]),
                "message": f"商品 {product_key} 已交付报价 {candidate['quote']}",
            }

    def deliver_round(self, *, limit: int | None = None) -> dict[str, Any]:
        """执行一轮交付：每个可领取商品各交付一条，已交付/核对中的商品跳过。"""
        with self._lock:
            self._counter["round"] += 1
            round_id = f"ROUND-{self._counter['round']:04d}"
            delivered: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            product_keys = self._product_keys()
            if limit is not None:
                product_keys = product_keys[: max(0, int(limit))]
            for product_key in product_keys:
                pending_job = self._pending_withdrawal(product_key)
                if pending_job is not None:
                    skipped.append({
                        "product_key": product_key,
                        "reason": f"来源批次 {pending_job['batch_id']} 撤回处理中，尚在核对",
                    })
                    continue
                active = self._active_delivery(product_key)
                if active is not None:
                    skipped.append({
                        "product_key": product_key,
                        "reason": f"已交付报价 {active['quote']}，不能同时交付两条",
                    })
                    continue
                candidate = self._select_candidate(product_key)
                if candidate is None:
                    skipped.append({
                        "product_key": product_key,
                        "reason": "没有仍有效的来源",
                    })
                    continue
                created = self._create_delivery(product_key, candidate, round_id=round_id)
                delivered.append({
                    "delivery": self._public_delivery(created["delivery"]),
                    "receipt": copy.deepcopy(created["receipt"]),
                })
            self._persist()
            return {
                "round_id": round_id,
                "delivered": delivered,
                "skipped": skipped,
                "delivered_count": len(delivered),
                "skipped_count": len(skipped),
                "board": self.board(),
            }

    def list_deliveries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._public_delivery(self._deliveries[delivery_id])
                    for delivery_id in self._delivery_order]

    def get_delivery(self, delivery_id: str) -> dict[str, Any]:
        with self._lock:
            delivery = self._deliveries.get(delivery_id)
            if delivery is None:
                raise DedupError(f"交付记录 {delivery_id} 不存在", 404)
            result = self._public_delivery(delivery)
            result["receipt"] = self.get_receipt(delivery["receipt_id"])
            return result

    def list_receipts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(self._receipts[receipt_id])
                    for receipt_id in self._receipt_order]

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        with self._lock:
            receipt = self._receipts.get(receipt_id)
            if receipt is None:
                raise DedupError(f"交付回执 {receipt_id} 不存在", 404)
            return copy.deepcopy(receipt)


from app.config import settings as _settings

dedup_registry = DedupRegistry(_settings.dedup_state_path)

