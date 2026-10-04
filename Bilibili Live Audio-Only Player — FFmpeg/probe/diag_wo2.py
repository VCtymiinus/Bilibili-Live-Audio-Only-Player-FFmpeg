"""继续缩小：单次 waveOutWrite 就失败，逐个变量试。

怀疑方向:
  A. dwBufferLength 超过某个上限
  B. 设备句柄本身可疑
  C. CALLBACK_EVENT 配置方式
  D. 回调参数/窗口句柄
"""

import ctypes
import math
import struct
import sys
from ctypes import POINTER, byref, c_void_p, c_uint, c_ulong
from ctypes import wintypes

winmm = ctypes.WinDLL("winmm")
kernel32 = ctypes.WinDLL("kernel32")

winmm.waveOutOpen.argtypes = [POINTER(c_void_p), c_uint,
                              ctypes.c_void_p, c_void_p, c_void_p, c_uint]
winmm.waveOutOpen.restype = c_uint
winmm.waveOutGetDevCapsW.argtypes = [c_uint, c_void_p, c_uint]
winmm.waveOutGetDevCapsW.restype = c_uint

RATE, CH = 48000, 2


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


class WAVEOUTCAPS(ctypes.Structure):
    _fields_ = [("wMid", ctypes.c_ushort), ("wPid", ctypes.c_ushort),
                ("vDriverVersion", ctypes.c_uint),
                ("szPname", ctypes.c_wchar * 32),
                ("dwFormats", ctypes.c_uint), ("wChannels", ctypes.c_ushort),
                ("wReserved1", ctypes.c_ushort), ("dwSupport", ctypes.c_uint)]


def tone(nbytes):
    n = nbytes // 4
    out = bytearray()
    for i in range(n):
        v = int(9000 * math.sin(2 * math.pi * 440 * i / RATE))
        out += struct.pack("<hh", v, v)
    return bytes(out[:nbytes])


print("=" * 68)
caps = WAVEOUTCAPS()
r = winmm.waveOutGetDevCapsW(0, byref(caps), ctypes.sizeof(caps))
print(f"设备 0: r={r} 名称={caps.szPname!r} 声道={caps.wChannels} "
      f"formats=0x{caps.dwFormats:X}")

print(f"\nWAVEHDR 大小 = {ctypes.sizeof(WAVEHDR)} （64 位应为 48）")
print(f"WAVEFORMATEX 大小 = {ctypes.sizeof(WAVEFORMATEX)} （应为 18）")

fmt = WAVEFORMATEX(wFormatTag=1, nChannels=CH, nSamplesPerSec=RATE,
                   nAvgBytesPerSec=RATE * CH * 2, nBlockAlign=CH * 2,
                   wBitsPerSample=16, cbSize=0)

for cb_mode, cb_val, label in (
        (0x00000000, 0, "CALLBACK_NULL"),
        (0x00050000, None, "CALLBACK_EVENT")):
    print(f"\n--- {label} ---")
    h = c_void_p()
    if cb_mode == 0x00050000:
        cb_val = kernel32.CreateEventW(None, False, False, None)
    r = winmm.waveOutOpen(byref(h), 0xFFFFFFFF, byref(fmt), cb_val, None, cb_mode)
    print(f"  open -> {r}  handle=0x{h.value or 0:X}")

    # 试不同 buffer 长度，看是否与大小有关
    for blen in (1024, 4800, 9600, 19200):
        buf = ctypes.create_string_buffer(blen)
        data = tone(blen)
        ctypes.memmove(buf, data, blen)
        hdr = WAVEHDR(lpData=ctypes.cast(buf, c_void_p), dwBufferLength=0)
        pr = winmm.waveOutPrepareHeader(h, byref(hdr), ctypes.sizeof(hdr))
        hdr.dwBufferLength = blen
        hdr.dwFlags = 0
        wr = winmm.waveOutWrite(h, byref(hdr), ctypes.sizeof(hdr))
        print(f"  len={blen:<6} prepare={pr} write={wr}"
              + ("   <-- OK" if wr == 0 else ""))
        if wr == 0:
            break
    if h:
        winmm.waveOutReset(h)
        winmm.waveOutClose(h)
    if cb_mode == 0x00050000 and cb_val:
        kernel32.CloseHandle(cb_val)

print("=" * 68)
