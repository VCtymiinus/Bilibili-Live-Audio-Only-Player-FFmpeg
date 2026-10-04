"""测 ffplay 探测参数对「启动到首帧」的影响。

当前参数: -infbuf -tls_verify 0（探测大小用 ffplay 默认值）
候选调优: 显式给 -probesize / -analyzeduration 一个小值

风险要一并检查：值给小了可能认不出流格式（尤其是 FLV，需要足够数据
才能判定 codec）。所以每次都要确认「真的开始播放」而不是「快速失败」。

全程音量 0，避免突然出声。
"""

import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bililive.biliapi import BiliLiveClient  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FFPLAY = os.path.join(ROOT, "tools", "ffplay.exe")
ROOM = int(sys.argv[1]) if len(sys.argv) > 1 else 21144080

# 只测第一条 flv 候选，保证各方案面对同样的流
VARIANTS = [
    ("当前（默认探测）", []),
    ("probesize=500k/analyzeduration=0", ["-probesize", "500000",
                                          "-analyzeduration", "0"]),
    ("probesize=32/analyzeduration=0", ["-probesize", "32",
                                        "-analyzeduration", "0"]),
]


def get_flv_url():
    c = BiliLiveClient()
    info = c.resolve_room(ROOM)
    for s in c.audio_streams(info["room_id"]):
        if s.format == "flv":
            return s.url, c.headers()
    return None, None


def run_variant(label, extra, url, hdr, timeout=12):
    """返回 (是否真的开始播放, 首个进度行耗时, 完整输出行)"""
    hdrs = "".join(f"{k}: {v}\r\n" for k, v in hdr.items())
    env = dict(os.environ)
    env["SDL_VIDEODRIVER"] = "dummy"
    env["SDL_AUDIODRIVER"] = "directsound"
    cmd = [FFPLAY, "-hide_banner", "-loglevel", "info", "-nodisp", "-vn",
           "-autoexit", "-volume", "0", "-infbuf", "-tls_verify", "0"]
    cmd += extra
    cmd += ["-headers", hdrs, url]

    t0 = time.time()
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         creationflags=0x08000000, env=env)
    lines = []
    stop = threading.Event()

    def rd():
        try:
            for raw in iter(p.stdout.readline, b""):
                dt = time.time() - t0
                line = raw.decode("utf-8", "replace").rstrip()
                lines.append((dt, line))
                if stop.is_set():
                    break
        except Exception:
            pass

    th = threading.Thread(target=rd, daemon=True)
    th.start()

    # 判断"真的开始播放"：日志里出现 Audio: aac 且随后有进度行
    # 进度行形如 "  12.34 M-A:  0.000 fd=   0 aq=   12KB ..."（我方用 nostats? 没加）
    first_progress = None
    saw_audio = False
    deadline = time.time() + timeout
    while time.time() < deadline:
        for dt, line in lines:
            if "Audio:" in line and ("aac" in line.lower() or "mp4a" in line.lower()):
                saw_audio = True
            # 进度行：以数字开头且含 aq=
            if "aq=" in line and re.match(r"^\s*[\d.]+", line):
                if first_progress is None:
                    first_progress = dt
        if first_progress is not None:
            break
        if p.poll() is not None:
            break
        time.sleep(0.05)

    stop.set()
    alive = p.poll() is None
    if alive:
        p.terminate()
    try:
        p.wait(timeout=3)
    except Exception:
        p.kill()

    return {
        "label": label,
        "playing": first_progress is not None,
        "first_progress": first_progress,
        "saw_audio": saw_audio,
        "alive": alive,
        "rc": p.returncode,
        "lines": lines,
    }


def main():
    print("=" * 74)
    print("ffplay 探测参数对首帧的影响（音量 0，不出声）")
    print("=" * 74)

    url, hdr = get_flv_url()
    if not url:
        print("!! 没拿到 flv 候选")
        return 1
    print(f"流: {url.split('/')[2]}  flv\n")

    results = []
    for label, extra in VARIANTS:
        print(f"--- {label} ---")
        r = run_variant(label, extra, url, hdr)
        results.append(r)
        print(f"  开始播放: {'是' if r['playing'] else '否'}   "
              f"首进度行: "
              f"{('+%.2fs' % r['first_progress']) if r['first_progress'] else 'N/A'}"
              f"   识别音频: {'是' if r['saw_audio'] else '否'}   "
              f"存活到超时: {'是' if r['alive'] else '否'}")
        # 打印最后几行便于看错误
        for dt, line in r["lines"][-4:]:
            print(f"      +{dt:5.2f}s  {line[:100]}")
        print()

    print("=" * 74)
    print("对比")
    print("=" * 74)
    for r in results:
        t = f"{r['first_progress']:.2f}s" if r["first_progress"] else "失败"
        print(f"  {r['label']:<38} {t:>8}   "
              f"{'OK' if r['playing'] else '未播放'}")
    print("\n注：首次运行含 DNS/CDN 冷启动，后续会略快；")
    print("    重点看各方案之间是否有量级差异，以及小 probesize 会不会导致识别失败。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
