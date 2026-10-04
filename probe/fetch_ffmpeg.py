"""多线程分块下载静态 ffmpeg —— 绕过单连接限速。

gyan.dev 的 essentials 包只接受单连接时约 1MB/分钟，太慢。
改成 8 个并发 Range 请求分块下载再拼接，通常能快一个数量级。
"""

import io
import os
import sys
import threading
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")

# 本脚本要解出**两个**程序：
#   ffmpeg.exe —— 通用转码工具（probe/ 里的离线验证脚本用得上）
#   ffplay.exe —— 本项目的播放器，**缺了它程序无法出声**
#
# *** 这里踩过一个坑，别只看 TARGET ***
# 早期版本只找 "ffmpeg.exe"，然后 README 让用户再跑一步
# `probe/extract_ffplay.py` 去解出 ffplay —— 而那个文件在仓库里根本不存在，
# 于是「按文档拿播放器」这条路是断的。
# 现在一次把两个都解出来，文档里那一步也就不需要了。
TARGETS = ("ffmpeg.exe", "ffplay.exe")

URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
THREADS = 8
PART = os.path.join(ROOT, "tools", "_ffmpeg_dl")


def head_size(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        n = r.headers.get("Content-Length")
        return int(n) if n and n.isdigit() else 0, r.headers.get("Accept-Ranges")


def fetch_range(url, start, end, path, idx, progress, total, lock):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            f.write(chunk)
            with lock:
                progress[idx] = f.tell()
                done = sum(progress)
                pct = done * 100 // total if total else 0
                print(f"\r  {done/1048576:.1f}/{total/1048576:.1f} MB ({pct}%)",
                      end="", flush=True)


def main():
    # 跳过判断看**两个**产物是否都已存在。
    # 只看 ffmpeg.exe 是不够的：本项目要的是 ffplay.exe，
    # 早先有 ffmpeg 就跳过，会导致 ffplay 永远拿不到。
    existing = [w for w in TARGETS if os.path.isfile(os.path.join(TOOLS, w))]
    if len(existing) == len(TARGETS):
        print(f"已存在，跳过：{', '.join(existing)}")
        return 0
    if existing:
        print(f"已有 {', '.join(existing)}，继续补齐其余文件")

    size, ranges = head_size(URL)
    print(f"文件 {size/1048576:.1f} MB  Accept-Ranges={ranges}")
    if not size or (ranges and ranges.lower() != "bytes"):
        print("不支持 Range，退回单连接下载")
        return fallback_single()

    os.makedirs(PART, exist_ok=True)
    step = size // THREADS
    spans = []
    for i in range(THREADS):
        s = i * step
        e = size - 1 if i == THREADS - 1 else (s + step - 1)
        spans.append((s, e, os.path.join(PART, f"part{i:02d}")))

    progress = [0] * THREADS
    lock = threading.Lock()
    t0 = time.time()
    threads = []
    for i, (s, e, p) in enumerate(spans):
        t = threading.Thread(target=fetch_range,
                             args=(URL, s, e, p, i, progress, size, lock),
                             daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    print(f"\n下载完成，用时 {time.time()-t0:.0f}s")

    blob = io.BytesIO()
    for _s, _e, p in spans:
        with open(p, "rb") as f:
            blob.write(f.read())
    data = blob.getvalue()
    print(f"拼接后 {len(data)/1048576:.1f} MB（应为 {size/1048576:.1f} MB）")
    if len(data) != size:
        print("!! 大小不符，分块可能有问题")
        return 1

    return extract(data)


def fallback_single():
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    buf = io.BytesIO()
    with urllib.request.urlopen(req, timeout=60) as r:
        while True:
            c = r.read(262144)
            if not c:
                break
            buf.write(c)
    return extract(buf.getvalue())


def extract(data):
    """从 zip 里解出 ffmpeg.exe 和 ffplay.exe 到 tools/。

    逐个查找、各自独立成败：只要 ffplay 到位，本项目就能出声；
    ffmpeg 缺失只影响 probe/ 里那些离线验证脚本，不该让整步失败。
    """
    zf = zipfile.ZipFile(io.BytesIO(data))
    os.makedirs(TOOLS, exist_ok=True)
    ok = False
    for want in TARGETS:
        names = [n for n in zf.namelist()
                 if n.lower().endswith(want.lower())]
        if not names:
            print(f"!! 包里没有 {want}")
            continue
        name = sorted(names, key=len)[0]      # 取路径最短的那个
        dst_path = os.path.join(TOOLS, want)
        with zf.open(name) as src, open(dst_path, "wb") as dst:
            dst.write(src.read())
        print(f"OK -> {dst_path} ({os.path.getsize(dst_path)/1048576:.1f} MB)")
        if want.lower() == "ffplay.exe":
            ok = True
    if not ok:
        print("!! 关键文件 ffplay.exe 没有拿到，本项目无法播放")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
