"""把 AAC 帧解成 PCM 播放。

分工（这是刻意的设计）:
    ffmpeg  只负责 AAC -> PCM 解码
    waveOut 负责出声

为什么不直接把流地址丢给 ffplay/mpv：
  * 它们开窗口、把解码和输出绑死，出问题不好定位。
  * 这里让每一段都可单独验证：收流、解复用、解码、输出。
    这样即使在没有声卡的环境里也能验证前三段（用 RMS 判断是否静音）。
"""

from __future__ import annotations

import os
import queue
import select
import shutil
import subprocess
import sys
import threading
import time

from .waveout import WaveOutPlayer, has_audio_device

# 解码目标格式：AAC-LC 源就是 48kHz 立体声，直接对齐最省事
OUT_RATE = 48000
OUT_CHANNELS = 2
OUT_BITS = 16
PCM_CHUNK = 8192
# 输入攒批阈值。不能设太大：攒着不写会让 ffmpeg 收不到数据而不产出 PCM，
# 表现出来就是「一片安静」。4KB 约 0.17 秒音频，兼顾系统调用次数与延迟。
INPUT_BATCH_BYTES = 4096

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def find_ffmpeg() -> str | None:
    """按优先级找 ffmpeg：环境变量 -> PATH -> 工具目录 -> 常见安装位置。"""
    env = os.environ.get("BILILIVE_FFMPEG")
    if env and os.path.isfile(env):
        return env

    found = shutil.which("ffmpeg")
    if found:
        return found

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(here, "tools", "ffmpeg.exe"),
        os.path.join(here, "tools", "ffmpeg", "bin", "ffmpeg.exe"),
        os.path.join(here, "ffmpeg.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c

    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        for sub in ("ffmpeg/bin/ffmpeg.exe", "ffmpeg/ffmpeg.exe"):
            p = os.path.join(base, sub.replace("/", os.sep))
            if os.path.isfile(p):
                return p
    return None


class FfmpegDecoder:
    """常驻 ffmpeg 进程：stdin 喂 AAC，stdout 出 PCM。

    输入用 ADTS 而非裸帧：FLV 里抠出来的是**无同步字的裸 AAC 帧**，
    `-f aac`（按 ADTS 解析）遇到裸帧可能直接判为无效数据丢掉。
    所以我们自己拼 7 字节 ADTS 头（见 flvdemux.build_adts_header），
    再以 `-f aac` 喂进去，解析就稳定了。
    """

    def __init__(self, ffmpeg: str | None = None, log=None):
        self.ffmpeg = ffmpeg or find_ffmpeg()
        self._log = log or (lambda m: None)
        self._proc: subprocess.Popen | None = None
        self._stderr_tail: list[str] = []
        self._reader: threading.Thread | None = None
        # PCM 经由队列从中转线程交给消费端，避免任何人阻塞在管道上
        self._pcm_q: queue.Queue[bytes] = queue.Queue(maxsize=512)
        self._pcm_done = threading.Event()
        self._pcm_thread: threading.Thread | None = None
        self._inbuf = bytearray()

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg)

    def _cmd(self) -> list[str]:
        return [
            self.ffmpeg,
            "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-fflags", "nobuffer",
            "-f", "aac",                    # 输入按 ADTS 解析（我们已补好头）
            "-i", "pipe:0",
            "-f", "s16le",                  # 输出裸 PCM
            "-acodec", "pcm_s16le",
            "-ar", str(OUT_RATE),
            "-ac", str(OUT_CHANNELS),
            "pipe:1",
        ]

    def start(self) -> None:
        if self._proc:
            return
        if not self.ffmpeg:
            raise RuntimeError("找不到 ffmpeg")
        creationflags = _CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            self._cmd(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
        )
        # 读 stderr，否则 ffmpeg 写满管道会卡死
        self._reader = threading.Thread(target=self._drain_stderr,
                                        name="ffmpeg-stderr", daemon=True)
        self._reader.start()
        # 独立线程持续抽 PCM 进队列。
        # 不能在主循环里轮询非阻塞读：喂一帧 AAC 时 ffmpeg 往往还没产出 PCM，
        # 轮询会读到「暂时没有」而把这段 PCM 丢掉，表现为断断续续或没声音。
        self._pcm_thread = threading.Thread(target=self._pump_pcm,
                                            name="ffmpeg-pcm", daemon=True)
        self._pcm_thread.start()
        self._log("ffmpeg 解码进程已启动")

    def _pump_pcm(self) -> None:
        """把 ffmpeg 的 PCM 输出源源不断搬进队列。"""
        assert self._proc and self._proc.stdout
        fd = self._proc.stdout.fileno()
        while self._proc is not None:
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(fd, PCM_CHUNK)
            except (OSError, ValueError):
                break
            if not data:
                break
            try:
                self._pcm_q.put(data, timeout=5.0)
            except queue.Full:
                # 消费端太慢，丢最旧的，保证不拖住解码
                try:
                    self._pcm_q.get_nowait()
                except queue.Empty:
                    pass
        self._pcm_done.set()

    def _drain_stderr(self) -> None:
        try:
            assert self._proc and self._proc.stderr
            for raw in iter(self._proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._stderr_tail.append(line)
                    del self._stderr_tail[:-20]
        except Exception:
            pass

    def feed(self, aac: bytes, adts: bytes | None = None) -> None:
        """喂一帧音频。优先用带 ADTS 头的版本，没有就退回裸帧。

        内部先攒批再 flush：直播每帧只有几百字节，逐帧 write+flush 会产生
        大量系统调用，是播放卡顿的常见原因。攒到 ~16KB 或调用 flush() 时
        才真正写给 ffmpeg。
        """
        if not self._proc or not self._proc.stdin:
            return
        self._inbuf += adts if adts else aac
        if len(self._inbuf) >= INPUT_BATCH_BYTES:
            self.flush()

    def flush(self) -> None:
        """把攒着的输入立刻写给 ffmpeg。"""
        if not self._proc or not self._proc.stdin or not self._inbuf:
            return
        payload, self._inbuf = self._inbuf, bytearray()
        try:
            self._proc.stdin.write(bytes(payload))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise RuntimeError("ffmpeg 解码进程已退出: "
                               + " | ".join(self._stderr_tail[-3:]))

    def read_pcm(self, timeout: float = 0.0) -> bytes | None:
        """从队列取一块已解码 PCM。

        返回 None 表示这段时间内没有新数据（正常，不是错误）。

        这里绝不直接阻塞在管道上：早期版本用阻塞式 os.read，一旦 ffmpeg
        因为任何原因不吐数据，整个程序会永久挂死且没有任何输出。
        现在由 _pump_pcm 线程负责读管道，本方法只取队列，天然带超时。
        """
        if self._pcm_done.is_set() and self._pcm_q.empty():
            return b""      # 解码进程已结束且数据取完
        try:
            return self._pcm_q.get(timeout=timeout) if timeout > 0 \
                else self._pcm_q.get_nowait()
        except queue.Empty:
            return None

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if not proc:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        # 唤醒可能等在队列上的消费者
        self._pcm_done.set()
        if self._pcm_thread:
            self._pcm_thread.join(timeout=2.0)
        self._inbuf.clear()

    @property
    def stderr_tail(self) -> str:
        return " | ".join(self._stderr_tail[-5:])


def pcm_rms(pcm: bytes) -> float:
    """算 PCM 的 RMS（0..1）。用于客观判断「是否真的在出声」而非静音。

    这是关键的自检手段：不需要声卡就能确认解码出了真实音频。
    """
    if len(pcm) < 2:
        return 0.0
    import array
    if len(pcm) % 2:
        pcm = pcm[:-1]
    a = array.array("h")
    a.frombytes(pcm)
    if not len(a):
        return 0.0
    # 抽样即可，避免长音频算太慢
    step = max(1, len(a) // 20000)
    total = 0
    count = 0
    for i in range(0, len(a), step):
        v = a[i] / 32768.0
        total += v * v
        count += 1
    return (total / count) ** 0.5 if count else 0.0


def describe_backends() -> list[dict]:
    """列出各输出后端的可用性，供 CLI 自检展示。"""
    ff = find_ffmpeg()
    out = []

    out.append({
        "name": "mf",
        "available": True,      # 运行时才能真正确定，见 output_mf
        "detail": "Media Foundation 内存字节流解码（零外部依赖，首次运行需实测）",
    })
    out.append({
        "name": "ffmpeg",
        "available": bool(ff) and has_audio_device(),
        "detail": (f"ffmpeg 解码 + waveOut 输出（ffmpeg={ff or '未找到'}，"
                   f"声卡={'有' if has_audio_device() else '无'}）"),
    })
    return out
