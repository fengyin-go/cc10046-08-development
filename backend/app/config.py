"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    app_name: str = "网络数据采集与RPA自动化平台"
    env: str = "local"
    port: int = 8010
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5174",
            "http://localhost:5174",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200
    # 跨批去重仓库的落盘路径；设为空串时只放内存（测试可用）。
    dedup_state_path: str = field(
        default_factory=lambda: os.environ.get(
            "DEDUP_STATE_PATH",
            "" if os.environ.get("APP_ENV") == "test"
            else str(_APP_DIR / ".data" / "dedup_state.json"),
        )
    )


settings = Settings()
