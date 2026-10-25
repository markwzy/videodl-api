"""让每个测试独立验证解析行为，避免前一个测试留下的缓存掩盖错误。"""

import pytest
import time

from fastapi.testclient import TestClient

from app.parse_cache import clear_parse_cache


@pytest.fixture(autouse=True)
def isolated_parse_cache():
    clear_parse_cache()
    yield
    clear_parse_cache()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def wait_for_task():
    def wait(client, task_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = client.get(f"/api/tasks/{task_id}")
            assert response.status_code == 200
            task = response.json()
            if task["status"] in {"succeeded", "failed"}:
                return task
            time.sleep(0.005)
        raise AssertionError("离线解析任务未在预期时间内完成")

    return wait
