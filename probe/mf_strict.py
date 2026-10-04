"""探测 F：严格声明 ctypes 类型后重测 Media Foundation。

探测 E 在本地文件上也报 0x80070005(ACCESS_DENIED)，可疑 —— 因为 ctypes 未声明
argtypes 时，c_wchar_p 对象可能被当成 Python 对象指针传递（64 位下有截断风险），
而 ole32.CoInitializeEx(None, 0x2) 用的是默认 int 转换。

本探测把 mfplat / mfreadwrite / ole32 的 argtypes 全部显式声明，排除绑定问题。
目的：判定 MF 到底是「沙箱不可用」还是「我刚才写错了」。
"""

import ctypes
import os
import struct
import sys
from ctypes import POINTER, byref, c_void_p, c_uint32, c_wchar_p, WINFUNCTYPE

TMP = os.environ.get("TEMP", r"C:\Windows\Temp")
WAV = os.path.join(TMP, "dsh_probe_tone.wav")

HRESULT = ctypes.c_long

ole32 = ctypes.WinDLL("ole32")
mfplat = ctypes.WinDLL("mfplat")
mfreadwrite = ctypes.WinDLL("mfreadwrite")

ole32.CoInitializeEx.argtypes = [c_void_p, ctypes.c_ulong]
ole32.CoInitializeEx.restype = HRESULT
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None

mfplat.MFStartup.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
mfplat.MFStartup.restype = HRESULT
mfplat.MFShutdown.argtypes = []
mfplat.MFShutdown.restype = HRESULT

mfreadwrite.MFCreateSourceReaderFromURL.argtypes = [c_wchar_p, c_void_p,
                                                    POINTER(c_void_p)]
mfreadwrite.MFCreateSourceReaderFromURL.restype = HRESULT

MF_VERSION = (0x0002 << 16) | 70
MFSTARTUP_FULL = 0


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def guid(s):
    s = s.strip("{}")
    p = s.split("-")
    g = GUID()
    g.Data1, g.Data2, g.Data3 = int(p[0], 16), int(p[1], 16), int(p[2], 16)
    tail = p[3] + p[4]
    for i in range(8):
        g.Data4[i] = int(tail[i * 2:i * 2 + 2], 16)
    return g


MFMediaType_Audio = guid("73647561-0000-0010-8000-00AA00389B71")
MFAudioFormat_PCM = guid("00000001-0000-0010-8000-00AA00389B71")
MF_MT_MAJOR_TYPE = 48
MF_MT_SUBTYPE = 49
MF_SOURCE_READER_FIRST_AUDIO_STREAM = 0xFFFFFFFD


def ensure_wav():
    if os.path.exists(WAV):
        return WAV
    rate, seconds = 44100, 0.5
    n = int(rate * seconds)
    frames = bytearray()
    import math
    for i in range(n):
        frames += struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
    with open(WAV, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
        f.write(b"data" + struct.pack("<I", len(frames)) + bytes(frames))
    return WAV


def main():
    wav = ensure_wav()
    print("=" * 68)

    hr = ole32.CoInitializeEx(None, 0x2)
    print(f"CoInitializeEx           -> 0x{hr & 0xFFFFFFFF:08X}"
          f"{' (RPC_E_CHANGED_MODE, 可容忍)' if (hr & 0xFFFFFFFF) == 0x80010106 else ''}")

    hr = mfplat.MFStartup(MF_VERSION, MFSTARTUP_FULL)
    print(f"MFStartup                -> 0x{hr & 0xFFFFFFFF:08X}")
    if hr < 0:
        print("!! MF 无法启动，这条路线在沙箱内不可用")
        return

    print(f"\n测试本地文件: {wav}")
    reader = c_void_p()
    hr = mfreadwrite.MFCreateSourceReaderFromURL(wav, None, byref(reader))
    hx = hr & 0xFFFFFFFF
    print(f"MFCreateSourceReaderFromURL(本地) -> 0x{hx:08X}")

    if hx == 0:
        print("*** MF 本地读取可用！")
        # 配置输出为 PCM，确认能解出音频
        vtbl = ctypes.cast(reader, POINTER(POINTER(c_void_p)))[0]

        # GetCurrentMediaType 是 vtable 第 4 个方法
        GetCurrentMediaType = WINFUNCTYPE(HRESULT, c_void_p, c_uint32,
                                          POINTER(c_void_p))(vtbl[4])
        mt = c_void_p()
        hr = GetCurrentMediaType(reader, MF_SOURCE_READER_FIRST_AUDIO_STREAM, byref(mt))
        print(f"GetCurrentMediaType      -> 0x{hr & 0xFFFFFFFF:08X} mt={'有' if mt else '空'}")

        ReadSample = WINFUNCTYPE(HRESULT, c_void_p, c_uint32, c_uint32,
                                 POINTER(c_uint32), POINTER(c_uint32),
                                 POINTER(ctypes.c_long),
                                 POINTER(c_void_p))(vtbl[8])
        si, fl, ts, smp = c_uint32(0), c_uint32(0), ctypes.c_long(0), c_void_p()
        hr = ReadSample(reader, MF_SOURCE_READER_FIRST_AUDIO_STREAM, 0,
                        byref(si), byref(fl), byref(ts), byref(smp))
        print(f"ReadSample               -> 0x{hr & 0xFFFFFFFF:08X} "
              f"sample={'有' if smp else '空'} flags={fl.value}")
    else:
        print("!! 本地读取也失败 —— MF 在本沙箱内不可用，与网络无关")

    mfplat.MFShutdown()
    print("=" * 68)


if __name__ == "__main__":
    main()
