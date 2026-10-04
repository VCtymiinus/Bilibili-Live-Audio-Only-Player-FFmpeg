"""播放控制状态 —— 悬浮窗和播放主循环之间的唯一共享对象。

为什么要单独一个文件
--------------------
悬浮窗跑在 Tkinter 的主线程里，而播放循环（取地址、盯 ffplay、重连）必须
跑在后台线程 —— 它动辄 `time.sleep(0.5)` 循环几十小时，放主线程会让窗口
直接卡死无响应。

于是两个线程需要共享几个状态：暂停没暂停、音量多少、要不要退出。本文件
就是把这点状态收在一起，并提供设置方法。

线程安全说明
------------
这里的做法是「**仅用线程原语传状态，不共享可变对象**」：

    paused / volume   : 由 Tkinter 线程写，播放线程读（轮询）
    stop_event        : threading.Event，本身线程安全

CPython 下单个属性的读写是原子的，而 paused 是 bool、volume 是 int，
所以不需要再加锁。播放循环用「轮询 paused + 等待 stop_event」的方式
感知变化（见 play.py 的暂停闸门），刻意**不**再做一套 resume 事件：
多一个事件就多一种「等错对象导致退不掉」的可能，实际就踩过。

也刻意**不**共享 FfplayPlayer 之类的复杂对象 —— 那种共享才是真正会
出问题的（一个线程正在 stop、另一个正在 start，抢同一个进程句柄）。
"""

from __future__ import annotations

import threading
import time


class PlayerControl:
    """悬浮窗 -> 播放循环的控制把手。"""

    def __init__(self, volume: int = 100):
        self._volume = max(0, min(100, int(volume)))
        self._paused = False

        # 播放线程靠等它来「秒醒」：关窗口时不用等满一个轮询间隔。
        self.stop_event = threading.Event()

        # 当前 ffplay **实际在用**的音量。None 表示「和 _volume 不一致，
        # 需要重启 ffplay 才能追上」。播放循环每次成功启动 ffplay 后会把它
        # 对齐成当时的 _volume。
        self.applied_volume = None

        # ---- 本次聆听的累计时长（秒）----
        # *** 不能直接用 ffplay 进程的存活时间当计时器 ***
        # 调音量和地址续期都会**重启 ffplay 进程**，进程存活时间随之归零，
        # 用户看到的就是「一调音量计时器就回到 0」。
        # 所以计时归这一层管：由播放循环在每轮开始/结束时调用
        # start_timer() / stop_timer() 累加，跨 ffplay 重启连续。
        self._elapsed = 0.0          # 已累计的秒数
        self._timer_t0 = None        # 本轮开始时间（None=当前没在计时）

        # 由 play.py / ui.py 填充，仅供界面显示。
        #
        # *** 初值是「运行中」，不是「正在启动 ...」 ***
        # 窗口一出现就说明程序已经启动完了，此时它只是在等用户输入房间号。
        # 显示「正在启动 ...」会在什么都没发生时给出一个**已经过期的动作描述**，
        # 看起来像卡在启动阶段（用户报的就是这个）。
        # 正常待机状态该说的是「运行中」—— 描述状态，而不是描述一个动作。
        self.status_text = "运行中"
        # 播放循环每次写 status_text 都把它 +1。
        # 界面靠这个区分「循环真的写了新状态」和「这是上一轮留下的旧值」——
        # 否则循环退出后，界面刚设的「已停止」会被旧的「运行中」立刻冲掉
        # （实测：_set_status('已停止...') 紧接着 _set_status('运行中')）。
        self.status_seq = 0
        # 界面靠它显示「播放中 / 正在连接 / 已暂停」。
        # *** 专门用一个布尔量，不要去解析 status_text 里的中文 ***
        # status_text 是给控制台日志用的自由文本，格式随时可能变；
        # 界面如果去猜它的含义（比如找「播放中」三个字），
        # 一旦文案调整界面就会失灵。这里给界面一个明确的开关。
        self.playing = False
        self.room = None            # 界面上要显示的房间号
        # 主播名 / 直播间标题。由 play.py 的后台线程取到后填进来
        # （room_info 接口），界面轮询到变化就显示。
        # 取不到就一直是空串，界面据此决定显不显示那一块。
        self.anchor = ""
        self.title = ""

        # ---- 给界面的错误提示通道 ----
        # *** 为什么必须有这个 ***
        # 播放循环遇到「房间号不存在」这类永久错误时会直接返回，但它原来
        # 只是往控制台 print 一句 —— 而打包成 windowed 后**没有控制台**，
        # 用户什么都看不到，只看到按钮卡在「正在连接 ...」不动，
        # 反馈就是「输入不存在的房间号会卡死」。
        # 现在把错误写进这个字段，界面轮询到就弹一个悬浮提示。
        # 用递增的 seq 而不是判断字符串变化：同一个错误提示两次也要能弹。
        self.error_text = ""
        self.error_seq = 0

        # ---- 未开播通道（与上面分开，提示文案和调性都不同）----
        self.offline_text = ""
        self.offline_seq = 0
        # 上一次已经提示过的「未开播」房间号。
        # 未开播是每 30 秒一轮的循环，如果每轮都上报，提示会每 30 秒
        # 重新弹一次，很烦。只对**新出现**的未开播房间提示。
        self.offline_room = None

        # 播放循环是否已经退出。界面靠它复位按钮 ——
        # 否则循环退了、按钮还停在「正在连接 ...」且是禁用的（实测的 bug）。
        self.finished = False

    # ------------------------------------------------------------ 界面通知

    def report_error(self, text: str) -> None:
        """上报一个要展示给用户的错误。界面轮询到就弹提示。"""
        self.error_text = str(text)
        self.error_seq += 1

    def report_offline(self, text: str) -> None:
        """上报「房间存在但没开播」。

        *** 为什么和 report_error 分开 ***
        这两种情况必须给用户**不同的**提示，因为该做的事完全不同：
            report_error   「房间不存在」-> 房间号打错了，**换一个**
            report_offline 「没开播」    -> 房间号是对的，**换一个正在播的，
                                            或者就在这儿等着**
        如果合成一种提示，用户会以为房间号打错了，然后去改一个本来正确的号。

        实现上单独用一个 seq：没开播是**周期性**的（每 30 秒一轮），
        要能反复提示；而且它不应该像错误那样把界面弄成"出错"的调性。
        """
        self.offline_text = str(text)
        self.offline_seq += 1

    def mark_finished(self) -> None:
        """标记本轮播放循环已退出。界面据此把按钮复位成可用状态。"""
        self.finished = True
        # 退出时不该再显示「播放中」
        self.playing = False
        # 把 status_text 归位成待机文案，并 +1 通知界面「这条是新的」。
        #
        # *** 不归位的话会出两种毛病 ***
        #   1. 界面在 finished 里设的「已停止」会被这条旧值立刻冲掉
        #      （实测到两次连续的 _set_status）
        #   2. 「已经出声过、然后循环退出」的情况下，状态栏会一直停在
        #      「播放中 X 分钟」——因为旧文本和界面记录的 _last_status 相同，
        #      差值判断认为不需要刷新。
        self.status_text = "运行中"
        self.status_seq += 1

    # ------------------------------------------------------------ 计时

    def start_timer(self) -> None:
        """开始/继续计时。重复调用不会重置已累计的时间。"""
        if self._timer_t0 is None:
            self._timer_t0 = time.time()

    def stop_timer(self) -> None:
        """暂停计时并把这一段累加进去。"""
        if self._timer_t0 is not None:
            self._elapsed += time.time() - self._timer_t0
            self._timer_t0 = None

    def reset_timer(self) -> None:
        """清零（换房间时用）。"""
        self._elapsed = 0.0
        self._timer_t0 = None

    @property
    def elapsed_minutes(self) -> float:
        """本次聆听累计分钟数（含当前正在计的这一段）。"""
        total = self._elapsed
        if self._timer_t0 is not None:
            total += time.time() - self._timer_t0
        return total / 60.0

    # ------------------------------------------------------------ 暂停/继续

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        """暂停：声音立刻停（播放循环会把 ffplay 关掉）。"""
        self._paused = True

    def resume(self) -> None:
        """继续：播放循环会重新取一条地址并启动 ffplay。

        为什么不是「把刚才那一条接着播」：
            直播不会因为你暂停而停下。继续时重新取一条地址，
            **直接从当下接着听**，不会把暂停期间的内容加速补播一遍。
            这是我们和用户明确约定过的行为。
        """
        self._paused = False

    def toggle(self) -> bool:
        """在暂停/继续之间切换，返回切换后是否处于暂停。"""
        if self._paused:
            self.resume()
        else:
            self.pause()
        return self._paused

    # ------------------------------------------------------------ 音量

    @property
    def volume(self) -> int:
        return self._volume

    def set_volume(self, value: int) -> int:
        """设置音量（0-100），返回夹紧后的实际值。

        只改这个数字。真正生效靠播放循环**重启 ffplay** 并传新的 -volume
        —— ffplay 不支持运行中改音量。为避免拖动滑块时反复重启，
        悬浮窗那边做了防抖（松手后才应用一次）。
        """
        self._volume = max(0, min(100, int(value)))
        return self._volume

    def request_volume_apply(self) -> None:
        """请求把当前音量真正应用到 ffplay。

        悬浮窗防抖到点后调这个。播放循环看到 applied_volume 变成 None，
        就会重启 ffplay 一次，用上新的 -volume。
        """
        self.applied_volume = None

    # ------------------------------------------------------------ 退出

    def request_stop(self) -> None:
        """请求整体退出（关窗口时调用）。

        只置一个 Event：播放循环的暂停闸门等的就是它，
        所以**暂停状态下关窗口也能立刻退出**，不会留在任务管理器里。
        """
        self.stop_event.set()

    def reset_stop(self) -> None:
        """换一个干净的停止标志，让播放循环能重新跑起来。

        「换一个房间号重新开始」时用：旧循环被 request_stop() 叫停并退出，
        新循环需要一份未置位的 Event 才能工作。

        *** 必须换新 Event，不能 clear() ***
        旧循环可能正要 wait 一个刚被 set 过的 Event；clear() 会让它立刻
        又继续跑，两边行为都不确定。换新对象后，旧循环仍看到自己那份
        「已停止」，新循环拿到干净的一份，互不干扰。
        """
        self.stop_event = threading.Event()

    @property
    def stop_requested(self) -> bool:
        return self.stop_event.is_set()
