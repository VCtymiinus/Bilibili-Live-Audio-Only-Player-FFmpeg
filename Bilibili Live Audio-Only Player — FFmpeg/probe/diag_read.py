"""诊断：为什么收流读到 0 字节？

之前 max_bytes=262144 一次读能通，现在 4096 小读循环读不到。
逐个变量隔离：URL / 请求头 / 读法 / CDN。
"""

import ssl
import sys
import time
import urllib.request
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.biliapi import BiliLiveClient  # noqa: E402

ROOM = 22388070


def try_read(label, url, headers, nbytes, timeout=20):
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout,
                                   context=ssl.create_default_context()) as r:
            t_open = time.time() - t0
            data = r.read(nbytes)
            t_read = time.time() - t0
            print(f"  [{label}] 打开 {t_open:.2f}s 读到 {len(data)}B "
                  f"共 {t_read:.2f}s  magic={data[:6].hex(' ')}")
            return data
    except Exception as e:
        print(f"  [{label}] 失败 {type(e).__name__}: {str(e)[:90]}  "
              f"({time.time()-t0:.2f}s)")
        return b""


def main():
    c = BiliLiveClient()
    info = c.resolve_room(ROOM)
    print(f"room_id={info['room_id']} live_status={info.get('live_status')}")

    streams = c.audio_streams(info["room_id"])
    print(f"拿到 {len(streams)} 条候选流")
    for i, s in enumerate(streams):
        print(f"  [{i}] {s.protocol}/{s.format} host={s.url.split('/')[2]}")

    h = c.headers()
    print(f"\n请求头 keys: {sorted(h.keys())}")
    print(f"Cookie 是否带上: {'Cookie' in h}")

    s0 = streams[0]
    print(f"\n--- 1. 首选流，一次大读 ---")
    try_read("大读 256KB", s0.url, h, 262144)

    print(f"\n--- 2. 同一条流，小读 4096 ---")
    try_read("小读 4096", s0.url, h, 4096)

    print(f"\n--- 3. 同一条流，不带 Cookie ---")
    h2 = {k: v for k, v in h.items() if k != "Cookie"}
    try_read("无Cookie", s0.url, h2, 65536)

    print(f"\n--- 4. 其它 CDN 候选 ---")
    for i, s in enumerate(streams[1:4], start=1):
        try_read(f"候选{i} {s.url.split('/')[2][:24]}", s.url, h, 65536)

    print(f"\n--- 5. 增量读：连续 5 次 4096 ---")
    try:
        req = urllib.request.Request(s0.url, headers=h)
        with urllib.request.urlopen(req, timeout=20,
                                   context=ssl.create_default_context()) as r:
            for k in range(5):
                t = time.time()
                d = r.read(4096)
                print(f"    第{k+1}次: {len(d)}B  {time.time()-t:.2f}s")
                if not d:
                    break
    except Exception as e:
        print(f"    失败 {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
