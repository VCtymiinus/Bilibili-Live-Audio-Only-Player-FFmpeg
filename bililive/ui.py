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

import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

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
_DANGER = "#ff6b6b"

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

    def __init__(self, control, room: int | None = None):
        self.control = control
        self.room = room
        self._drag = None
        self._last_status = None
        self._last_paused = None
        self._last_playing = None
        self._last_anchor = None
        self._last_title = None
        self._started = False
        # 当前正在听的房间号（用来判断用户是不是换了房间）
        self._room_now = None

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
        self._build_compact()
        self._build_full()
        self.scale.set(self.control.volume)
        self.scale_mini.set(self.control.volume)
        self.lbl_vol.config(text=f"{self.control.volume}%")
        self.lbl_mini_vol.config(text=f"{self.control.volume}%")
        self._ready = True
        # *** 构造完必须立刻刷一次界面 ***
        # 否则按钮停在 Tk 的默认状态上（实测：「暂停」一打开就是可点的，
        # 但那时根本没开始听，要等第一次 200ms 轮询才变灰）。
        # 界面第一次出现就该是正确状态，不能有一个错帧。
        self._refresh(force=True)
        self._center_full()
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

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
                                                     pady=(12, 16))

        # ---- 房间号 ----
        tk.Label(self.full, text="直播间房间号", bg=_BG, fg=_FG_DIM,
                 font=(_UI_FONT, 9)).pack(anchor="w", padx=24)
        self._room_var = tk.StringVar(value="")
        self.ent_room = self._mk_entry(self.full, self._room_var)
        self.ent_room.pack(padx=24, pady=(6, 0), fill="x")
        # 回车即开始 —— 用户明确要「点回车就能用」
        self.ent_room.bind("<Return>", lambda e: self._on_start())
        self.ent_room.bind("<KP_Enter>", lambda e: self._on_start())
        # *** 输入框内容变化要立刻重算按钮 ***
        # 主按钮的文字取决于「输入的房间号是不是当前这个」：
        # 播放中把它改成别的房间号，按钮要马上从「重新连接」变成「换到该房间」，
        # 用户才知道点了会换台。不绑这个的话要等 200ms 轮询才更新，
        # 而且旧逻辑根本不看输入框，按钮永远显示「正在播放」。
        self._room_var.trace_add("write", lambda *a: self._refresh())

        tk.Label(self.full, text="地址栏 live.bilibili.com/ 后面那串数字",
                 bg=_BG, fg=_FG_FAINT, font=(_UI_FONT, 8)).pack(anchor="w",
                                                                padx=24,
                                                                pady=(5, 0))

        # ---- 主播 / 直播间名（连上之前不占地方，连上后才显示）----
        # 用 pack/pack_forget 动态出入，而不是留一片空白占位：
        # 没连上时界面保持紧凑，连上后信息才出现。
        self.meta = tk.Frame(self.full, bg=_BG)
        self.lbl_anchor = tk.Label(self.meta, text="", bg=_BG, fg=_FG,
                                   font=(_UI_FONT, 10, "bold"), anchor="w")
        self.lbl_anchor.pack(anchor="w")
        self.lbl_title = tk.Label(self.meta, text="", bg=_BG, fg=_FG_DIM,
                                  font=(_UI_FONT, 9), anchor="w",
                                  justify="left")
        self.lbl_title.pack(anchor="w", pady=(2, 0))

        # ---- 音量 ----
        vrow = tk.Frame(self.full, bg=_BG)
        vrow.pack(fill="x", padx=24, pady=(16, 0))
        # 保存下来：主播信息那块连上后要插到它前面（见 _refresh_meta）。
        # pack 的 before= 需要一个具体控件，靠遍历 winfo_children 找太脆。
        self._vrow = vrow
        tk.Label(vrow, text="音量", bg=_BG, fg=_FG_DIM,
                 font=(_UI_FONT, 9)).pack(side="left")
        self.lbl_vol = tk.Label(vrow, text="100%", bg=_BG, fg=_FG,
                                font=(_UI_FONT, 9, "bold"))
        self.lbl_vol.pack(side="right")
        self.scale = _Slider(self.full, value=self.control.volume,
                             command=self._on_vol_move)
        self.scale.pack(fill="x", padx=24, pady=(4, 0))

        # ---- 状态 ----
        self.lbl_status = tk.Label(self.full, text="输入房间号后点「开始收听」，或直接按回车",
                                   bg=_BG, fg=_FG_FAINT, font=(_UI_FONT, 9),
                                   anchor="w", justify="left")
        # 换行宽度不写死，由 _apply_wraplengths() 按窗口实际宽度设置。
        # 写死 372 时窗口窄了文字会溢出被裁，而不是换行。
        self.lbl_status.pack(fill="x", padx=24, pady=(16, 0))

        # ---- 底部按钮 ----
        # 只保留两个：主按钮（开始/继续，同一个）+ 暂停。
        # 之前有四个（开始收听、暂停、缩成悬浮窗、停止），用户明确反馈
        # 「逻辑多余」「停止是什么？不需要」—— 一个按钮表达一件事就够了：
        #   开始 / 继续 -> 主按钮
        #   暂停        -> 暂停按钮
        #   停止        -> 关窗口就行（Job Object 会连带杀掉 ffplay）
        #   缩成悬浮窗  -> 「缩成悬浮窗」也在这一行，它是形态切换不是播放控制
        brow = tk.Frame(self.full, bg=_BG)
        brow.pack(fill="x", padx=22, pady=(14, 18))
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

    def _center_full(self, reposition: bool = True) -> None:
        """按内容定尺寸并居中。

        reposition=False 时只调高度、不动位置 —— 用于「连上之后主播信息
        出现」这种运行中长高的场景，避免窗口自己跳一下。
        """
        self.full.pack(fill="both", expand=True)
        self.win.update_idletasks()
        w = max(self.win.winfo_reqwidth(), 440)
        # 先按宽度算换行，再问高度 —— 否则文字换行后的高度没算进去，
        # 窗口会矮一截把底部按钮裁掉。
        self._apply_wraplengths(w)
        self.win.update_idletasks()
        h = max(self.win.winfo_reqheight(), 330)
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        if reposition:
            x, y = (sw - w) // 2, max(0, (sh - h) // 2 - 40)
        else:
            x, y = self.win.winfo_x(), self.win.winfo_y()
        self.win.geometry(f"{w}x{h}+{x}+{y}")

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
        """缩成右上角的迷你悬浮窗。"""
        self.full.pack_forget()
        self.compact.pack(fill="both", expand=True)
        self.win.attributes("-topmost", True)
        self._set_alpha(0.94)          # 迷你窗保留半透明：小窗不挡视线是优点
        self._fit_compact()

    def _to_full(self) -> None:
        """从悬浮窗展开回完整窗口。"""
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
        留 60px 余量给左右内边距和滚动条余量。
        """
        wrap = max(120, width - 60)
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

    # ------------------------------------------------------------ 拖动

    def _drag_start(self, event) -> None:
        self._drag = (event.x_root - self.win.winfo_x(),
                      event.y_root - self.win.winfo_y())

    def _drag_move(self, event) -> None:
        if self._drag:
            dx, dy = self._drag
            self.win.geometry(f"+{event.x_root - dx}+{event.y_root - dy}")

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
        self.control.request_stop()
        # 等旧线程真的结束，避免新旧两个循环同时操作同一个 ffplay
        self._join_play_threads(timeout=8.0)
        self.control.reset_stop()
        self.control.reset_timer()
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
        self.control.playing = False
        if self.control.paused:
            self.control.resume()
        self._started = True
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
        反正只是给几个控件设文字/可用性，开销可以忽略，
        换来的是永远不可能出现自相矛盾的状态。
        """
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
        if status != self._last_status:
            self._last_status = status
            self._set_status(status)
            self.lbl_mini_status.config(text=status)

        self._refresh_meta()

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
            self.btn_main.config(state="disabled",
                                 text="正在继续 ..." if resuming
                                 else "正在连接 ...")
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

    def _refresh_meta(self) -> None:
        anchor = (self.control.anchor or "").strip()
        title = (self.control.title or "").strip()
        if anchor == self._last_anchor and title == self._last_title:
            return
        self._last_anchor, self._last_title = anchor, title

        if not anchor and not title:
            # *** 没有信息时必须把旧的清掉 ***
            # 换房间时 _start() 会把 anchor/title 清空，如果这里直接 return，
            # 界面上残留的就是**上一个房间**的主播名和标题 ——
            # 用户会以为没换成功。实测踩过：换台后仍显示旧主播名。
            self.lbl_anchor.config(text="")
            self.lbl_title.config(text="")
            if self.meta.winfo_ismapped():
                self.meta.pack_forget()
                # 撤掉那一块后窗口要重新收一下高度
                self._center_full(reposition=False)
            return

        self.lbl_anchor.config(text=anchor or "（未取名）")
        self.lbl_title.config(text=title or "")
        if not self.meta.winfo_ismapped():
            # 插到「地址栏提示」之下、「音量」之上：
            # 顺序上是「你输入的房间 -> 这个房间是谁 -> 音量 -> 状态」。
            # before= 需要一个已存在的兄弟控件，这里用 vrow（音量那行的容器），
            # 它是 _build_full 里显式保存下来的，比靠遍历 winfo_children 稳。
            self.meta.pack(fill="x", padx=24, pady=(12, 0), before=self._vrow)
            # *** 出现新内容后必须重新调一次尺寸 ***
            # 否则窗口高度还是旧的，多出来的文字会把底部按钮挤出去（实测踩过：
            # 状态文字叠在「开始收听」上）。reposition=False 保持窗口不跳。
            self._center_full(reposition=False)
        if anchor:
            # 迷你窗标题也带上主播名，同时开多个房间时好区分
            self.lbl_mini.config(text=f"bililive  {anchor}")

    def _poll(self) -> None:
        try:
            self._refresh()
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
