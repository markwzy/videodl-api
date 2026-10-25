"""验证进程复用时的请求隔离、失败清理和服务退出。"""

from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from app import browser, main


class FakeRoot:
    def __init__(self):
        self.browser = SimpleNamespace(
            states=SimpleNamespace(is_alive=True), _run_cdp=self.command,
        )
        self.contexts = {}
        self.disposed = []
        self.closed = False
        self.fail_create = False
        self.fail_dispose = False
        self.sequence = 0

    def command(self, method, **kwargs):
        if method == "Target.createBrowserContext":
            self.sequence += 1
            identifier = str(self.sequence)
            self.contexts[identifier] = {}
            return {"browserContextId": identifier}
        if method == "Target.createTarget":
            if self.fail_create:
                raise RuntimeError("页面创建失败")
            return {"targetId": kwargs["browserContextId"]}
        if method == "Target.disposeBrowserContext":
            if self.fail_dispose:
                raise RuntimeError("环境销毁失败")
            self.disposed.append(kwargs["browserContextId"])
            del self.contexts[kwargs["browserContextId"]]
            return {}
        pytest.fail(f"未预期的浏览器命令：{method}")

    def get_tab(self, target_id):
        return SimpleNamespace(
            state=self.contexts[target_id], set=SimpleNamespace(headers=lambda value: None),
            listen=SimpleNamespace(listening=False, stop=lambda: pytest.fail("未启动的监听不应停止")),
        )

    def quit(self, **kwargs):
        self.closed = True
        self.contexts.clear()


@pytest.fixture
def pool(monkeypatch):
    roots = []

    class Grabber:
        def __init__(self, **kwargs):
            pass

        def prepareheaderscookies(self):
            return {"User-Agent": "test"}, {}, {}

        def buildbrowserpage(self):
            root = FakeRoot()
            roots.append(root)
            return root

    monkeypatch.setattr(browser, "SystemMediaGrabber", Grabber)
    shared = browser.DouyinBrowser()
    yield shared, roots
    shared.close()


def test_process_is_reused_but_request_state_is_destroyed(pool):
    shared, roots = pool
    with shared.page() as first:
        first.state["cookie"] = "第一个请求的状态"
    assert roots[0].contexts == {}
    assert not roots[0].closed
    with shared.page() as second:
        assert second.state == {}
    assert len(roots) == 1
    assert roots[0].disposed == ["1", "2"]


def test_failed_parse_releases_context_and_allows_next_request(pool):
    shared, roots = pool
    with pytest.raises(RuntimeError, match="导航失败"):
        with shared.page():
            raise RuntimeError("导航失败")
    assert roots[0].contexts == {}
    with shared.page():
        pass
    assert len(roots) == 1


def test_active_listener_is_stopped_without_restarting_browser(pool):
    shared, roots = pool
    stopped = []
    with shared.page() as page:
        page.listen.listening = True
        page.listen.stop = lambda: stopped.append(True)
    assert stopped == [True]
    assert roots[0].contexts == {}
    assert not roots[0].closed


def test_page_creation_failure_also_releases_context(pool):
    shared, roots = pool
    with shared.page():
        pass
    roots[0].fail_create = True
    with pytest.raises(RuntimeError, match="页面创建失败"):
        with shared.page():
            pytest.fail("创建失败不能交付页面")
    assert roots[0].contexts == {}


def test_cleanup_failure_discards_process_without_hiding_parse_error(pool):
    shared, roots = pool
    with pytest.raises(ValueError, match="原始解析错误"):
        with shared.page():
            roots[0].fail_dispose = True
            raise ValueError("原始解析错误")
    assert roots[0].closed
    with shared.page():
        pass
    assert len(roots) == 2


def test_dead_process_is_replaced(pool):
    shared, roots = pool
    with shared.page():
        pass
    roots[0].browser.states.is_alive = False
    with shared.page():
        pass
    assert len(roots) == 2
    assert roots[0].closed


def test_service_shutdown_releases_resident_browser(pool, monkeypatch):
    shared, roots = pool
    monkeypatch.setattr(browser, "DOUYIN_BROWSER", shared)
    with TestClient(main.app) as client:
        assert client.get("/api/health").status_code == 200
        with shared.page():
            pass
        assert not roots[0].closed
    assert roots[0].closed


def test_close_is_idempotent_and_next_request_can_start_again(pool):
    shared, roots = pool
    with shared.page():
        pass
    shared.close()
    shared.close()
    assert roots[0].closed
    with shared.page():
        pass
    assert len(roots) == 2
