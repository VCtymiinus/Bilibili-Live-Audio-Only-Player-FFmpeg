"""入口脚本。

独立运行时行为和 `python -m bililive` 完全一致。

这里显式关闭字节码写入，原因有三：
  1. 发行包是个绿色文件夹，运行它不该在里面生成 __pycache__；
     否则「只含 6 个在用模块」的说明就变成假话。
  2. 开发时「构建完再跑一次自测」会把 .pyc 留进发行产物里 —— 实际发生过。
  3. 关了之后可以从只读介质（U 盘、只读目录）直接运行。
start.bat 里也设了 PYTHONDONTWRITEBYTECODE，这里是双保险。

打包成 exe 后的两件特别处理
----------------------------
1. **stdout / stderr 可能是 None。**
   用 PyInstaller 的 --windowed 打包后，程序没有控制台，Python 里
   `sys.stdout` 和 `sys.stderr` 就是 **None**。此时任何一句 `print()`
   都会抛 AttributeError: 'NoneType' object has no attribute 'write'，
   而且因为是 windowed（无控制台）**连报错都看不到**，程序就这么没了 ——
   这正是第一次打包时踩的坑：控制台版跑得好好的，windowed 版双击
   一瞬间就退出，没有任何提示。
   所以这里在最开头把 None 换成丢弃写入的对象，让所有 print 变成无操作。

2. **崩溃必须留下证据。**
   同理，windowed 版出错时用户什么也看不到，只会觉得「点了没反应」。
   所以整个 main() 包在 try/except 里，异常写到 exe 同目录的
   bililive-error.log。没有这个文件，这类问题只能靠猜。
"""

import multiprocessing
import os
import sys
import traceback

# 必须在 import 本项目的包之前设置，否则 import 时已经写出 .pyc 了
sys.dont_write_bytecode = True


class _NullStream:
    """吃掉所有读写的假 stdout/stdin/stderr。

    只在打包成 windowed exe（没有控制台）时用得上。故意同时提供
    write/flush/isatty/encoding，因为不同代码路径会问到不同的属性。
    """

    encoding = "utf-8"
    errors = "replace"

    def write(self, _s):
        return len(_s) if isinstance(_s, str) else 0

    def flush(self):
        pass

    def isatty(self):
        # 返回 False。注意这只说明「不是交互式终端」，**不等于**没有界面 ——
        # 判断该不该显示 GUI 请用 play._has_console()，别用这个值。
        return False

    def readline(self, *_a):
        # stdin 被读时立刻当成 EOF，而不是抛 AttributeError/崩溃。
        return ""

    def read(self, *_a):
        return ""

    def writelines(self, lines):
        for _ in lines:
            pass

    def close(self):
        pass


def _ensure_streams() -> None:
    """把 windowed 打包后为 None 的标准流换成安全替身。

    *** stdin 也必须补 ***
    实测踩过：--windowed 打包后 sys.stdin 是 None，而 play.py 在没有房间号
    又判定「不要界面」时会调用 input()，直接抛
        RuntimeError: input(): lost sys.stdin
    用户看到的是一个崩溃对话框。所以三个流一个都不能漏。
    """
    if sys.stdout is None:
        sys.stdout = _NullStream()
    if sys.stderr is None:
        sys.stderr = _NullStream()
    if sys.stdin is None:
        sys.stdin = _NullStream()


_ensure_streams()


def _error_log_path() -> str:
    """错误日志放在「程序所在目录」，方便用户找到并发给我们。"""
    try:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, "bililive-error.log")
    except Exception:
        return os.path.join(os.getcwd(), "bililive-error.log")


def _report_crash(exc: BaseException) -> None:
    """把崩溃信息写到文件，并尽量弹一个对话框告诉用户。"""
    detail = "".join(traceback.format_exception(
        type(exc), exc, exc.__traceback__))
    path = _error_log_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("bililive 启动失败\n")
            f.write("=" * 40 + "\n")
            f.write(f"python: {sys.version}\n")
            f.write(f"frozen: {getattr(sys, 'frozen', False)}\n")
            f.write(f"exe   : {getattr(sys, 'executable', '?')}\n")
            f.write(f"cwd   : {os.getcwd()}\n")
            f.write("-" * 40 + "\n")
            f.write(detail)
            f.write("\n")
    except Exception:
        pass

    # 无控制台时，用户唯一的线索就是这个对话框。能弹就弹。
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            f"bililive 启动失败，详细信息已写入：\n{path}\n\n"
            f"{type(exc).__name__}: {exc}",
            "bililive 出错了",
            0x10,  # MB_ICONERROR
        )
    except Exception:
        pass


def _main() -> int:
    # --warm-up：只把打包内容解压好就退出，不播放、不开窗口。
    #
    # 为什么需要它：onefile 打包的程序**每次启动**都要先把自己解压出来
    # （约 2~5 秒），这期间双击是**没有反应**的，用户会以为没启动。
    # 更糟的是杀毒软件常在这段时间扫描新解压出来的文件，窗口可能
    # 要等十几秒才出现。
    #
    # 说明清楚：onefile **没有跨次缓存** —— 每次运行都是全新的 _MEI 目录。
    # 所以 --warm-up 并不能让下一次启动变快，它只是把「解压」这一步
    # 摊到启动器里做掉，让用户点完之后看到的等待更短、更可预期。
    # 想真正快，应该用 --onedir 打包（但那就不再是「单文件」了）。
    if "--warm-up" in sys.argv:
        return 0

    from bililive.play import main
    return main()


if __name__ == "__main__":
    # 冻成 exe 后若没有这句，任何用到子进程的路径都可能反复重启自己
    # （PyInstaller 的经典坑）。当前不做 exe，但留着无害。
    multiprocessing.freeze_support()
    try:
        sys.exit(_main())
    except SystemExit:
        raise
    except BaseException as e:          # noqa: BLE001 - 兜底，必须留住证据
        _report_crash(e)
        sys.exit(70)
