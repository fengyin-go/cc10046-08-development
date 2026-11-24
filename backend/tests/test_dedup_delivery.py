"""跨批去重与有来源交付：用 18/21 两次报价验证来源、撤回与回执行为。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.dedup_registry import dedup_registry


@pytest.fixture()
def client():
    dedup_registry.reset()
    yield TestClient(app)
    dedup_registry.reset()


def _ingest(client, batch_id, quote, product="SKU-1", **extra):
    return client.post("/api/dedup/batches", json={
        "batch_id": batch_id,
        "source_config": f"source-{batch_id}",
        "rows": [{"product_key": product, "quote": quote, **extra}],
    })


def _board(client, product="SKU-1"):
    payload = client.get("/api/dedup/board").json()
    return next(item for item in payload["items"] if item["product_key"] == product)


def test_two_quotes_keep_their_own_batch_sources(client):
    first = _ingest(client, "BATCH-1", 18)
    second = _ingest(client, "BATCH-2", 21)
    assert first.status_code == 200 and second.status_code == 200

    provenance = client.get("/api/dedup/provenance", params={"product_key": "SKU-1"}).json()
    batches_by_quote = {item["quote"]: item["batch_id"] for item in provenance["items"]}
    assert batches_by_quote == {18: "BATCH-1", 21: "BATCH-2"}

    dataset = client.get("/api/dedup/dataset").json()
    product = dataset["products"][0]
    assert product["selected"]["quote"] == 18
    assert product["selected"]["batch_id"] == "BATCH-1"
    assert product["alternatives"][0]["quote"] == 21
    assert product["alternatives"][0]["batch_id"] == "BATCH-2"


def test_reingesting_same_batch_does_not_add_sources(client):
    _ingest(client, "BATCH-1", 18)
    replay = client.post("/api/dedup/batches/BATCH-1/replay").json()
    assert replay["ingested"] == 0
    assert replay["skipped"] == 1
    assert replay["source_count"] == 1

    batch = client.get("/api/dedup/batches/BATCH-1").json()
    assert batch["batch"]["source_count"] == 1
    assert batch["batch"]["replay_count"] == 1


def test_withdrawal_in_halves_can_resume(client):
    _ingest(client, "BATCH-1", 18, product="SKU-1")
    _ingest(client, "BATCH-1", 19, product="SKU-2")
    _ingest(client, "BATCH-2", 21, product="SKU-1")

    job = client.post("/api/dedup/withdrawals", json={
        "batch_id": "BATCH-1", "step_size": 1}).json()["job"]
    assert job["status"] == "撤回中"
    assert job["processed"] == 1 and job["remaining"] == 1

    # 撤回做到一半时，涉及的商品都显示核对中，下一轮不会发出任何一条。
    assert _board(client)["state"] == "核对中"
    assert client.post("/api/dedup/deliver", json={}).json()["delivered"] == []

    resumed = client.post(
        f"/api/dedup/withdrawals/{job['job_id']}/resume", json={"steps": 1}).json()
    assert resumed["job"]["status"] == "已完成"
    assert resumed["job"]["processed"] == 2


def test_after_withdraw_claimable_quote_becomes_21(client):
    _ingest(client, "BATCH-1", 18)
    _ingest(client, "BATCH-2", 21)
    client.post("/api/dedup/withdrawals", json={"batch_id": "BATCH-1"})

    board = _board(client)
    assert board["state"] == "可领取"
    assert board["claimable_quote"] == 21
    assert board["claimable_source"]["batch_id"] == "BATCH-2"

    # 不能再交付已撤回的 18，但 21 可以正常领取。
    blocked = client.post("/api/dedup/claim", json={"product_key": "SKU-1", "quote": 18})
    assert blocked.status_code == 409
    accepted = client.post("/api/dedup/claim", json={"product_key": "SKU-1", "quote": 21})
    assert accepted.status_code == 200
    assert accepted.json()["receipt"]["quote"] == 21


def test_one_product_never_delivered_twice(client):
    _ingest(client, "BATCH-1", 18)
    _ingest(client, "BATCH-2", 21)
    assert client.post("/api/dedup/claim", json={"product_key": "SKU-1"}).json()[
        "delivery"]["quote"] == 18
    duplicate = client.post("/api/dedup/claim", json={"product_key": "SKU-1", "quote": 21})
    assert duplicate.status_code == 409

    round_result = client.post("/api/dedup/deliver", json={}).json()
    assert round_result["delivered"] == []
    assert round_result["skipped"][0]["product_key"] == "SKU-1"


def test_replay_after_withdrawal_does_not_restore_sources(client):
    _ingest(client, "BATCH-1", 18)
    _ingest(client, "BATCH-2", 21)
    client.post("/api/dedup/withdrawals", json={"batch_id": "BATCH-1"})

    replay = client.post("/api/dedup/batches/BATCH-1/replay").json()
    assert replay["ingested"] == 0
    assert replay["batch"]["withdrawn_count"] == 1
    assert replay["batch"]["valid_count"] == 0
    assert _board(client)["claimable_quote"] == 21

    # 已撤回批次里新出现的行也按撤回处理，不会变成有效来源。
    later = _ingest(client, "BATCH-1", 17).json()
    assert later["batch"]["withdrawn_count"] == 2
    assert later["batch"]["valid_count"] == 0
    assert _board(client)["claimable_quote"] == 21


def test_receipt_is_a_frozen_snapshot(client):
    _ingest(client, "BATCH-1", 18)
    _ingest(client, "BATCH-2", 21)
    claimed = client.post("/api/dedup/claim", json={"product_key": "SKU-1"}).json()
    receipt_id = claimed["receipt"]["receipt_id"]
    assert claimed["receipt"]["quote"] == 18

    client.post("/api/dedup/withdrawals", json={"batch_id": "BATCH-1"})
    client.post("/api/dedup/batches/BATCH-1/replay")

    # 刷新历史回执，仍是当时发出的 18 和它当时的来源批次。
    receipt = client.get(f"/api/dedup/receipts/{receipt_id}").json()
    assert receipt["quote"] == 18
    assert receipt["row"]["quote"] == 18
    assert receipt["provenance"][0]["batch_id"] == "BATCH-1"
    assert receipt["provenance"][0]["provenance_id"] == claimed["receipt"]["provenance"][0]["provenance_id"]

    listed = client.get("/api/dedup/receipts").json()["items"]
    assert [item["quote"] for item in listed] == [18]


def test_second_batch_sources_remain_queryable_after_withdrawal(client):
    _ingest(client, "BATCH-1", 18)
    _ingest(client, "BATCH-2", 21)
    round_result = client.post("/api/dedup/deliver", json={}).json()
    assert round_result["delivered"][0]["delivery"]["batch_id"] == "BATCH-1"

    client.post("/api/dedup/withdrawals", json={"batch_id": "BATCH-1"})
    next_round = client.post("/api/dedup/deliver", json={}).json()
    assert next_round["delivered"][0]["delivery"]["quote"] == 21
    assert next_round["delivered"][0]["delivery"]["batch_id"] == "BATCH-2"

    batch_two = client.get("/api/dedup/batches/BATCH-2").json()
    assert batch_two["batch"]["valid_count"] == 1
    assert batch_two["provenance"][0]["quote"] == 21
    assert [item["quote"] for item in batch_two["receipts"]] == [21]

    first_receipt = client.get(
        f"/api/dedup/receipts/{round_result['delivered'][0]['receipt']['receipt_id']}").json()
    assert first_receipt["quote"] == 18
    assert first_receipt["provenance"][0]["batch_id"] == "BATCH-1"


def test_state_survives_reprocess(tmp_path):
    from app.services.dedup_registry import DedupRegistry

    state_path = str(tmp_path / "dedup.json")
    first = DedupRegistry(state_path)
    first.ingest_batch("BATCH-1", [
        {"product_key": "SKU-1", "quote": 18},
        {"product_key": "SKU-2", "quote": 19},
    ])
    first.ingest_batch("BATCH-2", [{"product_key": "SKU-1", "quote": 21}])
    claimed = first.claim("SKU-1")
    receipt_id = claimed["receipt"]["receipt_id"]
    job = first.start_withdrawal("BATCH-1", step_size=1)

    # 模拟服务重启：新实例从状态文件恢复，回执、来源和撤回进度都还在。
    second = DedupRegistry(state_path)
    assert second.load() is True
    assert second.get_receipt(receipt_id)["quote"] == 18
    assert second.get_withdrawal(job["job"]["job_id"])["status"] == "撤回中"
    board = {item["product_key"]: item for item in second.board()["items"]}
    assert board["SKU-1"]["state"] == "核对中"
    second.resume_withdrawal(job["job"]["job_id"])
    board = {item["product_key"]: item for item in second.board()["items"]}
    assert board["SKU-1"]["claimable_quote"] == 21
    assert second.get_receipt(receipt_id)["provenance"][0]["batch_id"] == "BATCH-1"


def test_delivery_route_uses_sourced_dataset(client):
    client.post("/api/chains/delivery", json={
        "batch_id": "BATCH-1",
        "rows": [{"product_key": "SKU-7", "quote": 18}],
    })
    client.post("/api/chains/delivery", json={
        "batch_id": "BATCH-2",
        "rows": [{"product_key": "SKU-7", "quote": 21}],
    })
    client.post("/api/dedup/withdrawals", json={"batch_id": "BATCH-1"})
    result = client.post("/api/chains/delivery", json={
        "batch_id": "BATCH-3", "use_dedup_dataset": True}).json()
    assert result["source"] == "dedup_dataset"
    assert result["steps"]["store_result"]["批次标识"] == "BATCH-3"
    assert [item["quote"] for item in result["receipts"]] == [21]
    assert result["receipts"][0]["provenance"][0]["batch_id"] == "BATCH-2"
    assert _board(client, "SKU-7")["state"] == "已交付"
    assert _board(client, "SKU-7")["delivered_quote"] == 21
