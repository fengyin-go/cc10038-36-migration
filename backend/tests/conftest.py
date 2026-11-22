import pytest
import sys
import os
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.seed import SEED_ROWS


@pytest.fixture(autouse=True)
def reset_store():
    """每个测试前重置 store 为初始种子数据，并把交付文件落到临时目录。"""
    store._tables = {
        name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
    }
    previous_root = os.environ.get("DELIVERY_ROOT")
    tmp_root = tempfile.mkdtemp(prefix="delivery-root-")
    os.environ["DELIVERY_ROOT"] = tmp_root
    try:
        yield
    finally:
        if previous_root is None:
            os.environ.pop("DELIVERY_ROOT", None)
        else:
            os.environ["DELIVERY_ROOT"] = previous_root
        shutil.rmtree(tmp_root, ignore_errors=True)


@pytest.fixture
def pipeline():
    from app.services.pipeline import pipeline as p
    return p


@pytest.fixture
def sample_raw_logs():
    return [
        {"body": "2026-01-15T10:30:00|INFO|server started"},
        {"body": "2026-01-15T10:31:00|WARN|memory high"},
        {"body": "2026-01-15T10:32:00|ERROR|connection timeout"},
        {"body": "2026-01-15T10:33:00|INFO|request processed"},
        {"body": "2026-01-15T10:34:00|WARN|disk usage 85%"},
    ]