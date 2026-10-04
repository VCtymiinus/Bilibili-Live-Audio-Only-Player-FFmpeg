"""验证优化方案：禁用代理 + 并行抢流，能到多快？

目标是把首播延迟从 5~20 秒降到 1~3 秒。
本脚本只测量，不改主程序。

方案要点:
  1. API 走直连（不走系统代理）—— 这是最大的一项
  2. 候选 CDN 并行竞速，而不是串行逐个试
  3. API 请求之间并行（buvid 与 room_init 互不依赖）
"""

import json
import socket
import ssl
import sys
import threading
import time
import urllib.request

HOST = "api.live.bilibili.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
ROOM = 26774400
CTX = ssl.create_default_context()

# 关键：一个「不走代理」的 opener
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path, params=None, cookie=None, timeout=15):
    url = f"https://{HOST}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    h = {"User-Agent": UA, "Referer": "https://live.bilibili.com/",
         "Accept": "*/*", "Accept-Encoding": "gzip"}
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(url, headers=h)
    with DIRECT.open(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", "replace"))


def get_buvid():
    try:
        r = api("https://api.bilibili.com/x/frontend/finger/spi".replace(
            f"https://{HOST}", ""), None)
        return "buvid3=" + r["data"]["b_3"]
    except Exception:
        return None


def main():
    print("=" * 74)
    print("优化方案验证：禁用代理 + 并行")
    print("=" * 74)

    # ---------- 阶段 1：并行拿 buvid 与 room_init ----------
    print("\n[阶段1] buvid 与 room_init 并行（二者互不依赖）")
    box = {}

    def t_buvid():
        t0 = time.time()
        box["buvid"] = get_buvid()
        box["t_buvid"] = time.time() - t0

    def t_room():
        t0 = time.time()
        box["room"] = api(f"/room/v1/Room/room_init?id={ROOM}")
        box["t_room"] = time.time() - t0

    t0 = time.time()
    ths = [threading.Thread(target=t_buvid), threading.Thread(target=t_room)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    t_phase1 = time.time() - t0
    print(f"  buvid    {box.get('t_buvid', 0):5.2f}s")
    print(f"  room     {box.get('t_room', 0):5.2f}s")
    print(f"  并行墙钟 {t_phase1:5.2f}s  (串行会是 "
          f"{box.get('t_buvid',0)+box.get('t_room',0):.2f}s)")

    info = (box.get("room") or {}).get("data") or {}
    rid = info.get("room_id")
    live = info.get("live_status")
    print(f"  room_id={rid} live_status={live}")
    if live != 1:
        print("  !! 房间未开播，只能验证到这一步")
        return 0

    # ---------- 阶段 2：拿流地址 ----------
    print("\n[阶段2] getRoomPlayInfo（是否需要 buvid 会一并看出）")
    t0 = time.time()
    r = api("/xlive/web-room/v2/index/getRoomPlayInfo", {
        "room_id": rid, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
        "qn": "10000", "platform": "web", "only_audio": "1", "only_video": "0"})
    t_phase2 = time.time() - t0
    pi = (r.get("data") or {}).get("playurl_info")
    print(f"  耗时 {t_phase2:5.2f}s  code={r.get('code')}  "
          f"playurl_info={'有' if pi else '无'}")
    if not pi:
        print("  !! 没有可用流")
        return 1

    # 收集所有候选 URL
    urls = []
    for st in pi["playurl"]["stream"]:
        for f in st["format"]:
            for cc in f["codec"]:
                for u in cc.get("url_info") or []:
                    base = cc["base_url"]
                    path_part, _, bq = base.partition("?")
                    full = u["host"].rstrip("/") + "/" + path_part.lstrip("/")
                    q = "&".join(p for p in (bq, u.get("extra") or "") if p)
                    if q:
                        full += "?" + q
                    urls.append((st["protocol_name"], f["format_name"], full))
    flv = [u for u in urls if u[1] == "flv"]
    print(f"  候选总数 {len(urls)}（flv {len(flv)} 条）")

    # ---------- 阶段 3：并行抢流 ----------
    print("\n[阶段3] 候选 CDN 并行竞速 —— 谁先给字节就用谁")
    hdr = {"User-Agent": UA, "Referer": "https://live.bilibili.com/",
           "Accept": "*/*"}
    results = []
    lock = threading.Lock()
    winner = {"t": None, "host": None}

    def race(idx, url):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers=hdr)
            resp = DIRECT.open(req, timeout=20)
            first = resp.read(16384)
            dt = time.time() - t0
            host = url.split("/")[2]
            resp.close()
            with lock:
                results.append((idx, host, dt, len(first)))
                if first and (winner["t"] is None or dt < winner["t"]):
                    winner["t"] = dt
                    winner["host"] = host
        except Exception as e:
            with lock:
                results.append((idx, url.split("/")[2], time.time() - t0,
                                f"{type(e).__name__}"))

    t0 = time.time()
    ths = [threading.Thread(target=race, args=(i, u[2]), daemon=True)
           for i, u in enumerate(flv)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=25)
    wall = time.time() - t0

    for idx, host, dt, n in sorted(results):
        ok = isinstance(n, int) and n > 0
        print(f"  [{idx:2}] {host[:30]:<30} {dt:5.2f}s  "
              f"{'首包 '+str(n)+'B' if ok else n}")
    print(f"\n  并行墙钟 {wall:.2f}s")
    if winner["t"] is not None:
        print(f"  最快可用: {winner['host']}  {winner['t']:.2f}s")

    # ---------- 汇总 ----------
    print("\n" + "=" * 74)
    print("汇总：首播延迟构成")
    print("=" * 74)
    total = t_phase1 + t_phase2 + (winner["t"] or 0)
    print(f"  阶段1 并行元数据      {t_phase1:5.2f}s")
    print(f"  阶段2 拿流地址        {t_phase2:5.2f}s")
    print(f"  阶段3 抢到可用流      {winner['t'] or 0:5.2f}s")
    print(f"  ------------------------------------")
    print(f"  拿到可用流总计        {total:5.2f}s")
    print(f"\n  当前实现（串行 + 走代理）实测约 20.5s")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
