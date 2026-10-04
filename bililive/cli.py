"""命令行入口：只听声音地播放哔哩哔哩直播。

    python -m bililive.cli 房间号              # 直接播放
    python -m bililive.cli 房间号 --check      # 只验证链路，不占声卡
    python -m bililive.cli 房间号 --dump-wav x.wav --seconds 20   # 录一段用于核对
    python -m bililive.cli --list-backends     # 看后端可用性

并发约定（重要）:
    waveOut 不是线程安全的，全程序**只有一个线程**（解码线程）向它写数据。
    主线程只做状态展示和信号处理，不碰音频句柄。
"""

from __future__ import annotations

import argparse
import os
import signal
import struct
import sys
import threading
import time

from .player import (FfmpegDecoder, OUT_CHANNELS, OUT_RATE, describe_backends,
                     find_ffmpeg, has_audio_device, pcm_rms)
from .stream import LiveAudioEngine
from .waveout import WaveOutPlayer


# ------------------------------------------------------------------ 输出工具

class Console:
    """带时间戳和状态行的控制台输出。"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._status_len = 0

    def _ts(self) -> str:
        return time.strftime("%H:%M:%S")

    def info(self, msg: str) -> None:
        self._clear_status()
        print(f"[{self._ts()}] {msg}", flush=True)

    def debug(self, msg: str) -> None:
        if self.verbose:
            self.info(msg)

    def status(self, msg: str) -> None:
        """原地刷新的状态行。"""
        pad = " " * max(0, self._status_len - len(msg))
        sys.stdout.write("\r" + msg + pad)
        sys.stdout.flush()
        self._status_len = len(msg)

    def _clear_status(self) -> None:
        if self._status_len:
            sys.stdout.write("\r" + " " * self._status_len + "\r")
            sys.stdout.flush()
            self._status_len = 0


def write_wav(path: str, pcm: bytes, rate: int, channels: int) -> None:
    block = channels * 2
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                      rate * block, block, 16))
        f.write(b"data" + struct.pack("<I", len(pcm)))
        f.write(pcm)


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n:.1f}GB"


# ------------------------------------------------------------------ 子命令

def cmd_list_backends() -> int:
    print("输出后端可用性：")
    for b in describe_backends():
        mark = "OK  " if b["available"] else "不可用"
        print(f"  [{mark}] {b['name']:<8} {b['detail']}")
    print()
    print("提示: mf 后端为零外部依赖方案，其可用性只能在运行时的真实环境判定。")
    return 0


def cmd_check(room: int, seconds: float, qn: int) -> int:
    """不占声卡地验证链路：收流 -> 解复用 -> 解码 -> 检查是否静音。"""
    console = Console()
    console.info(f"验证模式：房间 {room}，采样 {seconds:.0f} 秒")

    ff = find_ffmpeg()
    console.info(f"ffmpeg = {ff or '未找到'}")
    if not ff:
        console.info("!! 需要 ffmpeg 才能验证解码环节（tools/ffmpeg.exe 或 PATH）")
        return 2

    eng = LiveAudioEngine(room, qn=qn, log=console.debug)
    eng.start()

    t0 = time.time()
    while eng.stats.state != "playing" and time.time() - t0 < 25:
        time.sleep(0.2)
    if eng.stats.state != "playing":
        console.info(f"!! 未能开始播放：state={eng.stats.state} "
                     f"detail={eng.stats.detail}")
        eng.stop()
        return 3

    info = eng.demuxer_info
    console.info(f"流参数: {info.describe() if info else '未知'}")

    dec = FfmpegDecoder(ff, log=console.debug)
    dec.start()

    pcm_total = bytearray()
    rms_list: list[float] = []
    fed = 0
    deadline = time.time() + seconds
    try:
        for frame in eng.frames(timeout=1.0):
            dec.feed(frame.aac)
            fed += 1
            got = dec.read_pcm()
            if got is None:
                continue
            if got:
                pcm_total += got
                if len(rms_list) < 4000:
                    rms_list.append(pcm_rms(got))
            if time.time() > deadline:
                break
    except KeyboardInterrupt:
        pass
    finally:
        dec.close()
        eng.stop()

    avg = sum(rms_list) / len(rms_list) if rms_list else 0.0
    print()
    console.info(f"AAC 帧 {fed}  入站 {human_bytes(eng.stats.bytes_in)}  "
                 f"PCM {human_bytes(len(pcm_total))}")
    console.info(f"RMS 平均 {avg:.4f}（>0.0005 视为有真实声音）")
    console.info(f"状态 {eng.stats.state}  重连 {eng.stats.reconnects}  "
                 f"续期 {eng.stats.renewals}")

    ok = fed > 0 and len(pcm_total) > 1000 and avg > 0.0005
    console.info("结论: 链路正常" if ok else "结论: 链路异常，见上")
    if dec.stderr_tail:
        console.info(f"ffmpeg: {dec.stderr_tail}")
    return 0 if ok else 1


def cmd_play(room: int, qn: int, backend: str, dump_wav: str | None,
             dump_seconds: float, verbose: bool) -> int:
    console = Console(verbose)
    console.info(f"准备播放房间 {room}（Ctrl+C 退出）")

    if not has_audio_device():
        console.info("!! 系统报告没有可用音频输出设备")
        return 4

    ff = find_ffmpeg()
    if backend == "auto":
        backend = "ffmpeg" if ff else "mf"
    if backend == "ffmpeg" and not ff:
        console.info("!! 选择 ffmpeg 后端但找不到 ffmpeg。")
        console.info("   可运行 probe/fetch_ffmpeg.py 下载到 tools/，"
                     "或设置 BILILIVE_FFMPEG 环境变量")
        return 2
    if backend == "mf":
        console.info("!! mf 后端尚未在本机验证，暂未实现完整解码。")
        console.info("   请先用 ffmpeg 后端：python -m bililive.cli "
                     f"{room} --backend ffmpeg")
        return 2
    if backend != "ffmpeg":
        console.info(f"!! 未知后端 {backend}")
        return 2

    eng = LiveAudioEngine(room, qn=qn, log=console.debug)
    eng.start()

    t0 = time.time()
    while eng.stats.state != "playing" and time.time() - t0 < 25:
        time.sleep(0.2)
    if eng.stats.state != "playing":
        console.info(f"!! 未能开始播放：state={eng.stats.state} "
                     f"detail={eng.stats.detail}")
        eng.stop()
        return 3

    info = eng.demuxer_info
    console.info(f"流参数 {info.describe() if info else '未知'}")
    if info and not info.has_video:
        console.info("确认是纯音频流（无视频轨）")

    dec = FfmpegDecoder(ff, log=console.debug)
    dec.start()
    player = WaveOutPlayer(OUT_RATE, OUT_CHANNELS, log=console.debug)
    try:
        player.open()
    except Exception as e:
        console.info(f"!! 打开音频输出失败: {e}")
        dec.close()
        eng.stop()
        return 5

    stop_event = threading.Event()
    dumped = bytearray()
    dump_done = False
    decode_error: list[str] = []

    def decode_loop() -> None:
        """唯一的音频写入线程：解码 -> 播放（或落盘）。"""
        nonlocal dump_done
        last_flush = time.time()
        try:
            for frame in eng.frames(timeout=1.0):
                if stop_event.is_set():
                    break
                dec.feed(frame.aac, frame.adts)
                # feed 内部攒批到 16KB 才真正写；这里做定时兜底，
                # 否则低码率时会攒很久，表现为声音延迟一大截
                now = time.time()
                if now - last_flush >= 0.25:
                    dec.flush()
                    last_flush = now
                got = dec.read_pcm(0.05)
                if got is None or not got:
                    continue
                if dump_wav and not dump_done:
                    dumped.extend(got)
                    if len(dumped) >= int(OUT_RATE * OUT_CHANNELS * 2 * dump_seconds):
                        write_wav(dump_wav, bytes(dumped), OUT_RATE, OUT_CHANNELS)
                        console.info(f"已写出 {dump_wav} "
                                     f"({human_bytes(len(dumped))})，继续播放")
                        dump_done = True
                else:
                    player.write(got)
        except Exception as e:
            decode_error.append(f"{type(e).__name__}: {e}")
        finally:
            try:
                player.drain(1.0)
            except Exception:
                pass

    thread = threading.Thread(target=decode_loop, name="decode", daemon=True)
    thread.start()

    console.info("开始播放，按 Ctrl+C 停止")
    interrupted = False

    def on_sigint(_sig, _frm):
        nonlocal interrupted
        interrupted = True
        stop_event.set()

    old_handler = None
    try:
        old_handler = signal.signal(signal.SIGINT, on_sigint)
    except (ValueError, OSError):
        pass

    try:
        while thread.is_alive() and not interrupted:
            time.sleep(1.0)
            s = eng.stats
            up = s.uptime
            console.status(
                f"  状态={s.state:<12} 已播 {up/60:5.1f} 分钟  "
                f"AAC {s.frames:>7}  收 {human_bytes(s.bytes_in):>9}  "
                f"重连 {s.reconnects}  续期 {s.renewals}"
            )
            if s.state == "offline":
                console.status(f"  主播未开播，等待中... {s.detail[:40]}")
            if decode_error:
                break
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop_event.set()
        if old_handler is not None:
            try:
                signal.signal(signal.SIGINT, old_handler)
            except Exception:
                pass
        print()
        console.info("停止中 ...")
        eng.stop()
        thread.join(timeout=3.0)
        try:
            player.drain(1.0)
        except Exception:
            pass
        player.close()
        dec.close()

    if decode_error:
        console.info(f"!! 解码/播放出错: {decode_error[0]}")
        return 6

    s = eng.stats
    console.info(f"已停止。总播放 {s.uptime/60:.1f} 分钟，AAC {s.frames} 帧，"
                 f"收流 {human_bytes(s.bytes_in)}，重连 {s.reconnects} 次，"
                 f"续期 {s.renewals} 次")
    return 0


# ------------------------------------------------------------------ 入口

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bililive",
        description="只听哔哩哔哩直播的声音（纯音频，后台挂机友好）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python -m bililive.cli 22388070\n"
               "  python -m bililive.cli 22388070 --check\n"
               "  python -m bililive.cli --list-backends\n",
    )
    p.add_argument("room", nargs="?", type=int, help="直播间房间号（短号也行）")
    p.add_argument("--check", action="store_true",
                   help="只验证链路，不占用声卡")
    p.add_argument("--seconds", type=float, default=12.0,
                   help="--check 的采样时长，或 --dump-wav 的录制时长（默认 12）")
    p.add_argument("--backend", default="auto",
                   choices=["auto", "ffmpeg", "mf"], help="音频后端")
    p.add_argument("--qn", type=int, default=10000, help="画质档位（对音频影响不大）")
    p.add_argument("--list-backends", action="store_true", help="列出后端可用性")
    p.add_argument("--dump-wav", metavar="路径",
                   help="把解码出的 PCM 存成 WAV 用于核对，同时照常播放")
    p.add_argument("-v", "--verbose", action="store_true", help="打印详细日志")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_backends:
        return cmd_list_backends()
    if args.room is None:
        build_parser().print_help()
        return 1
    if args.check:
        return cmd_check(args.room, args.seconds, args.qn)
    return cmd_play(args.room, args.qn, args.backend, args.dump_wav,
                    args.seconds, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
