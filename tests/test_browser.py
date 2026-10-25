"""验证进程复用时的请求隔离、失败清理和服务退出。"""

from types import SimpleNamespace
import os

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


@pytest.fixture
def browser_options(monkeypatch):
    monkeypatch.delenv("BROWSER_PATH", raising=False)
    monkeypatch.delenv("BROWSER_NO_SANDBOX", raising=False)
    seen = []
    monkeypatch.setattr(browser.ChromiumOptions, "auto_port", lambda self: self)
    monkeypatch.setattr(browser, "ChromiumPage", lambda options: seen.append(options) or SimpleNamespace())
    grabber = SimpleNamespace(
        prepareheaderscookies=lambda **kwargs: ({}, {}, {}),
        _autosetproxies=lambda: {}, browser_headless=True, browser_load_mode="eager",
        browser_disable_images=False, browser_timeout_sec=15,
    )
    return grabber, seen


def test_native_browser_uses_library_discovery_and_keeps_sandbox(browser_options):
    grabber, seen = browser_options
    browser.SystemMediaGrabber.buildbrowserpage(grabber)
    assert seen[0].browser_path == "chrome"
    assert "--no-sandbox" not in seen[0].arguments


def test_explicit_browser_and_container_sandbox_setting(monkeypatch, tmp_path, browser_options):
    grabber, seen = browser_options
    executable = tmp_path / "浏览器目录" / "chrome.exe"
    executable.parent.mkdir()
    executable.touch()
    monkeypatch.setenv("BROWSER_PATH", str(executable))
    monkeypatch.setenv("BROWSER_NO_SANDBOX", "1")
    browser.SystemMediaGrabber.buildbrowserpage(grabber)
    assert seen[0].browser_path == str(executable)
    assert "--no-sandbox" in seen[0].arguments


def test_wrong_browser_path_does_not_silently_launch_another_browser(monkeypatch, tmp_path, browser_options):
    grabber, seen = browser_options
    monkeypatch.setenv("BROWSER_PATH", str(tmp_path / "missing.exe"))
    with pytest.raises(FileNotFoundError, match="BROWSER_PATH"):
        browser.SystemMediaGrabber.buildbrowserpage(grabber)
    assert not seen


@pytest.mark.skipif(os.getenv("RUN_BROWSER_SMOKE") != "1", reason="仅在已安装浏览器的环境运行")
def test_real_headless_browser_can_create_isolated_context():
    shared = browser.DouyinBrowser()
    try:
        with shared.page() as page:
            page.get("about:blank")
            assert page.url == "about:blank"
        with shared.page() as page:
            assert page.url == "about:blank"
    finally:
        shared.close()
