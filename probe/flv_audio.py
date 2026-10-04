"""探测 C（修正版）：拉真实 FLV，验证能否手工抽出 AAC 音频帧。

用 _common.join_stream_url（已修正为 host + path + ? + base_query + & + extra）。
"""

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import api, get_bytes, join_stream_url  # noqa: E402

ROOM = 22388070


def parse_flv(data):
    if data[:3] != b"FLV":
        return None
    flags = data[4]
    header_size = struct.unpack(">I", data[5:9])[0]
    pos = header_size + 4

    a_cnt = v_cnt = aac_headers = 0
    samples = []
    while pos + 11 <= len(data):
        tag_type = data[pos]
        data_size = int.from_bytes(data[pos + 1:pos + 4], "big")
        body = pos + 11
        if body + data_size > len(data):
            break
        if tag_type == 8:
            a_cnt += 1
            pkt = data[body:body + data_size]
            if pkt and pkt[0] >> 4 == 10:
                t = pkt[1]
                if t == 0:
                    aac_headers += 1
                if len(samples) < 4:
                    samples.append((t, len(pkt) - 2, pkt[2:8].hex(" ")))
        elif tag_type == 9:
            v_cnt += 1
        pos = body + data_size + 4
    return {"has_audio": bool(flags & 0x04), "has_video": bool(flags & 0x01),
            "audio_tags": a_cnt, "video_tags": v_cnt,
            "aac_seq_headers": aac_headers, "samples": samples}


def main():
    params = {"room_id": ROOM, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
              "qn": "10000", "platform": "web", "only_audio": "1", "only_video": "0"}
    r = api("/xlive/web-room/v2/index/getRoomPlayInfo", params)
    streams = r["data"]["playurl_info"]["playurl"]["stream"]

    for stream in streams:
        proto = stream["protocol_name"]
        for fmt in stream["format"]:
            for codec in fmt["codec"]:
                url = join_stream_url(codec)
                print(f"\n{'=' * 72}")
                print(f"protocol={proto} format={fmt['format_name']} codec={codec['codec_name']}")
                print(f"URL = {url[:160]}")
                try:
                    status, headers, data = get_bytes(url, timeout=25, max_bytes=262144)
                except Exception as e:
                    print(f"  抓取失败: {type(e).__name__}: {e}")
                    continue
                print(f"  http={status} ctype={headers.get('Content-Type')} "
                      f"bytes={len(data)} magic={data[:8].hex(' ')}")

                if data[:7] == b"#EXTM3U":
                    print("  -> HLS 播放列表:")
                    for line in data.decode("utf-8", "replace").splitlines()[:12]:
                        print(f"     {line}")
                    continue

                info = parse_flv(data)
                if info is None:
                    print("  -> 不是 FLV")
                    continue
                print(f"  FLV has_audio={info['has_audio']} has_video={info['has_video']}")
                print(f"  tags: audio={info['audio_tags']} video={info['video_tags']} "
                      f"AAC序列头={info['aac_seq_headers']}")
                print(f"  前几个AAC帧 (类型,载荷长,前6字节): {info['samples']}")
                if info["audio_tags"]:
                    print(f"  => 音频帧可取；视频:音频 ≈ "
                          f"{info['video_tags'] / max(1, info['audio_tags']):.2f}")
    print("=" * 72)


if __name__ == "__main__":
    main()
