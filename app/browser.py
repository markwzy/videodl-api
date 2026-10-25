"""使用镜像内的浏览器，避免上游自动下载流程依赖特定处理器架构。"""

from contextlib import contextmanager
import logging
from threading import Lock

from DrissionPage import ChromiumOptions, ChromiumPage
from videodl.modules import WebMediaGrabber
from videodl.modules.utils.chromium import DrissionPageUtils


class SystemMediaGrabber(WebMediaGrabber):
    def buildbrowserpage(self, request_overrides: dict | None = None):
        headers, cookies, other_kwargs = self.prepareheaderscookies(request_overrides=request_overrides)
        options = ChromiumOptions(read_file=False)
        options.set_browser_path(self.browser_path)
        options.headless(self.browser_headless)
        options.set_load_mode(self.browser_load_mode)
        options.no_imgs(self.browser_disable_images)
        options.set_timeouts(
            base=self.browser_timeout_sec,
            page_load=self.browser_timeout_sec,
            script=self.browser_timeout_sec,
        )
        # 容器禁止提权，浏览器无法启动自身沙箱；容器仍以普通用户运行。
        options.set_argument('--no-sandbox')
        # 每次启动使用独立端口和用户目录，避免接管其他浏览器或关闭别人的进程。
        options.auto_port()
        proxies = other_kwargs.get('proxies') or self._autosetproxies()
        proxy = DrissionPageUtils.requestsproxytodrissionpage(proxies, mode='chromium', strip_auth_for_chromium=False)
        if proxy:
            options.set_proxy(proxy)
        page = ChromiumPage(options)
        try:
            if headers:
                page.set.headers(headers)
            if cookies:
                page.set.cookies(cookies)
        except Exception:
            page.quit(del_data=True)
            raise
        return page


class DouyinBrowser:
    """复用浏览器进程，每次借用独立环境，不能把登录状态留给下一个请求。"""

    def __init__(self):
        self._root = None
        self._headers = {}
        # 锁覆盖整个借用周期，关闭服务或其他平台启动浏览器时不能销毁正在解析的页面。
        self._lock = Lock()

    def _get_root(self):
        if self._root is not None and not self._root.browser.states.is_alive:
            self._close_locked()
        if self._root is None:
            grabber = SystemMediaGrabber(
                browser_path="/usr/bin/chromium", browser_timeout_sec=15,
                browser_disable_images=False,
            )
            self._headers = grabber.prepareheaderscookies()[0]
            self._root = grabber.buildbrowserpage()
        return self._root

    @contextmanager
    def page(self):
        with self._lock:
            root = self._get_root()
            context_id = None
            page = None
            try:
                # 当前库没有公开的环境销毁接口，统一使用浏览器级协议创建和销毁。
                # 显式指定环境也避免建页失败时被库自动改为共享默认环境。
                context_id = root.browser._run_cdp(
                    "Target.createBrowserContext", disposeOnDetach=True,
                )["browserContextId"]
                target_id = root.browser._run_cdp(
                    "Target.createTarget", url="about:blank", browserContextId=context_id,
                )["targetId"]
                page = root.get_tab(target_id)
                # 新环境不会继承原始页面的请求头，必须在导航前单独设置。
                page.set.headers(self._headers)
                yield page
            finally:
                if context_id is not None:
                    try:
                        # 监听尚未开始时库内没有连接，直接停止会误触发整进程重建。
                        if page is not None and page.listen.listening:
                            page.listen.stop()
                        # 同时销毁页面、弹窗和存储，单纯关闭标签页会遗留浏览环境。
                        root.browser._run_cdp("Target.disposeBrowserContext", browserContextId=context_id)
                    except Exception as exc:
                        # 清理失败的进程不能再交给其他请求；下一次调用重新启动。
                        logging.getLogger(__name__).warning("浏览环境清理失败（%s），释放浏览器", type(exc).__name__)
                        self._close_locked()
                elif not root.browser.states.is_alive:
                    self._close_locked()

    def _close_locked(self):
        root, self._root = self._root, None
        self._headers = {}
        if root is not None:
            try:
                root.quit(del_data=True)
            except Exception as exc:
                logging.getLogger(__name__).warning("浏览器退出失败（%s）", type(exc).__name__)

    def close(self):
        with self._lock:
            self._close_locked()


DOUYIN_BROWSER = DouyinBrowser()
