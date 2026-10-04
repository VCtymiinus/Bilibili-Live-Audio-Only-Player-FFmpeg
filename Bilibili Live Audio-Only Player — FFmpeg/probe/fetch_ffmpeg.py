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
TARGET = os.path.join(TOOLS, "ffmpeg.exe")

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
    if os.path.isfile(TARGET):
        print(f"已存在 {TARGET}，跳过")
        return 0

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
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = [n for n in zf.namelist() if n.lower().endswith("ffmpeg.exe")]
    if not names:
        print("!! 包里没有 ffmpeg.exe")
        return 1
    name = sorted(names, key=len)[0]
    os.makedirs(TOOLS, exist_ok=True)
    with zf.open(name) as src, open(TARGET, "wb") as dst:
        dst.write(src.read())
    print(f"OK -> {TARGET} ({os.path.getsize(TARGET)/1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
