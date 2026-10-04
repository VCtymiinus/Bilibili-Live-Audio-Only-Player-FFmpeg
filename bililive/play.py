"""A 方案主循环：拿一条音频 URL -> 交给 ffplay -> 盯着它 -> 过期或挂掉就换。

为什么单独开一个文件：不干扰原来基于管道的 cli.py 与 player.py，
等这条路线跑稳了再决定要不要清理旧的。

    python -m bililive.play 26774400
    python -m bililive.play 26774400 -v          # 详细日志
    python -m bililive.play 26774400 --volume 60
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time

from .biliapi import (BiliApiError, BiliLiveClient,  # noqa: E402
                      system_proxy_in_use)
from .ffplay import FfplayPlayer, find_ffplay
from .jobobject import JobObject

# URL 剩余不足这个秒数就提前换，避免正听着突然断
RENEW_MARGIN = 600.0

BACKOFF_START = 2.0
BACKOFF_MAX = 60.0

# B 站返回的业务错误码，命中即认为重试没有意义
#   60004 = 房间不存在
PERMANENT_CODES = {60004}


def is_permanent_error(exc: Exception) -> bool:
    """判断这个错误是否「重试也没用」。

    只认明确的业务错误码，**不做中文子串匹配**。
    早期版本用 PERMANENT_HINTS = ("房间不存在", "不存在", ...) 兜底，
    但裸子串匹配过宽：任何临时故障只要文案里带「不存在」
    （比如「接口不存在」）就会被判成永久错误，直接把程序退掉。
    宁可多试几次，也不能因为文案变化而误退出。
    """
    msg = str(exc)
    return any(f"code={code}" in msg for code in PERMANENT_CODES)


# 退出原因分类。抽成独立的纯函数是为了能离线做故障注入测试 ——
# 这类判定逻辑以前只写在主循环里，正常播放时永远不触发，
# 于是「读错对象状态」这种错能一直存活（见 P0-1）。
EXIT_RENEW = "renew"        # 地址过期，正常换新
EXIT_STREAM_ENDED = "end"   # 播了一阵才结束，多半是下播
EXIT_FAILED = "failed"      # 连接级失败


def classify_exit(expired: bool, rc, uptime: float, had_error: bool,
                  stream_end_min_uptime: float = 2.0) -> str:
    """判断 ffplay 为什么退出。

    *** 注意 ffplay 的退出码不可信 ***
    实测彻底连不上时 exit_code 依然是 0：
        http://127.0.0.1:1/nope.flv               -> 0  "Error number -138 occurred"
        http://nonexistent-host-xyz.invalid/a.flv -> 0  "I/O error"
    所以 had_error（来自 stderr）才是主要信号，退出码只作补充。
    """
    if expired:
        return EXIT_RENEW
    if had_error or (rc not in (0, None)):
        return EXIT_FAILED
    if uptime >= stream_end_min_uptime:
        return EXIT_STREAM_ENDED
    # 存活极短又没有任何错误信息：无法判断，按失败处理更安全
    return EXIT_FAILED


class Console:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._len = 0

    @staticmethod
    def _ts() -> str:
        return time.strftime("%H:%M:%S")

    def _clear(self) -> None:
        if self._len:
            sys.stdout.write("\r" + " " * self._len + "\r")
            sys.stdout.flush()
            self._len = 0

    def info(self, msg: str) -> None:
        self._clear()
        print(f"[{self._ts()}] {msg}", flush=True)

    def debug(self, msg: str) -> None:
        if self.verbose:
            self.info(msg)

    def status(self, msg: str) -> None:
        pad = " " * max(0, self._len - len(msg))
        sys.stdout.write("\r" + msg + pad)
        sys.stdout.flush()
        self._len = len(msg)


def run(room: int, volume: int, verbose: bool) -> int:
    from . import __version__

    con = Console(verbose)
    con.info(f"bililive v{__version__}  —— 只听哔哩哔哩直播的声音")

    ffplay = find_ffplay()
    if not ffplay:
        con.info("!! 找不到 ffplay")
        con.info("   运行 python probe/extract_ffplay.py 从已下载的包里解出，")
        con.info("   或设置 BILILIVE_FFPLAY 环境变量")
        return 2
    con.info(f"ffplay = {ffplay}")

    client = BiliLiveClient()

    # Job Object: 保证本进程一死，ffplay 一定跟着死。
    # 否则关掉 cmd 窗口后 ffplay 会变成孤儿进程继续放音 —— 这是实际发生过的
    # 问题，用户会以为机器中邪了。atexit/finally 在被强杀时不会执行，
    # 只有内核级的 Job Object 能兜住这种场景。
    job = JobObject(log=con.debug)
    if job.create():
        con.debug("Job Object 就绪：关闭窗口会自动停止声音")
    else:
        con.debug("Job Object 不可用，退化为普通子进程管理")

    player = FfplayPlayer(ffplay, volume=volume, log=con.debug, job=job)

    stop = threading.Event()

    def on_sigint(_s, _f):
        stop.set()

    try:
        signal.signal(signal.SIGINT, on_sigint)
    except (ValueError, OSError):
        pass

    room_id: int | None = None
    title = uname = ""
    backoff = BACKOFF_START
    started = time.time()
    play_sessions = 0
    # 候选流队列。一次接口调用拿到的全部候选都放这里，
    # 播放失败时从队首取下一个（P0-2 修复的核心状态）。
    cands: list = []
    meta_started = False

    con.info(f"准备播放房间 {room}（Ctrl+C 退出）")
    _px = system_proxy_in_use()
    if _px:
        con.debug(f"检测到系统代理 {_px}：API 优先直连，失败才回退代理")

    while not stop.is_set():
        # ---- 1. 解析房间 + 并行预热 buvid ----
        # 首播延迟优化点：
        #   (a) resolve_room 的返回里**已经带了 live_status**，早期版本之后
        #       又单独调一次查开播状态，白多一个请求（实测约 5 秒）。
        #   (b) buvid3 与 room_init 互不依赖，并行发出，省掉一次串行等待。
        if room_id is None:
            box: dict = {}

            def _resolve():
                try:
                    box["info"] = client.resolve_room(room)
                except BaseException as e:      # noqa: BLE001
                    box["err"] = e

            def _warm_buvid():
                try:
                    client.ensure_buvid()
                except Exception:
                    pass

            ths = [threading.Thread(target=_resolve, name="resolve"),
                   threading.Thread(target=_warm_buvid, name="buvid")]
            for t in ths:
                t.start()
            for t in ths:
                t.join()

            if "err" in box:
                e = box["err"]
                if isinstance(e, BiliApiError) and is_permanent_error(e):
                    # 区分「房间号本身有问题」和「接口临时抽风」。
                    # 前者重试一万次也不会好，直接告诉用户并退出，
                    # 否则打错一个数字就会看到无限重试的日志。
                    con.info(f"房间号有问题: {e}")
                    con.info("请确认房间号是否正确，然后重新运行。")
                    player.stop()
                    job.close()
                    return 3
                con.info(f"解析房间失败: {type(e).__name__}: {e}")
                stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue

            info = box.get("info") or {}
            room_id = info.get("room_id")
            if not room_id:
                con.info("解析房间失败：没拿到 room_id")
                stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            live_status = info.get("live_status")
            con.info(f"房间 {room} -> room_id={room_id}")
        else:
            # 后续轮次才需要单独查开播状态
            try:
                live_status = client.resolve_room(room).get("live_status")
            except Exception as e:
                con.info(f"查询开播状态异常({type(e).__name__}: {e})，稍后重试")
                stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue

        if live_status != 1:
            # 0/2 都表示当前没有直播内容
            con.info(f"该直播间当前没有开播（live_status={live_status}）。")
            con.info("工具会每 30 秒自动检查一次，一开播就会开始播放。")
            con.info("现在可以直接关掉窗口，或按 Ctrl+C 退出。")
            waited = 0.0
            while waited < 30.0 and not stop.is_set():
                stop.wait(1.0)
                waited += 1.0
                con.status(f"  等待开播中 ... {waited:.0f}s / 30s")
            continue
        backoff = BACKOFF_START

        # 标题/主播名**不阻塞播放**：它只为显示，放到后台线程去取。
        # 早期版本在启动路径上同步等它（实测约 5 秒），属于不必要的等待。
        if not meta_started:
            meta_started = True

            def _fetch_meta():
                nonlocal title, uname
                try:
                    m = client.room_info(room_id)
                    ri = m.get("room_info") or {}
                    ai = (m.get("anchor_info") or {}).get("base_info") or {}
                    title = ri.get("title") or ""
                    uname = ai.get("uname") or ""
                    if title or uname:
                        con.info(f"主播 {uname} | {title}")
                except Exception:
                    pass

            threading.Thread(target=_fetch_meta, name="room-info",
                             daemon=True).start()

        # ---- 3. 从候选列表里取一条可用的流 ----
        # 关键（P0-2 修复）：候选列表必须被真正轮换使用。
        # 早期版本永远只用 cands[0]，一旦排第一的 CDN 在本网络下不通，
        # 就会变成「取接口 -> 还是它 -> 立刻重试」的热循环，
        # 永远换不到别的 CDN。实测一次接口会给 12 条跨 CDN 候选，
        # 而各 CDN 建连耗时从 5.3s 到 17s 不等，轮换是有实际价值的。
        if not cands:
            try:
                cands = client.audio_streams(room_id)
            except BiliApiError as e:
                con.info(f"拿流失败: {e}")
                con.info("可能刚好下播了，30 秒后重试 ...")
                stop.wait(30.0)
                continue
            except Exception as e:
                con.info(f"拿流异常({type(e).__name__}: {e})，"
                         f"{backoff:.0f}s 后重试")
                stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue

        # 跳过已过期的候选（列表可能是上一轮取的，放了很久）
        # 注意：必须先把最新到期时间记下来，因为下面会把 cands 弹空。
        newest_left = max((c.seconds_left for c in cands), default=None)
        fresh = [c for c in cands if c.is_fresh(RENEW_MARGIN)]

        # 首播延迟优化：**同一层内并行抢流**，而不是串行试第一条。
        # 实测同层各 CDN 建连到首包差异很大（1.5s ~ 17s），串行碰上慢的
        # 就白等十几秒；层内并行后总耗时约等于「最快那条」。
        #
        # 注意 race_audio_stream 只在同 format/protocol 层内竞速：
        # 早先对全列表竞速会选中 HLS（m3u8 只有几百字节、首包快），
        # 从而绕过 audio_streams() 里按实测定下的 flv 优先规则。
        picked = None
        if fresh:
            if len(fresh) == 1:
                picked = fresh[0]
            else:
                t_race = time.time()
                picked = client.race_audio_stream(fresh)
                if picked is not None:
                    con.debug(f"并行抢流 {time.time()-t_race:.2f}s -> "
                              f"{picked.format}/{picked.protocol} "
                              f"{picked.url.split('/')[2]}")
            if picked is not None:
                # 未中选的放回队列，供后续失败时轮换
                cands = [c for c in fresh if c is not picked]

        if picked is None:
            # *** 这里必须节流 ***
            # 早期版本直接 continue，没有任何等待。如果所有候选都不新鲜，
            # 每轮都会「取接口 -> 拿到同样过期的结果 -> 立刻再来」，
            # 形成零等待死循环：每秒约 2 次请求，一天 25 万次打接口，
            # 被限流后同一 IP 访问 B 站都会受影响。
            #
            # 会持续触发的场景（重启也救不了的那个）：
            #   本机时钟快于真实时间 10 分钟以上。expires_at 是服务端下发的
            #   绝对时间戳，减去本机 time.time() 后剩余量恒为负，
            #   于是每次启动都立刻再次进入这个分支。
            if newest_left is not None and newest_left < 0:
                con.info(f"!! 所有候选都已过期（最新一条也过了 "
                         f"{-newest_left/60:.1f} 分钟）。")
                con.info("   这通常意味着本机时钟比真实时间快 —— "
                         "expires 是服务端时间戳，")
                con.info("   本地时钟偏快会让它一取回来就「已过期」。")
                con.info(f"   本机时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
                con.info("   请校准系统时间后重启本工具，否则会一直无法播放。")
            else:
                con.info("候主流全部即将过期，换一批地址 ...")
            cands = []
            # 至少等 5 秒，且不轻于当前退避值
            wait = max(backoff, 5.0)
            stop.wait(wait)
            backoff = min(wait * 2, BACKOFF_MAX)
            continue

        stream = picked
        con.info(f"音频流: {stream.format}/{stream.protocol} "
                 f"{stream.audio_codec} 有效 {stream.seconds_left/60:.1f} 分钟 "
                 f"host={stream.url.split('/')[2]}"
                 + (f"（还剩 {len(cands)} 条候选备用）" if cands else ""))

        # ---- 4. 交给 ffplay，边播边盯 ----
        try:
            player.start(stream.url, client.headers())
        except Exception as e:
            con.info(f"启动 ffplay 失败: {type(e).__name__}: {e}")
            stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        play_sessions += 1
        deadline = (stream.expires_at - RENEW_MARGIN
                    if stream.expires_at else time.time() + 3000)
        # 兜底：即使解析不到 expires，也不要无限播放同一条 URL
        if deadline - time.time() > 3000:
            deadline = time.time() + 3000

        expired = False
        while not stop.is_set():
            time.sleep(0.5)
            if not player.is_alive():
                break
            if time.time() > deadline:
                expired = True
                con.info("音频地址接近过期，主动换新")
                break
            con.status(f"  播放中 {player.uptime/60:5.1f} 分钟  "
                       f"累计 {play_sessions} 次连接  "
                       f"候选剩 {len(cands)}  host="
                       f"{stream.url.split('/')[2][:26]}")

        if stop.is_set():
            break

        # ---- 5. 判断退出原因，决定退避还是立刻换下一条 ----
        # P0-1 修复（两层）：
        #   (a) 必须在 stop() **之前**取 exit_code 和 uptime。
        #       stop() 会把 self._proc 置为 None、started_at 归零，
        #       之后 exit_code 恒为 None、uptime 恒为 0.0，
        #       `exit_code not in (0, None)` 永远为 False —— 曾是死代码。
        #   (b) 光看退出码还不够：实测 ffplay 在彻底连不上时**退出码仍是 0**
        #       （"I/O error"、"Error number -138"），所以失败判定必须结合
        #       stderr 里的错误特征，否则退避依然不会触发。
        rc = player.exit_code
        up = player.uptime
        had_err = player.had_error
        err_line = player.error_summary
        reason = classify_exit(expired, rc, up, had_err)
        player.stop()

        if reason == EXIT_RENEW:
            # 正常续期：不惩罚，立刻用新地址继续
            con.info(f"地址过期换新（本次播放 {up/60:.1f} 分钟）")
            backoff = BACKOFF_START
            continue

        if reason == EXIT_STREAM_ENDED:
            # 跑了不短的时间才结束，且没有错误 -> 多半是主播下播，不是故障
            con.info(f"流已结束（播放 {up/60:.1f} 分钟），重新取地址 ...")
            stop.wait(2.0)
            backoff = BACKOFF_START
            continue

        # EXIT_FAILED
        con.info(f"ffplay 异常退出（code={rc}, 存活 {up:.1f}s）"
                 + (f"：{err_line}" if err_line else ""))

        if cands:
            # 还有备用 CDN：立刻换一条试，不退避。
            # 这是应对「某条 CDN 在本网络不通」的主要手段，
            # 也是 P0-2 修复的意义所在。
            backoff = BACKOFF_START
            con.info(f"换用备用候选（还剩 {len(cands)} 条）")
            continue

        # 候选用尽：等久一点再重新取地址，避免热循环打接口
        if up < 10:
            con.info(f"等待 {backoff:.0f}s 后重新取地址 ...")
            stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
        else:
            # 播了很久才断，属于正常波动，不必重罚
            backoff = BACKOFF_START
            stop.wait(1.0)

    print()
    con.info("停止中 ...")
    player.stop()
    # 兜底：job 里若还有残留（比如 ffplay 又拉起了子进程），一并清掉
    job.terminate_all()
    job.close()
    con.info(f"已停止。累计播放 {play_sessions} 次连接，"
             f"总时长 {(time.time()-started)/60:.1f} 分钟")
    return 0


def main(argv: list[str] | None = None) -> int:
    from . import __version__

    p = argparse.ArgumentParser(
        prog="bililive",
        description=f"只听哔哩哔哩直播的声音（ffplay 直连版）v{__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "音量说明:\n"
            "  --volume 取 0-100，0=静音 100=最大（默认）。\n"
            "  超出范围会被夹紧，不会放大 —— 实测 ffplay 自身行为:\n"
            "    -volume=150 > 100, setting to 100\n"
            "  想更大声请用 Windows 音量合成器单独特调 ffplay.exe，\n"
            "  那样也不影响其他程序的声音。\n"
            "\n"
            "示例:\n"
            "  python -m bililive 26774400\n"
            "  python -m bililive 26774400 --volume 60\n"
            "  python -m bililive 26774400 -v\n"
        ),
    )
    p.add_argument("room", nargs="?", type=int, help="直播间房间号")
    p.add_argument("--volume", type=int, default=100,
                   help="音量 0-100，0=静音 100=最大（默认 100，超范围会被夹紧）")
    p.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    p.add_argument("--version", action="version",
                   version=f"bililive {__version__}")
    args = p.parse_args(argv)
    if args.room is None:
        p.print_help()
        return 1
    return run(args.room, args.volume, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
