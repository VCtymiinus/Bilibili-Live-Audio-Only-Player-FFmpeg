"""ADTS 头的字节级自检 —— 不依赖网络、ffmpeg 或声卡。

ADTS 头拼错的话解码器会静默丢帧，很难排查，所以这里按位核对。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.flvdemux import (FlvDemuxer, build_adts_header,  # noqa: E402
                               parse_audio_specific_config)

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: {got!r}"
          + ("" if ok else f"  期望 {want!r}"))
    if not ok:
        FAILS.append(name)


def main():
    print("=" * 68)
    print("1. build_adts_header 位布局核对")

    # AAC-LC(2) / 48000Hz(index 3) / 2ch / 载荷 100 字节
    h = build_adts_header(100, audio_object_type=2, sample_rate=48000, channels=2)
    check("长度", len(h), 7)
    check("同步字 0xFFF", (h[0] << 4) | (h[1] >> 4), 0xFFF)
    check("ID=0 (MPEG-4)", (h[1] >> 3) & 0x01, 0)
    check("layer=00", (h[1] >> 1) & 0x03, 0)
    check("protection_absent=1", h[1] & 0x01, 1)
    check("profile=AOT-1=1", (h[2] >> 6) & 0x03, 1)
    check("采样率索引=3 (48k)", (h[2] >> 2) & 0x0F, 3)
    check("声道配置高1位", h[2] & 0x01, 0)          # 2ch -> 10b, 高位 1? 见下
    check("声道配置", ((h[2] & 0x01) << 2) | ((h[3] >> 6) & 0x03), 2)
    aac_len = ((h[3] & 0x03) << 11) | (h[4] << 3) | (h[5] >> 5)
    check("帧长度字段 = 载荷+7", aac_len, 107)

    print("\n2. 不同采样率 / 声道的索引映射")
    for rate, want_idx in ((96000, 0), (48000, 3), (44100, 4), (32000, 5),
                          (16000, 8), (8000, 11)):
        hh = build_adts_header(50, 2, rate, 2)
        check(f"{rate}Hz -> 索引", (hh[2] >> 2) & 0x0F, want_idx)

    print("\n3. parse_audio_specific_config 对 ASC=0x11 0x90 的解析")
    # 0x1190 = 0001 0001 1001 0000
    #   AOT  = 00010 = 2 (AAC-LC)
    #   freq = 0011  = 3 -> 48000Hz
    #   chan = 0010  = 2
    aot, rate, ch = parse_audio_specific_config(bytes((0x11, 0x90)))
    check("audioObjectType", aot, 2)
    check("sample_rate", rate, 48000)
    check("channels", ch, 2)

    print("\n4. 纯音频 FLV 解复用（手工构造一个最小 FLV）")
    # 构造: FLV头 + [AAC 序列头 tag] + [一个 AAC 裸帧 tag] + [一个视频 tag]
    import struct

    def make_tag(tag_type, payload, ts=0):
        body = bytes((tag_type,)) + len(payload).to_bytes(3, "big") \
               + ts.to_bytes(3, "big") + b"\x00" + b"\x00\x00\x00" + payload
        return body + (len(payload) + 11).to_bytes(4, "big")

    flv = b"FLV" + bytes((1, 0x04)) + (9).to_bytes(4, "big") + (0).to_bytes(4, "big")
    sequ = make_tag(8, bytes((0xAF, 0x00, 0x11, 0x90)), 0)
    raw = make_tag(8, bytes((0xAF, 0x01)) + b"\xAA" * 20, 10)
    vid = make_tag(9, b"\x17\x00" + b"\xBB" * 10, 5)
    dm = FlvDemuxer()
    dm.feed(flv + sequ + raw + vid)
    frames = dm.frames()
    check("has_audio", dm.info.has_audio, True)
    check("has_video", dm.info.has_video, False)
    check("解出音频帧数", len(frames), 1)
    check("解析出采样率", dm.info.sample_rate, 48000)
    check("解析出声道", dm.info.channels, 2)
    if frames:
        check("ADTS 前缀正确", frames[0].adts[:2], b"\xff\xf1")
        check("ADTS+载荷长度", len(frames[0].adts), len(frames[0].aac) + 7)
        check("裸帧内容", frames[0].aac, b"\xAA" * 20)
    check("视频 tag 被计数", dm.stats_video_tags, 1)
    check("音频 tag 被计数", dm.stats_audio_tags, 2)

    print("\n5. 分片投喂（模拟网络 chunk 边界切在 tag 中间）")
    dm2 = FlvDemuxer()
    blob = flv + sequ + raw
    for i in range(0, len(blob), 3):     # 每次只喂 3 字节，强制跨边界
        dm2.feed(blob[i:i + 3])
    check("分片投喂仍解出 1 帧", len(dm2.frames()), 1)

    print("\n" + "=" * 68)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        return 1
    print("全部通过 —— ADTS 拼装与解复用逻辑正确")
    return 0


if __name__ == "__main__":
    sys.exit(main())
