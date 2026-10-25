"""在单个服务进程内管理有界解析队列，任务不依赖某次网页连接存活。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import secrets
import time
from typing import Callable, Hashable

from fastapi import HTTPException


@dataclass
class ParseJob:
    id: str
    key: Hashable
    url: str
    lane: str
    created_at: float = field(default_factory=time.time)
    status: str = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    result: dict | None = None
    error: dict | None = None
    queue_timer: asyncio.TimerHandle | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


class ParseJobs:
    def __init__(
        self, parser: Callable[[str], list[dict]], *, queue_size: int = 8,
        queue_timeout: float = 30, parse_timeout: float = 90,
        result_ttl: float = 120, max_results: int = 128,
    ):
        if queue_size < 1 or min(queue_timeout, parse_timeout, result_ttl, max_results) <= 0:
            raise ValueError("队列容量、等待时限和结果容量必须大于零")
        self._parser = parser
        self._queue_size = queue_size
        self._queue_timeout = queue_timeout
        self._parse_timeout = parse_timeout
        self._result_ttl = result_ttl
        self._max_results = max_results
        # 所有状态只在所属事件循环读写，工作线程仅返回解析结果，不能自行增删任务。
        self._jobs: dict[str, ParseJob] = {}
        self._active: dict[Hashable, ParseJob] = {}
        self._completed: OrderedDict[str, float] = OrderedDict()
        self._queues: dict[str, deque[ParseJob]] = {"browser": deque(), "api": deque()}
        self._limits = {"browser": 1, "api": 2}
        self._running = {"browser": 0, "api": 0}
        self._tasks: set[asyncio.Task] = set()
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="parse-job")
        self._closed = False

    def submit(self, url: str, lane: str, key: Hashable, request_id: str | None = None) -> dict:
        self._prune()
        if self._closed:
            raise HTTPException(status_code=503, detail="服务正在停止，请稍后重试")
        # 页面在发请求前保存标识，提交响应丢失时也能安全重试已经完成的任务。
        if request_id in self._jobs:
            job = self._jobs[request_id]
            if job.key != key:
                raise HTTPException(status_code=409, detail="提交标识已用于其他链接，请重新提交")
            return self._snapshot(job)
        # 先查共享任务，再检查容量；刷新或重复提交不能额外占用排队名额。
        if key in self._active:
            return self._snapshot(self._active[key])
        queue = self._queues[lane]
        if len(queue) >= self._queue_size:
            raise HTTPException(
                status_code=503, detail="解析队列已满，请稍后重试", headers={"Retry-After": "5"},
            )
        job = ParseJob(request_id or secrets.token_urlsafe(24), key, url, lane)
        self._jobs[job.id] = self._active[key] = job
        queue.append(job)
        job.queue_timer = asyncio.get_running_loop().call_later(self._queue_timeout, self._expire, job)
        self._dispatch(lane)
        return self._snapshot(job)

    def get(self, job_id: str) -> dict:
        self._prune()
        if job_id not in self._jobs:
            raise HTTPException(status_code=404, detail="解析任务不存在或已过期，请重新解析")
        return self._snapshot(self._jobs[job_id])

    async def wait(self, job_id: str) -> dict:
        self._prune()
        if job_id not in self._jobs:
            raise HTTPException(status_code=404, detail="解析任务不存在或已过期，请重新解析")
        job = self._jobs[job_id]
        # 只等待状态通知，连接取消不能取消共享解析；持有任务引用也避免结果被容量清理后丢失。
        await job.done.wait()
        if job.error:
            raise HTTPException(status_code=job.error["status_code"], detail=job.error["detail"])
        return job.result

    def _snapshot(self, job: ParseJob) -> dict:
        position = list(self._queues[job.lane]).index(job) + 1 if job.status == "queued" else None
        return {
            "id": job.id, "status": job.status, "input_url": job.url,
            "queue_position": position, "created_at": job.created_at,
            "started_at": job.started_at, "finished_at": job.finished_at,
            "result": job.result, "error": job.error,
        }

    def _fail(self, job: ParseJob, code: int, detail: str) -> None:
        job.status = "failed"
        job.finished_at = time.time()
        job.error = {"status_code": code, "detail": detail}
        job.done.set()

    def _remember(self, job: ParseJob) -> None:
        self._active.pop(job.key, None)
        self._completed[job.id] = time.monotonic() + self._result_ttl
        self._prune()

    def _prune(self) -> None:
        now = time.monotonic()
        while self._completed:
            job_id, deadline = next(iter(self._completed.items()))
            if deadline > now and len(self._completed) <= self._max_results:
                break
            self._completed.pop(job_id)
            del self._jobs[job_id]

    def _expire(self, job: ParseJob) -> None:
        if job.status != "queued":
            return
        self._queues[job.lane].remove(job)
        self._fail(job, 504, "排队等待超时，请稍后重新解析")
        self._remember(job)

    def _dispatch(self, lane: str) -> None:
        queue = self._queues[lane]
        while not self._closed and queue and self._running[lane] < self._limits[lane]:
            # 事件循环繁忙时定时回调可能尚未运行，出队前仍要检查实际等待时限。
            if queue[0].queue_timer.when() <= asyncio.get_running_loop().time():
                self._expire(queue[0])
                continue
            job = queue.popleft()
            job.queue_timer.cancel()
            job.status = "running"
            job.started_at = time.time()
            self._running[lane] += 1
            task = asyncio.create_task(self._run(job))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run(self, job: ParseJob) -> None:
        future = asyncio.get_running_loop().run_in_executor(self._executor, self._parser, job.url)
        try:
            try:
                results = await asyncio.wait_for(asyncio.shield(future), self._parse_timeout)
            except asyncio.TimeoutError:
                self._fail(job, 504, "解析超时，请稍后再试")
                # 线程不能强制取消。超时后仍等待真正退出，不能提前释放名额造成并发失控。
                try:
                    await future
                except Exception:
                    pass
                return
            if not results:
                raise HTTPException(status_code=502, detail="未解析到有效媒体地址；页面可能需要登录或验证，视频也可能已失效")
            job.result = {
                "success": True, "input_url": job.url, "count": len(results),
                "items": results, "message": "解析成功",
            }
            job.status = "succeeded"
            job.finished_at = time.time()
            job.done.set()
        except HTTPException as exc:
            self._fail(job, exc.status_code, exc.detail)
        except Exception as exc:
            # 平台异常可能包含登录信息或带凭证的链接，查询接口只暴露安全的错误类型。
            self._fail(job, 502, f"解析器执行失败（{type(exc).__name__}），请检查容器日志")
        finally:
            self._running[job.lane] -= 1
            self._remember(job)
            self._dispatch(job.lane)

    async def close(self) -> None:
        self._closed = True
        for queue in self._queues.values():
            for job in list(queue):
                job.queue_timer.cancel()
                queue.remove(job)
                self._fail(job, 503, "服务已停止，请重新解析")
                self._remember(job)
        # 服务退出不能遗留仍在操作浏览器的线程；浏览器由调用方在任务结束后统一释放。
        if self._tasks:
            await asyncio.gather(*self._tasks)
        self._executor.shutdown(wait=True)
