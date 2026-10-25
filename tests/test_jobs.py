"""用可控的阻塞解析验证容量边界，不能依赖真实平台的响应速度。"""

import asyncio
from threading import Event

from fastapi import HTTPException
import pytest

from app.jobs import ParseJobs


async def wait_status(jobs, task_id, status):
    async with asyncio.timeout(2):
        while jobs.get(task_id)["status"] != status:
            await asyncio.sleep(0.005)
    return jobs.get(task_id)


def test_bounded_queue_deduplicates_and_reports_position():
    async def scenario():
        release = Event()
        calls = []

        def parse(url):
            calls.append(url)
            assert release.wait(3)
            return [{"download_url": "https://cdn.example.org/a.mp4"}]

        jobs = ParseJobs(parse, queue_size=2)
        try:
            first = jobs.submit("first", "browser", "first")
            second = jobs.submit("second", "browser", "second")
            third = jobs.submit("third", "browser", "third")
            assert first["status"] == "running"
            assert second["queue_position"] == 1
            assert third["queue_position"] == 2
            assert jobs.submit("second", "browser", "second")["id"] == second["id"]
            with pytest.raises(HTTPException) as busy:
                jobs.submit("overflow", "browser", "overflow")
            assert busy.value.status_code == 503
            assert busy.value.headers["Retry-After"] == "5"
            release.set()
            await wait_status(jobs, third["id"], "succeeded")
            assert calls == ["first", "second", "third"]
        finally:
            release.set()
            await jobs.close()

    asyncio.run(scenario())


def test_queue_timeout_removes_waiting_job_without_parsing():
    async def scenario():
        release = Event()
        calls = []

        def parse(url):
            calls.append(url)
            assert release.wait(3)
            return [{"download_url": "https://cdn.example.org/a.mp4"}]

        jobs = ParseJobs(parse, queue_size=1, queue_timeout=0.03)
        try:
            jobs.submit("first", "browser", "first")
            waiting = jobs.submit("expired", "browser", "expired")
            task = await wait_status(jobs, waiting["id"], "failed")
            assert task["error"]["status_code"] == 504
            assert "排队" in task["error"]["detail"]
            replacement = jobs.submit("replacement", "browser", "replacement")
            release.set()
            await wait_status(jobs, replacement["id"], "succeeded")
            assert calls == ["first", "replacement"]
        finally:
            release.set()
            await jobs.close()

    asyncio.run(scenario())


def test_timed_out_thread_keeps_its_slot_until_it_exits():
    async def scenario():
        release = Event()
        calls = []

        def parse(url):
            calls.append(url)
            if url == "slow":
                assert release.wait(3)
            return [{"download_url": "https://cdn.example.org/a.mp4"}]

        jobs = ParseJobs(parse, queue_size=1, parse_timeout=0.03)
        try:
            slow = jobs.submit("slow", "browser", "slow")
            queued = jobs.submit("next", "browser", "next")
            failed = await wait_status(jobs, slow["id"], "failed")
            assert failed["error"]["status_code"] == 504
            assert jobs.submit("slow", "browser", "slow")["id"] == slow["id"]
            assert jobs.get(queued["id"])["status"] == "queued"
            assert calls == ["slow"]
            release.set()
            await wait_status(jobs, queued["id"], "succeeded")
            assert jobs.get(slow["id"])["status"] == "failed"
            assert calls == ["slow", "next"]
        finally:
            release.set()
            await jobs.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure,code,detail", [
    (HTTPException(status_code=403, detail="需要登录"), 403, "需要登录"),
    (ValueError("敏感凭证不能进入响应"), 502, "解析器执行失败（ValueError）"),
    (None, 502, "未解析到有效媒体地址"),
])
def test_failures_keep_safe_errors_and_allow_retry(failure, code, detail):
    async def scenario():
        def parse(url):
            if failure:
                raise failure
            return []

        jobs = ParseJobs(parse)
        try:
            first = jobs.submit("url", "browser", "key")
            result = await wait_status(jobs, first["id"], "failed")
            assert result["error"]["status_code"] == code
            assert detail in result["error"]["detail"]
            assert "敏感凭证" not in result["error"]["detail"]
            assert jobs.submit("url", "browser", "key")["id"] != first["id"]
        finally:
            await jobs.close()

    asyncio.run(scenario())


def test_completed_task_retention_is_bounded_and_expires():
    async def scenario():
        jobs = ParseJobs(lambda url: [{"download_url": url}], max_results=1, result_ttl=0.03)
        try:
            first = jobs.submit("first", "browser", "first")
            await wait_status(jobs, first["id"], "succeeded")
            second = jobs.submit("second", "browser", "second")
            await wait_status(jobs, second["id"], "succeeded")
            with pytest.raises(HTTPException) as evicted:
                jobs.get(first["id"])
            assert evicted.value.status_code == 404
            await asyncio.sleep(0.04)
            with pytest.raises(HTTPException):
                jobs.get(second["id"])
        finally:
            await jobs.close()

    asyncio.run(scenario())


def test_shutdown_discards_waiting_jobs_and_drains_running_threads():
    async def scenario():
        release = Event()
        calls = []

        def parse(url):
            calls.append(url)
            assert release.wait(3)
            return [{"download_url": url}]

        jobs = ParseJobs(parse)
        first = jobs.submit("running", "browser", "running")
        waiting = jobs.submit("waiting", "browser", "waiting")
        closing = asyncio.create_task(jobs.close())
        try:
            await wait_status(jobs, waiting["id"], "failed")
            assert not closing.done()
            with pytest.raises(HTTPException) as stopped:
                jobs.submit("new", "api", "new")
            assert stopped.value.status_code == 503
        finally:
            release.set()
            await closing
        assert calls == ["running"]
        assert jobs.get(first["id"])["status"] == "succeeded"

    asyncio.run(scenario())
