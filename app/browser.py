"""使用镜像内的浏览器，避免上游自动下载流程依赖特定处理器架构。"""

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
        # 每次解析使用独立端口和用户目录，避免并发请求共享登录状态或关闭彼此的浏览器。
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
