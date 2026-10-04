"""A 方案主循环：拿一条音频 URL -> 交给 ffplay -> 盯着它 -> 过期或挂掉就换。

为什么单独开一个文件：不干扰原来基于管道的 cli.py 与 player.py，
等这条路线跑稳了再决定要不要清理旧的。

    python -m bililive.play 26774400
    python -m bililive.play 26774400 -v          # 详细日志
    python -m bililive.play 26774400 --volume 60
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

from .biliapi import (BiliApiError, BiliLiveClient,  # noqa: E402
                      system_proxy_in_use)
from .control import PlayerControl
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


def run(room: int, volume: int, verbose: bool,
        control: PlayerControl | None = None, on_status=None) -> int:
    from . import __version__

    con = Console(verbose)
    con.info(f"bililive v{__version__}  —— 只听哔哩哔哩直播的声音")

    # control 由 main() 传进来（悬浮窗共享同一个对象）。直接调用 run() 时
    # 自己造一个，这样命令行用法完全不受影响。
    if control is None:
        control = PlayerControl(volume=volume)
    else:
        control.set_volume(volume)

    # 把状态同步给悬浮窗。on_status 可能为 None（纯命令行），所以统一走包装。
    def _status(text: str) -> None:
        control.status_text = text
        if on_status is not None:
            try:
                on_status(text)
            except Exception:
                pass

    ffplay = find_ffplay()
    if not ffplay:
        con.info("!! 找不到 ffplay")
        con.info("   运行 python probe/fetch_ffmpeg.py 下载并解出 ffplay.exe，")
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
    # 让播放器每次启动时现取音量，而不是用构造时的固定值。
    # 悬浮窗拖完滑块后，播放循环重启 ffplay 时就会自动带上新音量。
    player.set_volume_source(lambda: control.volume)

    # 统一的退出标志。用 control 上的那个，这样悬浮窗的「关闭」和
    # 命令行的 Ctrl+C 走的是同一条路径，不会出现两套退出逻辑打架。
    stop = control.stop_event

    def on_sigint(_s, _f):
        control.request_stop()

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

    while not control.stop_requested:
        # 每轮开头都先声明「没在播」。这样一旦流断了、地址过期了、
        # 正在重连，界面立刻就知道该显示连接中 —— 而不会停在「播放中」
        # 让用户以为还在放（我们为此得到过「明明断了却显示正在播放」的反馈）。
        control.playing = False

        # ---- 0. 暂停闸门 ----
        # 用户按了暂停就停在这里。ffplay 已经在监控循环里关掉了（声音立刻停）。
        #
        # 等待方式刻意用「**只等 stop_event，然后重新读 paused**」，
        # 而不是「等 resume_event」：
        #   * paused 是界面线程写的普通属性，轮询它最简单，不需要
        #     在两个 Event 之间做二选一（那种写法容易出现
        #     「暂停中关窗口，线程等错了 Event 就永远不退出」）。
        #   * stop_event.wait 能在关窗口时**立刻**醒来，不用等满 0.2 秒。
        if control.paused:
            _status("已暂停")
            con.info("已暂停（点悬浮窗的播放键继续，继续后从当下接着听）")
            # 每 0.2 秒醒一次重新读 control.paused；用 stop_event.wait 而不是
            # time.sleep，是为了关窗口时能立刻退出，不用等这 0.2 秒。
            while control.paused and not control.stop_requested:
                control.stop_event.wait(0.2)
            if control.stop_requested:
                break
            # 继续：候选地址可能已经放了很久，丢掉重新取，
            # 避免拿一条已经过期的地址去连。
            cands = []
            con.info("继续播放，重新取一条地址 ...")
            _status("正在继续 ...")
            continue
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
                    # 新接口（getRoomBaseInfo）把 title / uname 平铺在同一层。
                    # 兼容两种形状：平铺的，或老接口那种嵌套的。
                    if "title" in m or "uname" in m:
                        title = m.get("title") or ""
                        uname = m.get("uname") or ""
                    else:
                        ri = m.get("room_info") or {}
                        ai = (m.get("anchor_info") or {}).get("base_info") or {}
                        title = ri.get("title") or ""
                        uname = ai.get("uname") or ""
                    # 同步给界面显示（用户要求连上后显示主播名和直播间名）。
                    # 这两个字段是界面轮询读取的，所以在这里赋值就够了。
                    control.anchor = uname
                    control.title = title
                    if title or uname:
                        con.info(f"主播 {uname} | {title}")
                    else:
                        # 取不到就明说，别让用户以为界面坏了
                        con.info("（没取到主播名/直播间标题，界面不显示那一块）")
                except Exception as e:
                    con.info(f"取房间信息失败（不影响播放）: "
                             f"{type(e).__name__}: {e}")

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
        # 记录 ffplay 实际用上的音量，供悬浮窗判断「滑块和实际是否一致」
        control.applied_volume = control.volume
        # *** 先清掉「正在连接/正在继续」，再置 playing ***
        # 顺序很重要：界面每次轮询都按 (playing, status_text) 重算按钮。
        # 如果 status_text 还停在「正在继续 ...」而 playing 已经变 True，
        # 界面就会短暂显示「已经在播」这个组合 —— 实测每次恢复都有约
        # 0.2 秒的自相矛盾窗口。先把状态文字改成「缓冲中 ...」，
        # 按钮就会走「开始过但还没在播」那一支，等 playing 置上后再变
        # 「正在播放」，全程只出现合理的中间态。
        _status("缓冲中 ...")
        control.playing = True
        # 开始计时。用 control 这一层的累计时长，**不用 ffplay 的存活时间** ——
        # 调音量和地址续期都会重启 ffplay，进程存活时间会归零，
        # 用户就会看到「一调音量计时器就回到 0」（实测确认过这个 bug）。
        control.start_timer()
        deadline = (stream.expires_at - RENEW_MARGIN
                    if stream.expires_at else time.time() + 3000)
        # 兜底：即使解析不到 expires，也不要无限播放同一条 URL
        if deadline - time.time() > 3000:
            deadline = time.time() + 3000

        expired = False
        # 暂停也要能从这个循环里出来，所以两个条件一起看。
        # 用 paused_now 单独记一笔：暂停和「播完了」必须区分开，
        # 否则暂停会被当成 ffplay 异常退出，白白触发一次退避重连。
        paused_now = False
        volume_changed = False
        while not control.stop_requested:
            # *** 这里不能用 time.sleep(0.5) ***
            # 用 stop_event.wait 才能「暂停一按下就立刻醒」。
            # 用 sleep 的话，用户按下暂停后最多要等 0.5 秒循环才走到判断，
            # 加上杀掉 ffplay 的时间，手感明显发钝。
            # 实测：sleep(0.5) 时暂停延迟约 0.5~1.0 秒。
            control.stop_event.wait(0.5)
            # 悬浮窗调了音量：ffplay 不支持运行中改音量，只能重启一次。
            # 放在这里检测而不是让界面直接重启 —— 界面上做这个会卡住 UI，
            # 而且会和播放循环抢同一个进程句柄。
            if control.applied_volume != control.volume:
                volume_changed = True
                break
            if control.paused:
                paused_now = True
                break
            if not player.is_alive():
                break
            if time.time() > deadline:
                expired = True
                con.info("音频地址接近过期，主动换新")
                break
            mins = control.elapsed_minutes
            control.playing = True
            con.status(f"  播放中 {mins:5.1f} 分钟  "
                       f"累计 {play_sessions} 次连接  "
                       f"候选剩 {len(cands)}  host="
                       f"{stream.url.split('/')[2][:26]}")
            _status(f"播放中 {mins:.1f} 分钟")

        if volume_changed:
            # 重启 ffplay 以套用新音量。URL 还是同一条（还有效，没必要换），
            # 所以这里不算一次「故障」，不退避、也不消耗候选。
            # 代价：会有约 1~3 秒断音，这是为了「只用本工具的音量、
            # 不动系统总音量」而付的代价，已经和用户说明过。
            #
            # 状态文字要明确写「正在应用音量」而不是留给界面显示旧状态：
            # 重启期间界面不该再显示「播放中」，否则用户会以为卡住了。
            _status("正在应用音量 ...")
            con.info(f"音量改为 {control.volume}，重新启动播放（会短暂断一下）")
            player.stop()
            # 计时**不停**：这是我们自己为了调音量重启，不是用户暂停，
            # 累计时长必须连续（否则用户看到的就是「一调音量计时归零」）。
            try:
                player.start(stream.url, client.headers())
            except Exception as e:
                con.info(f"重启 ffplay 失败: {type(e).__name__}: {e}")
                control.stop_timer()
                stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            control.applied_volume = control.volume
            # 新一轮监控循环会立刻把状态刷回「播放中 X 分钟」
            continue

        if paused_now:
            # 暂停：把 ffplay 关掉，声音立刻停。
            # 这里**不要**去读 exit_code / had_error，也不要走下面的
            # 退出原因判定 —— 是我们自己杀掉的，那套逻辑毫无意义，
            # 而且会把「正常暂停」判成「连接失败」并触发退避。
            player.stop()
            # 暂停不计时：继续之后从原来的累计值接着涨，
            # 而不是把暂停的那段时间也算进去（那会像是在偷跑）。
            control.stop_timer()
            con.info("已暂停，声音已停止")
            continue

        if control.stop_requested:
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
    control.stop_timer()
    player.stop()
    # 兜底：job 里若还有残留（比如 ffplay 又拉起了子进程），一并清掉
    job.terminate_all()
    job.close()
    con.info(f"已停止。累计播放 {play_sessions} 次连接，"
             f"总时长 {(time.time()-started)/60:.1f} 分钟")
    return 0


def _has_console() -> bool:
    """当前进程有没有一个真正的控制台窗口？

    为什么不能直接用 sys.stdout.isatty()：
        * PyInstaller 的 --windowed 打包后 sys.stdout 是 None（见
          bililive_main.py 的处理），isatty 无从谈起。
        * 被重定向到文件时 isatty() 也是 False，但那时**有**控制台，
          只是输出被接走了。
    所以直接问 Windows：本进程挂着几个控制台窗口。0 就是没有。
    非 Windows 一律当作「有」（走控制台路径，行为最保守）。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        return ctypes.windll.kernel32.GetConsoleWindow() != 0
    except Exception:
        return False


def _ask_room_console() -> int | None:
    """在控制台里问房间号。拿不到就返回 None。

    只在「命令行没给房间号、又明确不要界面」时用得上
    （--no-gui，或者输出被重定向到文件）。
    有界面的情况一律交给主窗口去问 —— 那比弹一个单行对话框自然得多。

    *** 必须防 sys.stdin 为 None ***
    PyInstaller --windowed 打包后 sys.stdin 就是 None，此时 input() 会抛
        RuntimeError: input(): lost sys.stdin
    而不是返回空串。实测因此崩过一个交付版，所以这里三重保护：
    先查 None，再查 isatty，最后 try/except 整个 input。
    """
    if sys.stdin is None:
        return None
    try:
        if not sys.stdin.isatty():
            # 不是交互式终端（被重定向/管道），问也问不出来
            return None
    except Exception:
        return None
    try:
        sys.stdout.write("请输入直播间房间号（地址栏 live.bilibili.com/ 后面那串数字）：")
        sys.stdout.flush()
    except Exception:
        pass
    try:
        raw = input().strip()
    except (EOFError, KeyboardInterrupt, RuntimeError, OSError, ValueError):
        return None
    return _parse_room(raw)


def _parse_room(raw: str) -> int | None:
    """把用户输入变成房间号。容忍直接粘整条网址。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        digits = "".join(ch for ch in raw if ch.isdigit())
        return int(digits) if digits else None


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
    p.add_argument("--gui", action="store_true",
                   help="强制显示悬浮窗（暂停/继续、调音量）")
    p.add_argument("--no-gui", action="store_true",
                   help="不显示悬浮窗，只用命令行")
    p.add_argument("--version", action="version",
                   version=f"bililive {__version__}")
    args = p.parse_args(argv)

    if args.gui and args.no_gui:
        p.error("--gui 和 --no-gui 不能同时用")

    # ---- 拿房间号 ----
    #
    # *** 这里踩过一个严重的坑，改之前务必读完 ***
    # 打包成 --windowed（无控制台）后，如果**双击**运行，用户根本没地方
    # 输入房间号。而原来的代码在这种情况下是 `p.print_help(); return 1`
    # —— 在无控制台的窗口版里，print_help 的输出**无处可去**，
    # 于是进程一秒内静默退出，用户看到的就是「双击什么都没发生」。
    #
    # 所以按运行环境分情况：
    #   命令行给了房间号  -> 直接用
    #   没给 + 要显示界面 -> **交给界面去问**（主窗口里输入，最自然）
    #   没给 + 不要界面   -> 在控制台里问
    room = args.room

    # 要不要显示界面？判定规则（默认显示）：
    #   --no-gui / BILILIVE_NO_GUI  -> 不要
    #   --gui                       -> 一定要
    #   没有控制台（打包版双击）    -> 一定要（否则用户没有任何界面可用）
    #   有控制台但输出被重定向      -> 不要（脚本在跑，弹窗口会挂着等）
    #   其余                        -> 显示
    #
    # *** 这个判定必须看「有没有控制台」，不能看 sys.stdout.isatty() ***
    # 踩过两次，都是同一个根因：
    #   * PyInstaller --windowed 打包后 sys.stdout 是 None，isatty() 无从谈起；
    #   * 即使补了个假 stdout，isatty() 也是 False。
    # 早期版本把它当成「不要界面」，于是打包版双击后既不显示窗口，
    # 又转去问控制台（input()），在 stdin 为 None 时直接
    # RuntimeError: input(): lost sys.stdin 崩掉。
    # 正确问法是问 Windows：本进程到底挂着几个控制台窗口。
    has_console = _has_console()
    show_gui = True
    if args.no_gui or os.environ.get("BILILIVE_NO_GUI"):
        show_gui = False
    elif not args.gui and has_console and sys.stdout is not None:
        try:
            if not sys.stdout.isatty():
                show_gui = False      # 有控制台但输出被重定向 -> 脚本场景
        except Exception:
            pass

    if not show_gui:
        if room is None:
            room = _ask_room_console()
            if room is None:
                # 拿不到房间号就别静默退出（windowed 版里用户什么都看不到）。
                # 这里已经在「不要界面」的分支上，所以至少把用法打到 stdout。
                p.print_help()
                return 1
        return run(room, args.volume, args.verbose)

    # ---- 有界面的路径 ----
    # 播放循环必须在后台线程：它是个几十小时的长循环，放主线程窗口会卡死。
    # Tkinter 则**必须**在主线程（否则会随机崩），所以 mainloop 留在主线程。
    #
    # 注意：房间号可能是 None（双击 exe 就是这种）。这时**不**提前启动
    # 播放线程，而是等用户在主窗口里填好、点了「开始收听」再启动
    # （见 ui.py 的 _on_start）。这样界面先出来，用户有地方可操作。
    from .ui import run_with_overlay
    control = PlayerControl(volume=args.volume)
    control.room = room

    worker = None
    if room is not None:
        worker = threading.Thread(
            target=run,
            args=(room, args.volume, args.verbose, control),
            name="play-loop",
            daemon=True,          # 主窗口关了就别留着它
        )
        worker.start()

    run_with_overlay(control, room=room)
    # 窗口关闭后，确保播放线程也被要求退出（点 X 时已经设过一次，
    # 这里再设一次是为了兜住「窗口因为别的原因消失」的情况）。
    # ui.py 里用户点「开始收听」时可能自己起了一个线程，所以这里
    # 不只 join 我们创建的那个，还要等一下界面起的那个。
    control.request_stop()
    if worker is not None:
        worker.join(timeout=5.0)
    for t in threading.enumerate():
        if t.name == "play-loop" and t.is_alive():
            t.join(timeout=3.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
