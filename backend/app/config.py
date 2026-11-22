"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


_BACKEND_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    app_name: str = "多源日志采集与解析中心"
    env: str = "local"
    port: int = 8013
    # 版本化日志交付清单的落地目录；测试或部署时可用 DELIVERY_ROOT 覆盖。
    delivery_root: str = os.environ.get(
        "DELIVERY_ROOT", str(_BACKEND_ROOT / "var" / "deliveries")
    )
    # 当前清单结构版本。1 代表旧日志聚合交付记录，2 代表版本化清单。
    delivery_manifest_version: int = 2
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5193",
            "http://localhost:5193",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200


settings = Settings()
