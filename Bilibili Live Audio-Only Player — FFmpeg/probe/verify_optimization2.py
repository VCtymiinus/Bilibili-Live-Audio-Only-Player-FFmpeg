"""完整测量：优化后从「启动」到「首个音频字节」的总耗时。

自动挑一个正在直播的房间，跑完整链路：
  并行(room_init + buvid) -> getRoomPlayInfo -> 并行抢流 -> 首字节
对比当前实现（串行 API + 走系统代理）。
"""

import gzip
import json
import ssl
import sys
import threading
import time
import urllib.request

HOST = "api.live.bilibili.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
VIA_PROXY = urllib.request.build_opener()          # 默认 = 走系统代理


def api(path, params=None, opener=DIRECT, timeout=15):
    url = f"https://{HOST}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://live.bilibili.com/",
        "Accept": "*/*", "Accept-Encoding": "gzip"})
    with opener.open(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", "replace"))


def pick_live_room():
    r = api("/room/v1/room/get_user_recommend", {"page": "1", "page_size": "8"})
    data = r.get("data")
    if not isinstance(data, list):
        return None
    for d in data:
        if isinstance(d, dict) and d.get("roomid"):
            rid = int(d["roomid"])
            info = api("/room/v1/Room/room_init", {"id": rid})
            di = info.get("data") or {}
            if di.get("live_status") == 1:
                return di["room_id"], d.get("title", "")
    return None


def collect_urls(pi):
    urls = []
    for st in pi["playurl"]["stream"]:
        for f in st["format"]:
            for cc in f["codec"]:
                for u in cc.get("url_info") or []:
                    part, _, bq = cc["base_url"].partition("?")
                    full = u["host"].rstrip("/") + "/" + part.lstrip("/")
                    q = "&".join(p for p in (bq, u.get("extra") or "") if p)
                    if q:
                        full += "?" + q
                    urls.append((st["protocol_name"], f["format_name"], full))
    return urls


def main():
    print("=" * 74)
    print("优化后首播延迟：完整链路实测")
    print("=" * 74)

    found = pick_live_room()
    if not found:
        print("!! 没找到在播房间")
        return 1
    rid, title = found
    print(f"在播房间 room_id={rid}  {title}")
    hdr = {"User-Agent": UA, "Referer": "https://live.bilibili.com/",
           "Accept": "*/*"}

    # ============ A. 当前实现：串行 + 走系统代理 ============
    print("\n" + "-" * 74)
    print("A. 当前实现（串行 API + 走系统代理）")
    print("-" * 74)
    t_start = time.time()
    t0 = time.time()
    api("/room/v1/Room/room_init", {"id": rid}, opener=VIA_PROXY)
    t_resolve = time.time() - t0
    print(f"  1. resolve_room             {t_resolve:5.2f}s")

    t0 = time.time()
    try:
        api("/xlive/web-room/v1/index/getInfoByRoom", {"room_id": rid},
            opener=VIA_PROXY)
    except Exception:
        pass
    t_info = time.time() - t0
    print(f"  2. room_info（仅显示用）     {t_info:5.2f}s")

    t0 = time.time()
    r = api("/xlive/web-room/v2/index/getRoomPlayInfo", {
        "room_id": rid, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
        "qn": "10000", "platform": "web", "only_audio": "1", "only_video": "0"},
        opener=VIA_PROXY)
    t_play = time.time() - t0
    print(f"  3. getRoomPlayInfo          {t_play:5.2f}s")
    pi = (r.get("data") or {}).get("playurl_info")

    t_race = None
    if pi:
        flv = [u for u in collect_urls(pi) if u[1] == "flv"]
        if flv:
            t0 = time.time()
            try:
                req = urllib.request.Request(flv[0][2], headers=hdr)
                resp = VIA_PROXY.open(req, timeout=30)
                resp.read(16384)
                resp.close()
            except Exception as e:
                print(f"     首条流异常 {type(e).__name__}")
            t_race = time.time() - t0
            print(f"  4. 首条 CDN 建连+首包      {t_race:5.2f}s")
    t_A = time.time() - t_start
    print(f"  ------------------------------------")
    print(f"  合计到可用流                {t_A:5.2f}s")

    # ============ B. 优化后：并行 + 直连 ============
    print("\n" + "-" * 74)
    print("B. 优化后（API 直连 + 并行元数据 + 并行抢流）")
    print("-" * 74)
    t_start = time.time()
    box = {}

    def t_room():
        box["room"] = api("/room/v1/Room/room_init", {"id": rid})

    def t_buvid():
        try:
            box["buvid"] = api("https://api.bilibili.com/x/frontend/finger/spi"
                               .replace(f"https://{HOST}", ""))
        except Exception:
            box["buvid"] = None

    ths = [threading.Thread(target=t_room), threading.Thread(target=t_buvid)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    t_meta = time.time() - t_start
    print(f"  1. room_init + buvid 并行   {t_meta:5.2f}s")

    t0 = time.time()
    r = api("/xlive/web-room/v2/index/getRoomPlayInfo", {
        "room_id": rid, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
        "qn": "10000", "platform": "web", "only_audio": "1", "only_video": "0"})
    t_play2 = time.time() - t0
    print(f"  2. getRoomPlayInfo          {t_play2:5.2f}s")
    pi2 = (r.get("data") or {}).get("playurl_info")

    win_t = None
    if pi2:
        flv2 = [u for u in collect_urls(pi2) if u[1] == "flv"]
        print(f"     候选 {len(flv2)} 条 flv，并行竞速 ...")
        lock = threading.Lock()
        win = {"t": None, "host": None, "n": 0}
        done = threading.Event()

        def race(url):
            t0 = time.time()
            try:
                req = urllib.request.Request(url, headers=hdr)
                resp = DIRECT.open(req, timeout=20)
                first = resp.read(16384)
                dt = time.time() - t0
                resp.close()
                if first:
                    with lock:
                        if win["t"] is None or dt < win["t"]:
                            win.update(t=dt, host=url.split("/")[2], n=len(first))
                    done.set()
            except Exception:
                pass

        threads = [threading.Thread(target=race, args=(u[2],), daemon=True)
                   for u in flv2]
        for t in threads:
            t.start()
        done.wait(timeout=20)
        for t in threads:
            t.join(timeout=0.5)
        win_t = win["t"]
        if win_t:
            print(f"  3. 抢到可用流              {win_t:5.2f}s"
                  f"   ({win['host']}, 首包 {win['n']}B)")
    t_B = time.time() - t_start
    print(f"  ------------------------------------")
    print(f"  合计到可用流                {t_B:5.2f}s")

    # ============ 对比 ============
    print("\n" + "=" * 74)
    print("对比")
    print("=" * 74)
    print(f"  A 当前实现   {t_A:6.2f}s")
    print(f"  B 优化后     {t_B:6.2f}s")
    if t_B > 0:
        print(f"  => 快了 {t_A/t_B:.1f} 倍，减少 {t_A-t_B:.1f}s")
    print("\n  注意：B 还没算 ffplay 自身的启动与探测缓冲，")
    print("        那部分由 -probesize / -analyzeduration 控制。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
