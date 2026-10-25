"""模拟操作系统异步边界，验证取消时不会遗留句柄或无限等待。"""

import importlib.util
from pathlib import Path
import sys
from threading import Event, Thread
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def pipe_api(monkeypatch):
    class Handle:
        def __init__(self):
            self.closed = False
            self.event = Event()

        def Close(self):
            self.closed = True

    class ApiError(OSError):
        def __init__(self, code):
            self.winerror = code

    state = SimpleNamespace(events=[], canceled=[], results=[], writes=[], flushes=[], pending=False, connected=False, issued=Event())
    modules = {name: ModuleType(name) for name in ("pywintypes", "win32event", "win32file", "win32pipe", "winerror")}
    types, event, file, pipe, error = (modules[name] for name in ("pywintypes", "win32event", "win32file", "win32pipe", "winerror"))
    for name, code in {"ERROR_IO_PENDING": 997, "ERROR_PIPE_CONNECTED": 535, "ERROR_OPERATION_ABORTED": 995, "ERROR_NOT_FOUND": 1168}.items():
        setattr(error, name, code)
    types.OVERLAPPED = SimpleNamespace
    types.error = ApiError
    event.WAIT_TIMEOUT = 258

    def create_event(*args):
        handle = Handle()
        state.events.append(handle)
        return handle

    event.CreateEvent = create_event
    event.WaitForSingleObject = lambda handle, timeout: 0 if handle.event.wait(0.002) else 258
    state.handle = Handle()
    for index, name in enumerate(("PIPE_ACCESS_OUTBOUND", "FILE_FLAG_FIRST_PIPE_INSTANCE", "PIPE_TYPE_BYTE", "PIPE_READMODE_BYTE", "PIPE_WAIT", "PIPE_REJECT_REMOTE_CLIENTS")):
        setattr(pipe, name, 1 << index)
    file.FILE_FLAG_OVERLAPPED = 1 << 6

    def create_pipe(*args):
        state.creation = args
        return state.handle

    def connect(handle, overlapped):
        state.issued.set()
        return error.ERROR_PIPE_CONNECTED if state.connected else error.ERROR_IO_PENDING

    def write(handle, data, overlapped):
        state.writes.append(data)
        state.issued.set()
        return (error.ERROR_IO_PENDING if state.pending else 0), len(data)

    def result(handle, overlapped, wait):
        state.results.append(overlapped)
        if overlapped in state.canceled:
            raise ApiError(error.ERROR_OPERATION_ABORTED)
        return len(state.writes[-1]) if state.writes else 0

    def cancel(handle, overlapped):
        state.canceled.append(overlapped)
        overlapped.hEvent.event.set()

    pipe.CreateNamedPipe = create_pipe
    pipe.ConnectNamedPipe = connect
    file.WriteFile = write
    file.GetOverlappedResult = result
    file.CancelIoEx = cancel
    file.FlushFileBuffers = lambda handle: state.flushes.append(handle)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("pipe_under_test", Path(__file__).parents[1] / "app/windows_pipe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.WindowsPipe, state


def test_connected_pipe_preserves_tail_before_closing_and_uses_local_single_instance(pipe_api):
    factory, state = pipe_api
    state.connected = True
    pipe = factory()
    with pipe:
        assert pipe.write(b"audio tail") == 10
    assert state.flushes == [state.handle]
    assert state.handle.closed
    assert all(event.closed for event in state.events)
    assert state.creation[2] & 32
    assert state.creation[3] == 1
    pipe.close()
    with pytest.raises(OSError, match="已关闭"):
        pipe.write(b"late")


@pytest.mark.parametrize("pending_write", [False, True])
def test_close_cancels_pending_connect_or_write_and_reaps_operation(pipe_api, pending_write):
    factory, state = pipe_api
    pipe = factory()
    state.pending = pending_write
    errors = []

    def worker():
        try:
            pipe.write(b"audio") if pending_write else pipe.__enter__()
        except OSError as exc:
            errors.append(exc)

    thread = Thread(target=worker, daemon=True)
    thread.start()
    try:
        assert state.issued.wait(1)
        pipe.close()
        thread.join(1)
        assert not thread.is_alive()
        assert len(errors) == 1
        assert len(state.canceled) == 1
        assert state.results == state.canceled
        assert all(event.closed for event in state.events)
        assert state.handle.closed
    finally:
        pipe.close()


def test_failed_write_closes_pipe_without_flushing(pipe_api):
    factory, state = pipe_api
    state.connected = True
    with pytest.raises(ValueError):
        with factory():
            raise ValueError("上游断流")
    assert not state.flushes
    assert state.handle.closed


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实的系统管道")
def test_native_pipe_preserves_bytes_beyond_buffer_and_delivers_eof():
    from app.windows_pipe import WindowsPipe

    pipe = WindowsPipe()
    received = bytearray()
    errors = []
    payload = b"audio tail\x00" * 100000

    def reader():
        try:
            with open(pipe.path, "rb", buffering=0) as source:
                while chunk := source.read(4096):
                    received.extend(chunk)
        except OSError as exc:
            errors.append(exc)

    thread = Thread(target=reader, daemon=True)
    thread.start()
    try:
        with pipe:
            for offset in range(0, len(payload), 65536):
                chunk = payload[offset:offset + 65536]
                assert pipe.write(chunk) == len(chunk)
        thread.join(3)
        assert not thread.is_alive()
        assert not errors
        assert bytes(received) == payload
    finally:
        pipe.close()


@pytest.mark.skipif(sys.platform != "win32", reason="需要真实的系统管道")
@pytest.mark.parametrize("pending_write", [False, True])
def test_native_pipe_cancels_pending_operations(pending_write):
    from app.windows_pipe import WindowsPipe

    pipe = WindowsPipe()
    started, connected, release = Event(), Event(), Event()
    errors = []

    def reader():
        with open(pipe.path, "rb", buffering=0):
            connected.set()
            release.wait(5)

    def writer():
        try:
            if pending_write:
                with pipe:
                    started.set()
                    pipe.write(b"a" * (1024 * 1024))
            else:
                started.set()
                with pipe:
                    pass
        except OSError as exc:
            errors.append(exc)

    consumer = Thread(target=reader, daemon=True) if pending_write else None
    worker = Thread(target=writer, daemon=True)
    try:
        if consumer:
            consumer.start()
        worker.start()
        assert started.wait(3)
        if consumer:
            assert connected.wait(3)
        pipe.close()
        worker.join(3)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert pipe._handle is None
    finally:
        release.set()
        pipe.close()
        if consumer:
            consumer.join(3)
