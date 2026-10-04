"""检查指定房间是否可用，并逐条试读候选流。"""

import os
import ssl
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.biliapi import BiliLiveClient  # noqa: E402
from bililive.flvdemux import FlvDemuxer     # noqa: E402

ROOM = int(sys.argv[1]) if len(sys.argv) > 1 else 26774400
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0


def main():
    c = BiliLiveClient()
    info = c.resolve_room(ROOM)
    rid = info["room_id"]
    print(f"房间 {ROOM} -> room_id={rid}  live_status={info.get('live_status')}")

    meta = c.room_info(rid)
    ri = meta.get("room_info") or {}
    ai = (meta.get("anchor_info") or {}).get("base_info") or {}
    print(f"  标题: {ri.get('title')}")
    print(f"  主播: {ai.get('uname')}   分区: {ri.get('area_name')}")
    if info.get("live_status") != 1:
        print("!! 该房间当前未开播")
        return 2

    cands = c.audio_streams(rid)
    print(f"\n共 {len(cands)} 条候选流")
    for i, s in enumerate(cands):
        print(f"  [{i}] {s.protocol}/{s.format} host={s.url.split('/')[2]}")

    print(f"\n--- 逐条试读 {SECONDS:.0f} 秒 ---")
    for i, s in enumerate(cands):
        dm = FlvDemuxer()
        t0 = time.time()
        try:
            req = urllib.request.Request(s.url, headers=c.headers())
            resp = urllib.request.urlopen(req, timeout=30,
                                         context=ssl.create_default_context())
            t_conn = time.time() - t0
            nb = 0
            first = None
            eof = False
            with resp:
                tl = time.time()
                while time.time() - tl < SECONDS:
                    ch = resp.read(16384)
                    if not ch:
                        eof = True
                        break
                    if first is None:
                        first = time.time() - t0
                    nb += len(ch)
                    dm.feed(ch)
                    dm.frames()
            tag = "EOF" if eof else "OK "
            print(f"  [{i}] {tag} 建连{t_conn:5.1f}s 首包{(first or 0):5.1f}s "
                  f"收{nb:>8}B audio={dm.stats_audio_tags:>4} "
                  f"video={dm.stats_video_tags} {dm.info.describe()}")
            if dm.stats_audio_tags > 0:
                print(f"\n*** 房间 {ROOM} 可用，host={s.url.split('/')[2]}")
                print(f"*** 流参数 {dm.info.describe()}  "
                      f"ASC={dm.info.asc.hex(' ')}")
                return 0
        except Exception as e:
            print(f"  [{i}] 异常 {type(e).__name__}: {str(e)[:70]}")
    print("\n!! 所有候选都读不到音频")
    return 1


if __name__ == "__main__":
    sys.exit(main())
