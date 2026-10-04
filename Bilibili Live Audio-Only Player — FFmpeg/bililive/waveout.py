"""基于 waveOut 的流式 PCM 播放器。

为什么不用 ffplay / mpv：它们会开窗口、且解码和输出绑在一起，不好控制。
这里只让 ffmpeg 干「解码」这一件事，播放交给 winmm.waveOut —— 短路径、无窗口、
易排错。

waveOut 流式播放要点（踩过才知道）:
  * 必须用 CALLBACK_EVENT，否则无法知道缓冲区何时播完，只能瞎 sleep。
  * 多个缓冲区轮转，不能只用一个 —— 单缓冲区会在两次 write 之间断音。
  * lpData 指向的内存必须保持有效直到 waveOutUnprepareHeader 返回，
    所以要持有 buffer 引用，不能被 GC 回收。
  * 必须等 WOM_DONE（事件被 set）才能 unprepare+复用，否则会爆音或崩溃。
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import POINTER, byref, c_void_p, c_uint
from ctypes import wintypes

winmm = ctypes.WinDLL("winmm")
kernel32 = ctypes.WinDLL("kernel32")

WAVE_MAPPER = c_uint(-1).value
WAVE_FORMAT_PCM = 1
CALLBACK_EVENT = 0x00050000
WAVE_HEADER_DONE = 0x00000001
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0x00000000


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_uint),
        ("nAvgBytesPerSec", ctypes.c_uint),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", c_void_p),
        ("dwBufferLength", ctypes.c_uint),
        ("dwBytesRecorded", ctypes.c_uint),
        ("dwUser", c_void_p),
        ("dwFlags", ctypes.c_uint),
        ("dwLoops", ctypes.c_uint),
        ("lpNext", c_void_p),
        ("reserved", c_void_p),
    ]


winmm.waveOutOpen.argtypes = [POINTER(c_void_p), ctypes.c_uint,
                              POINTER(WAVEFORMATEX), c_void_p, c_void_p, ctypes.c_uint]
winmm.waveOutOpen.restype = ctypes.c_uint
winmm.waveOutPrepareHeader.argtypes = [c_void_p, POINTER(WAVEHDR), ctypes.c_uint]
winmm.waveOutPrepareHeader.restype = ctypes.c_uint
winmm.waveOutWrite.argtypes = [c_void_p, POINTER(WAVEHDR), ctypes.c_uint]
winmm.waveOutWrite.restype = ctypes.c_uint
winmm.waveOutUnprepareHeader.argtypes = [c_void_p, POINTER(WAVEHDR), ctypes.c_uint]
winmm.waveOutUnprepareHeader.restype = ctypes.c_uint
winmm.waveOutClose.argtypes = [c_void_p]
winmm.waveOutClose.restype = ctypes.c_uint
winmm.waveOutReset.argtypes = [c_void_p]
winmm.waveOutReset.restype = ctypes.c_uint
winmm.waveOutGetNumDevs.argtypes = []
winmm.waveOutGetNumDevs.restype = ctypes.c_uint

kernel32.CreateEventW.argtypes = [c_void_p, wintypes.BOOL, wintypes.BOOL,
                                  wintypes.LPCWSTR]
kernel32.CreateEventW.restype = c_void_p
kernel32.WaitForSingleObject.argtypes = [c_void_p, ctypes.c_uint]
kernel32.WaitForSingleObject.restype = ctypes.c_uint
kernel32.CloseHandle.argtypes = [c_void_p]
kernel32.CloseHandle.restype = wintypes.BOOL


class WaveOutPlayer:
    """阻塞式流播放器：write() 传入 PCM 字节，内部做缓冲轮转。

    线程模型：调用者（通常是一个专门线程）从解码器读 PCM 并 write()；
    本类内部用事件同步等待缓冲区播完，因此 write() 会在必要时阻塞，
    这正是我们要的背压 —— 防止解码跑太快把内存吃满。
    """

    def __init__(self, sample_rate: int, channels: int, bits: int = 16,
                 buffers: int = 4, buffer_ms: int = 250, log=None):
        self.sample_rate = sample_rate
        self.channels = channels
        self.bits = bits
        self.block_align = channels * bits // 8
        self.buffer_bytes = int(sample_rate * buffer_ms / 1000) * self.block_align
        self.buffer_count = buffers
        self._log = log or (lambda m: None)

        self._handle = c_void_p()
        self._event = None
        self._lock = threading.Lock()
        self._headers: list[tuple[WAVEHDR, ctypes.Array, bool]] = []
        self._next_slot = 0
        self._opened = False
        self._closed = False

    # ------------------------------------------------------------ 打开/关闭

    def open(self) -> None:
        if self._opened:
            return
        if winmm.waveOutGetNumDevs() == 0:
            raise RuntimeError("系统没有可用的音频输出设备")

        fmt = WAVEFORMATEX(
            wFormatTag=WAVE_FORMAT_PCM,
            nChannels=self.channels,
            nSamplesPerSec=self.sample_rate,
            nAvgBytesPerSec=self.sample_rate * self.block_align,
            nBlockAlign=self.block_align,
            wBitsPerSample=self.bits,
            cbSize=0,
        )
        self._event = kernel32.CreateEventW(None, False, False, None)
        if not self._event:
            raise RuntimeError("CreateEventW 失败")

        hr = winmm.waveOutOpen(byref(self._handle), WAVE_MAPPER, byref(fmt),
                               self._event, None, CALLBACK_EVENT)
        if hr != 0:
            kernel32.CloseHandle(self._event)
            self._event = None
            raise RuntimeError(f"waveOutOpen 失败: 0x{hr & 0xFFFFFFFF:08X}"
                               f"（{self.sample_rate}Hz {self.channels}ch）")

        for _ in range(self.buffer_count):
            buf = ctypes.create_string_buffer(self.buffer_bytes)
            hdr = WAVEHDR(lpData=ctypes.cast(buf, c_void_p), dwBufferLength=0)
            hr = winmm.waveOutPrepareHeader(self._handle, byref(hdr),
                                            ctypes.sizeof(hdr))
            if hr != 0:
                raise RuntimeError(f"waveOutPrepareHeader 失败: "
                                   f"0x{hr & 0xFFFFFFFF:08X}")
            self._headers.append((hdr, buf, False))   # (头, 缓冲, 是否在播)

        self._opened = True
        self._log(f"waveOut 已打开: {self.sample_rate}Hz {self.channels}ch "
                  f"{self.bits}bit, {self.buffer_count}×{self.buffer_bytes}B")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._opened:
            return
        try:
            winmm.waveOutReset(self._handle)      # 丢弃未播完的数据，唤醒等待
        except Exception:
            pass
        with self._lock:
            for hdr, _buf, inflight in self._headers:
                if inflight:
                    kernel32.WaitForSingleObject(self._event, 1000)
                # 同样不碰 dwFlags —— WHDR_PREPARED 必须保留到 unprepare
                winmm.waveOutUnprepareHeader(self._handle, byref(hdr),
                                             ctypes.sizeof(hdr))
        winmm.waveOutClose(self._handle)
        if self._event:
            kernel32.CloseHandle(self._event)
            self._event = None
        self._opened = False

    # ------------------------------------------------------------ 写入

    def write(self, pcm: bytes) -> None:
        """写入 PCM，必要时阻塞等待空闲缓冲区。"""
        if self._closed or not self._opened:
            return
        view = memoryview(pcm)
        while view:
            slot = self._acquire_slot()
            hdr, buf, _ = self._headers[slot]
            n = min(len(view), self.buffer_bytes)
            chunk = view[:n]
            ctypes.memmove(buf, bytes(chunk), n)
            hdr.dwBufferLength = n
            # 绝对不要动 hdr.dwFlags！
            # waveOutPrepareHeader 会把它置为 WHDR_PREPARED(0x2)，
            # 一旦清零，waveOutWrite 立刻返回 34 = MMSYSERR_INVALPARAM。
            # （实测：保留 flags 或显式写回 WHDR_PREPARED 都能成功，
            #   清零必失败。MSDN 也明确说调用方不得修改 dwFlags。）
            with self._lock:
                self._headers[slot] = (hdr, buf, True)
            hr = winmm.waveOutWrite(self._handle, byref(hdr), ctypes.sizeof(hdr))
            if hr != 0:
                with self._lock:
                    self._headers[slot] = (hdr, buf, False)
                # 注意: 这里的返回值是 MMSYSERR 码（如 34），不是 HRESULT
                raise RuntimeError(f"waveOutWrite 失败: MMSYSERR={hr}"
                                   f"（33=UNSUPPORTED 34=INVALPARAM "
                                   f"5=INVALHANDLE 8=BADFORMAT）")
            view = view[n:]

    def _acquire_slot(self) -> int:
        """找一个空闲缓冲区；全都满载就等一个播完。"""
        for _ in range(self.buffer_count * 4):
            with self._lock:
                for i, (hdr, _b, inflight) in enumerate(self._headers):
                    if not inflight:
                        return i
            # 都在播，等任意一个完成
            kernel32.WaitForSingleObject(self._event, 200)
            self._reap()
        # 极端情况：强制回收一个，避免死锁
        with self._lock:
            for i, (hdr, buf, inflight) in enumerate(self._headers):
                if inflight:
                    # 标记为可复用（有一定风险，但胜过卡死）
                    self._headers[i] = (hdr, buf, False)
                    return i
        return 0

    def _reap(self) -> None:
        """把已完成播放的缓冲区标记为空闲。

        CALLBACK_EVENT 下 waveOut 会周期性 set 事件，我们无法直接知道是哪个
        缓冲区完成了，所以用 WHDR_DONE 标志位判断。
        """
        with self._lock:
            for i, (hdr, buf, inflight) in enumerate(self._headers):
                if inflight and (hdr.dwFlags & WAVE_HEADER_DONE):
                    self._headers[i] = (hdr, buf, False)

    def drain(self, timeout: float = 3.0) -> None:
        """等已写入的数据播完。"""
        if not self._opened:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not any(f for _h, _b, f in self._headers):
                    return
            kernel32.WaitForSingleObject(self._event, 100)
            self._reap()


def has_audio_device() -> bool:
    try:
        return winmm.waveOutGetNumDevs() > 0
    except Exception:
        return False
