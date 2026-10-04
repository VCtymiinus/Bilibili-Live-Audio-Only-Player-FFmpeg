"""验证：对真实纯音频流跑一遍解复用器，确认能正确抠出 AAC 帧。

这个验证**不需要声卡**，所以能在受限环境里跑通 —— 它证明的是「拿到音频数据」
这一半链路成立。出声那一半得由用户在有桌面的环境确认。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.biliapi import BiliLiveClient, BiliApiError  # noqa: E402
from bililive.flvdemux import FlvDemuxer  # noqa: E402

ROOM = int(sys.argv[1]) if len(sys.argv) > 1 else 22388070
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
CHUNK = 16384


def main():
    client = BiliLiveClient()
    print("=" * 72)

    info = client.resolve_room(ROOM)
    room_id = info["room_id"]
    print(f"房间 {ROOM} -> room_id={room_id}  live_status={info.get('live_status')}")
    if info.get("live_status") != 1:
        print("!! 该房间当前未开播，换一个房间号再试")
        return 2

    meta = client.room_info(room_id)
    if meta:
        title = (meta.get("room_info") or {}).get("title")
        uname = (meta.get("anchor_info") or {}).get("base_info", {}).get("uname")
        print(f"标题: {title}")
        print(f"主播: {uname}")

    stream = client.best_audio_stream(room_id)
    print(f"\n选中流: protocol={stream.protocol} format={stream.format} "
          f"codec={stream.codec} qn={stream.current_qn}")
    print(f"  音频编码 = {stream.audio_codec}")
    print(f"  URL 剩余有效 {stream.seconds_left / 60:.1f} 分钟")
    print(f"  URL = {stream.url[:130]}...")

    print(f"\n开始读取 {SECONDS} 秒音频，chunk={CHUNK} 字节 ...")
    dm = FlvDemuxer()
    import urllib.request
    import ssl

    req = urllib.request.Request(stream.url, headers=client.headers())
    frames_total = 0
    frames_bytes = 0
    first_ts = last_ts = None
    t0 = time.time()

    with urllib.request.urlopen(req, timeout=20,
                               context=ssl.create_default_context()) as resp:
        print(f"  HTTP {resp.status}  Content-Type={resp.headers.get('Content-Type')}")
        while time.time() - t0 < SECONDS:
            chunk = resp.read(CHUNK)
            if not chunk:
                print("  !! 流被服务端关闭")
                break
            dm.feed(chunk)
            for fr in dm.frames():
                frames_total += 1
                frames_bytes += len(fr.aac)
                if first_ts is None:
                    first_ts = fr.timestamp
                last_ts = fr.timestamp

    elapsed = time.time() - t0
    print("\n" + "-" * 72)
    print(f"实际耗时        : {elapsed:.2f} 秒")
    print(f"FLV 头部        : audio={dm.info.has_audio} video={dm.info.has_video}")
    print(f"音频参数        : {dm.info.describe()}")
    print(f"ASC 原始字节    : {dm.info.asc.hex(' ')}")
    print(f"tag 统计        : audio={dm.stats_audio_tags} video={dm.stats_video_tags} "
          f"other={dm.stats_other_tags}")
    print(f"AAC 帧          : {frames_total} 帧 / {frames_bytes} 字节")
    print(f"时间戳范围      : {first_ts} .. {last_ts} ms "
          f"(跨度 {(last_ts - first_ts) if first_ts is not None else 0} ms)")
    print(f"入站字节        : {dm.stats_bytes_in}")
    print(f"实测码率        : {dm.bitrate_kbps(elapsed):.1f} kbps")
    if frames_total == 0:
        print("  !! 一帧都没抠出来，解复用器有问题")
    else:
        avg = frames_bytes / frames_total
        print(f"平均帧大小      : {avg:.1f} 字节")

    print("-" * 72)
    ok = (frames_total > 0 and dm.info.has_audio and not dm.info.has_video
          and dm.stats_video_tags == 0)
    if ok:
        print("结论: 纯音频链路成立 —— 无需 ffmpeg，可自行解复用出 AAC")
    else:
        print("结论: 需要复查（见上面的统计）")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
