"""用 ffplay 直接播放音频 URL —— 不经任何管道。

这是 A 方案：把「解码 + 输出」整体交给 ffplay，Python 只负责拿地址、续期、重连。

为什么放弃管道方案：
    之前的做法是自己读 HTTP 流 -> 解 FLV -> 把 ADTS 喂给 ffmpeg stdin -> 读 stdout。
    这条链在 Windows 上会死锁：ffmpeg 等它自己的读缓冲被填满，我这边等它的输出，
    双方互等。命令行直接吃文件完全正常，说明是管道时序问题而非数据格式问题。

    现在改成把 URL 直接作为参数传给 ffplay，由它自己取流、解码、出声。
    没有 stdin/stdout 交互，死锁的土壤就不存在了。

代价（如实记录）：
    * URL 约 1 小时过期，续期需重启 ffplay，会有约 0.3~1 秒断音。
    * 音量靠 ffplay 的 -volume 参数，运行时调节需重启进程。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def app_dir() -> str:
    """返回「程序所在目录」——打包成 exe 后也能正确指向。

    这是打包场景的关键：PyInstaller onefile 模式下 `__file__` 指向
    临时解包目录（_MEIPASS），把 ffplay.exe 放在那里是错的，
    用户根本找不到。正确做法是用 sys.executable 的所在目录。

        源码运行:  <项目根>/bililive/ffplay.py -> <项目根>
        exe 运行:  <文件夹>/bililive.exe       -> <文件夹>
    """
    if getattr(sys, "frozen", False):
        # 打包后：exe 所在目录就是「程序目录」
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_ffplay() -> str | None:
    """按优先级找 ffplay。

    1. 环境变量 BILILIVE_FFPLAY（最高优先级，方便临时覆盖）
    2. 程序目录下的 ffplay.exe / tools/ffplay.exe
    3. PATH

    exe 分发时 ffplay.exe 与 bililive.exe 放在同一层，靠第 2 条命中。
    """
    env = os.environ.get("BILILIVE_FFPLAY")
    if env and os.path.isfile(env):
        return env

    base = app_dir()
    for cand in (
        os.path.join(base, "ffplay.exe"),
        os.path.join(base, "tools", "ffplay.exe"),
        os.path.join(base, "bin", "ffplay.exe"),
    ):
        if os.path.isfile(cand):
            return cand

    import shutil
    return shutil.which("ffplay")


class FfplayPlayer:
    """把一条音频 URL 交给 ffplay 播放，可被反复重启（换流/续期）。"""

    def __init__(self, ffplay: str | None = None, volume: int = 100,
                 log=None, extra_args: list[str] | None = None, job=None):
        self.ffplay = ffplay or find_ffplay()
        self.volume = max(0, min(100, volume))
        # 悬浮窗调音量时，ffplay 不支持运行中改，只能在**重启时**用上新值。
        # 所以允许调用方传一个「取当前音量」的可调用对象（通常是
        # PlayerControl.volume），每次 start() 时现取一次。
        # 这样滑块拖完 -> 播放循环重启 ffplay -> 新音量自动生效，
        # 不需要播放循环额外维护一份音量副本（副本必然会不同步）。
        self._volume_source = None
        self._log = log or (lambda m: None)
        self._proc: subprocess.Popen | None = None
        self._extra = list(extra_args or [])
        self._stderr_tail: list[str] = []
        self._reader: threading.Thread | None = None
        self.started_at = 0.0
        # stop() 会把 _proc 置空，之后仍要能查到退出码，所以单独缓存一份
        self._last_exit_code: int | None = None
        # Job Object：父进程一死（包括被强杀、关窗口），ffplay 自动被终止。
        # 没有它就会出现「关了 cmd 窗口还在响」的孤儿进程。
        self._job = job

    @property
    def available(self) -> bool:
        return bool(self.ffplay)

    def set_volume_source(self, source) -> None:
        """设置「取当前音量」的可调用对象。每次 start() 时现取。

        传 None 则退回到构造时的固定 self.volume。
        悬浮窗调音量就靠这条通路生效：滑块改值 -> 播放循环重启 ffplay
        -> _cmd() 现取到新音量 -> 写进 -volume。
        """
        self._volume_source = source

    def _current_volume(self) -> int:
        if self._volume_source is not None:
            try:
                return max(0, min(100, int(self._volume_source())))
            except Exception:
                pass
        return self.volume

    def _cmd(self, url: str, headers: str) -> list[str]:
        args = [
            self.ffplay,
            "-hide_banner",
            # 用 info 而不是 warning：warning 下 ffplay 播放成功时**完全静默**，
            # 出问题时看不到任何现场信息。这与本项目「把坑都记下来」的做法相悖。
            # 输出由 _drain_stderr 收着，只在出错时展示，不会刷屏。
            "-loglevel", "info",
            "-nostats",
            "-nodisp",            # 不开视频窗口
            "-vn",                # 丢弃视频流（我们只要声音）
            "-autoexit",          # 流结束就退出，让上层能感知
            "-volume", str(self._current_volume()),
            # 只保留 -infbuf。早期同时给了 -fflags nobuffer，
            # 但两者目标相反：nobuffer 意在少缓冲，-infbuf 取消输入缓冲上限。
            # 对冲的结果是直播延迟只增不减（抖动累积不自愈），
            # 连续挂几小时会明显落后于实时。直播场景选 -infbuf：
            # 宁可延迟略大，也不能因为缓冲不足而断续。
            "-infbuf",
            # ---- 首播延迟优化 ----
            # 实测（同一流、同一时刻、音量 0）:
            #     默认探测       ffplay 识别到音频参数需 5.91s
            #     probesize=32   ffplay 识别到音频参数需 0.92s   <- 快 6.4 倍
            # 32 字节就够 FLV 判定容器格式与首个音频 tag，
            # 实测音频识别完全正常（Audio: aac (LC), 48000Hz, stereo）。
            "-probesize", "32",
            "-analyzeduration", "0",
            # 注意：这只能加快 ffplay 的**探测**阶段。
            # 实测从启动到「真正开始出声」仍受 CDN 首批数据到达时间支配
            # （该房间的两个 flv 候选首包分别要 21~22 秒，Python 直读同样慢），
            # 那部分不是 ffplay 参数能解决的。
            # B 站 CDN 的证书链用 gnutls 校验会失败
            # （报 "Peer certificate failed verification"），实测必须关掉。
            #
            # 安全代价（必须知情）：关掉校验等于放弃对服务器身份的验证，
            # 理论上中间人可以替换音频内容。对本工具的风险画像
            # （只听公开直播的声音、不传任何凭据、不涉及隐私）可以接受，
            # 但它确实是一个真实的降级，不是「证书链的小毛病」。
            # 想更稳妥可改用带 openssl 后端的 ffmpeg 构建，
            # 或用 -ca_file 指定系统 CA 包，那样就不需要这一行。
            "-tls_verify", "0",
        ]
        if headers:
            args += ["-headers", headers]
        args += self._extra
        args.append(url)
        return args

    def start(self, url: str, headers: dict[str, str] | None = None) -> None:
        """启动播放。会先停掉上一个进程。"""
        if not self.ffplay:
            raise RuntimeError("找不到 ffplay（可运行 probe/fetch_ffmpeg.py 获取）")
        self.stop()

        # ffplay 的 -headers 需要 "Key: Value\r\n" 形式，且必须带 Referer，
        # 否则 B 站 CDN 可能拒绝。
        hdr = ""
        if headers:
            hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())

        env = dict(os.environ)
        # 抑制 SDL 建窗口（-nodisp 之外的保险），避免任务栏闪一下
        env.setdefault("SDL_VIDEODRIVER", "dummy")
        env["SDL_AUDIODRIVER"] = env.get("SDL_AUDIODRIVER", "directsound")

        self._proc = subprocess.Popen(
            self._cmd(url, hdr),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
            env=env,
        )
        # 关键：把 ffplay 放进 job。这样父进程无论怎么退出（正常结束、
        # Ctrl+C、关 cmd 窗口、被强杀），Windows 都会连带终止 ffplay，
        # 不会留下继续放音的孤儿进程。
        if self._job is not None:
            self._job.assign(self._proc)
        self.started_at = time.time()
        self._stderr_tail = []
        self._last_exit_code = None
        self._reader = threading.Thread(target=self._drain_stderr,
                                        args=(self._proc,),
                                        name="ffplay-stderr", daemon=True)
        self._reader.start()
        self._log(f"ffplay 已启动 pid={self._proc.pid}")

    def _drain_stderr(self, proc: "subprocess.Popen") -> None:
        """收 ffplay 的输出。

        proc 由参数传入而不是读 self._proc —— 后者会被 start()/stop() 改写，
        旧线程可能因此转去读**新进程**的管道，造成两个线程读同一条管道。
        虽然实测旧线程通常会及时退出，但把句柄显式绑给线程才是正确的写法。
        """
        try:
            if not proc.stderr:
                return
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self._stderr_tail.append(line)
                # 保留更多现场：早期只留 20 行且只打印含 error/failed 的行，
                # 真出问题时反而什么都看不到。
                del self._stderr_tail[:-60]
                low = line.lower()
                if any(k in low for k in ("error", "failed", "invalid",
                                          "unable", "denied", "refused",
                                          "timed out", "protocol")):
                    self._log(f"ffplay: {line}")
        except Exception:
            pass

    def is_alive(self) -> bool:
        """进程是否还活着。顺带把退出码记下来，供 stop() 之后查询。"""
        if self._proc is None:
            return False
        rc = self._proc.poll()
        if rc is not None:
            self._last_exit_code = rc
            return False
        return True

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    @property
    def stderr_tail(self) -> str:
        return " | ".join(self._stderr_tail[-5:])

    # ffplay 的连接级错误特征。
    #
    # *** 为什么不能只看退出码 ***
    # 实测（2026-10）ffplay 在彻底连不上时**退出码仍然是 0**：
    #     http://127.0.0.1:1/nope.flv               -> exit_code=0  "Error number -138 occurred"
    #     http://nonexistent-host-xyz.invalid/a.flv -> exit_code=0  "I/O error"
    # 所以「退出码非零 = 失败」这个假设是错的，退避逻辑靠它永远不触发。
    # 真正可靠的信号是 stderr 里的错误文本。
    _ERROR_MARKERS = (
        "error number", "i/o error", "connection refused", "connection timed out",
        "failed to", "could not", "unable to", "no such file",
        "invalid data", "server returned", "http error", "protocol not found",
        "input/output error", "network is unreachable", "name or service not known",
        "certificate", "handshake",
    )

    @property
    def had_error(self) -> bool:
        """本次播放是否出现过连接级错误。

        只看尾部若干行：loglevel=info 下正常播放也会打不少信息，
        但错误总是靠近结尾（进程随后就退了）。
        """
        tail = " ".join(self._stderr_tail[-8:]).lower()
        return any(m in tail for m in self._ERROR_MARKERS)

    @property
    def error_summary(self) -> str:
        """挑出最有价值的一行错误，用于日志展示。"""
        for line in reversed(self._stderr_tail):
            low = line.lower()
            if any(m in low for m in self._ERROR_MARKERS):
                return line.strip()
        return self._stderr_tail[-1].strip() if self._stderr_tail else ""

    def set_volume(self, volume: int) -> None:
        """记录音量。ffplay 不支持运行时改音量，下次 start() 生效。"""
        self.volume = max(0, min(100, volume))

    def stop(self, timeout: float = 3.0) -> None:
        proc, self._proc = self._proc, None
        # 先把旧 reader 线程收干净，再让 start() 复用 _reader 字段。
        # 否则旧线程可能还活着，而它读的管道句柄已随 _proc 一起被换掉。
        reader, self._reader = self._reader, None
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        except Exception:
            pass
        # 记录退出码，这样 stop() 之后 exit_code 依然可查
        try:
            self._last_exit_code = proc.poll()
        except Exception:
            pass
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.0)
        # 进程已停，这里再补收一次残留输出
        try:
            if proc.stderr:
                rest = proc.stderr.read()
                if rest:
                    for raw in rest.decode("utf-8", "replace").splitlines():
                        if raw.strip():
                            self._stderr_tail.append(raw.strip())
                    del self._stderr_tail[:-60]
        except Exception:
            pass
        self.started_at = 0.0

    @property
    def exit_code(self) -> int | None:
        """上一个进程的退出码。stop() 之后仍然有效。

        注意：主循环必须**在 stop() 之前**读取它。stop() 会把 _proc 置空，
        之后再读就恒为 None —— 这正是 P0-1 那个退避死代码的成因。
        这里额外缓存一份 _last_exit_code，作为兜底。
        """
        if self._proc is not None:
            return self._proc.poll()
        return self._last_exit_code
