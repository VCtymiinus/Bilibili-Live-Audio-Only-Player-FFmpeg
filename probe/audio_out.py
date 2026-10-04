"""探测 A：Windows 原生音频输出能力（无 ffmpeg / 无浏览器）。

分两级：
  A1. winmm.waveOut —— 直接推 PCM，最低风险。
  A2. mfplat/mfreadwrite —— 探测 Media Foundation 是否可用（B 站是 AAC，需要 MP3/AAC 解码）。

只做能力判定，不产生刺耳长音；成功会听到约 0.6 秒轻响。
"""

import ctypes
import math
import struct
import sys
from ctypes import POINTER, byref, c_void_p, c_uint32, c_ulong, c_wchar_p

# ---------------------------------------------------------------- A1: waveOut

waveOutOpen = ctypes.windll.winmm.waveOutOpen
waveOutPrepareHeader = ctypes.windll.winmm.waveOutPrepareHeader
waveOutWrite = ctypes.windll.winmm.waveOutWrite
waveOutClose = ctypes.windll.winmm.waveOutClose
waveOutUnprepareHeader = ctypes.windll.winmm.waveOutUnprepareHeader

WAVE_MAPPER = ctypes.c_uint(-1)
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0


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
        ("dwUser", ctypes.c_void_p),
        ("dwFlags", ctypes.c_uint),
        ("dwLoops", ctypes.c_uint),
        ("lpNext", c_void_p),
        ("reserved", c_void_p),
    ]


def beep_via_waveout(seconds=0.6, freq=440.0, rate=44100):
    fmt = WAVEFORMATEX(
        wFormatTag=WAVE_FORMAT_PCM,
        nChannels=1,
        nSamplesPerSec=rate,
        nAvgBytesPerSec=rate * 2,
        nBlockAlign=2,
        wBitsPerSample=16,
        cbSize=0,
    )
    handle = ctypes.c_void_p()
    hr = waveOutOpen(byref(handle), WAVE_MAPPER, byref(fmt), 0, 0, CALLBACK_NULL)
    if hr != 0:
        raise OSError(f"waveOutOpen 失败: 0x{hr & 0xFFFFFFFF:08X}")

    n = int(rate * seconds)
    # 带淡入淡出，避免爆音
    pcm = bytearray()
    for i in range(n):
        env = min(1.0, i / (rate * 0.05), (n - i) / (rate * 0.05))
        pcm += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * freq * i / rate)))

    buf = ctypes.create_string_buffer(bytes(pcm))
    hdr = WAVEHDR(lpData=ctypes.cast(buf, c_void_p), dwBufferLength=len(pcm))
    if waveOutPrepareHeader(handle, byref(hdr), ctypes.sizeof(hdr)) != 0:
        raise OSError("waveOutPrepareHeader 失败")
    if waveOutWrite(handle, byref(hdr), ctypes.sizeof(hdr)) != 0:
        raise OSError("waveOutWrite 失败")

    ctypes.windll.winmm.waveOutUnprepareHeader(handle, byref(hdr), ctypes.sizeof(hdr))
    waveOutClose(handle)
    return len(pcm)


# ------------------------------------------------------- A2: Media Foundation

def probe_media_foundation():
    """只判断 MF 能否启动、能否创建 SourceReader（B 站 AAC 解码要靠它）。"""
    mfplat = ctypes.windll.mfplat
    mfreadwrite = ctypes.windll.mfreadwrite
    ole32 = ctypes.windll.ole32

    MF_VERSION = (0x0002 << 16) | 70
    hr = ole32.CoInitializeEx(None, 0x2)
    if hr < 0 and hr != 0x80010106:  # RPC_E_CHANGED_MODE 可容忍
        return False, f"CoInitializeEx 0x{hr & 0xFFFFFFFF:08X}"
    hr = mfplat.MFStartup(MF_VERSION, 0)
    if hr < 0:
        return False, f"MFStartup 0x{hr & 0xFFFFFFFF:08X}"

    # 用一个不存在的 URL 创建 SourceReader：只要不是 E_INVALIDARG / 找不到 DLL 即说明组件在
    reader = c_void_p()
    hr = mfreadwrite.MFCreateSourceReaderFromURL(
        c_wchar_p("http://127.0.0.1:1/none.flv"), None, byref(reader)
    )
    hx = hr & 0xFFFFFFFF
    mfplat.MFShutdown()
    # 0x80070002 文件未找到 / 0xC00D36C4 不支持格式 都说明 MF 链路是活的
    alive = hx in (0x80070002, 0xC00D36C4, 0x80072EE7, 0x800C0005, 0x80070005)
    return alive, f"MFCreateSourceReaderFromURL -> 0x{hx:08X}"


def main():
    print("=" * 60)
    try:
        n = beep_via_waveout()
        print(f"[A1] waveOut OK —— 已推送 {n} 字节 PCM，应听到约 0.6 秒轻响")
    except Exception as e:
        print(f"[A1] waveOut 失败: {e}")

    print("-" * 60)
    try:
        ok, detail = probe_media_foundation()
        print(f"[A2] Media Foundation {'可用' if ok else '可疑'} —— {detail}")
    except Exception as e:
        print(f"[A2] Media Foundation 探测异常: {type(e).__name__} {e}")
    print("=" * 60)


if __name__ == "__main__":
    sys.exit(main())
