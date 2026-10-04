"""bililive 的前端窗口 —— 居中大窗口 + 右上角迷你悬浮窗,两种形态可切换。

设计目标(用户明确提出)
----------------------
1. **好看一点**。深色卡片风格,统一配色,圆角按钮(Style.map 做悬停效果),
   不要 Tkinter 默认那种灰扑扑的 90 年代样子。
2. **大一点、居中**。默认 420x300 左右,屏幕正中。
3. **两种形态**:
   * 完整窗口 —— 输入房间号、调音量、看状态,一切在这里控制
   * 迷你悬浮窗 —— 缩到右上角,只剩暂停/继续 + 音量
   两者用「缩成悬浮窗 / 展开」按钮自由切换,**不是**一点开始就自动变小。
4. 回车就能开始,不用非得点按钮。

线程模型(改前必读)
------------------
    Tkinter  : 必须在主线程,否则会随机崩
    播放循环 : 后台线程(它是几十小时的长循环,放主线程窗口必卡死)

所以本文件在主线程,只做两件事:用 after() 轮询 control 的状态刷新界面;
把用户的操作翻译成对 control 的调用。

*** 绝对不要在按钮回调里做耗时操作 ***
比如「开始」如果在这里同步等「拿到流地址」,窗口会直接卡住(用户看到
「无响应」)。所以只启动线程,状态交给轮询去显示。

*** 也不要在这里停 ffplay ***
暂停只改 control.paused,由播放循环去关进程。回调里等子进程结束 = 卡界面。
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
from tkinter import ttk

# 封面图要用 Pillow。为什么不用 tk.PhotoImage：
#   PhotoImage 只认 PNG/GIF，而实测 B 站的封面**两种格式都有** ——
#   开播中的是 .png，未开播的是 .jpg（`.../new_room_cover/...jpg`）。
#   用 PhotoImage 的话未开播的封面会整片加载失败。
try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except Exception:          # 打包漏了 Pillow 时也不该整个程序起不来
    _HAS_PIL = False

# ---------------------------------------------------------------- 配色
# 主题：B 站粉。官方品牌色是 #FB7299（B 站 Logo 和 App 主色）。
# 深色底 + 粉色强调，粉色才跳得出来；底色刻意保留中性深灰，
# 而不是染成粉调 —— 整片粉会显得腻，也让状态文字难读。
_BG = "#17161a"          # 窗口底（带一点点暖调，和粉更搭）
_CARD = "#211f25"        # 卡片
_CARD_HI = "#2a2730"     # 悬停
_LINE = "#332f39"        # 分隔线
_FG = "#ece9ee"          # 主文字
_FG_DIM = "#a49daa"      # 次要文字
_FG_FAINT = "#736d7a"    # 提示文字
_ACCENT = "#fb7299"      # B 站粉（品牌主色）
_ACCENT_HI = "#ff8fb0"   # 悬停/按下时更亮一档
_DANGER = "#ff6b6b"      # 错误（红）
_WARN = "#f0b34a"        # 提示/未开播（琥珀）—— 和错误区分开调性

_UI_FONT = "Microsoft YaHei UI"

_POLL_MS = 200           # 状态轮询间隔

# 正在被拖动的滑块。自绘滑块在 <Button-1>/<B1-Motion> 里把它设成自己,
# 回调据此决定「同步另一个滑块」而不是回头去设自己(那样会无限递归)。
# 用模块级字典而不是实例属性,是因为回调是绑在方法上的,拿不到「谁在调我」。
_ACTIVE: dict = {"slider": None}


class _Slider(tk.Canvas):
    """自绘音量滑块。

    为什么不用 ttk.Scale(第一版就是它):
        clam 主题的 Scale 会画一个带 3D 边框的滑块方块，在深色界面上很突兀。
        我试过改 Scale.slider 的 background/bordercolor/lightcolor,甚至
        relief=flat,得到的仍然是一个「空心蓝框」的怪东西 —— 实测截图确认过。
        与其继续猜 ttk 主题的内部元素名,不如用 Canvas 自己画:
        圆角轨道 + 已填充部分 + 圆形滑块,外观完全可控,也更容易保持一致。
    """

    H = 22               # 控件高度
    TRACK = 6            # 轨道粗细
    THUMB = 8            # 滑块半径

    def __init__(self, parent, value: float, command, width: int = 200):
        super().__init__(parent, height=self.H, width=width, bg=_BG,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._cmd = command
        # *** 千万不要用 self._w 存宽度 ***
        # _w 是 tkinter 内部保存「本控件的 Tcl 路径名」（形如 '.!canvas'）的
        # 属性，tkinter 自己会用它去调 Tcl。把它覆盖成整数 110 之后，
        # 任何 self.delete()/self.tk.call() 都会炸：
        #     TclError: invalid command name "110"
        # 实测踩过，且报错信息完全看不出是命名冲突。用一个明确的名字。
        self._bar_w = width
        self._value = max(0.0, min(100.0, float(value)))
        self._enabled = True
        self.bind("<Configure>", self._on_resize)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._redraw()

    # ---- 对外接口 ----

    def get(self) -> float:
        return self._value

    def set(self, value: float) -> None:
        """程序性设值:**不**回调节器,避免无限递归。"""
        v = max(0.0, min(100.0, float(value)))
        if abs(v - self._value) < 0.01:
            return
        self._value = v
        self._redraw()

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        self._redraw()

    # ---- 绘制 ----

    def _track_rect(self):
        pad = self.THUMB + 2
        y = self.H // 2
        return pad, y, max(pad + 10, self._bar_w - pad), y

    def _value_to_x(self) -> float:
        x0, _, x1, _ = self._track_rect()
        return x0 + (x1 - x0) * (self._value / 100.0)

    def _on_resize(self, ev):
        self._bar_w = max(40, ev.width)
        self._redraw()

    def _redraw(self):
        self.delete("all")
        x0, y, x1, _ = self._track_rect()
        active = _ACCENT if self._enabled else _LINE
        # 轨道底
        self.create_line(x0, y, x1, y, fill=_LINE, width=self.TRACK,
                         capstyle="round")
        # 已填充部分
        tx = self._value_to_x()
        if tx > x0 + 1:
            self.create_line(x0, y, tx, y, fill=active, width=self.TRACK,
                             capstyle="round")
        # 圆形滑块（外面一圈底色，做出「挖空」的干净观感）
        r = self.THUMB
        self.create_oval(tx - r, y - r, tx + r, y + r, fill=_BG,
                         outline=active, width=3)

    # ---- 交互 ----

    def _x_to_value(self, x: float) -> float:
        x0, _, x1, _ = self._track_rect()
        if x1 <= x0:
            return self._value
        return max(0.0, min(100.0, (x - x0) / (x1 - x0) * 100.0))

    def _on_press(self, ev):
        if not self._enabled:
            return
        _ACTIVE["slider"] = self          # 标记「是自己在被拖」
        self._value = self._x_to_value(ev.x)
        self._redraw()
        self._cmd(self._value, False)

    def _on_drag(self, ev):
        if not self._enabled:
            return
        _ACTIVE["slider"] = self
        self._value = self._x_to_value(ev.x)
        self._redraw()
        self._cmd(self._value, False)

    def _on_release(self, _ev):
        if not self._enabled:
            return
        # 松手 -> 告诉上层「可以真正应用了」
        self._cmd(self._value, True)
        _ACTIVE["slider"] = None


class Overlay:
    """主窗口 + 迷你悬浮窗,两副面孔同一个 Tk 根窗口。"""

    # ---- 固定尺寸（用户要求：不要自适应，以左列为基准）----
    LEFT_W = 460           # 左列（播放控制）固定宽度
    MIN_H = 560            # 主窗口最小高度；实际取「左列换行后需要的高度」
    # ---- 封面图相关常量 ----
    COVER_MAIN = 96        # 主界面封面边长
    COVER_CARD = 44        # 收藏卡片封面边长
    # 收藏面板宽度。两列卡片：每张约 (380-20-8)/2 = 176px。
    # 这个值要压住 —— 太宽会把主窗口撑到 900px 以上，看着很空。
    _SIDE_W = 380
    # 向 B 站图床要缩略图的后缀。原图 121 KB，加这个变成约 1.5 KB（82 倍）。
    # 实测它返回 WEBP，Pillow 能读。
    _THUMB = "@%dw_%dh_1c.webp"

    def __init__(self, control, room: int | None = None):
        self.control = control
        self.room = room
        self._drag = None
        self._last_status = None
        self._last_paused = None
        self._last_playing = None
        self._last_anchor = None
        self._last_title = None
        self._last_cover = None
        # 上一次「有没有房间」的状态，用来决定那块信息（含黄星）显不显示
        self._last_has_room = None
        self._started = False
        # 当前正在听的房间号（用来判断用户是不是换了房间）
        self._room_now = None
        # ---- 收藏 / 封面 ----
        # 已经下载并缩放好的封面图，key 是「URL + 尺寸」。
        # *** 必须留住引用 ***：PhotoImage 被垃圾回收后，控件上就变成一片空白 ——
        # 这是 Tkinter 里最经典的坑之一。
        self._img_cache: dict = {}
        self._img_lock = threading.Lock()
        # 正在下载的 URL，避免同一张图被并发重复下载
        self._img_pending: set = set()
        # 后台线程下载+解码好的图，等主线程来取（Image 对象，还没变成 PhotoImage）
        self._img_decoded: dict = {}
        # 下载/解码失败的 key，用来把对应回调丢掉，避免无限堆积
        self._img_failed: set = set()
        # 等图用的回调队列 [(key, cb)]，主线程轮询时兑现
        self._img_wait: list = []
        # 后台刷新开播状态的交接变量（主线程轮询取，见 _poll_bookmark_done）
        self._bm_done = False
        self._bm_result: dict = {}
        self._bm_auto = False
        # 收藏卡片的控件，更新开播状态时按房间号找回来
        self._bm_cards: dict = {}
        # 需要按真实宽度裁剪的文字标签，以及它们的原始文案。
        # 用 id(lbl) 做键：标签是每次重建新建的，用弱引用没必要，重建时整体清空。
        self._bm_fit_labels: list = []
        self._bm_texts: dict = {}
        # 收藏面板当前是否只显示开播中的
        self._bm_only_live = False
        # 正在刷新开播状态（防重复点击）
        self._bm_refreshing = False
        # 主界面封面当前显示的是哪个 URL（避免重复重设）
        self._cover_shown = None
        # 悬浮提示的状态
        self._last_error_seq = 0
        self._last_offline_seq = 0
        self._last_status_seq = 0
        self._toast_job = None
        # 是否正在「主动换房间」——用来区分「循环退出」是换台还是真结束，
        # 免得换台过程中误报「已停止」
        self._switch_in_progress = False

        self.win = tk.Tk()
        self.win.title("bililive")
        self.win.configure(bg=_BG)
        # *** 主窗口保留系统标题栏，迷你悬浮窗才去边框 ***
        # 一开始两种形态都用 overrideredirect(True)（无边框）。但主窗口没有
        # 标题栏时，Windows 不把它当成「正常窗口」，焦点行为很怪：
        # 用户点击界面后按键会落进房间号输入框，把房间号改掉（实测截图里
        # 房间号变成了「6」），甚至触发一次重新连接。
        # 有标题栏之后，焦点、最小化、任务栏行为都回归正常。
        self.win.resizable(False, False)
        # *** 主窗口刻意**不**设透明 ***
        # 用户明确要求主界面不要透明度。之前设了 -alpha 0.98，在浅色壁纸上
        # 会让整个窗口发灰、文字对比度下降。迷你悬浮窗仍然保持半透明
        # （见 _to_compact），那种小窗不挡视线是优点，主窗口不是。

        self._setup_style()
        self._set_window_icon()
        # *** 建界面期间不要触发音量回调 ***
        # 回调里要更新「主窗口滑块」和「迷你悬浮窗滑块」两个控件，
        # 而它们是分别构造的 —— 先建哪个，另一个都还不存在。
        # 实测先 full 后 compact 会报 lbl_mini_vol 不存在，反过来报 scale 不存在。
        # 自绘滑块的 set() 本身不回调，所以这里主要防的是别处意外触发；
        # 保留 _ready 是一个便宜且明确的保险。
        self._ready = False
        # 收藏列表。读盘失败时 BookmarkStore 内部退回空列表，不会抛出来。
        try:
            from .bookmarks import BookmarkStore
            self.store = BookmarkStore()
        except Exception:
            self.store = None
        self._build_compact()
        self._build_full()
        self._build_toast()
        self.scale.set(self.control.volume)
        self.scale_mini.set(self.control.volume)
        self.lbl_vol.config(text=f"{self.control.volume}%")
        self.lbl_mini_vol.config(text=f"{self.control.volume}%")
        self._ready = True
        # 先把已有的收藏画出来（用盘里的快照，不发任何网络请求）
        self.rebuild_bookmarks()
        # *** 构造完必须立刻刷一次界面 ***
        # 否则按钮停在 Tk 的默认状态上（实测：「暂停」一打开就是可点的，
        # 但那时根本没开始听，要等第一次 200ms 轮询才变灰）。
        # 界面第一次出现就该是正确状态，不能有一个错帧。
        self._refresh(force=True)
        self._center_full()
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)
        # *** 每次打开软件都触发一次「查看开播状态」***（用户明确要求）。
        # 延后 400ms 是为了先让窗口出来，别把启动路径堵在网络上。
        self.win.after(400, lambda: self.refresh_bookmarks(auto=True))

        if room is not None:
            self._room_var.set(str(room))

    # ------------------------------------------------------------ 样式

    def _set_window_icon(self) -> None:
        """给窗口和任务栏设图标。

        为什么要单独做这件事：exe 的图标（PyInstaller --icon）只决定
        **资源管理器里** exe 文件长什么样，以及部分场景下的任务栏；
        而 Tkinter 窗口的标题栏和任务栏按钮是**运行时**取的，
        不设的话就是一个空的默认图标（不是 exe 的图标）。
        两者都设上，图标才在哪儿都一致。

        两种形态用的是同一个 Tk 根窗口，所以设一次就够。
        """
        for path in self._icon_candidates():
            try:
                if os.path.isfile(path):
                    # *** 不能写 iconbitmap(default=path) ***
                    # 带 default= 时，Tk 设的是「**今后新建**的顶层窗口的默认
                    # 图标」，**不包括当前这个窗口** —— 实测设完之后
                    # win.iconbitmap() 返回空串、窗口类里的 HICON 也是 None，
                    # 也就是完全没生效。
                    # 直接传路径才是设当前窗口。
                    self.win.iconbitmap(path)
                    return
            except Exception:
                continue

    @staticmethod
    def _icon_candidates():
        """图标可能的落点，按优先级排。

        打包后（onedir）图标和 exe 并排；源码运行时在源码目录里。
        和 ffplay.py 的 app_dir() 是同一个思路：不依赖绝对路径。
        """
        here = os.path.dirname(os.path.abspath(__file__))
        cands = [
            os.path.join(here, "bililive.ico"),          # 源码目录
            os.path.join(here, "_iconout", "bililive.ico"),
        ]
        if getattr(sys, "frozen", False):
            # 打包后：exe 所在目录
            exe_dir = os.path.dirname(os.path.abspath(sys.executable))
            cands.insert(0, os.path.join(exe_dir, "bililive.ico"))
        return cands

    def _setup_style(self) -> None:
        st = ttk.Style(self.win)
        try:
            st.theme_use("clam")     # clam 才允许自定义背景色
        except Exception:
            pass
        st.configure("bi.TButton", font=(_UI_FONT, 10), padding=(14, 9),
                     background=_ACCENT, foreground="#ffffff",
                     borderwidth=0, focuscolor=_ACCENT)
        st.map("bi.TButton",
               background=[("active", _ACCENT_HI), ("disabled", _LINE)],
               foreground=[("disabled", _FG_FAINT)])
        st.configure("ghost.TButton", font=(_UI_FONT, 9), padding=(10, 6),
                     background=_CARD, foreground=_FG_DIM, borderwidth=0,
                     focuscolor=_CARD)
        st.map("ghost.TButton",
               background=[("active", _CARD_HI)],
               foreground=[("active", _FG)])
        st.configure("big.TButton", font=(_UI_FONT, 13), padding=(16, 10),
                     background=_ACCENT, foreground="#ffffff",
                     borderwidth=0, focuscolor=_ACCENT)
        st.map("big.TButton",
               background=[("active", _ACCENT_HI), ("disabled", _LINE)],
               foreground=[("disabled", _FG_FAINT)])
        # 音量滑块是自绘的（见 _Slider），这里不再需要 ttk.Scale 的样式，
        # 相关尝试已删除 —— 保留只会让人以为改样式能影响滑块外观。

    def _mk_entry(self, parent, textvariable, width=22):
        # 刻意**不**加 validatecommand 限制只能输数字。
        # 我一度加过「只许数字」的校验，想把乱按的键挡掉；但用户澄清那个
        # 数字是他自己输入的，而这条限制会顺带挡住「直接粘贴整条直播间网址」
        # 这种正常用法（_parse_room 本来能从网址里抠出房间号）。
        # 所以这里保持宽松：允许任何文本，等到点「开始收听」时再取数字。
        return tk.Entry(parent, textvariable=textvariable, width=width,
                        font=(_UI_FONT, 22), justify="center",
                        bg=_CARD_HI, fg=_FG, insertbackground=_ACCENT,
                        relief="flat", highlightthickness=2,
                        highlightbackground=_CARD_HI,
                        highlightcolor=_ACCENT)

    # ------------------------------------------------------------ 完整窗口

    def _build_full(self) -> None:
        self.full = tk.Frame(self.win, bg=_BG)
        self.full.pack(fill="both", expand=True)

        # ---- 标题 ----
        # 注意：这里**没有**自画的关闭按钮 —— 主窗口现在有系统标题栏，
        # 右上角已经有 ✕ 了，再画一个就是重复。
        head = tk.Frame(self.full, bg=_BG)
        head.pack(fill="x", padx=22, pady=(18, 0))
        tk.Label(head, text="bililive", bg=_BG, fg=_ACCENT,
                 font=(_UI_FONT, 17, "bold")).pack(side="left")
        tk.Label(head, text="只听哔哩哔哩直播的声音", bg=_BG, fg=_FG_DIM,
                 font=(_UI_FONT, 9)).pack(side="left", padx=(8, 0), pady=(6, 0))

        tk.Frame(self.full, bg=_LINE, height=1).pack(fill="x", padx=22,
                                                     pady=(12, 14))

        # ---- 主体：左（播放控制）+ 右（收藏列表）----
        # 用两列而不是单列：收藏是「边看边点」的东西，放右侧不打断左侧操作。
        #
        # *** 左列宽度钉死，这是整个窗口尺寸的基准 ***
        # 原来左列是 fill+expand，宽度随窗口走 —— 那就没有"基准"可言。
        # 现在左列固定 LEFT_W，右列吃掉剩余空间，窗口总宽固定，
        # 内容变化只在列内消化（文字换行、收藏栏滚动）。
        body = tk.Frame(self.full, bg=_BG)
        body.pack(fill="both", expand=True)
        self._body = body

        col = tk.Frame(body, bg=_BG, width=self.LEFT_W)
        col.pack(side="left", fill="both")
        # 关掉尺寸传播：不关的话左列会被内容撑宽（长标题、长状态文字），
        # 基准就守不住了。关掉之后宽度严格等于 LEFT_W。
        col.pack_propagate(False)
        self._main_col = col

        # 窗口本身也不允许拖拽缩放 —— 固定尺寸的意图要贯彻到底
        self.win.resizable(False, False)

        tk.Frame(body, bg=_LINE, width=1).pack(side="left", fill="y",
                                              padx=(6, 0))

        side = tk.Frame(body, bg=_BG, width=self._SIDE_W)
        side.pack(side="left", fill="both", expand=True)
        side.pack_propagate(False)      # 保持固定宽度，不被内容撑开
        self._side = side
        self._build_bookmarks(side)

        # ---- 房间号 ----
        tk.Label(col, text="直播间房间号", bg=_BG, fg=_FG_DIM,
                 font=(_UI_FONT, 9)).pack(anchor="w", padx=(24, 16))
        self._room_var = tk.StringVar(value="")
        self.ent_room = self._mk_entry(col, self._room_var)
        self.ent_room.pack(padx=(24, 16), pady=(6, 0), fill="x")
        # 回车即开始 —— 用户明确要「点回车就能用」
        self.ent_room.bind("<Return>", lambda e: self._on_start())
        self.ent_room.bind("<KP_Enter>", lambda e: self._on_start())
        # *** 输入框内容变化要立刻重算按钮 ***
        # 主按钮的文字取决于「输入的房间号是不是当前这个」：
        # 播放中把它改成别的房间号，按钮要马上从「重新连接」变成「换到该房间」，
        # 用户才知道点了会换台。不绑这个的话要等 200ms 轮询才更新，
        # 而且旧逻辑根本不看输入框，按钮永远显示「正在播放」。
        self._room_var.trace_add("write", lambda *a: self._refresh())

        tk.Label(col, text="地址栏 live.bilibili.com/ 后面那串数字",
                 bg=_BG, fg=_FG_FAINT, font=(_UI_FONT, 8)).pack(
                     anchor="w", padx=(24, 16), pady=(5, 0))

        # ---- 主播 / 封面 / 标题（连上之前不占地方，连上后才显示）----
        # 用 pack/pack_forget 动态出入，而不是留一片空白占位：
        # 没连上时界面保持紧凑，连上后信息才出现。
        self.meta = tk.Frame(col, bg=_BG)
        # 封面在左，文字在右；收藏星标放在封面**上方**（用户要求
        # 「封面上面有个收藏的黄星按钮」）。
        self._cover_box = tk.Frame(self.meta, bg=_BG, width=self.COVER_MAIN,
                                   height=self.COVER_MAIN)
        self._cover_box.pack(side="left")
        self._cover_box.pack_propagate(False)
        cv = self.COVER_MAIN
        self.cv_cover = tk.Canvas(self._cover_box, width=cv, height=cv,
                                  bg=_CARD, highlightthickness=0, bd=0)
        self.cv_cover.place(x=0, y=0)
        # 星标压在封面右上角。用 Canvas 里画的星形而不是字符 ★：
        # 字符在不同字体下大小/基线差别很大，画出来的才能精确控制位置和大小。
        self.btn_star = tk.Canvas(self._cover_box, width=26, height=26,
                                  bg=_BG, highlightthickness=0, bd=0,
                                  cursor="hand2")
        self.btn_star.place(x=cv - 26, y=0)
        self._star_on = None            # 当前画的是「已收藏」还是「未收藏」
        self.btn_star.bind("<Button-1>", lambda e: self._on_star())
        self.btn_star.bind("<Enter>", lambda e: self._draw_star(hover=True))
        self.btn_star.bind("<Leave>", lambda e: self._draw_star(hover=False))
        self._draw_star()

        info = tk.Frame(self.meta, bg=_BG)
        info.pack(side="left", fill="both", expand=True, padx=(12, 16))
        self.lbl_anchor = tk.Label(info, text="", bg=_BG, fg=_FG,
                                   font=(_UI_FONT, 10, "bold"), anchor="w")
        self.lbl_anchor.pack(anchor="w")
        self.lbl_title = tk.Label(info, text="", bg=_BG, fg=_FG_DIM,
                                  font=(_UI_FONT, 9), anchor="w",
                                  justify="left")
        self.lbl_title.pack(anchor="w", pady=(2, 0))

        # ---- 音量 ----
        vrow = tk.Frame(col, bg=_BG)
        vrow.pack(fill="x", padx=(24, 16), pady=(16, 0))
        # 保存下来：主播信息那块连上后要插到它前面（见 _refresh_meta）。
        # pack 的 before= 需要一个具体控件，靠遍历 winfo_children 找太脆。
        self._vrow = vrow
        tk.Label(vrow, text="音量", bg=_BG, fg=_FG_DIM,
                 font=(_UI_FONT, 9)).pack(side="left")
        self.lbl_vol = tk.Label(vrow, text="100%", bg=_BG, fg=_FG,
                                font=(_UI_FONT, 9, "bold"))
        self.lbl_vol.pack(side="right")
        self.scale = _Slider(col, value=self.control.volume,
                             command=self._on_vol_move)
        self.scale.pack(fill="x", padx=(24, 16), pady=(4, 0))

        # ---- 状态 ----
        self.lbl_status = tk.Label(col, text="输入房间号后点「开始收听」，或直接按回车",
                                   bg=_BG, fg=_FG_FAINT, font=(_UI_FONT, 9),
                                   anchor="w", justify="left")
        # 换行宽度不写死，由 _apply_wraplengths() 按窗口实际宽度设置。
        # 写死 372 时窗口窄了文字会溢出被裁，而不是换行。
        self.lbl_status.pack(fill="x", padx=(24, 16), pady=(16, 0))

        # ---- 底部按钮 ----
        # 只保留两个：主按钮（开始/继续，同一个）+ 暂停。
        # 之前有四个（开始收听、暂停、缩成悬浮窗、停止），用户明确反馈
        # 「逻辑多余」「停止是什么？不需要」—— 一个按钮表达一件事就够了：
        #   开始 / 继续 -> 主按钮
        #   暂停        -> 暂停按钮
        #   停止        -> 关窗口就行（Job Object 会连带杀掉 ffplay）
        #   缩成悬浮窗  -> 「缩成悬浮窗」也在这一行，它是形态切换不是播放控制
        brow = tk.Frame(col, bg=_BG)
        brow.pack(fill="x", padx=(22, 16), pady=(14, 18))
        self.btn_main = ttk.Button(brow, text="开始收听", style="big.TButton",
                                   command=self._on_main)
        self.btn_main.pack(side="left", fill="x", expand=True)
        self.btn_pause = ttk.Button(brow, text="暂停", style="ghost.TButton",
                                    command=self._on_toggle)
        self.btn_pause.pack(side="left", padx=(8, 0))
        self.btn_mini = ttk.Button(brow, text="缩成悬浮窗",
                                   style="ghost.TButton",
                                   command=self._to_compact)
        self.btn_mini.pack(side="left", padx=(8, 0))

    # ================================================== 收藏（书签）列表

    def _build_bookmarks(self, parent) -> None:
        """右侧收藏面板：标题栏 + 刷新按钮 + 可滚动的两列卡片。"""
        # ---- 面板标题 ----
        hd = tk.Frame(parent, bg=_BG)
        hd.pack(fill="x", padx=(12, 12), pady=(0, 8))
        tk.Label(hd, text="收藏", bg=_BG, fg=_FG,
                 font=(_UI_FONT, 11, "bold")).pack(side="left")
        self.lbl_bm_count = tk.Label(hd, text="", bg=_BG, fg=_FG_FAINT,
                                     font=(_UI_FONT, 8))
        self.lbl_bm_count.pack(side="left", padx=(6, 0), pady=(3, 0))
        # 「只看开播」开关：收藏多了以后，用户最想做的是从正在播的里面挑一个
        self.btn_bm_live = tk.Label(hd, text="只看开播", bg=_BG, fg=_FG_FAINT,
                                    font=(_UI_FONT, 8), cursor="hand2",
                                    padx=6, pady=2)
        self.btn_bm_live.pack(side="right")
        self.btn_bm_live.bind("<Button-1>", lambda e: self._toggle_only_live())
        self.btn_refresh = tk.Label(hd, text="刷新状态", bg=_CARD, fg=_FG,
                                    font=(_UI_FONT, 8), cursor="hand2",
                                    padx=8, pady=3)
        self.btn_refresh.pack(side="right", padx=(0, 8))
        self.btn_refresh.bind("<Button-1>", lambda e: self.refresh_bookmarks())
        self.btn_refresh.bind("<Enter>",
                              lambda e: self.btn_refresh.config(bg=_CARD_HI))
        self.btn_refresh.bind("<Leave>",
                              lambda e: self.btn_refresh.config(bg=_CARD))

        # ---- 可滚动区域 ----
        # Tkinter 没有现成的滚动容器，标准做法就是 Canvas + 内嵌 Frame。
        wrap = tk.Frame(parent, bg=_BG)
        wrap.pack(fill="both", expand=True, padx=(12, 8), pady=(0, 12))
        self._bm_canvas = tk.Canvas(wrap, bg=_BG, highlightthickness=0, bd=0)
        self._bm_canvas.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(wrap, orient="vertical",
                           command=self._bm_canvas.yview)
        sb.pack(side="right", fill="y")
        self._bm_canvas.configure(yscrollcommand=sb.set)
        # 卡片容器
        self._bm_inner = tk.Frame(self._bm_canvas, bg=_BG)
        self._bm_win = self._bm_canvas.create_window(
            (0, 0), window=self._bm_inner, anchor="nw")
        self._bm_inner.bind(
            "<Configure>",
            lambda e: self._bm_canvas.configure(
                scrollregion=self._bm_canvas.bbox("all")))
        self._bm_canvas.bind(
            "<Configure>",
            lambda e: self._bm_canvas.itemconfigure(self._bm_win,
                                                    width=e.width))
        # 滚轮。绑在 canvas 和卡片容器上，鼠标移到列表上就能滚。
        for w in (self._bm_canvas, self._bm_inner):
            w.bind("<MouseWheel>", self._bm_wheel)

        # 空列表时的提示。**不要在这里建好留引用**：
        # 每次重建都会 destroy 掉 _bm_inner 的所有子控件，留着的引用就指向
        # 一个已销毁的控件，下次 config() 会抛
        # "invalid command name ...!label"（实测踩到，异常还会刷在控制台）。
        # 改成每次重建时按需新建，引用即时可用即时丢。

    def _bm_wheel(self, event) -> None:
        try:
            self._bm_canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass

    def _toggle_only_live(self) -> None:
        self._bm_only_live = not self._bm_only_live
        self.btn_bm_live.config(
            fg=_ACCENT if self._bm_only_live else _FG_FAINT,
            text="只看开播" if not self._bm_only_live else "只看开播 ✓")
        self.rebuild_bookmarks()

    def rebuild_bookmarks(self) -> None:
        """按当前 store 重建收藏卡片。

        *** 只在主线程调用 ***
        原来这里写的是 `self.win.after(0, ...)`，注释还说「任何线程都能调」——
        那句注释是错的，也是危险的：它把「后台线程碰 Tk 没关系」变成了
        看起来理所当然的事。实际调用点（__init__ / _on_star /
        _toggle_only_live）全都在主线程，没有任何理由绕一圈。
        后台线程想刷新的话，写个标志让 _poll 去处理（见 _poll_bookmark_done）。
        """
        try:
            self._rebuild_bookmarks_ui()
        except Exception:
            pass

    def _rebuild_bookmarks_ui(self) -> None:
        if not self._ready:
            return
        for w in self._bm_inner.winfo_children():
            w.destroy()
        self._bm_cards.clear()

        items = self.store.all() if self.store else []
        # 排序：开播的排最前（用户最想点的是正在播的），未开播次之，
        # 「状态未知」排最后 —— 未知说明还没查到，不该排在已知未开播的前面。
        # 同一档内按收藏时间倒序（最近收藏的靠前）。
        # 注意走 _live_of：直接 `live_status or -1` 会把 0 也当成未知。
        def _rank(it):
            ls = self._live_of(it)
            return (0 if ls == 1 else 1 if ls == 0 else 2,
                    -float(it.get("added_at") or 0))
        items.sort(key=_rank)
        if self._bm_only_live:
            items = [it for it in items if self._live_of(it) == 1]

        all_items = self.store.all() if self.store else []
        live_n = sum(1 for it in all_items if self._live_of(it) == 1)
        total = len(all_items)
        self.lbl_bm_count.config(text=f"{live_n}/{total} 开播" if total else "")

        if not items:
            # 每次新建，不缓存引用（见 _build_bookmarks 里的说明）
            tip = ("没有正在开播的收藏" if self._bm_only_live and total
                   else "还没有收藏。\n播放时点封面上的 ☆ 收藏当前直播间。")
            tk.Label(self._bm_inner, text=tip, bg=_BG, fg=_FG_FAINT,
                     font=(_UI_FONT, 9), justify="left").pack(
                         anchor="w", padx=6, pady=(8, 0))
            self._bm_canvas.configure(scrollregion=(0, 0, 0, 0))
            return

        for i, it in enumerate(items):
            self._make_bm_card(self._bm_inner, it, i // 2, i % 2)
        # *** 两列必须等宽，而且必须能收缩 ***
        # 不配 columnconfigure 的话，grid 的列宽完全由内容决定：
        # 实测两列分别是 192 和 196（不等宽），总请求宽度 404，
        # 而收藏栏只有 360 —— **第二列右边被裁掉 36px**，
        # 用户看到的就是「并排两个直播间显示不全」。
        # uniform 保证两列等宽，weight 让它们平分可用宽度，
        # minsize 给一个下限免得窗口极窄时压成一条。
        self._bm_inner.columnconfigure(0, weight=1, uniform="bm", minsize=110)
        self._bm_inner.columnconfigure(1, weight=1, uniform="bm", minsize=110)
        self._bm_inner.update_idletasks()
        # *** 布局出来之后再按真实宽度裁文字 ***
        # 顺序很重要：此刻每个标签的 winfo_width 才是它在格子里的实际宽度。
        # 先裁后布局的话拿到的宽度是错的（实测因此少显示一个字）。
        for lbl in self._bm_fit_labels:
            self._fit_label(lbl, self._bm_texts.get(id(lbl), lbl.cget("text")))
        self._bm_fit_labels.clear()
        self._bm_texts.clear()
        self._bm_canvas.configure(
            scrollregion=self._bm_canvas.bbox("all"))

    @staticmethod
    def _cover_placeholder(canvas, size: int, name: str = ""):
        """封面缺失时的占位。

        *** 为什么不用一个灰方块 ***
        实测有些房间（比如房间 3）getRoomBaseInfo 返回的 cover 就是空串，
        keyframe / user_cover 也是空，getInfoByRoom 又是 -352 —— 什么图都拿不到。
        只画一个灰方块看起来像「界面坏了」。
        首次收藏时也会走到这里：那时还没刷新过，cover 字段还是空的。

        所以画**主播名的首字**：一个明确的圆形头像记号。
        它传达了「这里有一个主播，只是没有封面图」，比灰块或音符清楚得多，
        而且和整个列表的视觉语言一致。
        """
        try:
            canvas.delete("all")
            canvas.create_rectangle(0, 0, size, size, fill=_CARD_HI, outline="")
            ch = ""
            if name:
                s = "".join(str(name).split())
                if s:
                    ch = s[0]
            if ch:
                r = size * 0.34
                cx = cy = size / 2.0
                canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                   fill=_CARD, outline=_LINE)
                canvas.create_text(cx, cy, text=ch, fill=_FG_DIM,
                                   font=(_UI_FONT, max(9, int(size * 0.34)),
                                         "bold"))
            else:
                canvas.create_text(size / 2, size / 2, text="?",
                                   fill=_FG_FAINT,
                                   font=(_UI_FONT, max(9, int(size * 0.3))))
        except Exception:
            pass

    @staticmethod
    def _live_of(item) -> int:
        """取 live_status：1 开播 / 0 未开播 / -1 未知。

        *** 不能用 `item.get("live_status") or -1` ***
        0 是 falsy，`0 or -1` 得到 -1 —— 于是「未开播」会被显示成
        「状态未知」，而且排序也把未开播排到了未知的后面。
        实测踩到：明明存的是 0，卡片上却是「? 状态未知」。
        必须显式判 None。
        """
        v = item.get("live_status")
        if v is None:
            return -1
        try:
            return int(v)
        except Exception:
            return -1

    def _make_bm_card(self, parent, item, r: int, c: int) -> None:
        """画一张收藏卡片：封面 + 主播名 + 房间号 + 标题前几位。"""
        rid = int(item["room_id"])
        live = self._live_of(item)
        card = tk.Frame(parent, bg=_CARD)
        card.grid(row=r, column=c, sticky="nsew", padx=(0, 8), pady=(0, 8))
        # *** 关掉尺寸传播，宽度完全交给 grid 决定 ***
        # 不关的话卡片会按内容「请求宽度」，而请求宽度会被长标题撑大：
        # 实测两列 request 合计 448，收藏栏只有 360 —— 于是第二列被裁。
        # 关掉之后列宽严格由 columnconfigure 的 weight/uniform 决定，
        # 文字只能在自己的格子里被截断，不可能再把布局撑出去。
        card.grid_propagate(False)

        top = tk.Frame(card, bg=_CARD)
        top.pack(fill="x", padx=8, pady=(8, 4))

        # 封面。用 Canvas + create_image，这样图片能精确贴边。
        sz = self.COVER_CARD
        cv = tk.Canvas(top, width=sz, height=sz, bg=_CARD_HI,
                       highlightthickness=0, bd=0)
        cv.pack(side="left")
        uname_raw = (item.get("uname") or "").strip()
        # 先放占位，图下好了再覆盖（见 _set_card_cover）。
        # 这样「没有封面」和「封面还在下载」都有像样的显示，而不是一块灰。
        self._cover_placeholder(cv, sz, uname_raw)
        # 一圈细边框，让占位和封面两种情况都有明确的边界
        cv.create_rectangle(0, 0, sz, sz, outline=_LINE, width=1)
        # 开播状态点画在**最上层**：占位图里也有内容，不放在最后会被盖住
        dot = _ACCENT if live == 1 else (_LINE if live == 0 else _FG_FAINT)
        cv.create_oval(3, 3, 11, 11, fill=dot, outline=_CARD, width=1,
                       tags="dot")
        cv.tag_raise("dot")

        txt = tk.Frame(top, bg=_CARD)
        txt.pack(side="left", fill="both", expand=True, padx=(8, 0))
        # *** 顺序：主播名在上，房间号在下 ***（用户明确要求）
        # 主播名是「人」的标识，更常用来认；房间号是精确标识，放次要位置。
        uname = uname_raw or "（未知主播）"
        # 文字先用完整内容建好，等布局出来再用 _fit_label 按**真实可用宽度**
        # 裁剪（见 _rebuild_bookmarks_ui 末尾）。在这里估算像素是不可靠的。
        lb_u = tk.Label(txt, text=uname, bg=_CARD, fg=_FG,
                        font=(_UI_FONT, 9), anchor="w")
        lb_u.pack(anchor="w")
        # 房间号是精确标识，放次要位置（用户要求主播名在上）
        lb_id = tk.Label(txt, text=str(rid), bg=_CARD, fg=_ACCENT,
                         font=(_UI_FONT, 8, "bold"), anchor="w")
        lb_id.pack(anchor="w")
        title = (item.get("title") or "").strip() or "—"
        lb_t = tk.Label(txt, text=title, bg=_CARD, fg=_FG_DIM,
                        font=(_UI_FONT, 8), anchor="w")
        lb_t.pack(anchor="w")
        # 记下原始文案：重建/缩放时要重新裁（已裁过的不能再裁，否则越裁越短）
        self._bm_texts[id(lb_u)] = uname
        self._bm_texts[id(lb_t)] = title
        self._bm_fit_labels.append(lb_u)
        self._bm_fit_labels.append(lb_t)

        # 底部一行：开播状态文字
        if live == 1:
            stat, sc = "● 正在直播", _ACCENT
        elif live == 0:
            stat, sc = "○ 未开播", _FG_FAINT
        else:
            stat, sc = "? 状态未知", _FG_FAINT
        lb_s = tk.Label(card, text=stat, bg=_CARD, fg=sc,
                        font=(_UI_FONT, 8), anchor="w")
        lb_s.pack(anchor="w", padx=8, pady=(0, 8))

        # *** 量出需要的高度，再把尺寸传播关掉 ***
        # grid_propagate(False) 会让卡片高度也失去依据（变成 1px），
        # 所以不能拍一个数字：先让它按内容算一次（这时还算得出来），
        # 再锁定尺寸。宽度直接按两列平分的目标值给，与 grid 的列宽一致。
        card.update_idletasks()
        want_h = card.winfo_reqheight()
        card.grid_propagate(False)
        card.configure(width=self._bm_card_w() - 8, height=want_h)

        widgets = [card, top, cv, txt, lb_u, lb_t, lb_s]
        for w in widgets:
            w.bind("<Button-1>", lambda e, x=rid: self.play_bookmark(x))
            w.bind("<Enter>", lambda e, cc=card: self._bm_hover(cc, True))
            w.bind("<Leave>", lambda e, cc=card: self._bm_hover(cc, False))
            try:
                w.configure(cursor="hand2")
            except Exception:
                pass

        self._bm_cards[rid] = {"card": card, "canvas": cv, "stat": lb_s,
                               "uname": lb_u, "title": lb_t, "cover": None}
        # 异步取封面图
        self.load_cover(item.get("cover") or "", self.COVER_CARD,
                        lambda img, rid=rid: self._set_card_cover(rid, img))

    @staticmethod
    def _bm_hover(card, on: bool) -> None:
        """悬停时整张卡片换底色。只改 card 自己，子控件保持 _CARD 会突兀，
        所以子控件也跟着换 —— 用遍历而不是逐个记录，免得漏掉。"""
        want = _CARD_HI if on else _CARD
        stack = [card]
        while stack:
            w = stack.pop()
            try:
                if w.cget("bg") in (_CARD, _CARD_HI):
                    w.configure(bg=want)
            except Exception:
                pass
            try:
                stack.extend(w.winfo_children())
            except Exception:
                pass

    def _bm_card_w(self) -> int:
        """一张收藏卡片的可用宽度（两列平分收藏栏）。

        减掉：两列之间的 8px 间距、右列右侧的 8px 内边距。
        """
        w = int(getattr(self._bm_canvas, "winfo_width", lambda: 0)() or 0)
        if w <= 1:
            w = self._SIDE_W - 12 - 8 - 8      # 还没布局出来时的估算
        return max(110, (w - 8) // 2)

    @staticmethod
    def _fit(text: str, px: int, font) -> str:
        """把文字裁到**真实像素宽度** px 以内，放不下就加省略号。

        *** 为什么不用「字数」或「估算的 px/字」 ***
        一开始我按「中文 9.5px/字」估算，实测真实值是 **13px/字**，
        于是算出来放得下、实际却溢出被切（「七海Nana7mi」的「米」没了、
        「...赛事官方账号」的「号」没了）。字体度量是字体自己的事，
        猜不得 —— 这里直接用 Tk 字体的 measure() 量。
        """
        t = " ".join(str(text).split())
        if px <= 0 or not t:
            return t
        try:
            if font.measure(t) <= px:
                return t
            ell = "…"
            ew = font.measure(ell)
            # 二分找最长的可显示前缀
            lo, hi, best = 0, len(t), 0
            while lo <= hi:
                mid = (lo + hi) // 2
                if font.measure(t[:mid]) + ew <= px:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            return (t[:best] + ell) if best > 0 else ell
        except Exception:
            return t

    def _fit_label(self, lbl, text: str) -> None:
        """按标签**当前实际宽度**裁剪文字再塞进去。

        必须用实际宽度，不能拿窗口宽度去算：实测卡片里文字起点在 x=60
        （封面 44 + 左右内边距），能用的只有 104px 左右，
        按窗口宽度算出来的可用量偏大，文字就会溢出被切。
        """
        try:
            lbl.update_idletasks()
            font = tkfont.Font(font=lbl.cget("font"))
            px = lbl.winfo_width()
            if px <= 1:
                px = lbl.winfo_reqwidth()
            # 留 2px 余量，避免刚好贴边被裁
            lbl.config(text=self._fit(text, max(20, px - 2), font))
        except Exception:
            try:
                lbl.config(text=text)
            except Exception:
                pass

    @staticmethod
    def _clip_px(text: str, px: int) -> str:
        """纯估算版（没有标签可用时的兜底）。真实场景请用 _fit_label。"""
        t = " ".join(str(text).split())
        if px <= 0:
            return t
        budget = px / 13.0          # 实测中文 13px/字
        used, out = 0.0, []
        for ch in t:
            w = 1.0 if ord(ch) > 0x2E80 else 0.56
            if used + w > budget:
                return ("".join(out) + "…") if out else t[:1]
            used += w
            out.append(ch)
        return t

    @staticmethod
    def _clip(text: str, n: int) -> str:
        """按字数截断。保留给不需要像素精度的场合。"""
        t = " ".join(str(text).split())
        return t if len(t) <= n else t[:n] + "…"

    def _set_card_cover(self, rid: int, img) -> None:
        entry = self._bm_cards.get(int(rid))
        if not entry or img is None:
            return
        try:
            entry["canvas"].delete("cover")
            entry["canvas"].create_image(0, 0, anchor="nw", image=img,
                                         tags="cover")
            entry["cover"] = img          # 留引用，别被回收
        except Exception:
            pass

    # ------------------------------------------------------------ 封面下载

    def load_cover(self, url: str, size: int, cb) -> None:
        """异步取封面并缩放到 size×size，然后回调 cb(PhotoImage)。

        **必须在后台线程下载**：封面虽然只有 1.5 KB，但建连 + 图床响应
        实测要 1~5 秒。放主线程会把界面冻住。

        *** 后台线程不碰 Tk ***
        下载完只把结果塞进 _img_ready/_img_wait 队列，由主线程的
        _poll_covers() 取出来创建 PhotoImage 并回调。
        第一版在后台线程里调 self.win.after(0, finish)，那是跨线程碰 Tk ——
        已经因此吃过一次工作线程卡死（见 refresh_bookmarks 的说明）。
        """
        if not url or not _HAS_PIL:
            return
        key = (url, size)
        with self._img_lock:
            if self._img_cache.get(key) is not None:
                # 已经下过：直接挂进队列，主线程下一轮 _poll_covers 就回调
                self._img_wait.append((key, cb))
                return
            # 同一张图正在下载，或还没开始 —— 都只是把回调挂上去。
            # 已经在下的话不重复起线程。
            self._img_wait.append((key, cb))
            if key in self._img_pending:
                return
            self._img_pending.add(key)

        def worker():
            img = None
            try:
                # 图床支持用 @宽_高 要缩略图：121 KB -> 1.5 KB，省 82 倍。
                # 部分老封面不认这个参数，那就退回原图自己缩。
                for u in (url + self._THUMB % (size, size), url):
                    try:
                        req = urllib.request.Request(
                            u, headers={"User-Agent": "Mozilla/5.0",
                                        "Referer": "https://live.bilibili.com/"})
                        with urllib.request.urlopen(req, timeout=12) as r:
                            data = r.read(3 * 1024 * 1024)
                        im = Image.open(io.BytesIO(data))
                        im.load()
                        img = im.convert("RGB")
                        break
                    except Exception:
                        continue
                if img is None:
                    return
                # 正方裁切（封面是 16:9，直接缩小会变形）
                w, h = img.size
                m = min(w, h)
                img = img.crop(((w - m) // 2, (h - m) // 2,
                                (w - m) // 2 + m, (h - m) // 2 + m))
                img = img.resize((size, size), Image.LANCZOS)
            except Exception:
                img = None
            finally:
                with self._img_lock:
                    self._img_pending.discard(key)
            if img is not None:
                # 只写共享变量：Image 对象本身是安全的，
                # 需要主线程做的只有「创建 PhotoImage」这一步。
                with self._img_lock:
                    self._img_decoded[key] = img
            else:
                # *** 下载/解码失败也要留个记录 ***
                # 不留的话 _img_wait 里的回调永远不会被兑现，也不会被清掉 ——
                # 每次重建收藏卡片都往里塞一批，几次之后就无限堆积了。
                # 用一个哨兵值表示「这张图拿不到」，_poll_covers 见到就丢弃回调。
                with self._img_lock:
                    self._img_failed.add(key)

        threading.Thread(target=worker, name="cover", daemon=True).start()

    def _poll_covers(self) -> None:
        """主线程轮询：把下载好的图变成 PhotoImage 并回调。由 _poll 调用。

        这里只做三件事，保持简单：
          1. 把后台刚解码好的图（_img_decoded）转成 PhotoImage 存进缓存
          2. 对缓存里已经有的，取出对应的等待回调并执行
          3. 还没好的留在队列里等下一轮
        """
        if not getattr(self, "_img_wait", None):
            return
        with self._img_lock:
            decoded = dict(self._img_decoded)
            self._img_decoded.clear()
            waits = list(self._img_wait)
        # 1. 先在主线程创建 PhotoImage（这一步只能在主线程做）
        for key, im in decoded.items():
            try:
                with self._img_lock:
                    self._img_cache[key] = ImageTk.PhotoImage(im)
            except Exception:
                pass
        # 2/3. 能兑现的回调就执行，失败的丢弃，其余留到下一轮
        still, fire = [], []
        with self._img_lock:
            for key, cb in waits:
                if key in self._img_failed:
                    continue            # 拿不到，直接丢弃，别无限堆积
                ph = self._img_cache.get(key)
                if ph is not None:
                    fire.append((cb, ph))
                else:
                    still.append((key, cb))
            self._img_wait = still
            # 失败记录只在还有等待者时需要保留
            if not self._img_wait:
                self._img_failed.clear()
        for cb, ph in fire:
            try:
                cb(ph)
            except Exception:
                pass

    # ------------------------------------------------------------ 收藏操作

    def _on_star(self) -> None:
        """点黄星：收藏/取消收藏当前正在听的直播间。"""
        rid = self._room_now or self.control.room
        try:
            rid = int(rid) if rid else None
        except Exception:
            rid = None
        if not rid:
            # 没有正在听的房间，星标没有意义 —— 给个明确反馈而不是静默
            self.toast("先开始收听一个直播间，才能收藏它", kind="offline")
            return
        if self.store is None:
            self.toast("收藏功能不可用（配置目录不可写）", kind="error")
            return
        saved = self.store.toggle(
            rid,
            uname=self.control.anchor or "",
            title=self.control.title or "",
            cover=self.control.cover or "",
            # 正在出声就是开播中。不能默认 -1，否则刚收藏的直播间
            # 会显示成「状态未知」直到下次刷新。
            live_status=1 if self.control.playing else None,
        )
        self._star_on = None            # 强制重画
        self._draw_star()
        self.rebuild_bookmarks()
        self.toast(f"已收藏 {self.control.anchor or rid}" if saved
                   else f"已取消收藏 {self.control.anchor or rid}",
                   kind="offline", ms=2600)

    def _draw_star(self, hover: bool = False) -> None:
        """画收藏星标。已收藏=实心金黄，未收藏=空心灰。"""
        rid = self._room_now or self.control.room
        on = bool(self.store and self.store.has(rid)) if self.store else False
        if self._star_on == on and not hover:
            return
        self._star_on = on
        c = self.btn_star
        c.delete("all")
        # 星形顶点：外半径/内半径交替 10 个点
        import math
        cx = cy = 13.0
        col = "#ffc53d" if on else (_FG if hover else _FG_FAINT)
        pts = []
        for i in range(10):
            r = 9.0 if i % 2 == 0 else 3.9
            a = -math.pi / 2 + i * math.pi / 5
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
        if on:
            c.create_polygon(pts, fill=col, outline="#c99a20", width=1)
        else:
            c.create_polygon(pts, fill=_CARD, outline=col, width=2)

    def play_bookmark(self, rid: int) -> None:
        """点收藏卡片 -> 直接开始听这个房间。"""
        self._room_var.set(str(rid))
        self._on_start()

    # ------------------------------------------------------------ 开播状态

    def refresh_bookmarks(self, auto: bool = False) -> None:
        """查一遍所有收藏的开播状态。auto=True 表示是启动时自动查的。

        *** 线程规则：后台线程只查网络 + 写 store，绝不碰 Tk ***
        第一版在 check_live 的 on_one 回调里调了 self.win.after(0, ...) 想逐条
        更新界面，结果**工作线程卡死在第一间房之后**（实测：check_live 打印
        「开始」就再没打印「结束」，_bm_refreshing 永远是 True，按钮一直
        「刷新中…」）。Tk 不是线程安全的，从后台线程调 after 会出这种事。
        现在改成：后台跑完再排一次界面重画，全程不在后台碰 Tk。
        """
        if self._bm_refreshing:
            return
        if self.store is None or len(self.store) == 0:
            if not auto:
                self.toast("还没有收藏，先收藏一个直播间吧", kind="offline",
                           ms=2600)
            return
        self._bm_refreshing = True
        self.btn_refresh.config(text="刷新中…", fg=_FG_FAINT)
        ids = [int(it["room_id"]) for it in self.store.all()]

        # 整体超时基准。定义在 worker 之前，避免读起来像「先用后定义」。
        t_start = time.time()

        def _deadline_passed() -> bool:
            return time.time() - t_start > 30.0

        def worker():
            """后台：只做网络请求和写盘。

            *** 绝不调用任何 Tk 方法，连 win.after() 也不调 ***
            第一版在这里调 self.win.after(0, ...) 把结果送回主线程，结果
            工作线程卡死在第一间房之后（实测：check_live 打了「开始」就再没
            打「结束」，按钮永远停在「刷新中…」）。
            Tk 不是线程安全的，从后台线程调 after 会出这种事 ——
            我在测试脚本里犯同一个错时，脚本直接把整个进程挂死了。
            现在的做法是「后台只写共享变量，主线程轮询取结果」，
            也就是本项目 control 对象一直在用的模式。
            """
            result = {}
            try:
                from .bookmarks import check_live
                # 单次请求 6 秒、整体 30 秒封顶。收藏多或网络差时宁可少查几个，
                # 也不能让「刷新中」无限转下去。
                result = check_live(ids, timeout=6.0,
                                    should_stop=_deadline_passed)
            except Exception:
                result = {}
            for rid, info in result.items():
                try:
                    self.store.update_meta(
                        rid, uname=info.get("uname", ""),
                        title=info.get("title", ""),
                        cover=info.get("cover", ""),
                        live_status=info.get("live_status"))
                except Exception:
                    pass
            # 只写变量。主线程在 _poll_bookmark_done() 里取。
            self._bm_result = result
            self._bm_auto = auto
            self._bm_done = True

        threading.Thread(target=worker, name="bm-live", daemon=True).start()

    def _poll_bookmark_done(self) -> None:
        """主线程轮询：后台刷新完了就收尾。由 _poll 每 200ms 调一次。"""
        if not getattr(self, "_bm_done", False):
            return
        self._bm_done = False
        result = self._bm_result or {}
        self._bm_result = {}
        auto = self._bm_auto
        self._bm_auto = False
        self._refreshing_done(result, auto)

    def _refreshing_done(self, result: dict, auto: bool) -> None:
        """刷新收尾。只在主线程执行。"""
        self._bm_refreshing = False
        try:
            self.btn_refresh.config(text="刷新状态", fg=_FG)
        except Exception:
            pass
        self._rebuild_bookmarks_ui()
        if auto and result:
            live_n = sum(1 for it in self.store.all()
                         if self._live_of(it) == 1)
            if live_n:
                self.toast(f"收藏里有 {live_n} 个直播间正在开播",
                           kind="offline", ms=3200)

    def _center_full(self, reposition: bool = True) -> None:
        """把主窗口摆成**固定尺寸**并居中。

        *** 尺寸固定，不再按内容自适应 ***
        原来这里是按内容算的（`max(winfo_reqwidth(), 440)` /
        `max(winfo_reqheight(), 330)`），结果是窗口会随着内容变来变去：
        主播信息出现时长高、消失时缩回、提示弹出时又长高。
        用户明确要求固定大小、以左列为基准。

        现在宽度 = 左列固定宽 + 分隔线 + 收藏栏固定宽，
        高度也是固定的，内容变化一律靠**内部滚动/换行**消化，
        窗口本身不动。好处是界面不会"跳"，用户也不用每次都重新找位置。

        reposition=False 仍然保留（调用方语义上表示"别挪位置"），
        但因为尺寸已经固定，实际上不会再改变几何 —— 只有 reposition=True
        时才重新居中。
        """
        if not reposition:
            return
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        w, h = self._full_size()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - 40)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

        # *** 必须再设一次，而且中间要 update() ***
        # 这不是保险，是必需的。实测（逐行打印出来的）：
        #     设完 geometry       geom='260x130+633+256'  位置已写入
        #     update() 之后        pos=(1435,12) 440x367   位置被系统改回右上角
        #     再设一次 + update    pos=(633,256) 440x367   这才生效
        # 原因：切换 overrideredirect 时 Tk 会**销毁并重建包装窗口**，
        # Windows 在重建时用默认位置覆盖刚设的坐标（尺寸保住了、位置被冲掉）。
        # 所以要在窗口真正映射之后重新设一遍。
        # 只调一次的话，从悬浮窗展开回主界面会停在右上角 —— 用户报的就是这个。
        # update() 在窗口正在销毁时会抛 TclError，所以包一层。
        try:
            self.win.update()
            self.win.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass
        self._reset_drag()

    def _full_size(self) -> tuple:
        """主窗口的固定尺寸 (宽, 高)。以左列为基准算出来。

        高度按「左列按固定宽度换行后需要多高」定，这样最长的那条状态文字
        也能完整显示；主播信息块和提示卡片出现时不会再撑高窗口。
        """
        # 先按左列固定宽度设好换行宽度，再量需要多高 —— 顺序反了会算矮
        self._apply_wraplengths(self.LEFT_W + self._SIDE_W + 7)
        try:
            self.win.update_idletasks()
        except Exception:
            pass
        try:
            need = self.win.winfo_reqheight()
        except Exception:
            need = 0
        h = max(self.MIN_H, int(need) if need else self.MIN_H)
        return (self.LEFT_W + 7 + self._SIDE_W, h)

    def _relayout(self) -> None:
        """内容变了之后重新安排内部布局（**不改变窗口尺寸**）。

        原来这些地方是调 _center_full(reposition=False) 让窗口长高/缩回，
        现在窗口固定，只要让内部重新计算即可。

        *** main 列用 grid 而不是 pack ***
        窗口高度固定之后，pack 从顶部堆叠会在内容变多时把底部按钮**挤出
        可视区**（实测：主播信息出现后「重新连接」那一行看不见了）。
        grid 里把 status/按钮行放在会伸缩的那一行下面，内容变多时按钮
        始终贴底，不会被挤出去。
        """
        try:
            self.win.update_idletasks()
        except Exception:
            pass

    # ------------------------------------------------------------ 迷你悬浮窗

    def _build_compact(self) -> None:
        self.compact = tk.Frame(self.win, bg=_BG)

        bar = tk.Frame(self.compact, bg=_CARD, height=26)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self.lbl_mini = tk.Label(bar, text="bililive", bg=_CARD, fg=_FG,
                                 font=(_UI_FONT, 9))
        self.lbl_mini.pack(side="left", padx=8)
        btn_x = tk.Label(bar, text="✕", bg=_CARD, fg=_FG_FAINT, width=3,
                         font=(_UI_FONT, 10), cursor="hand2")
        btn_x.pack(side="right")
        btn_x.bind("<Button-1>", lambda e: self._on_close())
        btn_x.bind("<Enter>", lambda e: btn_x.config(fg=_DANGER))
        btn_x.bind("<Leave>", lambda e: btn_x.config(fg=_FG_FAINT))
        for w in (bar, self.lbl_mini):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        self.lbl_mini_status = tk.Label(self.compact, text="播放中", bg=_BG,
                                        fg=_FG_DIM, font=(_UI_FONT, 8),
                                        anchor="w")
        self.lbl_mini_status.pack(fill="x", padx=10, pady=(6, 2))

        row = tk.Frame(self.compact, bg=_BG)
        row.pack(fill="x", padx=10, pady=(0, 6))
        # 这一行用 grid 而不是 pack。
        # pack 的 expand 控件会吃掉「当前窗口」的剩余宽度 —— 迷你窗刚从
        # 430 宽缩到 250 时，pack 仍按旧宽度把滑块拉到 325，于是百分比标签
        # 被推到 x=369，超出 250 的窗口直接看不见。
        # grid 的列权重按窗口实时宽度分配，不会残留旧宽度。
        row.columnconfigure(1, weight=1)
        self.btn_play = tk.Label(row, text="❚❚", bg=_ACCENT, fg="#ffffff",
                                 font=(_UI_FONT, 10), width=3, cursor="hand2",
                                 padx=2, pady=2)
        self.btn_play.grid(row=0, column=0, sticky="w")
        self.btn_play.bind("<Button-1>", lambda e: self._on_toggle())
        self.scale_mini = _Slider(row, value=self.control.volume,
                                  command=self._on_vol_move, width=110)
        self.scale_mini.grid(row=0, column=1, sticky="ew", padx=(8, 6))
        self.lbl_mini_vol = tk.Label(row, text="100%", bg=_BG, fg=_FG_DIM,
                                     font=(_UI_FONT, 8), width=5, anchor="e")
        self.lbl_mini_vol.grid(row=0, column=2, sticky="e")

        self.btn_expand = ttk.Button(self.compact, text="展开设置",
                                     style="ghost.TButton",
                                     command=self._to_full)
        self.btn_expand.pack(fill="x", padx=10, pady=(0, 10))

    def _to_compact(self) -> None:
        """缩成右上角的迷你悬浮窗。

        *** 必须去掉系统标题栏 ***
        悬浮窗自己画了 ❚❚ / ✕（迷你窗顶栏那几个），如果外面再套一层
        Windows 标题栏，就会出现两套按钮，看起来也不像个悬浮窗 ——
        用户截图反馈的就是这个。

        之前这里是漏实现的：注释写着「迷你悬浮窗才去边框」，但代码里
        从来没有调用过 overrideredirect，所以悬浮窗一直带着标题栏。

        overrideredirect(True) 的连带效果正好都是悬浮窗想要的：
          * 没有标题栏、没有系统按钮 —— 不再有两套按钮
          * 不出现在任务栏和 Alt+Tab 里 —— 悬浮窗不该占这两个位置
          * 系统不再给它画边框和阴影
        代价是失去系统的移动/关闭，但这两件事界面里都自己做了
        （顶栏可拖动、✕ 关程序）。
        """
        self.full.pack_forget()
        # 先去掉边框，再摆位置：反过来的话窗口会先画一次带标题栏的样子
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.compact.pack(fill="both", expand=True)
        self._set_alpha(0.94)          # 迷你窗保留半透明：小窗不挡视线是优点
        self._fit_compact()

    def _to_full(self) -> None:
        """从悬浮窗展开回完整窗口。"""
        # *** 展开时必须把标题栏装回去 ***
        # 主窗口没有系统标题栏时焦点行为很怪（实测：点击界面后按键会落进
        # 房间号输入框，把房间号改掉）。所以主窗口一定要有标题栏。
        self.win.overrideredirect(False)
        # 从无边框切回普通窗口后，Windows 可能把窗口留在「不显示」状态，
        # deiconify + lift 让它回来并拿到焦点
        try:
            self.win.deiconify()
            self.win.lift()
        except Exception:
            pass
        self.compact.pack_forget()
        self.win.attributes("-topmost", False)
        self._set_alpha(1.0)           # 主窗口完全不透明，用户明确要求
        self._center_full()

    def _set_alpha(self, value: float) -> None:
        """设置窗口不透明度。失败就忽略（不是所有平台都支持）。"""
        try:
            self.win.attributes("-alpha", value)
        except Exception:
            pass

    def _apply_wraplengths(self, width: int) -> None:
        """按窗口实际宽度设置换行宽度。

        *** 必须跟着窗口宽度走，不能写死 ***
        第一版把主窗口的 wraplength 写死成 372、迷你窗那行干脆没设，
        结果窗口窄的时候文字**溢出被裁**，而不是换行：
        迷你窗只有 250 宽，却用 372 的换行宽度，于是
        「播放中 0.1 分钟」尾巴被切掉，看起来像一直停在「正在连接」。

        *** 加了收藏栏之后又要改一次 ***
        原来传进来的是**整窗宽度**，但左侧那一列只占
        「整窗 - 收藏栏宽度 - 分隔线 - 左右内边距」。继续按整窗算的话，
        状态文字和标题的换行点会跑到收藏栏底下，文字被裁。
        所以这里先扣掉右侧那部分。min() 兜住窗口被压得极窄的情况。
        """
        # 扣掉：收藏栏 + 分隔线(padx 6) + 1 + 左列左右内边距(24+16)
        left = max(180, width - self._SIDE_W - 7 - 40)
        wrap = max(120, left - 20)
        for lbl in (getattr(self, "lbl_status", None),
                    getattr(self, "lbl_title", None)):
            if lbl is not None:
                try:
                    lbl.config(wraplength=wrap)
                except Exception:
                    pass
        mini = getattr(self, "lbl_mini_status", None)
        if mini is not None:
            try:
                mini.config(wraplength=max(80, width - 40))
            except Exception:
                pass

    def _fit_compact(self) -> None:
        """摆好迷你悬浮窗：右上角、按内容定尺寸。

        *** 先定宽度，再问高度 ***
        顺序错了会得到内容被裁的窗口，这是实测踩出来的：
        pack() 之后如果直接用 compact.winfo_reqheight()，此时布局还按
        **旧窗口宽度**（主窗口 430）算过一遍，行内控件被拉到 410 宽，
        百分比标签就被推到窗口外面看不见了。
        所以先 geometry 把宽度定下来、让 Tk 重新传播一次，再取高度。
        """
        w = 260
        sw = self.win.winfo_screenwidth()
        self.win.geometry(f"{w}x{self.compact.winfo_reqheight()}"
                          f"+{sw - w - 12}+12")
        self.win.update_idletasks()
        self._apply_wraplengths(w)
        self.win.update_idletasks()
        h = max(120, self.compact.winfo_reqheight())
        self.win.geometry(f"{w}x{h}+{sw - w - 12}+12")
        # 窗口被自动挪到右上角了，拖动基准点作废
        self._reset_drag()

    # ------------------------------------------------------------ 拖动

    def _drag_start(self, event) -> None:
        self._drag = (event.x_root - self.win.winfo_x(),
                      event.y_root - self.win.winfo_y())

    def _drag_move(self, event) -> None:
        if self._drag:
            dx, dy = self._drag
            self.win.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

    def _reset_drag(self) -> None:
        """清掉拖动基准点。

        *** 自动挪过窗口之后必须调用 ***
        拖动时记的是「按下那一刻：鼠标 - 窗口位置」的差值。如果窗口在拖动
        过程中被程序自己挪走了（换台、连上后长高、进出悬浮窗都会），这个
        差值就过期了，下一次 <Motion> 会把窗口**猛地弹到鼠标位置**。
        """
        self._drag = None

    # ------------------------------------------------------------ 交互

    def _on_main(self) -> None:
        """主按钮。它显示什么就做什么：

            [开始收听]     -> 第一次开始
            [继续收听]     -> 从暂停恢复（暂停中）
            [换到该房间]   -> 输入的是新房间号，切过去
            [重新连接]     -> 输入的还是当前房间，重连一次（用户主动要求时才有意义）
        """
        if self.control.paused:
            self.control.resume()
            self._started = True
            self._last_status = None
            self._refresh(force=True)
            return
        # 其余情况统一走 _on_start：它内部会判断「同房间」还是「换房间」。
        self._on_start()

    def _on_start(self) -> None:
        """开始收听 / 换房间。

        两种情况：
            第一次点或输入了新房间 -> 启动一轮播放（换房间时先停掉旧的）
            输入的还是当前房间     -> 重新连接（换台失败或想重来时的兜底）

        换房间为什么不复用同一个播放线程：
            那个线程的 room 是启动参数，中途换不了；而且它内部有一堆
            跟这个房间绑定的状态（room_id、已取到的标题、候选 CDN 列表）。
            让它干净退出、重新起一个线程，比在线程里塞一个「换台」分支
            简单得多，也不会漏掉某个没重置的变量。
        """
        entered = self._entered_room()
        if entered is None:
            self._set_status("请先填房间号（地址栏 live.bilibili.com/ 后面那串数字）",
                             error=True)
            return
        room = entered

        if not self._started:
            self._start(room)
            return

        # 已经在播：要么换房间，要么重连当前房间
        if room != self._room_now:
            self._set_status(f"正在切到房间 {room} ...")
        else:
            self._set_status(f"正在重新连接房间 {room} ...")
        # 标记「这是主动换台」：旧循环退出后会 mark_finished()，
        # 界面要能分辨「换台导致的退出」和「真的结束了」，
        # 否则换台过程中会闪一下「已停止」（实测过的观感问题）。
        self._switch_in_progress = True
        try:
            self.control.request_stop()
            # 等旧线程真的结束，避免新旧两个循环同时操作同一个 ffplay
            self._join_play_threads(timeout=8.0)
            self.control.reset_stop()
            self.control.reset_timer()
        finally:
            self._switch_in_progress = False
        self._start(room)

    def _join_play_threads(self, timeout: float = 8.0) -> None:
        """等播放线程退出。超时就继续（不能为了等它把界面卡死）。"""
        deadline = time.time() + timeout
        for t in threading.enumerate():
            if t.name != "play-loop" or not t.is_alive():
                continue
            left = max(0.1, deadline - time.time())
            t.join(timeout=left)
            if t.is_alive():
                # 不阻塞界面：它迟早会看到 stop_requested 而退出
                break

    def _start(self, room: int) -> None:
        """真正启动一轮播放。"""
        self.room = room
        self._room_now = room
        self.control.room = room
        self.control.set_volume(int(round(self.scale.get())))
        # 清掉上一个房间的痕迹，否则界面会短暂显示旧的主播名/标题
        self.control.anchor = ""
        self.control.title = ""
        # 封面也要清：不清的话新房间会先显示上一个房间的封面，
        # 直到元数据取回来才换掉（实测能看出明显的错帧）。
        self.control.cover = ""
        self.control.playing = False
        if self.control.paused:
            self.control.resume()
        # *** 用户已经开始新一轮了，立刻收掉上一轮的提示 ***
        # 否则「房间号不存在 / 没有开播」会挂到自己的计时器到点为止，
        # 而那时新房间可能都已经出声了（用户报的正是这个）。
        self.hide_toast()
        self._started = True
        # 清掉上一轮的「已结束」标记，新的一轮从头开始。
        # 不清的话，_refresh 会在下一轮立刻把它当成「循环已退出」而复位按钮。
        self.control.finished = False
        # 换房间后重新允许弹「未开播」提示（见 play.py 里 offline_room 的用途）
        self.control.offline_room = None
        self._last_anchor = None
        self._last_title = None
        # 按钮文字/可用性统一交给 _refresh 里的状态机决定，
        # 这里不再自己设一遍（两处设就会打架，之前正是这么出错的）。
        self.lbl_mini.config(text=f"bililive  房间 {room}")
        self._set_status("正在连接，首次可能要等几秒 ...")
        self._last_status = None
        self._refresh(force=True)

        from . import play
        threading.Thread(
            target=play.run,
            args=(room, self.control.volume, False, self.control),
            name="play-loop", daemon=True).start()

    def _on_toggle(self) -> None:
        self.control.toggle()
        self._refresh(force=True)

    def _on_vol_move(self, value: float, released: bool = False) -> None:
        """滑块动了的回调。

        released=False 表示还在拖/刚按下:只更新数字,不重启播放。
        released=True  表示松手了:**这才是真正应用音量的时刻**。

        为什么不用定时器防抖(第一版是那么做的):
            自绘滑块能直接知道「松手」这个事件,比猜「用户停手 350ms」
            准确得多,也不用担心定时器在窗口关闭时还挂着。
        """
        if not getattr(self, "_ready", False):
            return
        val = int(round(value))
        self.control.set_volume(val)
        self.lbl_vol.config(text=f"{val}%")
        self.lbl_mini_vol.config(text=f"{val}%")
        # 同步「另一个」滑块。set() 不会回调,所以不会来回递归。
        active = _ACTIVE.get("slider")
        for s in (self.scale, self.scale_mini):
            if s is not active:
                try:
                    s.set(val)
                except Exception:
                    pass
        if released:
            self.control.request_volume_apply()

    def _set_status(self, text: str, error: bool = False) -> None:
        self.lbl_status.config(text=text, fg=_DANGER if error else _FG_DIM)

    def _on_close(self) -> None:
        try:
            self.control.request_stop()
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------ 刷新

    def _refresh(self, force: bool = False) -> None:
        """按 control 的状态刷新界面。每 200ms 调一次。

        *** 按钮状态只有一个来源，就是这里的输入 ***
        之前踩过两次坑，都源于「按钮更新搭在别的状态变化上」：
          1. 把更新按钮的代码嵌在「暂停状态变了」的 if 里，于是 `_started`
             从 False 变 True 时按钮永远不更新，一直停在「正在连接 ...」。
          2. 状态文字变化时又去调一次 _update_buttons，两处各自为政，
             出现过「状态写正在继续、按钮却是可点的开始收听」这种自相矛盾。
        现在改成：**每次刷新都无条件按当前状态重算一遍按钮**。
        """
        # ---- 播放循环退出了：必须把界面复位 ----
        # 这是「输入不存在的房间号会卡死」那个 bug 的根因：
        # 循环早就干净退出了，却没人通知界面，于是按钮永远停在
        # 「正在连接 ...」且是禁用的，用户只能关掉程序。
        if self.control.finished:
            self.control.finished = False
            if self._started:
                self._started = False
                self.control.playing = False
                # 按钮文字由下面的状态机统一重算，这里不自己设
                if not self._switch_in_progress:
                    # 不是「换房间」主动停的，说明这轮真的结束了
                    self._set_status("已停止。可以输入房间号重新开始。")
                # 把「已处理到哪个状态序号」记下来。
                #
                # *** 这里绝不能写 _last_status = None ***
                # 一开始我写的就是 None，结果下面那个刷新条件
                # （status != self._last_status）**必然为真**，于是又把
                # control 里残留的旧文本同步回标签，把刚设的「已停止」冲掉 ——
                # 实测日志里就是连续两次 _set_status。
                # 正确做法是承认「这个序号已经处理过了」，而不是把记录清空。
                self._last_status = self.control.status_text
                self._last_status_seq = self.control.status_seq

        paused = self.control.paused
        playing = self.control.playing
        self._last_playing = playing
        self._last_paused = paused

        # 暂停按钮（迷你悬浮窗上那个）
        self.btn_play.config(text="▶" if paused else "❚❚",
                             bg="#463a41" if paused else _ACCENT)

        # 主按钮：无条件重算
        self._update_buttons(playing, paused)

        status = self.control.status_text or ""
        # *** 只在循环真的写过新状态时才据此刷新 ***
        # control.status_text 在循环退出后仍然是上一轮留下的文本。若不加这个
        # 判断，上面刚设的「已停止」会立刻被那条旧值冲掉：
        # 实测日志里出现连续两次 _set_status，第二次把第一次覆盖了。
        # status_seq 由 play.py 的 _status() 和 control.mark_finished() 递增。
        if status != self._last_status and (
                self.control.status_seq != self._last_status_seq
                or not self._started):
            self._last_status = status
            self._last_status_seq = self.control.status_seq
            self._set_status(status)
            self.lbl_mini_status.config(text=status)

        self._refresh_meta()
        self._poll_error()

    def _update_buttons(self, playing: bool, paused: bool) -> None:
        """按当前状态更新按钮。

        *** 主按钮永远是「点下去会发生什么」，不是状态显示 ***
        早先播放中把主按钮做成禁用的「正在播放」——那等于把状态显示占了
        按钮的位置，用户改了房间号想点它换台却发现点不动（实际反馈：
        「应该搞成按按钮也可以」）。现在改成按**输入框是否变化**决定：

            没在播                      -> [开始收听]      可点
            在播且输入框==当前房间      -> [重新连接]      可点（想重连可以点）
            在播且输入框是新房间        -> [换到该房间]    可点
            暂停中                      -> [继续收听]      可点
            正在连接/缓冲中             -> [正在连接 ...]  禁用

        也就是「播放中」这个状态不再占用主按钮 —— 它由下面的状态文字表达。
        """
        entered = self._entered_room()
        # 这四个标签都要实时看用户输入，所以顺带把输入框变化绑上 _refresh
        # （见 _mk_entry 的 trace 绑定），否则用户改数字后按钮文字不会跟着变。
        if paused:
            self.btn_main.config(state="normal", text="继续收听")
            self.btn_pause.config(state="disabled")
            return

        if playing:
            if entered is not None and entered != self._room_now:
                self.btn_main.config(state="normal", text="换到该房间")
            else:
                self.btn_main.config(state="normal", text="重新连接")
            self.btn_pause.config(state="normal")
            return

        if self._started:
            resuming = "继续" in (self.control.status_text or "")
            if resuming:
                self.btn_main.config(state="disabled", text="正在继续 ...")
            else:
                # 「没开播」时循环还在跑（每 30 秒重查一次），但用户最想做的
                # 往往是「换一个正在播的房间」。所以按钮保持**可点**：
                # 点了就换到输入框里那个号。文字也据实说明。
                offline = "未开播" in (self.control.status_text or "")
                if entered is not None and entered != self._room_now:
                    self.btn_main.config(state="normal", text="换到该房间")
                elif offline:
                    self.btn_main.config(state="normal", text="重新连接")
                else:
                    self.btn_main.config(state="disabled", text="正在连接 ...")
            self.btn_pause.config(state="disabled")
            return

        self.btn_main.config(state="normal", text="开始收听")
        self.btn_pause.config(state="disabled")

    def _entered_room(self):
        """把输入框里的内容解析成房间号，解析不出来返回 None。

        容忍直接粘整条直播间网址（_parse_room 会抠出数字）。
        """
        from .play import _parse_room
        try:
            return _parse_room(self._room_var.get())
        except Exception:
            return None

    # ------------------------------------------------------------ 悬浮提示

    def _poll_error(self) -> None:
        """检查播放循环上报的状态，该提示就提示。

        两种**必须区分**的情况：
            error_seq   房间不存在  -> 房间号打错了，换一个（红色，错误调性）
            offline_seq 没开播      -> 房间号是对的，等着或换一个正在播的
                                        （琥珀色，提示调性，不是"出错"）
        合成一种提示会让用户以为房间号打错了，然后去改一个本来正确的号。
        """
        if self.control.error_seq != self._last_error_seq:
            self._last_error_seq = self.control.error_seq
            self.toast(self.control.error_text or "出错了", kind="error")
        elif self.control.offline_seq != self._last_offline_seq:
            self._last_offline_seq = self.control.offline_seq
            # 未开播是周期性重复的，给更长的展示时间（8 秒），
            # 免得用户一转头就错过了
            self.toast(self.control.offline_text or "未开播",
                       kind="offline", ms=8000)

    def toast(self, text: str, ms: int = 4200, kind: str = "error") -> None:
        """在窗口上方浮出一张提示卡片，几秒后自动消失。

        为什么不用 messagebox：模态对话框会阻塞，而且用户必须点一下才能
        继续 —— 对一个「输入错了重输就行」的场景太重了。
        为什么不用 lbl_status 显示：那行字很小、位置在底部，容易被忽略；
        而这两种情况都是必须被看到的信息。

        kind 决定配色：
            error   -> 红色竖条（"出问题了，你得做点什么"）
            offline -> 琥珀色竖条（"没问题，只是现在没有"）
        用颜色区分调性，比在文案里解释更直接。
        """
        try:
            self._toast_card.pack_forget()
        except Exception:
            pass
        color = _WARN if kind == "offline" else _DANGER
        try:
            self._toast_bar.config(bg=color)
        except Exception:
            pass
        self.lbl_toast.config(text=text)
        # 贴到 full 帧的**最上方**，这样不会挤进原有布局的中间
        try:
            first = self.full.winfo_children()[0]
        except Exception:
            first = None
        if first is not None:
            self._toast_card.pack(fill="x", padx=18, pady=(10, 0),
                                  side="top", before=first)
        else:
            self._toast_card.pack(fill="x", padx=18, pady=(10, 0), side="top")
        self._toast_card.lift()
        # 卡片占了空间，窗口要长高一点，否则会挤掉底部按钮
        self._relayout()
        if self._toast_job is not None:
            try:
                self.win.after_cancel(self._toast_job)
            except Exception:
                pass
        self._toast_job = self.win.after(ms, self._toast_hide)

    def _toast_hide(self) -> None:
        self._toast_job = None
        try:
            self._toast_card.pack_forget()
        except Exception:
            pass
        # 收掉卡片后把窗口高度收回来
        self._relayout()

    def hide_toast(self) -> None:
        """立刻收掉提示卡片，并取消它自己的计时器。

        *** 开始新一轮播放时必须调用 ***
        提示卡片有独立的寿命计时（错误 4.2 秒、未开播 8 秒），不会因为
        用户换了房间而消失。实测的现象就是：
            输入新房间号 -> 点击开始 -> 已经出声了，
            上一次的「房间号不存在 / 没有开播」还挂在窗口上，
            要等原来的 4.2/8 秒计时到点才没 —— 用户报的「两三秒之后才会没」。
        根因是计时器没人取消。所以一旦用户做出新动作，就立刻收掉旧提示。
        """
        if self._toast_job is not None:
            try:
                self.win.after_cancel(self._toast_job)
            except Exception:
                pass
            self._toast_job = None
        try:
            if self._toast_card.winfo_ismapped():
                self._toast_card.pack_forget()
                self._relayout()
        except Exception:
            pass

    def _build_toast(self) -> None:
        """搭好提示卡片（先不显示，等 toast() 调用才 pack）。"""
        card = tk.Frame(self.win, bg=_CARD)
        # 左边一条强调色竖条。颜色由 kind 决定（错误红 / 未开播琥珀），
        # 所以要把这条竖条存下来，toast() 里改它的底色。
        self._toast_bar = tk.Frame(card, bg=_DANGER, width=4)
        self._toast_bar.pack(side="left", fill="y")
        self.lbl_toast = tk.Label(card, text="", bg=_CARD, fg=_FG,
                                  font=(_UI_FONT, 9), justify="left",
                                  anchor="w", wraplength=380)
        self.lbl_toast.pack(side="left", fill="x", expand=True,
                            padx=10, pady=9)
        self._toast_card = card

    def _refresh_meta(self) -> None:
        anchor = (self.control.anchor or "").strip()
        title = (self.control.title or "").strip()
        cover = (self.control.cover or "").strip()
        # *** 显示条件：只要「有一个在听/在等的房间」就显示 ***
        # 不再要求元数据非空。
        #
        # 原来是在 anchor 和 title 都为空时整块隐藏，后果就是用户反馈的：
        # 没开播的直播间取不到元数据（旧代码只在开播成功后才去取），
        # 于是封面和收藏黄星都不出现 —— 用户「根本没有收藏的地方」。
        # 而没开播恰恰是最想先收藏的时候（先把房间收起来等主播开播）。
        #
        # 现在：有房间号就显示这块，主播名/封面取不到就退回占位，黄星始终可用。
        has_room = bool(self._room_now or self.control.room)
        if (anchor == self._last_anchor and title == self._last_title
                and cover == self._last_cover
                and has_room == self._last_has_room):
            return
        self._last_anchor, self._last_title = anchor, title
        self._last_cover = cover
        self._last_has_room = has_room
        # 星标状态可能因为「换房间」而变化（新房间可能不在收藏里），
        # 所以跟着主播信息一起重画
        self._star_on = None
        self._draw_star()

        if not has_room:
            # 一个房间都没有：清掉并收起这块
            self.lbl_anchor.config(text="")
            self.lbl_title.config(text="")
            self._cover_shown = None
            self._paint_cover(None)         # 封面也要清掉，别留着上一个房间的
            if self.meta.winfo_ismapped():
                self.meta.pack_forget()
                # 撤掉那一块后窗口要重新收一下高度
                self._relayout()
            return

        # 有房间就一定有内容可显示。取不到主播名就用房间号兜底 ——
        # 让用户看到「这个房间」，而不是一片空白。
        rid = self._room_now or self.control.room
        self.lbl_anchor.config(text=anchor or f"房间 {rid}")
        self.lbl_title.config(text=title or "（还没取到直播间标题）")
        # 封面：只有 URL 变了才重新下载，否则每 200ms 轮询都会重下
        if cover and cover != self._cover_shown:
            self._cover_shown = cover
            self.load_cover(cover, self.COVER_MAIN, self._paint_cover)
        elif not cover:
            self._cover_shown = None
            self._paint_cover(None)
        if not self.meta.winfo_ismapped():
            # 插到「地址栏提示」之下、「音量」之上：
            # 顺序上是「你输入的房间 -> 这个房间是谁 -> 音量 -> 状态」。
            # before= 需要一个已存在的兄弟控件，这里用 vrow（音量那行的容器），
            # 它是 _build_full 里显式保存下来的，比靠遍历 winfo_children 稳。
            self.meta.pack(fill="x", padx=(24, 16), pady=(12, 0),
                           before=self._vrow)
            # *** 出现新内容后必须重新调一次尺寸 ***
            # 否则窗口高度还是旧的，多出来的文字会把底部按钮挤出去（实测踩过：
            # 状态文字叠在「开始收听」上）。reposition=False 保持窗口不跳。
            self._relayout()
        if anchor:
            # 迷你窗标题也带上主播名，同时开多个房间时好区分
            self.lbl_mini.config(text=f"bililive  {anchor}")

    def _paint_cover(self, img) -> None:
        """把封面画到主界面的 Canvas 上。img 为 None 时画占位。"""
        cv = self.COVER_MAIN
        try:
            self.cv_cover.delete("all")
            if img is None:
                # 占位和收藏卡片用同一套（主播名首字的圆形记号），
                # 而不是一个音符 —— 没见过这个房间的人也能认出「这是谁」。
                self._cover_placeholder(self.cv_cover, cv,
                                        self.control.anchor or "")
            else:
                self.cv_cover.create_image(0, 0, anchor="nw", image=img)
                # *** 必须留引用 ***
                # PhotoImage 被回收后 Canvas 上会变空白，这是 Tkinter 的经典坑。
                self._cover_main_img = img
        except Exception:
            pass

    def _poll(self) -> None:
        """主线程的 200ms 心跳。所有跨线程而来的结果都在这里取。"""
        try:
            self._refresh()
        except Exception:
            pass
        # 后台下载好的封面（只在这里创建 PhotoImage，Tk 才安全）
        try:
            self._poll_covers()
        except Exception:
            pass
        # 后台刷新好的开播状态
        try:
            self._poll_bookmark_done()
        except Exception:
            pass
        try:
            if self.win.winfo_exists():
                self.win.after(_POLL_MS, self._poll)
        except Exception:
            pass

    # ------------------------------------------------------------ 运行

    def run(self) -> None:
        # 没有房间号时把焦点放在输入框,用户直接就能打字/回车
        if self.room is None:
            self.ent_room.focus_set()
        self._bind_hotkeys()
        self._poll()
        self.win.mainloop()

    def _bind_hotkeys(self) -> None:
        """窗口级快捷键。

        *** 用 bind_all 而不是 bind ***
        bind 只在某个控件拿到键盘焦点时才触发；主窗口里控件很多
        （输入框、滑块、按钮），用户点哪儿都可能，靠单一控件的焦点
        来收快捷键很不可靠。bind_all 挂到 "all" 绑定标签上，
        无论焦点在哪都会先经过它。
        """
        w = self.win
        # 空格：暂停/继续
        w.bind_all("<space>", self._hk_toggle)
        # 回车：开始收听（输入框自己已经绑了一次，这里兜住「焦点不在输入框」的情况）
        w.bind_all("<Return>", self._hk_start)
        w.bind_all("<KP_Enter>", self._hk_start)
        # Esc：从迷你窗回到完整窗口，或直接退出
        w.bind_all("<Escape>", self._hk_escape)
        # 上下键：调音量（每次 5）
        w.bind_all("<Up>", lambda e: self._hk_volume(+5))
        w.bind_all("<Down>", lambda e: self._hk_volume(-5))

    def _hk_toggle(self, event):
        # 焦点在输入框里时不抢空格（用户可能在编辑房间号）
        if event.widget is self.ent_room and self.ent_room.focus_get() == self.ent_room:
            return None
        self._on_toggle()
        return "break"

    def _hk_start(self, event):
        self._on_start()
        return "break"

    def _hk_escape(self, _event):
        if self.compact.winfo_ismapped():
            self._to_full()
        return "break"

    def _hk_volume(self, delta: int):
        val = max(0, min(100, int(round(self.scale.get())) + delta))
        self.scale.set(val)
        self._on_vol_move(val, released=True)
        return "break"


def run_with_overlay(control, room: int | None = None) -> None:
    """在主线程跑前端窗口,直到用户关掉它。

    调用前请确保(如果已经有房间号)播放循环已在后台线程跑起来。
    """
    Overlay(control, room=room).run()
