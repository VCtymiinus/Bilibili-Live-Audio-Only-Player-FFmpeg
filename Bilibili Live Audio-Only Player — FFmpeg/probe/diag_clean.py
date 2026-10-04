"""干净的诊断：房间是否还在播？单次读取到底拿到什么？

上一版诊断脚本自身有 bug：t_b 在 urlopen **之前**赋值，4 秒预算被建连吃光，
循环一次都没执行，所以「0 帧」是脚本的假象而非流的问题。这里分开计时。
"""

import os
import ssl
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.biliapi import BiliLiveClient  # noqa: E402
from bililive.flvdemux import FlvDemuxer     # noqa: E402


def check_room(c, room):
    info = c.resolve_room(room)
    rid = info["room_id"]
    meta = c.room_info(rid)
    title = (meta.get("room_info") or {}).get("title")
    uname = (meta.get("anchor_info") or {}).get("base_info", {}).get("uname")
    return rid, info.get("live_status"), title, uname


def try_stream(c, room, seconds=8.0):
    rid, live, title, uname = check_room(c, room)
    print(f"\n房间 {room} -> {rid}  live_status={live}  {uname} / {title}")
    if live != 1:
        print("  -> 未开播，跳过")
        return None

    cands = c.audio_streams(rid)
    print(f"  {len(cands)} 条候选")
    for i, s in enumerate(cands):
        host = s.url.split("/")[2]
        t_conn = t_first = None
        dm = FlvDemuxer()
        try:
            t0 = time.time()
            req = urllib.request.Request(s.url, headers=c.headers())
            resp = urllib.request.urlopen(req, timeout=30,
                                         context=ssl.create_default_context())
            t_conn = time.time() - t0
            t_loop = time.time()
            nbytes = 0
            hit_eof = False
            with resp:
                while time.time() - t_loop < seconds:
                    chunk = resp.read(16384)
                    if not chunk:
                        hit_eof = True
                        break
                    if t_first is None:
                        t_first = time.time() - t0
                    nbytes += len(chunk)
                    dm.feed(chunk)
                    dm.frames()
            if hit_eof:
                print(f"  [{i}] {s.protocol}/{s.format} "
                      f"{host[:26]:<26} 建连{t_conn:5.1f}s 读到EOF ({nbytes}B)")
            else:
                print(f"  [{i}] {s.protocol}/{s.format} {host[:26]:<26} "
                      f"建连{t_conn:5.1f}s 首包{t_first or 0:5.1f}s "
                      f"收{nbytes}B audio_tag={dm.stats_audio_tags} "
                      f"video_tag={dm.stats_video_tags} {dm.info.describe()}")
                if dm.stats_audio_tags > 0:
                    return s
        except Exception as e:
            print(f"  [{i}] {s.protocol}/{s.format} {host[:26]:<26} "
                  f"异常 {type(e).__name__}: {str(e)[:60]}")
    return None


def main():
    c = BiliLiveClient()
    # 先找一个确认在播的房间
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    rooms = []
    try:
        r = c._api("/room/v1/room/get_user_recommend",
                   {"page": "1", "page_size": "6"})
        data = r.get("data")
        if isinstance(data, list):
            rooms = [int(d["roomid"]) for d in data
                     if isinstance(d, dict) and d.get("roomid")]
    except Exception as e:
        print("取推荐房间失败:", e)

    print(f"候选房间: {rooms}")
    for room in rooms:
        try:
            got = try_stream(c, room)
            if got is not None:
                print(f"\n*** 可用: 房间 {room}  host={got.url.split('/')[2]}")
                return 0
        except Exception as e:
            print(f"  房间 {room} 处理异常: {type(e).__name__}: {e}")
    print("\n没有找到可用的流")
    return 1


if __name__ == "__main__":
    sys.exit(main())
