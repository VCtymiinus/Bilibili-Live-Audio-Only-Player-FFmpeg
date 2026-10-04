"""Windows Job Object —— 保证父进程一死，子进程（ffplay）跟着死。

解决的问题：
    关闭 cmd 窗口时，cmd -> python -> ffplay 这条链会断在最上层，
    ffplay 变成孤儿进程继续放音，用户会以为"关了窗口还在响，见鬼了"。

原理：
    Job Object 是内核级的进程组。设置 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 后，
    只要 job 的最后一个句柄被关闭（父进程无论怎么死都会触发），
    Windows 会自动终止 job 里所有进程。这是唯一能覆盖「被强杀」场景的办法
    —— atexit / finally 在进程被 TerminateProcess 时根本不会执行。

用法:
    job = JobObject()
    job.create()
    job.assign_child(proc)      # proc 是 subprocess.Popen 实例
    ...
    job.close()
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes

# 只有 Windows 才有的东西，导入时不要炸
_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong)]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


class JobObject:
    """把子进程放进一个「父死子必死」的 job 里。

    非 Windows 或创建失败时自动降级为 no-op，不影响主流程。
    """

    def __init__(self, log=None):
        self._log = log or (lambda m: None)
        self.handle = None
        self.active = False

    def create(self) -> bool:
        if not _IS_WINDOWS:
            return False
        try:
            h = kernel32.CreateJobObjectW(None, None)
            if not h:
                self._log(f"CreateJobObject 失败: {ctypes.get_last_error()}")
                return False

            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = \
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                h, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info))
            if not ok:
                self._log(f"SetInformationJobObject 失败: "
                          f"{ctypes.get_last_error()}")
                kernel32.CloseHandle(h)
                return False

            self.handle = h
            self.active = True
            return True
        except Exception as e:
            self._log(f"JobObject 创建异常: {type(e).__name__}: {e}")
            return False

    def assign(self, proc: "subprocess.Popen") -> bool:
        """把已启动的进程加入 job。失败不致命，只是失去自动清理保护。"""
        if not self.active or not self.handle:
            return False
        try:
            h = kernel32.OpenProcess(
                PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
            if not h:
                self._log(f"OpenProcess({proc.pid}) 失败: "
                          f"{ctypes.get_last_error()}")
                return False
            try:
                ok = kernel32.AssignProcessToJobObject(self.handle, h)
                if not ok:
                    self._log(f"AssignProcessToJobObject 失败: "
                              f"{ctypes.get_last_error()}")
                return bool(ok)
            finally:
                kernel32.CloseHandle(h)
        except Exception as e:
            self._log(f"assign 异常: {type(e).__name__}: {e}")
            return False

    def terminate_all(self) -> None:
        """立即终止 job 里所有进程。"""
        if not self.active or not self.handle:
            return
        try:
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE,
                                                    wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL
            kernel32.TerminateJobObject(self.handle, 0)
        except Exception:
            pass

    def close(self) -> None:
        if self.handle:
            try:
                kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None
        self.active = False


if _IS_WINDOWS:
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
