"""实测优化后的首播延迟：从「启动进程」到「ffplay 真正开始播放」。

判据用 ffplay 进程出现为准（它就是出声的那一刻），
并且额外打印 run() 内部各阶段日志的时间戳，便于归因。

不联网测不了，所以需要房间在播。选一个自动挑在播房间的方式。
"""

import gzip
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HOST = "https://api.live.bilibili.com"


def api(path, params=None):
    url = HOST + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://live.bilibili.com/",
        "Accept-Encoding": "gzip"})
    with DIRECT.open(req, timeout=15) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", "replace"))


def pick_live():
    r = api("/room/v1/room/get_user_recommend", {"page": "1", "page_size": "8"})
    for d in (r.get("data") or []):
        if isinstance(d, dict) and d.get("roomid"):
            rid = int(d["roomid"])
            di = (api("/room/v1/Room/room_init", {"id": rid}).get("data") or {})
            if di.get("live_status") == 1:
                return di["room_id"], d.get("uname", ""), d.get("title", "")
    return None


def main():
    print("=" * 72)
    print("优化后首播延迟实测")
    print("=" * 72)

    found = pick_live()
    if not found:
        print("!! 没找到在播房间")
        return 1
    room, uname, title = found
    print(f"房间 {room}  {uname} | {title}\n")

    for attempt in (1, 2):
        print(f"--- 第 {attempt} 次 ---")
        t_start = time.time()
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        p = subprocess.Popen(
            [PY, "-u", os.path.join(ROOT, "bililive_main.py"), str(room)],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, text=True, encoding="utf-8", errors="replace")

        # 轮询 ffplay 进程出现 —— 那一刻就是开始出声
        import ctypes
        t_play = None
        lines = []

        def ffplay_running():
            # 用 tasklist 太慢，直接查进程快照
            import subprocess as sp
            try:
                out = sp.run(["tasklist", "/FI", "IMAGENAME eq ffplay.exe",
                              "/NH"], capture_output=True, text=True, timeout=5,
                             creationflags=0x08000000)
                return "ffplay.exe" in out.stdout
            except Exception:
                return False

        import threading
        stop_poll = threading.Event()

        def poll():
            nonlocal t_play
            while not stop_poll.is_set():
                if ffplay_running():
                    t_play = time.time() - t_start
                    stop_poll.set()
                    return
                time.sleep(0.05)

        th = threading.Thread(target=poll, daemon=True)
        th.start()

        # 收日志
        def reader():
            try:
                for line in p.stdout:
                    lines.append((time.time() - t_start, line.rstrip()))
            except Exception:
                pass

        tr = threading.Thread(target=reader, daemon=True)
        tr.start()

        # 最长等 40 秒
        deadline = time.time() + 40
        while time.time() < deadline:
            if t_play is not None:
                break
            if p.poll() is not None:
                break
            time.sleep(0.1)

        print(f"\n  日志时间线（相对启动秒数）:")
        for dt, line in lines[:14]:
            print(f"    +{dt:5.2f}s  {line}")

        if t_play is not None:
            print(f"\n  *** ffplay 开始播放: +{t_play:.2f}s ***")
        else:
            print("\n  !! 40 秒内未开始播放")

        stop_poll.set()
        p.terminate()
        try:
            p.wait(timeout=5)
        except Exception:
            p.kill()
        # 清掉可能残留的 ffplay
        subprocess.run(["taskkill", "/F", "/IM", "ffplay.exe"],
                       capture_output=True, creationflags=0x08000000)
        time.sleep(1.5)
        print()

    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
