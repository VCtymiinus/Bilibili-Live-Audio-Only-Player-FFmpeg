"""探测 G：mciSendString 能否出声？

MF 的 SourceReader 在沙箱内被 ACCESS_DENIED，所以需要一个**能在沙箱内验证**的
输出后端。MCI 是 Windows 老接口，走 msvcrt/winmm，路径短、依赖少，值得一试。

若能出声 -> 我就能在受限环境里自证「解码+播放」可跑，而不是全押在用户机器上。
"""

import ctypes
import math
import os
import struct
import sys
import time
from ctypes import wintypes

winmm = ctypes.WinDLL("winmm")
winmm.mciSendStringW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                 wintypes.UINT, wintypes.HWND]
winmm.mciSendStringW.restype = wintypes.UINT

TMP = os.environ.get("TEMP", r"C:\Windows\Temp")
WAV = os.path.join(TMP, "dsh_probe_mci.wav")


def make_wav(path, seconds=1.2, rate=44100, freq=440.0):
    n = int(rate * seconds)
    frames = bytearray()
    for i in range(n):
        env = min(1.0, i / (rate * 0.05), (n - i) / (rate * 0.05))
        frames += struct.pack("<h", int(8000 * env * math.sin(2 * math.pi * freq * i / rate)))
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
        f.write(b"data" + struct.pack("<I", len(frames)) + bytes(frames))
    return len(frames)


def mci(cmd):
    buf = ctypes.create_unicode_buffer(512)
    err = winmm.mciSendStringW(cmd, buf, 512, None)
    if err:
        ebuf = ctypes.create_unicode_buffer(512)
        winmm.mciGetErrorStringW(err, ebuf, 512)
        return False, f"err={err} {ebuf.value}"
    return True, buf.value


def main():
    print("=" * 68)
    n = make_wav(WAV)
    print(f"生成 WAV: {WAV} ({n} 字节 PCM)")

    # 先试最简单的：MCI 直接播放波形文件
    ok, msg = mci(f'open "{WAV}" type waveaudio alias probe')
    print(f"[G1] open  -> {'OK' if ok else 'FAIL'} {msg}")
    if not ok:
        print("!! MCI waveaudio 不可用")
        print("=" * 68)
        return 1

    ok, msg = mci("play probe wait")
    print(f"[G2] play  -> {'OK' if ok else 'FAIL'} {msg}")
    mci("close probe")
    print("     -> 你应该听到约 1.2 秒 440Hz 轻响")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
