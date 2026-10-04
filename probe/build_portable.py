"""组装绿色便携版目录。

产物: <项目根>/bililive-portable-v<VERSION>/

设计要点:
  * 只带 ffplay.exe。ffmpeg.exe / ffprobe.exe 对当前方案无用，
    带上白白多 200 MB。
  * 启动器纯 ASCII —— cmd.exe 按 OEM 代码页读 .bat，中文会撕碎脚本。
  * 不写死 Python 路径，按 py 启动器 -> PATH -> 常见位置逐级查找。
  * 所有路径都相对脚本自身，整个文件夹可以随便搬。
  * 不打包已弃用的管道方案模块（flvdemux/stream/player/waveout/cli），
    否则用户拿到的一半是废弃代码，还会误导人以为有两条可用路径。
"""

import os
import shutil
import sys

# *** 必须在任何 import 之前设置 ***
# 构建过程本身绝不能产生 .pyc：早期版本的校验步骤会读包内容，
# 触发 Python 生成 __pycache__，把「构建后才跑的自测」的产物留在发行包里，
# 与 README「只含 6 个在用模块」自相矛盾。
# 关掉字节码写入，从根上杜绝，比事后清理可靠。
sys.dont_write_bytecode = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 当前方案真正需要的模块
MODULES = [
    "__init__.py",
    "__main__.py",
    "biliapi.py",
    "ffplay.py",
    "play.py",
    "jobobject.py",
]

# 已弃用的管道方案，明确排除并记录原因
EXCLUDED = {
    "flvdemux.py": "管道方案的 FLV 解复用，现方案不需要",
    "stream.py": "管道方案的收流引擎，已被 ffplay 直连取代",
    "player.py": "管道方案的 ffmpeg 解码编排，在 Windows 上会死锁",
    "waveout.py": "管道方案的 waveOut 输出，已被 ffplay 取代",
    "cli.py": "管道方案 CLI，依赖上面四个已弃用模块",
}

README_TMPL = """bililive v{version} - 只听哔哩哔哩直播的声音
================================================

怎么用
------
1. 双击 start.bat
2. 按提示输入直播间房间号（浏览器地址栏 live.bilibili.com/ 后面那串数字）
3. 回车，等 5~20 秒开始出声

也可以带参数直接跑：
    start.bat 26774400           普通启动
    start.bat 26774400 60        音量 60（0-100，默认 100）
    start.bat 26774400 0         静音（只连不响，用于测试）

停止播放：关掉这个窗口，或按 Ctrl+C。声音会立刻停。

查看版本：python bililive_main.py --version


需要什么
--------
* Windows 系统
* Python 3.8 或更高版本（https://www.python.org/downloads/，
  安装时记得勾选 "Add python.exe to PATH"）
* 不需要装 ffmpeg —— ffplay.exe 已经放在同一个文件夹里了

如果 start.bat 说找不到 Python，就设一个环境变量指过去：
    set BILILIVE_PYTHON=D:\\你的路径\\python.exe


这个文件夹可以搬走
------------------
ffplay.exe 是静态自包含的，整个文件夹压缩后可以拷到任何
Windows 机器上用（目标机器只要有 Python）。
所有路径都相对于本文件夹，不依赖任何绝对路径、不写注册表。


它省多少流量
------------
实测同一个房间：

    只听声音      150 kbps    65 MB/小时    1.5 GB/天
    看直播画面    692 kbps   297 MB/小时    7.0 GB/天

省约 78%。而且这条纯音频流里视频数据是一个字节都没传
（实测 video tag = 0），不是"传了不用"。

**这个数字的局限如实说明**：来自单次 10 秒抽样的两点外推，
样本很小；692 kbps 是 B 站默认档位，不是你平时实际观看的档位。
方向（纯音频确实省得多）是可靠的，具体倍数会随房间和画质变化。
想自己复现或重测，见开发目录下的 probe/measure_traffic.py。


音量怎么调
----------
启动时指定：start.bat <房间号> <0-100>

注意 100 就是上限，填 150 也只会是 100（ffplay 自身限制，
超范围会被夹紧，不是放大）。

运行中想调：右键任务栏喇叭图标 -> 打开音量合成器 ->
找到 ffplay.exe 单独拖动。这样只影响这个工具，
不会把微信、浏览器一起调小。


主播没开播会怎样
----------------
不会报错退出，而是挂着等：

    该直播间当前没有开播（live_status=2）。
    工具会每 30 秒自动检查一次，一开播就会开始播放。

适合提前挂着等主播开播。想退出就关窗口。
房间号打错则会直接退出，不会无限重试。


稳定性设计
----------
* 音频地址约 1 小时过期 -> 提前 10 分钟主动换新
* 一次接口拿 12 条跨 CDN 候选 -> 当前一条失败会自动换下一条
* ffplay 异常退出 -> 指数退避重连（2s 起，60s 封顶）
* 关掉窗口 -> Job Object 保证 ffplay 一起终止，不会留下继续放音


已知限制（请如实了解）
----------------------
* 依赖 B 站的私有接口，属于逆向实现。官方改协议就得跟着改。
* 音频地址续期时会重启播放，有约 0.3~1 秒断音。听人说话基本无感。
* 不登录只能拿默认音质档位。
* **关闭了 ffplay 的 TLS 证书校验**。原因是 B 站 CDN 的证书链用
  gnutls 校验会失败（实测报 "Peer certificate failed verification"）。
  代价要说清楚：关掉校验等于放弃验证服务器身份，理论上中间人可以
  替换音频内容。本工具只听公开直播的声音、不传任何凭据，
  风险可以接受；但它确实是一个真实的降级，不是小毛病。
  想更稳妥可以换用带 openssl 后端的 ffmpeg 构建。


请仅用于自己收听，不要录播或转发。
"""

FAQ = """没声音？按顺序排查
==================

1. 窗口里有没有出现 "音频流: flv/http_stream ..." 这行？
   * 有 -> 连接成功了，问题在播放或系统音量
   * 没有 -> 连接就没成功，看第 4 条

2. 系统音量、音量合成器里 ffplay.exe 那一条，是不是被静音了？
   右键任务栏喇叭 -> 打开音量合成器 -> 找 ffplay.exe

3. 是不是用了音量 0？
   0 就是静音，这是设计如此。窗口里 AAC 帧数会照常涨。

4. 一直卡在 "准备播放房间" / 没反应？
   首次连接要等 CDN，实测有的 CDN 建连要 17 秒，
   再等一会儿。超过 1 分钟没动静看第 5 条。

5. 报 "该直播间当前没有开播"？
   那就是真的没开播，工具会在后台每 30 秒自动重试。
   换个正在播的房间号试试。

6. 报 "房间号有问题 ... code=60004"？
   房间号打错了。地址栏里 live.bilibili.com/ 后面那串数字才是房间号。

7. 报连不上 / 超时？
   检查网络能不能打开 bilibili.com。公司网络或代理有时会拦。

8. 声音还在响，但窗口关了？
   正常情况不该发生 —— 关窗口会经 Job Object 连带终止播放进程。
   万一遇到，在任务管理器里结束 ffplay.exe 即可。


想确认它到底有没有在收数据
--------------------------
不要用音量 0 猜，直接看窗口里的状态行：

    播放中 X.X 分钟  累计 N 次连接  候选剩 M  host=...

数字在涨、host 有值，就说明工作正常。


遇到问题时请把窗口里的日志保留下来
----------------------------------
从 v1.0 起 ffplay 的诊断信息会被保留，并在出错时打印出来，所以报错行通常能直接指出原因。
"""


def read_version():
    init = os.path.join(ROOT, "bililive", "__init__.py")
    with open(init, encoding="utf-8") as f:
        for line in f:
            if line.startswith("__version__"):
                return line.split("=")[1].strip().strip('"').strip("'")
    raise RuntimeError("找不到 __version__")


def main():
    version = read_version()
    dist = os.path.join(ROOT, f"bililive-portable-v{version}")

    print("=" * 68)
    print(f"组装绿色便携版  v{version}")
    print(f"目标: {dist}")
    print("=" * 68)

    if os.path.isdir(dist):
        print("清除旧目录 ...")
        shutil.rmtree(dist)
    os.makedirs(dist, exist_ok=True)

    problems = []

    # ---- 1. 程序包 ----
    pkg_src = os.path.join(ROOT, "bililive")
    pkg_dst = os.path.join(dist, "bililive")
    os.makedirs(pkg_dst, exist_ok=True)
    copied = 0
    for name in MODULES:
        src = os.path.join(pkg_src, name)
        if not os.path.isfile(src):
            problems.append(f"缺少模块 {name}")
            continue
        shutil.copy2(src, os.path.join(pkg_dst, name))
        copied += 1
    print(f"程序包: 复制 {copied}/{len(MODULES)} 个模块")
    print(f"  已排除 {len(EXCLUDED)} 个弃用模块:")
    for name, why in EXCLUDED.items():
        print(f"    - {name}: {why}")

    # ---- 2. 入口脚本 ----
    shutil.copy2(os.path.join(ROOT, "bililive_main.py"),
                 os.path.join(dist, "bililive_main.py"))
    print("入口脚本: bililive_main.py")

    # ---- 3. ffplay ----
    ffplay_src = os.path.join(ROOT, "tools", "ffplay.exe")
    if not os.path.isfile(ffplay_src):
        problems.append("找不到 tools/ffplay.exe")
    else:
        shutil.copy2(ffplay_src, os.path.join(dist, "ffplay.exe"))
        print(f"播放器: ffplay.exe "
              f"({os.path.getsize(ffplay_src)/1048576:.1f} MB)")

    # ---- 4. start.bat / unblock.bat（模板，含版本号替换）----
    tpl_dir = os.path.join(ROOT, "probe", "portable_template")
    tpl = os.path.join(tpl_dir, "start.bat")
    bat_dst = os.path.join(dist, "start.bat")
    if not os.path.isfile(tpl):
        problems.append(f"缺少启动器模板 {tpl}")
    else:
        with open(tpl, encoding="ascii") as f:
            bat = f.read()
        bat = bat.replace("@VERSION@", version)
        with open(bat_dst, "w", encoding="ascii", newline="\r\n") as f:
            f.write(bat)
        print(f"启动器: start.bat (v{version})")

    # 独立的"解除封锁"脚本：给已经解压好、正被安全警告困扰的用户用
    ub_dst = os.path.join(dist, "unblock.bat")
    ub_tpl = os.path.join(tpl_dir, "unblock.bat")
    if os.path.isfile(ub_tpl):
        with open(ub_tpl, encoding="ascii") as f:
            ub = f.read()
        with open(ub_dst, "w", encoding="ascii", newline="\r\n") as f:
            f.write(ub)
        print("辅助脚本: unblock.bat（解除安全警告用）")
    else:
        problems.append(f"缺少 unblock.bat 模板 {ub_tpl}")

    # ---- 5. 文档 ----
    with open(os.path.join(dist, "README.txt"), "w", encoding="utf-8") as f:
        f.write(README_TMPL.format(version=version))
    with open(os.path.join(dist, "没声音看这里.txt"), "w", encoding="utf-8") as f:
        f.write(FAQ)
    print("文档: README.txt / 没声音看这里.txt")

    # ---- 6. 校验（先全部收集，再统一打印，不提前报 OK）----
    print("\n" + "-" * 68)
    print("校验:")

    if os.path.isfile(bat_dst):
        with open(bat_dst, "rb") as f:
            raw = f.read()
        bad = sum(1 for b in raw if b > 127)
        if bad:
            problems.append(f"start.bat 含 {bad} 个非 ASCII 字节，cmd 会解析失败")
        else:
            print("  [OK] start.bat 纯 ASCII")
        if b"@VERSION@" in raw:
            problems.append("start.bat 里 @VERSION@ 未被替换")
        elif version.encode() not in raw:
            problems.append("start.bat 里找不到版本号")
        else:
            print(f"  [OK] start.bat 含版本号 v{version}")

    # 关键：发行的代码里不能出现开发机的绝对路径
    leaked_paths = []
    # 用拼接构造模式串，避免本文件自己把「待检查的字面量」写出来 ——
    # 否则扫描器会命中自己，产生假阳性（实际踩过这个坑）。
    _needles = (os.sep.join(("E:", "wokbdyanddsh")),
                "C:" + os.sep + os.sep.join(("Users",)),
                os.sep.join(("E:", "1D")))
    for dirpath, _d, files in os.walk(dist):
        for fn in files:
            if not fn.endswith((".py", ".bat")):
                continue
            p = os.path.join(dirpath, fn)
            with open(p, encoding="utf-8", errors="replace") as f:
                txt = f.read()
            for needle in _needles:
                if needle in txt:
                    leaked_paths.append(f"{fn} -> 含本机路径")
    if leaked_paths:
        problems.extend(f"含开发机绝对路径: {x}" for x in leaked_paths)
    else:
        print("  [OK] 无开发机绝对路径")

    # 弃用模块不能混进来
    leaked = [n for n in EXCLUDED if os.path.isfile(os.path.join(pkg_dst, n))]
    if leaked:
        problems.append(f"弃用模块被误打包: {leaked}")
    else:
        print("  [OK] 弃用模块未混入")

    # 多余目录
    junk = [p for p in os.listdir(dist)
            if p in ("__pycache__", ".pytest_cache", "tools")]
    if junk:
        problems.append(f"多余目录: {junk}")
    else:
        print("  [OK] 无多余目录")
    # __pycache__ 会被开发时跑过的 compileall 带进来，必须显式清掉：
    # 它既增大体积，也可能让用户拿到与源码不匹配的旧字节码。
    pycache = os.path.join(pkg_dst, "__pycache__")
    if os.path.isdir(pycache):
        shutil.rmtree(pycache)
        print("  [OK] 已清除 __pycache__")
    for dirpath, dirnames, _files in os.walk(dist):
        if "__pycache__" in dirnames:
            problems.append(f"残留 __pycache__: {dirpath}")

    # ---- 7. 最终复查 ----
    # 上面所有校验都可能已经 import 过包内容，所以放在最末尾再扫一次，
    # 确保没有字节码在「校验之后」才被写出来。
    for dirpath, dirnames, _files in os.walk(dist):
        for d in list(dirnames):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
                print(f"  [OK] 末尾复查清除了 {os.path.join(dirpath, d)}")
    leftovers = []
    for dirpath, dirnames, _files in os.walk(dist):
        if "__pycache__" in dirnames:
            leftovers.append(dirpath)
    if leftovers:
        problems.append(f"最终仍有 __pycache__: {leftovers}")
    else:
        print("  [OK] 最终无字节码残留")

    # 版本一致性
    with open(os.path.join(pkg_dst, "__init__.py"), encoding="utf-8") as f:
        if f'__version__ = "{version}"' not in f.read():
            problems.append("包内版本号与目录版本号不一致")
        else:
            print(f"  [OK] 版本号 v{version} 一致")

    total = 0
    for dirpath, _d, files in os.walk(dist):
        for fn in files:
            total += os.path.getsize(os.path.join(dirpath, fn))

    print("-" * 68)
    if problems:
        print(f"发现 {len(problems)} 个问题:")
        for p in problems:
            print(f"  !! {p}")
        return 1
    print(f"完成 -> {dist}")
    print(f"总体积 {total/1048576:.1f} MB，整个文件夹可直接压缩带走。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
