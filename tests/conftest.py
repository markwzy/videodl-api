"""让每个测试独立验证解析行为，避免前一个测试留下的缓存掩盖错误。"""

import pytest

from app.parse_cache import clear_parse_cache


@pytest.fixture(autouse=True)
def isolated_parse_cache():
    clear_parse_cache()
    yield
    clear_parse_cache()
