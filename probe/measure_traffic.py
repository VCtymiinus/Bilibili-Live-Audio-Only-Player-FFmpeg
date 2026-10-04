"""实测：只听音频 vs 看直播，到底差多少流量？

用户的核心诉求是省流量，所以这个必须实测，不能靠猜。
方法：同一个房间，分别拉「纯音频流(only_audio=1)」和「完整视频流」，
各计 10 秒接收字节数，算 kbps，再外推日/月用量。
"""

import os
import ssl
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.biliapi import BiliLiveClient, join_stream_url  # noqa: E402
from bililive.flvdemux import FlvDemuxer                     # noqa: E402

ROOM = int(sys.argv[1]) if len(sys.argv) > 1 else 26774400
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0


def measure(url, headers, seconds, label):
    """计一段时间内实际收到的字节数。"""
    dm = FlvDemuxer()
    total = 0
    t0 = time.time()
    first = None
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=30,
                                     context=ssl.create_default_context())
    except Exception as e:
        print(f"  [{label}] 建连失败 {type(e).__name__}: {str(e)[:60]}")
        return None
    with resp:
        while time.time() - t0 < seconds:
            try:
                chunk = resp.read(65536)
            except (TimeoutError, OSError):
                continue
            if not chunk:
                print(f"  [{label}] 流被关闭")
                break
            if first is None:
                first = time.time() - t0
            total += len(chunk)
            dm.feed(chunk)
            dm.frames()
    dt = time.time() - t0
    kbps = total * 8 / dt / 1000
    print(f"  [{label}] {dt:.1f}s 收到 {total} 字节 = {kbps:.0f} kbps"
          f"   首包 {first or 0:.1f}s")
    print(f"      FLV: audio_tag={dm.stats_audio_tags} "
          f"video_tag={dm.stats_video_tags}  {dm.info.describe()}")
    return {"bytes": total, "sec": dt, "kbps": kbps,
            "audio_tags": dm.stats_audio_tags,
            "video_tags": dm.stats_video_tags}


def human(kbps):
    """kbps -> 每小时 / 每天 / 每月 的流量"""
    per_hour = kbps * 1000 / 8 * 3600 / 1048576      # MB
    return (f"{per_hour:.0f} MB/小时   {per_hour*24/1024:.2f} GB/天   "
            f"{per_hour*24*30/1024:.0f} GB/月")


def main():
    print("=" * 74)
    c = BiliLiveClient()
    info = c.resolve_room(ROOM)
    rid = info["room_id"]
    print(f"房间 {ROOM} -> {rid}  live_status={info.get('live_status')}")
    if info.get("live_status") != 1:
        print("!! 未开播")
        return 2
    h = c.headers()

    # ---- 1. 纯音频流 ----
    print(f"\n[1] 纯音频流（only_audio=1）—— 本工具用的就是这条")
    audio_streams = c.audio_streams(rid)
    a = None
    for s in audio_streams:
        if s.format == "flv":
            a = measure(s.url, h, SECONDS, f"audio {s.format}")
            if a and a["video_tags"] == 0:
                break

    # ---- 2. 完整视频流（模拟"点开直播间看"） ----
    print(f"\n[2] 完整视频流（不带 only_audio）—— 模拟浏览器看直播")
    params = {"room_id": rid, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
              "qn": "10000", "platform": "web"}
    import urllib.parse
    url = ("https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?"
           + urllib.parse.urlencode(params))
    _s, _hd, raw = c._get(url)
    import json
    r = json.loads(raw.decode("utf-8", "replace"))
    pi = (r.get("data") or {}).get("playurl_info")
    v = None
    if pi:
        for stream in pi["playurl"]["stream"]:
            for fmt in stream["format"]:
                for codec in fmt["codec"]:
                    u = join_stream_url(codec, 0)
                    v = measure(u, h, SECONDS,
                                f"video {fmt['format_name']}/{codec['codec_name']}")
                    if v:
                        break
                if v:
                    break
            if v:
                break
    else:
        print("  拿不到视频流信息")

    # ---- 3. 对比 ----
    print("\n" + "=" * 74)
    print("对比（按实测码率外推）")
    print("-" * 74)
    if a:
        print(f"纯音频   {a['kbps']:6.0f} kbps   {human(a['kbps'])}")
    if v:
        print(f"看视频   {v['kbps']:6.0f} kbps   {human(v['kbps'])}")
    if a and v and v["kbps"] > 0:
        ratio = a["kbps"] / v["kbps"]
        print("-" * 74)
        print(f"音频 / 视频 = {ratio*100:.1f}%   即省下 {(1-ratio)*100:.0f}% 流量")
        if ratio > 0.9:
            print("!! 注意：两者几乎一样，说明这条「纯音频流」实际上仍在传视频数据")
        elif ratio > 0.5:
            print("=> 省流量有限，主要在省 CPU/内存，而不是省流量")
        else:
            print("=> 确实显著省流量")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
