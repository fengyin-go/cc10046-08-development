"""跨批去重与交付接口：批次来源、去重数据集、领取页、交付回执和撤回任务。

这里刻意不经过通用资源路由：通用路由面向「模块表格」的增删改查，而这一组接口
围绕的是同一份跨批次状态，需要事务式地一起读写来源、交付和回执。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.dedup_registry import DedupError, dedup_registry

router = APIRouter(prefix="/api/dedup", tags=["跨批去重与交付"])


class BatchPayload(BaseModel):
    batch_id: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    source_config: str = ""


class ClaimPayload(BaseModel):
    product_key: str
    quote: str | int | float | None = None


class DeliverPayload(BaseModel):
    limit: int | None = Field(default=None, ge=0)


class WithdrawPayload(BaseModel):
    batch_id: str
    step_size: int = Field(default=1, ge=1)
    provenance_ids: list[str] | None = None


class ResumePayload(BaseModel):
    steps: int = Field(default=1, ge=1)


def _handle(error: DedupError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.message)


@router.get("/batches")
def list_batches() -> dict[str, Any]:
    """已登记的采集批次及其来源统计。"""
    items = dedup_registry.list_batches()
    return {"items": items, "total": len(items)}


@router.post("/batches")
def ingest_batch(payload: BatchPayload) -> dict[str, Any]:
    """登记采集批次；同一批次重发按内容幂等，不会多记来源。"""
    try:
        return dedup_registry.ingest_batch(
            payload.batch_id, payload.rows, source_config=payload.source_config)
    except DedupError as error:
        raise _handle(error) from error


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, Any]:
    """回查批次：来源行、撤回任务、该批次参与过的交付与回执。"""
    try:
        return dedup_registry.batch_detail(batch_id)
    except DedupError as error:
        raise _handle(error) from error


@router.post("/batches/{batch_id}/replay")
def replay_batch(batch_id: str) -> dict[str, Any]:
    """重放批次保存的原始行；已撤回的来源不会因此恢复。"""
    try:
        return dedup_registry.replay_batch(batch_id)
    except DedupError as error:
        raise _handle(error) from error


@router.get("/dataset")
def get_dataset() -> dict[str, Any]:
    """有来源的去重数据集：每个业务键一条选中记录，附带备选来源。"""
    return dedup_registry.dataset()


@router.get("/board")
def get_board() -> dict[str, Any]:
    """值班领取页：每个商品当前可领取、已交付或核对中的状态。"""
    return dedup_registry.board()


@router.post("/claim")
def claim(payload: ClaimPayload) -> dict[str, Any]:
    """领取单个商品；同一商品同时只允许一条有效交付。"""
    try:
        return dedup_registry.claim(payload.product_key, quote=payload.quote)
    except DedupError as error:
        raise _handle(error) from error


@router.post("/deliver")
def deliver(payload: DeliverPayload) -> dict[str, Any]:
    """按去重数据集执行一轮交付，返回本轮发出的回执。"""
    return dedup_registry.deliver_round(limit=payload.limit)


@router.get("/deliveries")
def list_deliveries() -> dict[str, Any]:
    items = dedup_registry.list_deliveries()
    return {"items": items, "total": len(items)}


@router.get("/deliveries/{delivery_id}")
def get_delivery(delivery_id: str) -> dict[str, Any]:
    try:
        return dedup_registry.get_delivery(delivery_id)
    except DedupError as error:
        raise _handle(error) from error


@router.get("/receipts")
def list_receipts() -> dict[str, Any]:
    """历史回执：固定当时发出的行数据和来源，不受后续撤回影响。"""
    items = dedup_registry.list_receipts()
    return {"items": items, "total": len(items)}


@router.get("/receipts/{receipt_id}")
def get_receipt(receipt_id: str) -> dict[str, Any]:
    try:
        receipt = dedup_registry.get_receipt(receipt_id)
    except DedupError as error:
        raise _handle(error) from error
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"交付回执 {receipt_id} 不存在")
    return receipt


@router.get("/withdrawals")
def list_withdrawals() -> dict[str, Any]:
    items = dedup_registry.list_withdrawals()
    return {"items": items, "total": len(items)}


@router.post("/withdrawals")
def start_withdrawal(payload: WithdrawPayload) -> dict[str, Any]:
    """发起撤回来源；可分批推进，未完成前该商品在领取页显示为核对中。"""
    try:
        return dedup_registry.start_withdrawal(
            payload.batch_id,
            step_size=payload.step_size,
            provenance_ids=payload.provenance_ids,
        )
    except DedupError as error:
        raise _handle(error) from error


@router.get("/withdrawals/{job_id}")
def get_withdrawal(job_id: str) -> dict[str, Any]:
    try:
        return dedup_registry.get_withdrawal(job_id)
    except DedupError as error:
        raise _handle(error) from error


@router.post("/withdrawals/{job_id}/resume")
def resume_withdrawal(job_id: str, payload: ResumePayload) -> dict[str, Any]:
    """续跑未完成的撤回任务。"""
    try:
        return dedup_registry.resume_withdrawal(job_id, steps=payload.steps)
    except DedupError as error:
        raise _handle(error) from error


@router.get("/snapshot")
def snapshot() -> dict[str, Any]:
    """完整状态快照：批次来源、去重数据集、交付、回执与撤回任务。"""
    return {
        "batches": dedup_registry.list_batches(),
        "dataset": dedup_registry.dataset(),
        "deliveries": dedup_registry.list_deliveries(),
        "receipts": dedup_registry.list_receipts(),
        "withdrawals": dedup_registry.list_withdrawals(),
    }


@router.get("/provenance")
def list_provenance(
    batch_id: str | None = Query(default=None),
    product_key: str | None = Query(default=None),
    withdrawn: bool | None = Query(default=None),
) -> dict[str, Any]:
    """来源明细：可回答「同一商品的两个报价各出自哪次采集」。"""
    items = dedup_registry.list_provenance(
        batch_id=batch_id, product_key=product_key, withdrawn=withdrawn)
    return {"items": items, "total": len(items)}
