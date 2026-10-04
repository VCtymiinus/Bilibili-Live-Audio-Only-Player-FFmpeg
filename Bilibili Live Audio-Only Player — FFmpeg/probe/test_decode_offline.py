"""不碰网络的解码链路测试。

用本地生成的 ADTS 文件当输入，验证:
    FfmpegDecoder 起进程 -> feed ADTS -> 中转到队列 -> read_pcm 取 PCM -> 算 RMS
再把它推到 waveOut 上实际出声。

这样能把「解码 + 播放」这段完全离线地验证掉，不受网络和房间状态影响。
"""

import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.flvdemux import build_adts_header  # noqa: E402
from bililive.player import (OUT_CHANNELS, OUT_RATE, FfmpegDecoder,  # noqa: E402
                             find_ffmpeg, pcm_rms)
from bililive.waveout import WaveOutPlayer, has_audio_device  # noqa: E402

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_diag")
os.makedirs(TMP, exist_ok=True)
WAV = os.path.join(TMP, "tone.wav")


def make_tone_adts(seconds=2.0, freq=440.0):
    """生成 AAC 编码的 ADTS 数据 —— 用 ffmpeg 自己编码，保证是合法 AAC。"""
    ff = find_ffmpeg()
    raw = os.path.join(TMP, "tone.pcm")
    n = int(OUT_RATE * seconds)
    pcm = bytearray()
    for i in range(n):
        env = min(1.0, i / (OUT_RATE * 0.05), (n - i) / (OUT_RATE * 0.05))
        v = int(9000 * env * math.sin(2 * math.pi * freq * i / OUT_RATE))
        pcm += struct.pack("<hh", v, v)
    with open(raw, "wb") as f:
        f.write(bytes(pcm))

    import subprocess
    adts = os.path.join(TMP, "tone.aac")
    cmd = [ff, "-hide_banner", "-y", "-f", "s16le", "-ar", str(OUT_RATE),
           "-ac", str(OUT_CHANNELS), "-i", raw, "-c:a", "aac",
           "-b:a", "128k", "-f", "adts", adts]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        print("编码失败:", p.stderr[-600:])
        return None
    with open(adts, "rb") as f:
        return f.read()


def split_adts(blob):
    """把 ADTS 流按帧切开（每帧 7 字节头里含长度）。"""
    frames = []
    i = 0
    while i + 7 <= len(blob):
        if blob[i] != 0xFF or (blob[i + 1] & 0xF0) != 0xF0:
            i += 1
            continue
        flen = ((blob[i + 3] & 0x03) << 11) | (blob[i + 4] << 3) | (blob[i + 5] >> 5)
        if flen < 7 or i + flen > len(blob):
            break
        frames.append(blob[i:i + flen])
        i += flen
    return frames


def main():
    print("=" * 68)
    ff = find_ffmpeg()
    print(f"ffmpeg: {ff}")
    print(f"声卡  : {'有' if has_audio_device() else '无'}")

    print("\n[1] 用 ffmpeg 生成一段 2 秒 AAC(ADTS)")
    blob = make_tone_adts(2.0)
    if not blob:
        return 1
    frames = split_adts(blob)
    print(f"    {len(blob)} 字节，切成 {len(frames)} 个 ADTS 帧")
    if not frames:
        return 1

    print("\n[2] 起 FfmpegDecoder，喂 ADTS，从中转队列取 PCM")
    dec = FfmpegDecoder(ff)
    dec.start()
    collected = bytearray()
    fed = 0
    t0 = time.time()
    last_flush = t0
    # 按实时节奏喂（模拟直播到达），同时不断取 PCM
    for idx, fr in enumerate(frames):
        dec.feed(fr, fr)          # 已经是 ADTS，两参数都给同一份
        fed += 1
        now = time.time()
        if now - last_flush >= 0.1:   # 定时兜底 flush，否则数据攒着不发
            dec.flush()
            last_flush = now
        for _ in range(4):
            got = dec.read_pcm(0.01)
            if got is None:
                break
            if got:
                collected += got
        time.sleep(0.02)          # 约 2 秒喂完（1024 样本/帧 → 46 帧/秒）
    dec.flush()                   # 关键：收尾必须 flush，否则残留在缓冲区
    # 收尾：把剩余 PCM 抽干
    tail_deadline = time.time() + 3.0
    while time.time() < tail_deadline:
        got = dec.read_pcm(0.2)
        if got is None:
            continue
        if got:
            collected += got
        elif got == b"":
            break
    dec.close()

    print(f"    喂入 {fed} 帧，收到 PCM {len(collected)} 字节，"
          f"耗时 {time.time()-t0:.1f}s")
    if not collected:
        print("    !! 没解出 PCM")
        print(f"    ffmpeg stderr: {dec.stderr_tail}")
        return 1
    rms = pcm_rms(bytes(collected))
    dur = len(collected) / (OUT_RATE * OUT_CHANNELS * 2)
    print(f"    RMS = {rms:.4f}   时长 {dur:.2f}s（预期约 2.0s）")
    if rms < 0.001:
        print("    !! RMS 接近 0，解出来是静音")
        return 1
    print("    -> 解码链路正常，且不是静音")

    print("\n[3] 把解出的 PCM 推到 waveOut（这一步会真的出声）")
    if not has_audio_device():
        print("    无声卡，跳过")
        return 0
    try:
        p = WaveOutPlayer(OUT_RATE, OUT_CHANNELS)
        p.open()
        t0 = time.time()
        p.write(bytes(collected))
        p.drain(4.0)
        p.close()
        print(f"    播放完成，耗时 {time.time()-t0:.1f}s")
        print("    -> 应听到约 2 秒 440Hz 平稳音")
    except Exception as e:
        print(f"    !! waveOut 失败: {type(e).__name__}: {e}")
        return 1

    print("=" * 68)
    print("结论: 解码 -> 播放 全段离线验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
