"""探测 H：能否绕开 MF 的 URL/文件处理器，改用「内存字节流」喂它？

思路：Python 自己收流 + 解 FLV（已验证可行），只让 MF 干一件事 —— 把 AAC 解成 PCM。
这样就不需要 MF 打开网络或文件，可能绕过沙箱的 ACCESS_DENIED。

本探测先不实现完整 IMFByteStream（那要写十几个 COM 方法），
只判断一件事：MFCreateSourceReaderFromByteStream 在沙箱内是否可调用。
若可调用 -> 完整实现值得投入；若仍 ACCESS_DENIED -> 老实在文档里说明。
"""

import ctypes
from ctypes import POINTER, byref, c_void_p, c_ulong, WINFUNCTYPE

HRESULT = ctypes.c_long
mfplat = ctypes.WinDLL("mfplat")
mfreadwrite = ctypes.WinDLL("mfreadwrite")
ole32 = ctypes.WinDLL("ole32")

ole32.CoInitializeEx.argtypes = [c_void_p, c_ulong]
ole32.CoInitializeEx.restype = HRESULT
mfplat.MFStartup.argtypes = [c_ulong, c_ulong]
mfplat.MFStartup.restype = HRESULT
mfplat.MFShutdown.argtypes = []
mfplat.MFShutdown.restype = HRESULT

# MFCreateSourceReaderFromByteStream(IMFByteStream*, IMFAttributes*, IMFSourceReader**)
mfreadwrite.MFCreateSourceReaderFromByteStream.argtypes = [c_void_p, c_void_p,
                                                           POINTER(c_void_p)]
mfreadwrite.MFCreateSourceReaderFromByteStream.restype = HRESULT

# MFCreateMFByteStreamOnStream(IStream*, IMFByteStream**)  需要 IStream，先看是否可导出
HAS_ON_STREAM = hasattr(mfplat, "MFCreateMFByteStreamOnStream")

MF_VERSION = (0x0002 << 16) | 70


def main():
    print("=" * 68)
    hr = ole32.CoInitializeEx(None, 0x2)
    print(f"CoInitializeEx -> 0x{hr & 0xFFFFFFFF:08X}")
    hr = mfplat.MFStartup(MF_VERSION, 0)
    print(f"MFStartup      -> 0x{hr & 0xFFFFFFFF:08X}")

    print(f"\nMFCreateMFByteStreamOnStream 可导出? {HAS_ON_STREAM}")

    # 传 NULL 字节流：预期 E_POINTER(0x80004003)，关键看是不是 ACCESS_DENIED(0x80070005)
    reader = c_void_p()
    hr = mfreadwrite.MFCreateSourceReaderFromByteStream(None, None, byref(reader))
    hx = hr & 0xFFFFFFFF
    names = {
        0x80004003: "E_POINTER  <- 好现象：函数入口可达，只是没给字节流",
        0x80070005: "E_ACCESSDENIED <- 沙箱仍然拦截",
        0xC00D36B4: "MF_E_UNEXPECTED",
        0x80070057: "E_INVALIDARG",
    }
    print(f"MFCreateSourceReaderFromByteStream(NULL) -> 0x{hx:08X}  "
          f"{names.get(hx, '')}")

    if hx == 0x80004003:
        print("\n*** 好消息：内存字节流路线没有被沙箱拦截，值得完整实现 IMFByteStream")
    elif hx == 0x80070005:
        print("\n*** 内存字节流同样被拦。MF 解码路线在本环境无法验证。")
    mfplat.MFShutdown()
    print("=" * 68)


if __name__ == "__main__":
    main()
