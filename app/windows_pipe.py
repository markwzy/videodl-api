"""用本机字节管道给媒体进程输送音轨，不继承描述符，也不创建媒体文件。"""

import secrets
from threading import Event, Lock

import pywintypes
import win32event
import win32file
import win32pipe
import winerror


class WindowsPipe:
    def __init__(self):
        self.path = rf"\\.\pipe\videodl-{secrets.token_hex(24)}"
        self._lock = Lock()
        self._closed = Event()
        # 随机名称、单实例和拒绝远程连接将管道限定在本机本次合成；句柄不传给子进程。
        self._handle = win32pipe.CreateNamedPipe(
            self.path,
            win32pipe.PIPE_ACCESS_OUTBOUND | win32file.FILE_FLAG_OVERLAPPED | win32pipe.FILE_FLAG_FIRST_PIPE_INSTANCE,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT | win32pipe.PIPE_REJECT_REMOTE_CLIENTS,
            1, 64 * 1024, 64 * 1024, 0, None,
        )

    def _run(self, operation):
        with self._lock:
            if self._closed.is_set():
                raise OSError("音轨管道已关闭")
            overlapped = pywintypes.OVERLAPPED()
            overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
            try:
                status = operation(overlapped)
                if status == winerror.ERROR_PIPE_CONNECTED:
                    return 0
                if status == winerror.ERROR_IO_PENDING:
                    # 异步连接和写入都必须能取消，否则下载中断会永久占住工作线程。
                    while win32event.WaitForSingleObject(overlapped.hEvent, 100) == win32event.WAIT_TIMEOUT:
                        if self._closed.is_set():
                            try:
                                win32file.CancelIoEx(self._handle, overlapped)
                            except pywintypes.error as exc:
                                # 完成与取消可能同时发生；没有待取消操作时仍须收取完成结果。
                                if exc.winerror != winerror.ERROR_NOT_FOUND:
                                    raise
                            try:
                                win32file.GetOverlappedResult(self._handle, overlapped, True)
                            except pywintypes.error as exc:
                                if exc.winerror != winerror.ERROR_OPERATION_ABORTED:
                                    raise
                            raise OSError("音轨管道已关闭")
                return win32file.GetOverlappedResult(self._handle, overlapped, True)
            finally:
                # 完成或取消操作以后才释放事件，避免系统仍在引用已回收的异步状态。
                overlapped.hEvent.Close()

    def write(self, data):
        return self._run(lambda overlapped: win32file.WriteFile(self._handle, data, overlapped)[0])

    def close(self):
        self._closed.set()
        with self._lock:
            if self._handle is not None:
                self._handle.Close()
                self._handle = None

    def __enter__(self):
        self._run(lambda overlapped: win32pipe.ConnectNamedPipe(self._handle, overlapped))
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                with self._lock:
                    if not self._closed.is_set():
                        # 正常结束须等待读端消费尾部数据；异常清理先终止媒体进程，使等待解除。
                        win32file.FlushFileBuffers(self._handle)
        finally:
            self.close()
