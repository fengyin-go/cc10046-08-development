"""测试统一把跨批去重状态指到临时目录，避免污染仓库里的 .data。"""
import os
import sys

os.environ.setdefault("APP_ENV", "test")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from app.services.dedup_registry import dedup_registry


@pytest.fixture(autouse=True)
def isolated_dedup_state(tmp_path):
    original = dedup_registry._state_path
    dedup_registry._state_path = str(tmp_path / "dedup_state.json")
    dedup_registry.reset()
    yield
    dedup_registry.reset()
    dedup_registry._state_path = original
