"""首播延迟分解测量：找出 5~20 秒到底花在哪。

逐段计时，不猜：
  1. resolve_room          房间号 -> room_id（+live_status）
  2. ensure_buvid          取 buvid3 cookie（目前无条件调用）
  3. room_info             取标题/主播名（只为显示）
  4. audio_streams         取纯音频流地址
  5. 首个 CDN 建连         拿到 URL 后到第一个字节
  6. ffplay 到出声         启动进程到它真正开始输出

同时验证一个关键假设：**getRoomPlayInfo 到底需不需要 buvid3？**
如果不需要，那就是白白多一次请求。
"""

import os
import ssl
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.biliapi import (API_HOST, BiliLiveClient,  # noqa: E402
                              join_stream_url)
import json  # noqa: E402

ROOM = int(sys.argv[1]) if len(sys.argv) > 1 else 26774400


class Timer:
    def __init__(self, label):
        self.label = label
        self.rows = []

    def run(self, fn, name):
        t0 = time.time()
        try:
            r = fn()
            dt = time.time() - t0
            self.rows.append((name, dt, "ok"))
            return r
        except Exception as e:
            dt = time.time() - t0
            self.rows.append((name, dt, f"{type(e).__name__}: {str(e)[:50]}"))
            return None

    def report(self):
        print(f"\n--- {self.label} ---")
        total = 0.0
        for name, dt, status in self.rows:
            total += dt
            print(f"  {name:<34} {dt:6.2f}s   {status}")
        print(f"  {'合计':<34} {total:6.2f}s")
        return total


def main():
    print("=" * 70)
    print(f"首播延迟分解  room={ROOM}")
    print("=" * 70)

    t = Timer("逐段耗时")

    # --- 1. resolve_room ---
    c1 = BiliLiveClient()
    info = t.run(lambda: c1.resolve_room(ROOM), "1. resolve_room")
    if not info:
        t.report()
        return 1
    rid = info["room_id"]
    print(f"\n  room_id={rid} live_status={info.get('live_status')}")

    # --- 2. buvid 有无的对比：getRoomPlayInfo 到底需不需要它 ---
    print("\n  [关键验证] getRoomPlayInfo 是否真的需要 buvid3 cookie？")
    params = {"room_id": rid, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
              "qn": "10000", "platform": "web", "only_audio": "1", "only_video": "0"}
    url = API_HOST + "/xlive/web-room/v2/index/getRoomPlayInfo?" + \
        "&".join(f"{k}={v}" for k, v in params.items())

    from bililive.biliapi import UA
    for label, cookie in (("不带 Cookie", None), ("带 buvid3", c1.ensure_buvid())):
        hdr = {"User-Agent": UA, "Referer": "https://live.bilibili.com/",
               "Accept": "*/*", "Accept-Encoding": "gzip"}
        if cookie:
            hdr["Cookie"] = cookie
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=25,
                                       context=ssl.create_default_context()) as r:
                import gzip
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                rr = json.loads(raw.decode("utf-8", "replace"))
            dt = time.time() - t0
            pi = (rr.get("data") or {}).get("playurl_info")
            n = 0
            if pi:
                for st in pi["playurl"]["stream"]:
                    for f in st["format"]:
                        for cc in f["codec"]:
                            n += len(cc.get("url_info") or [])
            print(f"    {label:<14} {dt:5.2f}s  code={rr.get('code')}  "
                  f"playurl_info={'有' if pi else '无'}  候选={n}")
        except Exception as e:
            print(f"    {label:<14} 异常 {type(e).__name__}: {e}")

    # --- 3. ensure_buvid 单独耗时 ---
    c2 = BiliLiveClient()
    t.run(lambda: c2.ensure_buvid(), "2. ensure_buvid（首次）")

    # --- 4. room_info 耗时（只为显示标题）---
    t.run(lambda: c2.room_info(rid), "3. room_info（仅显示用）")

    # --- 5. audio_streams ---
    streams = t.run(lambda: c2.audio_streams(rid), "4. audio_streams")
    t.report()

    if not streams:
        print("!! 没拿到流")
        return 1

    # --- 6. 各候选 CDN 的建连耗时（串行对比）---
    print("\n--- 各候选 CDN 建连到首字节 ---")
    hdr = c2.headers()
    best = None
    for i, s in enumerate(streams):
        host = s.url.split("/")[2]
        t0 = time.time()
        try:
            req = urllib.request.Request(s.url, headers=hdr)
            resp = urllib.request.urlopen(req, timeout=30,
                                         context=ssl.create_default_context())
            t_conn = time.time() - t0
            first = resp.read(16384)
            t_first = time.time() - t0
            resp.close()
            ok = len(first) > 0
            print(f"  [{i:2}] {host[:32]:<32} 建连{t_conn:5.2f}s "
                  f"首包{t_first:5.2f}s {'OK' if ok else 'EOF'}")
            if ok and best is None:
                best = (i, host, t_first)
        except Exception as e:
            print(f"  [{i:2}] {host[:32]:<32} 异常 {type(e).__name__}: "
                  f"{str(e)[:40]}")
    if best:
        print(f"\n  最快可用: 候选#{best[0]} {best[1]}  首包 {best[2]:.2f}s")

    print("\n" + "=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
