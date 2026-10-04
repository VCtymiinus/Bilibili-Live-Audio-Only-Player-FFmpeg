"""探测 B：getRoomPlayInfo 到底给不给「纯音频流」。

这是整个方案的分叉点：
  * 若 only_audio=1 能返回独立音频流 -> 无需 ffmpeg，链路极简。
  * 若只能拿到合并流       -> 必须引入 ffmpeg 抽音轨。

已踩过的坑（勿重蹈）：
  * /xlive/web-interface/v1/second/getList 不带 buvid3 cookie 会返回 code=-352（风控）。
  * /room/v1/room/get_user_recommend 的 data 是 **list**，不是 dict。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import UA, api, get_bytes, get_json, join_stream_url  # noqa: E402


def obtain_buvid():
    """取 buvid3 cookie —— 这是绕过 -352 风控的关键。"""
    for url in (
        "https://api.bilibili.com/x/frontend/finger/spi",
        "https://www.bilibili.com/",
    ):
        try:
            if "spi" in url:
                _s, payload = get_json(url, referer="https://www.bilibili.com/")
                data = payload.get("data") or {}
                b3 = data.get("b_3")
                if b3:
                    return f"buvid3={b3}"
            else:
                _s, headers, _b = get_bytes(url, referer="https://www.bilibili.com/",
                                            max_bytes=1)
                for ck in headers.get("Set-Cookie", "").split(","):
                    if "buvid3=" in ck:
                        return ck.strip().split(";")[0]
        except Exception as e:
            print(f"    buvid 尝试失败 {url}: {type(e).__name__} {e}")
    return None


def norm_list(data):
    """data 可能是 dict{list:[...]} 也可能是裸 list。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("list", "room_list", "rooms"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def find_live_rooms(cookie, limit=8):
    candidates = [
        ("/xlive/web-interface/v1/second/getList",
         {"platform": "web", "parent_area_id": "1", "area_id": "0",
          "sort_type": "online", "page": "1"}),
        ("/room/v1/room/get_user_recommend", {"page": "1", "page_size": str(limit)}),
        ("/xlive/web-interface/v1/index/getList",
         {"platform": "web", "parent_area_id": "1", "area_id": "0",
          "sort_type": "online", "page": "1"}),
    ]
    for path, params in candidates:
        try:
            r = api(path, params, cookie=cookie)
        except Exception as e:
            print(f"  [{path}] 异常 {type(e).__name__}: {e}")
            continue
        if r.get("code") != 0:
            print(f"  [{path}] code={r.get('code')} msg={r.get('message') or r.get('msg')}")
            continue
        rooms = norm_list(r.get("data"))
        ids = []
        for d in rooms:
            if not isinstance(d, dict):
                continue
            rid = d.get("roomid") or d.get("room_id") or d.get("id")
            if rid:
                ids.append((int(rid), str(d.get("title", ""))[:26],
                            str(d.get("uname", ""))))
        if ids:
            print(f"  [{path}] 拿到 {len(ids)} 个房间")
            return ids[:limit]
        print(f"  [{path}] code=0 但列表为空")
    return []


def sniff_container(url, cookie, nbytes=65536):
    try:
        status, headers, data = get_bytes(url, cookie=cookie, timeout=20,
                                         max_bytes=nbytes)
    except Exception as e:
        return "抓取失败", f"{type(e).__name__}: {e}", {}
    magic = data[:16]
    if data[:3] == b"FLV":
        kind = "FLV"
    elif data[:4] == b"\x1a\x45\xdf\xa3":
        kind = "Matroska/WebM"
    elif data[:4] == b"fLaC":
        kind = "FLAC(纯音频)"
    elif data[:4] == b"OggS":
        kind = "Ogg(纯音频)"
    elif data[4:8] == b"ftyp":
        kind = f"MP4/M4A(纯音频?) brand={data[8:12].decode('ascii','replace')}"
    elif data[:7] == b"#EXTM3U":
        kind = "HLS 播放列表(m3u8)"
    elif data[:1] == b"<":
        kind = "XML/文本"
    else:
        kind = "未知"
    return kind, "", {"status": status, "ctype": headers.get("Content-Type"),
                      "magic": magic.hex(" "),
                      "clen": headers.get("Content-Length")}


def main():
    out = []
    p = out.append

    p("=" * 72)
    p("步骤 1: 取 buvid3 cookie（治 -352 风控）")
    cookie = obtain_buvid()
    p(f"    cookie = {cookie}")

    p("\n步骤 2: 找正在直播的房间")
    rooms = find_live_rooms(cookie)
    if not rooms:
        p("    !! 列表接口全军覆没，改用兜底房间号")
        rooms = [(5440, "兜底 room1", ""), (6, "兜底 room6", ""),
                 (21452505, "兜底", "")]
    for rid, title, uname in rooms:
        p(f"    {rid:>10}  {uname}  {title}")

    p("\n步骤 3: room_init 换真实 room_id")
    target = None
    for rid, title, uname in rooms:
        try:
            info = api("/room/v1/Room/room_init", {"id": rid}, cookie=cookie)["data"]
        except Exception as e:
            p(f"    {rid} 异常 {e}")
            continue
        live = info.get("live_status")
        p(f"    {rid} -> room_id={info.get('room_id')} live_status={live}"
          f" {'*** 直播中 ***' if live == 1 else '(未开播)'}")
        if live == 1 and target is None:
            target = info["room_id"]
    if target is None:
        p("\n!! 候选房间全部未开播，无法验证流地址")
        p("=" * 72)
        print("\n".join(out))
        return 2

    p(f"\n步骤 4: getRoomPlayInfo(only_audio=1)  room_id={target}")
    params = {"room_id": target, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
              "qn": "10000", "platform": "web", "only_audio": "1", "only_video": "0"}
    r = api("/xlive/web-room/v2/index/getRoomPlayInfo", params, cookie=cookie)
    p(f"    code={r.get('code')} message={r.get('message') or r.get('msg')}")
    if r.get("code") != 0:
        p("=" * 72)
        print("\n".join(out))
        return 3

    pi = (r.get("data") or {}).get("playurl_info")
    if not pi:
        p("    playurl_info 为 null")
        p("=" * 72)
        print("\n".join(out))
        return 3

    found = []
    for stream in pi["playurl"]["stream"]:
        p(f"\n  protocol = {stream['protocol_name']}")
        for fmt in stream["format"]:
            for codec in fmt["codec"]:
                url = join_stream_url(codec)
                found.append((stream["protocol_name"], fmt["format_name"],
                              codec["codec_name"], codec["current_qn"],
                              codec["accept_qn"], url))
                p(f"    format={fmt['format_name']:<5} codec={codec['codec_name']:<5} "
                  f"qn={codec['current_qn']:<6} accept={codec['accept_qn']}")
                p(f"      {url[:110]}...")

    p("\n步骤 5: 嗅探每个流真实容器（各抓 64KB）")
    for proto, fname, cname, qn, _acc, url in found:
        kind, note, meta = sniff_container(url, cookie)
        p(f"    {proto}/{fname}/{cname} qn={qn}")
        p(f"      容器={kind}  http={meta.get('status')} ctype={meta.get('ctype')}")
        p(f"      magic={meta.get('magic')} {note}")

    p("=" * 72)
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
