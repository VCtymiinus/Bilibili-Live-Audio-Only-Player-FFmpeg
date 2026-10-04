"""下载静态 ffplay.exe（以及可选的 ffmpeg/ffprobe）到 tools/ 目录。

这是给「克隆仓库后怎么跑起来」用的脚本。发行包里已经自带 ffplay.exe，
只有从源码运行时才需要执行它。

    python probe/fetch_ffplay.py            # 只解出 ffplay.exe（当前方案够用）
    python probe/fetch_ffplay.py --all      # 顺便解出 ffmpeg.exe / ffprobe.exe
                                            # （只有流量测量脚本需要 ffmpeg）

下载源：gyan.dev 的 essentials 静态构建（GPL），单包内含三个可执行文件。
实测直连较慢，所以用多线程分块下载绕开单连接限速。

为什么不用 GitHub 上现成的单文件 ffplay：
    没有可靠的「只含 ffplay」的静态构建发布，BtbN / gyan 都是整合包。
"""

import io
import os
import shutil
import sys
import threading
import time
import urllib.request
import zipfile

# 路径全部相对本文件推导，不写死任何机器相关路径
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOLS = os.path.join(ROOT, "tools")
PART_DIR = os.path.join(TOOLS, "_dl_parts")

URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
THREADS = 8
# ffplay.exe 是当前方案唯一必需的；其余按需解出
WANT_BASE = ("ffplay.exe",)
WANT_ALL = ("ffplay.exe", "ffmpeg.exe", "ffprobe.exe")


def head_size(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        n = r.headers.get("Content-Length")
        return int(n) if n and n.isdigit() else 0, r.headers.get("Accept-Ranges")


def fetch_range(url, start, end, path, idx, progress, total, lock, quiet=False):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=60) as r, open(path, "wb") as f:
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            f.write(chunk)
            if quiet:
                continue
            with lock:
                progress[idx] = f.tell()
                done = sum(progress)
                pct = done * 100 // total if total else 0
                print(f"\r  {done/1048576:.1f}/{total/1048576:.1f} MB ({pct}%)",
                      end="", flush=True)


def download(url, quiet=False):
    """多线程分块下载，返回完整字节。"""
    size, ranges = head_size(url)
    print(f"包大小 {size/1048576:.1f} MB  Accept-Ranges={ranges}")

    if not size or (ranges and ranges.lower() != "bytes"):
        print("不支持 Range，改用单连接下载（会慢很多）...")
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        buf = io.BytesIO()
        with urllib.request.urlopen(req, timeout=60) as r:
            while True:
                c = r.read(262144)
                if not c:
                    break
                buf.write(c)
        return buf.getvalue()

    os.makedirs(PART_DIR, exist_ok=True)
    step = size // THREADS
    spans = []
    for i in range(THREADS):
        s = i * step
        e = size - 1 if i == THREADS - 1 else (s + step - 1)
        spans.append((s, e, os.path.join(PART_DIR, f"part{i:02d}")))

    progress = [0] * THREADS
    lock = threading.Lock()
    t0 = time.time()
    threads = []
    for i, (s, e, p) in enumerate(spans):
        t = threading.Thread(
            target=fetch_range,
            args=(url, s, e, p, i, progress, size, lock, quiet), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    if not quiet:
        print(f"\n下载完成，用时 {time.time()-t0:.0f}s")

    blob = io.BytesIO()
    for _s, _e, p in spans:
        with open(p, "rb") as f:
            blob.write(f.read())
    data = blob.getvalue()
    if len(data) != size:
        raise RuntimeError(f"拼接后大小不符: {len(data)} != {size}")
    return data


def extract(data, wanted):
    zf = zipfile.ZipFile(io.BytesIO(data))
    os.makedirs(TOOLS, exist_ok=True)
    got = []
    for want in wanted:
        names = [n for n in zf.namelist() if n.lower().endswith(want)]
        if not names:
            print(f"  !! 包里没有 {want}")
            continue
        # 多个候选时取路径最短的，避开 doc/ 之类
        name = sorted(names, key=len)[0]
        target = os.path.join(TOOLS, want)
        with zf.open(name) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        got.append(want)
        print(f"  [OK] {want}  ({os.path.getsize(target)/1048576:.1f} MB)")
    return got


def main():
    want_all = "--all" in sys.argv
    wanted = WANT_ALL if want_all else WANT_BASE

    print("=" * 66)
    print("获取 ffplay" + ("（含 ffmpeg/ffprobe）" if want_all else ""))
    print(f"目标目录: {TOOLS}")
    print("=" * 66)

    already = [w for w in wanted if os.path.isfile(os.path.join(TOOLS, w))]
    if len(already) == len(wanted):
        print("所需文件已存在，无需下载:")
        for w in already:
            print(f"  {w}")
        return 0
    if already:
        print(f"已存在: {already}，仍需获取: "
              f"{[w for w in wanted if w not in already]}")

    try:
        data = download(URL)
    except Exception as e:
        print(f"\n!! 下载失败: {type(e).__name__}: {e}")
        print("   可手动下载后用解压工具取出 ffplay.exe 放进 tools/ :")
        print(f"   {URL}")
        return 1

    print("解压中 ...")
    got = extract(data, wanted)
    shutil.rmtree(PART_DIR, ignore_errors=True)

    print("-" * 66)
    if "ffplay.exe" not in got:
        print("!! 关键文件 ffplay.exe 没拿到，程序无法播放")
        return 1
    print(f"完成。ffplay.exe 已就位: {os.path.join(TOOLS, 'ffplay.exe')}")
    print("现在可以运行: python bililive_main.py <房间号>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
