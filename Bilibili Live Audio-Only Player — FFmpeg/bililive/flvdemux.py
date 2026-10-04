"""增量 FLV 解复用 —— 从直播流中抠出裸 AAC 帧。

为什么自己写而不用 ffmpeg：实测 B 站在 `only_audio=1` 下返回的 FLV 里
**只有 audio tag，video tag 为 0**，所以剥离容器这件事本身很简单，
不需要为了这一步引入一个几十 MB 的外部依赖。

FLV 结构回顾:
    'FLV' + version(1) + flags(1) + DataOffset(4) + PreviousTagSize0(4)
    flags bit0 = 有视频, bit2 = 有音频

    然后是一串 tag，每个 tag:
        TagType(1)  DataSize(3, 大端)  Timestamp(3) TimestampExt(1)
        StreamID(3)  Data(DataSize)  PreviousTagSize(4)
    TagType: 8=audio, 9=video, 18=script

    audio tag 的 Data:
        第 1 字节: SoundFormat 高 4 位 (10 = AAC) | SoundRate | SoundSize | SoundType
        若为 AAC，第 2 字节是 AACPacketType:
            0 = AudioSpecificConfig（解码器初始化信息，必须先拿到）
            1 = 裸 AAC 帧

设计要点:
  * 增量喂数据：网络 chunk 边界和 tag 边界不重合，必须缓存半截 tag。
  * 遇到半截 tag 就停下等更多数据，不能丢字节。
  * 视频 tag 直接跳过（B 站音频流里本来也没有，但别的流可能有，防御性处理）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

TAG_AUDIO = 8
TAG_VIDEO = 9
TAG_SCRIPT = 18

SOUND_FORMAT_AAC = 10
AAC_SEQUENCE_HEADER = 0
AAC_RAW = 1


@dataclass
class AudioFrame:
    aac: bytes          # 裸 AAC 帧（不含 FLV 头部）
    timestamp: int      # 毫秒
    keyframe: bool = False
    adts: bytes = b""   # 可选：带 ADTS 头的版本（下游解码器更稳）


def build_adts_header(aac_len: int, audio_object_type: int = 2,
                      sample_rate: int = 48000, channels: int = 2) -> bytes:
    """拼一个 7 字节 ADTS 头（无 CRC）。

    为什么需要它：从 FLV 里抠出来的 AAC 是**裸帧**，没有同步字。
    下游如果用 `ffmpeg -f aac` 这类按 ADTS 解析的解码器，裸帧可能被当成
    无效数据丢弃；补上 ADTS 头就能稳定解析。

    位布局（共 56 bit）:
        syncword 12bit = 0xFFF | ID 1bit | layer 2bit | protection_absent 1bit
        profile 2bit (= audioObjectType - 1) | sampling_freq_index 4bit
        private 1bit | channel_config 3bit
        ... 后面是帧长度等信息
    """
    profile = max(0, min(3, audio_object_type - 1))
    freq_idx = _ADTS_RATE_INDEX.get(sample_rate, 3)   # 默认 48000
    ch_cfg = max(0, min(7, channels))
    frame_len = aac_len + 7

    b0 = 0xFF
    b1 = 0xF1                                  # 1111 0001: 无 CRC
    b2 = (profile << 6) | (freq_idx << 2) | ((ch_cfg >> 2) & 0x01)
    b3 = ((ch_cfg & 0x03) << 6) | ((frame_len >> 11) & 0x03)
    b4 = (frame_len >> 3) & 0xFF
    b5 = ((frame_len & 0x07) << 5) | 0x1F     # 0x1F = buffer fullness 高位
    b6 = 0xFC                                  # buffer fullness 低位 + 帧数-1
    return bytes((b0, b1, b2, b3, b4, b5, b6))


@dataclass
class StreamInfo:
    """从 FLV 头部和 AudioSpecificConfig 里解析出来的流参数。"""

    header_parsed: bool = False
    has_audio: bool = False
    has_video: bool = False
    # AudioSpecificConfig
    audio_object_type: int = 0       # 2 = AAC-LC
    sample_rate: int = 0
    channels: int = 0
    asc: bytes = b""

    @property
    def profile_name(self) -> str:
        return {1: "AAC-Main", 2: "AAC-LC", 3: "AAC-SSR", 5: "HE-AAC(SBR)"}.get(
            self.audio_object_type, f"AOT{self.audio_object_type}")

    def describe(self) -> str:
        if not self.asc:
            return "（尚未收到 AAC 序列头）"
        return (f"{self.profile_name} {self.sample_rate}Hz "
                f"{self.channels}ch")


# AudioSpecificConfig 的采样率索引表
_SAMPLE_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
                 16000, 12000, 11025, 8000, 7350]

# 放在 _SAMPLE_RATES 之后：供 ADTS 头查采样率索引
_ADTS_RATE_INDEX = {r: i for i, r in enumerate(_SAMPLE_RATES)}


def parse_audio_specific_config(asc: bytes) -> tuple[int, int, int]:
    """解析 2 字节 AudioSpecificConfig -> (object_type, sample_rate, channels)。

    位布局:
        audioObjectType    5 bit
        samplingFrequency  4 bit
        channelConfiguration 4 bit
    """
    if len(asc) < 2:
        return 0, 0, 0
    bits = int.from_bytes(asc[:2], "big")
    aot = (bits >> 11) & 0x1F
    freq_idx = (bits >> 7) & 0x0F
    channels = (bits >> 3) & 0x0F
    rate = _SAMPLE_RATES[freq_idx] if freq_idx < len(_SAMPLE_RATES) else 0
    # AOT 31 表示扩展，需要额外 6 bit，这里不处理（直播基本不会遇到）
    return aot, rate, channels


@dataclass
class FlvDemuxer:
    """把字节流喂进来，吐出 AudioFrame。

    用法:
        dm = FlvDemuxer()
        dm.feed(chunk)
        for frame in dm.frames():
            ...
    """

    info: StreamInfo = field(default_factory=StreamInfo)
    _buf: bytearray = field(default_factory=bytearray)
    _skip: int = 0                 # 还有多少字节要丢弃（跳过 tag 用）
    _pending: list[AudioFrame] = field(default_factory=list)
    _saw_seq_header: bool = False
    stats_audio_tags: int = 0
    stats_video_tags: int = 0
    stats_other_tags: int = 0
    stats_bytes_in: int = 0

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        self.stats_bytes_in += len(chunk)
        self._parse()

    def frames(self) -> list[AudioFrame]:
        """取出当前已解析出的音频帧（取走后清空）。"""
        out, self._pending = self._pending, []
        return out

    # ------------------------------------------------------------ 内部解析

    def _need(self, n: int) -> bool:
        return len(self._buf) >= n

    def _parse(self) -> None:
        while True:
            # 先处理需要跳过的大块数据
            if self._skip:
                take = min(self._skip, len(self._buf))
                if take == 0:
                    return
                del self._buf[:take]
                self._skip -= take
                if self._skip:
                    return
                continue

            if not self.info.header_parsed:
                if not self._need(9):
                    return
                if bytes(self._buf[:3]) != b"FLV":
                    # 不是 FLV，别硬解，清掉避免越堆越多
                    del self._buf[:]
                    raise ValueError("流不是 FLV 格式（magic != 'FLV'）")
                flags = self._buf[4]
                self.info.has_audio = bool(flags & 0x04)
                self.info.has_video = bool(flags & 0x01)
                data_offset = struct.unpack(">I", bytes(self._buf[5:9]))[0]
                # DataOffset 至少要覆盖到 PreviousTagSize0 结束
                if data_offset < 9:
                    data_offset = 9
                skip = data_offset - 9 + 4     # +4 = PreviousTagSize0
                del self._buf[:9]
                self._skip = skip
                self.info.header_parsed = True
                continue

            if not self._need(11):
                return
            tag_type = self._buf[0]
            data_size = int.from_bytes(self._buf[1:4], "big")
            ts = int.from_bytes(self._buf[4:7], "big") | (self._buf[7] << 24)
            body_start = 11

            if not self._need(body_start + data_size + 4):
                return  # 半截 tag，等更多数据
            body = bytes(self._buf[body_start:body_start + data_size])
            del self._buf[:body_start + data_size + 4]   # 连同 PreviousTagSize 一起消费

            if tag_type == TAG_AUDIO:
                self.stats_audio_tags += 1
                self._handle_audio(body, ts)
            elif tag_type == TAG_VIDEO:
                self.stats_video_tags += 1
            else:
                self.stats_other_tags += 1

    def _handle_audio(self, body: bytes, ts: int) -> None:
        if len(body) < 2:
            return
        sound_format = body[0] >> 4
        if sound_format != SOUND_FORMAT_AAC:
            return  # 非 AAC，忽略
        aac_type = body[1]
        payload = body[2:]
        if aac_type == AAC_SEQUENCE_HEADER:
            self.info.asc = payload
            aot, rate, ch = parse_audio_specific_config(payload)
            self.info.audio_object_type = aot
            self.info.sample_rate = rate
            self.info.channels = ch
            self._saw_seq_header = True
        elif aac_type == AAC_RAW:
            if not payload:
                return
            # 没有序列头就没法拼 ADTS，直接跳过更安全
            if self._saw_seq_header:
                hdr = build_adts_header(len(payload),
                                        self.info.audio_object_type or 2,
                                        self.info.sample_rate or 48000,
                                        self.info.channels or 2)
                self._pending.append(AudioFrame(aac=payload, timestamp=ts,
                                                adts=hdr + payload))

    # ------------------------------------------------------------ 统计信息

    def bitrate_kbps(self, elapsed_seconds: float) -> float:
        if elapsed_seconds <= 0:
            return 0.0
        return self.stats_bytes_in * 8 / elapsed_seconds / 1000.0
