"""探测 E：Media Foundation 的本地解码链路是否可用？

上一轮 MFCreateSourceReaderFromURL(http://...) 返回 0xC00D0035
(MF_E_UNSUPPORTED_BYTESTREAM_TYPE)，但那可能只是沙箱挡住了 MF 的 HTTP/WinHTTP
处理器，而非 MF 本身不可用。

本探测完全离线：先用 Python 生成一个本地 WAV 文件，再让 MF 的 SourceReader
从**本地文件**读它。若本地路径 OK，说明 MF 可用、只是网络处理器受限。

同时对比 waveOut，确定哪条是现实可行的输出路径。
"""

import ctypes
import math
import os
import struct
import sys
from ctypes import POINTER, byref, c_void_p, c_wchar_p, c_uint32

TMP = os.environ.get("TEMP", r"C:\Windows\Temp")
WAV = os.path.join(TMP, "dsh_probe_tone.wav")


def make_wav(path, seconds=1.0, rate=44100, freq=440.0):
    n = int(rate * seconds)
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / (rate * 0.05), (n - i) / (rate * 0.05))
        frames += struct.pack("<h", int(8000 * env * math.sin(2 * math.pi * freq * i / rate)))
    data_size = len(frames)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
        f.write(b"data" + struct.pack("<I", data_size))
        f.write(frames)
    return data_size


# ------------------------------------------------------------------ waveOut

class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_uint), ("nAvgBytesPerSec", ctypes.c_uint),
                ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", c_void_p), ("dwBufferLength", ctypes.c_uint),
                ("dwBytesRecorded", ctypes.c_uint), ("dwUser", c_void_p),
                ("dwFlags", ctypes.c_uint), ("dwLoops", ctypes.c_uint),
                ("lpNext", c_void_p), ("reserved", c_void_p)]


def play_wav_via_waveout(path):
    with open(path, "rb") as f:
        raw = f.read()
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        chunk = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", chunk[:16])
        elif cid == b"data":
            data = chunk
        pos += 8 + size + (size & 1)
    wf = WAVEFORMATEX(wFormatTag=fmt[0], nChannels=fmt[1], nSamplesPerSec=fmt[2],
                      nAvgBytesPerSec=fmt[3], nBlockAlign=fmt[4],
                      wBitsPerSample=fmt[5], cbSize=0)
    handle = c_void_p()
    hr = ctypes.windll.winmm.waveOutOpen(byref(handle), c_uint32(-1), byref(wf), 0, 0, 0)
    if hr != 0:
        return f"waveOutOpen 0x{hr & 0xFFFFFFFF:08X}"
    buf = ctypes.create_string_buffer(data)
    hdr = WAVEHDR(lpData=ctypes.cast(buf, c_void_p), dwBufferLength=len(data))
    ctypes.windll.winmm.waveOutPrepareHeader(handle, byref(hdr), ctypes.sizeof(hdr))
    hr = ctypes.windll.winmm.waveOutWrite(handle, byref(hdr), ctypes.sizeof(hdr))
    if hr != 0:
        return f"waveOutWrite 0x{hr & 0xFFFFFFFF:08X}"
    import time
    time.sleep(len(data) / (fmt[2] * fmt[4]) + 0.2)
    ctypes.windll.winmm.waveOutUnprepareHeader(handle, byref(hdr), ctypes.sizeof(hdr))
    ctypes.windll.winmm.waveOutClose(handle)
    return f"OK，播放了 {len(data)} 字节 PCM"


# ------------------------------------------------------- Media Foundation

def mf_read_local(path):
    """让 MF SourceReader 从本地文件读，完全离线。"""
    mfplat = ctypes.windll.mfplat
    mfreadwrite = ctypes.windll.mfreadwrite
    ole32 = ctypes.windll.ole32

    MF_VERSION = (0x0002 << 16) | 70
    hr = ole32.CoInitializeEx(None, 0x2)
    if hr < 0 and (hr & 0xFFFFFFFF) != 0x80010106:
        return f"CoInitializeEx 0x{hr & 0xFFFFFFFF:08X}"
    hr = mfplat.MFStartup(MF_VERSION, 0)
    if hr < 0:
        return f"MFStartup 0x{hr & 0xFFFFFFFF:08X}"

    reader = c_void_p()
    hr = mfreadwrite.MFCreateSourceReaderFromURL(c_wchar_p(path), None, byref(reader))
    hx = hr & 0xFFFFFFFF
    if hx != 0:
        mfplat.MFShutdown()
        return f"MFCreateSourceReaderFromURL(本地) -> 0x{hx:08X}"

    # 读第一个 sample，确认真能解出数据
    got = "（未读 sample）"
    try:
        # IMFSourceReader::ReadSample 是 vtable 第 8 个方法
        vtbl = ctypes.cast(reader, POINTER(POINTER(c_void_p)))[0]
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, c_void_p, c_uint32, c_uint32,
                                   POINTER(c_uint32), POINTER(c_uint32),
                                   POINTER(ctypes.c_long), POINTER(c_void_p))
        ReadSample = proto(vtbl[8])
        stream_index = c_uint32(0)
        flags = c_uint32(0)
        timestamp = ctypes.c_long(0)
        sample = c_void_p()
        hr2 = ReadSample(reader, 0xFFFFFFFD, 0, byref(stream_index), byref(flags),
                         byref(timestamp), byref(sample))
        got = f"ReadSample -> 0x{hr2 & 0xFFFFFFFF:08X} sample={'有' if sample else '空'}"
    except Exception as e:
        got = f"ReadSample 异常 {type(e).__name__}: {e}"

    mfplat.MFShutdown()
    return f"本地文件 OK；{got}"


def main():
    print("=" * 68)
    size = make_wav(WAV)
    print(f"已生成测试 WAV: {WAV}  ({size} 字节 PCM)")

    print("\n[E1] waveOut 播放本地 WAV")
    try:
        print("     " + play_wav_via_waveout(WAV))
        print("     -> 你应该听到约 1 秒 440Hz 轻响")
    except Exception as e:
        print(f"     失败 {type(e).__name__}: {e}")

    print("\n[E2] Media Foundation 读本地 WAV（离线，用于隔离沙箱影响）")
    try:
        print("     " + mf_read_local(WAV))
    except Exception as e:
        print(f"     异常 {type(e).__name__}: {e}")
    print("=" * 68)


if __name__ == "__main__":
    main()
