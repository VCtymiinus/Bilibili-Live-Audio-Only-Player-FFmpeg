"""验证：是不是我把 dwFlags 清零、抹掉了 WHDR_PREPARED 导致 write 失败？

waveOutPrepareHeader 会把 dwFlags 置为 WHDR_PREPARED(0x2)。
MSDN 明确说调用方不应修改 dwFlags。之前为了「干净」把它清零，很可能就是
MMSYSERR_INVALPARAM(34) 的根因。

对照三种写法：
  A. 保留 prepare 设的 flags（不清零）
  B. 清零 flags
  C. 显式保留 WHDR_PREPARED 位
"""

import ctypes
import math
import struct
from ctypes import POINTER, byref, c_void_p, c_uint
from ctypes import wintypes

winmm = ctypes.WinDLL("winmm")
kernel32 = ctypes.WinDLL("kernel32")

# 这次显式声明 argtypes/restype，排除绑定因素
winmm.waveOutOpen.argtypes = [POINTER(c_void_p), c_uint, c_void_p,
                              c_void_p, c_void_p, c_uint]
winmm.waveOutOpen.restype = c_uint
winmm.waveOutPrepareHeader.argtypes = [c_void_p, c_void_p, c_uint]
winmm.waveOutPrepareHeader.restype = c_uint
winmm.waveOutWrite.argtypes = [c_void_p, c_void_p, c_uint]
winmm.waveOutWrite.restype = c_uint
winmm.waveOutReset.argtypes = [c_void_p]
winmm.waveOutReset.restype = c_uint
winmm.waveOutClose.argtypes = [c_void_p]
winmm.waveOutClose.restype = c_uint

RATE, CH = 48000, 2
WHDR_PREPARED = 0x2


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort),
                ("nSamplesPerSec", ctypes.c_uint), ("nAvgBytesPerSec", ctypes.c_uint),
                ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
                ("cbSize", ctypes.c_ushort)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", c_void_p), ("dwBufferLength", ctypes.c_uint),
                ("dwBytesRecorded", ctypes.c_uint), ("dwUser", c_void_p),
                ("dwFlags", ctypes.c_uint), ("dwLoops", ctypes.c_uint),
                ("lpNext", c_void_p), ("reserved", c_void_p)]


def tone(nbytes):
    n = nbytes // 4
    out = bytearray()
    for i in range(n):
        v = int(9000 * math.sin(2 * math.pi * 440 * i / RATE))
        out += struct.pack("<hh", v, v)
    return bytes(out[:nbytes])


def trial(label, mode, blen=9600):
    fmt = WAVEFORMATEX(wFormatTag=1, nChannels=CH, nSamplesPerSec=RATE,
                       nAvgBytesPerSec=RATE * CH * 2, nBlockAlign=CH * 2,
                       wBitsPerSample=16, cbSize=0)
    h = c_void_p()
    r = winmm.waveOutOpen(byref(h), 0xFFFFFFFF, byref(fmt), None, None, 0)
    if r:
        print(f"  [{label}] open 失败 {r}")
        return
    buf = ctypes.create_string_buffer(blen)
    data = tone(blen)
    ctypes.memmove(buf, data, blen)
    hdr = WAVEHDR(lpData=ctypes.cast(buf, c_void_p), dwBufferLength=blen)
    pr = winmm.waveOutPrepareHeader(h, byref(hdr), ctypes.sizeof(hdr))
    flags_after_prepare = hdr.dwFlags

    if mode == "keep":
        pass                                  # 原样保留
    elif mode == "zero":
        hdr.dwFlags = 0
    elif mode == "prepared":
        hdr.dwFlags = WHDR_PREPARED

    wr = winmm.waveOutWrite(h, byref(hdr), ctypes.sizeof(hdr))
    print(f"  [{label}] prepare={pr} flags_after_prepare=0x{flags_after_prepare:X} "
          f"flags_at_write=0x{hdr.dwFlags:X} -> write={wr}"
          + ("   <-- 成功！" if wr == 0 else ""))
    if wr == 0:
        kernel32.WaitForSingleObject(
            kernel32.CreateEventW(None, False, False, None), 300)
    winmm.waveOutReset(h)
    winmm.waveOutClose(h)


print("=" * 68)
trial("A 保留 prepare 的 flags", "keep")
trial("B 清零 flags", "zero")
trial("C 显式 WHDR_PREPARED", "prepared")
print("=" * 68)
