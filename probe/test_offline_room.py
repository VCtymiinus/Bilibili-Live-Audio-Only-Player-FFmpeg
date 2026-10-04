"""实测：直播间未开播时会发生什么。

只测「拿流」这一步（也就是 play.py 主循环里会走到的分支），
不会启动 ffplay，因此不会出声。

覆盖几种未开播情形：
  A. 房间真实存在但没开播
  B. 房间号根本不存在
  C. 房间被封禁/隐藏
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.biliapi import BiliApiError, BiliLiveClient  # noqa: E402

# 这些房间在探测期间都是未开播状态
CASES = [
    (1, "房间存在但长期未开播"),
    (6, "房间存在但未开播"),
    (999999999, "房间号不存在"),
    (26774400, "对照组：正在开播"),
]


def probe(c, room, label):
    print(f"\n{'=' * 70}")
    print(f"房间 {room}  —— {label}")
    print("-" * 70)
    t0 = time.time()
    try:
        info = c.resolve_room(room)
    except BiliApiError as e:
        print(f"  resolve_room 抛 BiliApiError: {e}")
        print(f"  耗时 {time.time()-t0:.2f}s")
        return "resolve_fail"
    except Exception as e:
        print(f"  resolve_room 抛 {type(e).__name__}: {e}")
        return "resolve_error"

    rid = info["room_id"]
    live = info.get("live_status")
    status_map = {0: "未开播", 1: "直播中", 2: "轮播/未开播"}
    print(f"  room_id={rid}  live_status={live} ({status_map.get(live, '?')})")

    try:
        cands = c.audio_streams(rid)
        print(f"  拿到 {len(cands)} 条候选流")
        print(f"  首条: {cands[0].format}/{cands[0].protocol} "
              f"剩余 {cands[0].seconds_left/60:.1f} 分钟")
        print(f"  ==> 会正常播放")
        return "ok"
    except BiliApiError as e:
        print(f"  audio_streams 抛 BiliApiError:")
        print(f"      {e}")
        print(f"  耗时 {time.time()-t0:.2f}s")
        print(f"  ==> play.py 会打印提示，然后等待 60 秒重试")
        return "offline"
    except Exception as e:
        print(f"  audio_streams 抛 {type(e).__name__}: {e}")
        return "error"


def main():
    c = BiliLiveClient()
    results = {}
    for room, label in CASES:
        results[room] = probe(c, room, label)

    print(f"\n{'=' * 70}")
    print("汇总")
    print("-" * 70)
    for room, label in CASES:
        print(f"  {room:<12} {results[room]:<14} {label}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
