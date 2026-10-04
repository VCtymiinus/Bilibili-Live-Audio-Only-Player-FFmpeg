"""探测 D：用断言把 URL 拼接和抓取逐层打出来，定位 https:// 前缀丢失的原因。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import UA, api, get_bytes  # noqa: E402

ROOM = 22388070


def build_url(codec, idx=0):
    info = codec["url_info"][idx]
    base, host, extra = codec["base_url"], info["host"], info["extra"]
    print(f"    [debug] base = {base!r}")
    print(f"    [debug] host = {host!r}")
    print(f"    [debug] extra[:60] = {extra[:60]!r}")
    if not host.startswith("http"):
        host = "https://" + host
    sep = "" if base.endswith(("?", "&")) else ("&" if "?" in base else "?")
    full = base + sep + host + ("&" + extra if extra else "")
    print(f"    [debug] sep = {sep!r}")
    print(f"    [debug] full[:110] = {full[:110]!r}")
    return full


def main():
    params = {"room_id": ROOM, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
              "qn": "10000", "platform": "web", "only_audio": "1", "only_video": "0"}
    r = api("/xlive/web-room/v2/index/getRoomPlayInfo", params)
    streams = r["data"]["playurl_info"]["playurl"]["stream"]

    codec = streams[0]["format"][0]["codec"][0]
    url = build_url(codec)

    print(f"\n  [断言] url 以 http 开头? {url.startswith('http')}")
    print(f"  [断言] url 里有 '//'? {'//' in url}")
    print(f"  [断言] url 类型 = {type(url)}")
    print(f"  [断言] url repr 前 130 = {url[:130]!r}")

    # 直接看底层 Request 造出来的东西
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    print(f"  [断言] Request.full_url 前 130 = {req.full_url[:130]!r}")
    print(f"  [断言] Request.type = {req.type!r}")

    print("\n  --- 尝试抓取 ---")
    try:
        status, headers, data = get_bytes(url, timeout=25, max_bytes=131072)
        print(f"  http={status} bytes={len(data)} magic={data[:8].hex(' ')}")
    except Exception as e:
        print(f"  失败: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
