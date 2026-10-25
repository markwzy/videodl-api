"""用本机管道合成音视频，媒体不落盘，合成进程不接收远程地址。"""

import os
import subprocess
from threading import BoundedSemaphore, Lock, Thread

from fastapi import HTTPException


_slots = BoundedSemaphore(2)


class MuxStream:
    def __init__(self, video, audio):
        self._responses = (video, audio)
        self._process = None
        self._lock = Lock()
        self._closed = False
        self._writers = []
        self._audio_pipe = None
        if not _slots.acquire(blocking=False):
            video.close()
            audio.close()
            raise HTTPException(status_code=503, detail="视频合成任务已满，请稍后下载", headers={"Retry-After": "5"})
        read_fd = write_fd = None
        try:
            process_options = {}
            if os.name == "nt":
                from .windows_pipe import WindowsPipe

                self._audio_pipe = WindowsPipe()
                # 文件协议只用于随机生成的本机管道；限定容器格式，不能解释外部播放列表。
                audio_input = ["-protocol_whitelist", "file", "-f", "mov", "-seekable", "0", "-i", self._audio_pipe.path]
                process_options["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                read_fd, write_fd = os.pipe()
                audio_input = ["-protocol_whitelist", "pipe", "-i", f"pipe:{read_fd}"]
                process_options["pass_fds"] = (read_fd,)
            self._process = subprocess.Popen([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                "-protocol_whitelist", "pipe", "-i", "pipe:0",
                *audio_input,
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4", "pipe:1",
            ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **process_options)
            if read_fd is not None:
                os.close(read_fd)
                read_fd = None
                self._audio_pipe = os.fdopen(write_fd, "wb")
                write_fd = None
            self._writers = [self._process.stdin, self._audio_pipe]
            # 两条流必须并行喂入；管道背压限制内存，不能先把一条完整读完再处理另一条。
            for response, writer in zip(self._responses, self._writers):
                Thread(target=self._pump, args=(response, writer), daemon=True).start()
        except Exception as exc:
            for fd in (read_fd, write_fd):
                if fd is not None:
                    os.close(fd)
            self.close()
            raise HTTPException(status_code=503, detail="无法启动视频合成，请检查服务中的媒体处理依赖") from exc

    def _pump(self, response, writer):
        try:
            with writer:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    writer.write(chunk)
        except Exception:
            # 输入中断不能让另一条流和合成进程继续挂住；断连也通过相同路径回收。
            if self._process.poll() is None:
                self._process.terminate()
        finally:
            response.close()

    def __iter__(self):
        return self

    def __next__(self):
        chunk = self._process.stdout.read1(64 * 1024)
        if chunk:
            return chunk
        code = self._process.wait()
        self.close()
        if code:
            raise RuntimeError("音视频合成中断，请重新解析后下载")
        raise StopIteration

    def close(self):
        # 流迭代退出和响应后台清理都会调用，资源与并发名额只能释放一次。
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                if self._process is not None:
                    if self._process.poll() is None:
                        self._process.terminate()
                        try:
                            self._process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            self._process.kill()
                            self._process.wait()
                    self._process.stdout.close()
                if self._audio_pipe is not None:
                    self._audio_pipe.close()
                for response in self._responses:
                    response.close()
            finally:
                _slots.release()
