"""端到端验证（不需要声卡）。

验证链路: 房间号 -> 流地址 -> 收流 -> FLV 解复用 -> AAC 帧 -> ffmpeg 解码 -> PCM

关键点：用 PCM 的 RMS 客观判断「解码出来的到底是不是真实音频」，
而不是静音。这样即使在听不到声音的环境里也能确认前四段成立。

最后把解码结果落成 WAV，供人工播放确认（这一步才需要声卡）。
"""

import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive import player as P                      # noqa: E402
from bililive.stream import LiveAudioEngine           # noqa: E402

ROOM = int(sys.argv[1]) if len(sys.argv) > 1 else 22388070
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
OUT_WAV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_out.wav")


def write_wav(path, pcm, rate, channels, bits=16):
    block = channels * bits // 8
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate,
                                      rate * block, block, bits))
        f.write(b"data" + struct.pack("<I", len(pcm)))
        f.write(pcm)


def main():
    print("=" * 72)
    print("bililive 端到端验证")
    print("=" * 72)

    ff = P.find_ffmpeg()
    print(f"ffmpeg        : {ff or '未找到'}")
    print(f"音频输出设备  : {'有' if P.has_audio_device() else '无'}")
    print(f"房间          : {ROOM}")
    print(f"验证时长      : {SECONDS} 秒")
    if not ff:
        print("\n!! 没找到 ffmpeg，无法验证解码环节。")
        print("   可设置环境变量 BILILIVE_FFMPEG 指向 ffmpeg.exe")
        return 2

    log_lines: list[str] = []
    eng = LiveAudioEngine(ROOM, log=lambda m: log_lines.append(m))
    eng.start()

    # 等引擎真正开始播放
    t0 = time.time()
    while eng.stats.state not in ("playing",) and time.time() - t0 < 25:
        time.sleep(0.25)
    if eng.stats.state != "playing":
        print(f"\n!! 引擎未进入播放状态: state={eng.stats.state} "
              f"detail={eng.stats.detail}")
        for line in log_lines:
            print("   log:", line)
        eng.stop()
        return 3

    info = eng.demuxer_info
    print(f"\n流参数        : {info.describe() if info else '未知'}")
    print("收流 + 解码中 ...")

    dec = P.FfmpegDecoder(ff)
    dec.start()

    pcm_total = bytearray()
    rms_samples: list[float] = []
    frames_fed = 0
    deadline = time.time() + SECONDS

    try:
        for frame in eng.frames(timeout=1.0):
            dec.feed(frame.aac, frame.adts)
            frames_fed += 1
            # 边收边读，避免 ffmpeg 输出管道写满造成阻塞
            got = dec.read_pcm()
            if got:
                pcm_total += got
                if len(rms_samples) < 4000:
                    rms_samples.append(P.pcm_rms(got))
            if time.time() > deadline:
                break
    finally:
        dec.close()
        eng.stop()

    # 把剩余 PCM 读出来
    elapsed = time.time() - t0
    print("\n" + "-" * 72)
    print(f"送入 AAC 帧   : {frames_fed}")
    print(f"引擎统计      : state={eng.stats.state} frames={eng.stats.frames} "
          f"reconnects={eng.stats.reconnects} renewals={eng.stats.renewals}")
    print(f"入站字节      : {eng.stats.bytes_in}")
    print(f"解出 PCM 字节 : {len(pcm_total)}")

    expected = int(P.OUT_RATE * P.OUT_CHANNELS * 2 * min(SECONDS, elapsed))
    if expected:
        print(f"预期 PCM 约   : {expected} 字节 "
              f"（达成率 {len(pcm_total) / expected * 100:.0f}%）")

    avg_rms = sum(rms_samples) / len(rms_samples) if rms_samples else 0.0
    peak_rms = max(rms_samples) if rms_samples else 0.0
    print(f"PCM RMS       : 平均 {avg_rms:.4f}  峰值 {peak_rms:.4f}")

    if len(pcm_total) > 1000:
        write_wav(OUT_WAV, bytes(pcm_total), P.OUT_RATE, P.OUT_CHANNELS)
        print(f"已写出 WAV    : {OUT_WAV}")

    print("-" * 72)
    ok_stream = eng.stats.frames > 0
    ok_decode = len(pcm_total) > 1000
    ok_audible = avg_rms > 0.0005
    print(f"收流正常      : {'是' if ok_stream else '否'}")
    print(f"解码出 PCM    : {'是' if ok_decode else '否'}")
    print(f"音频非静音    : {'是' if ok_audible else '否'}  (RMS={avg_rms:.4f})")
    if ok_stream and ok_decode and ok_audible:
        print("\n结论: 全链路成立 —— 收流/解复用/解码都验证通过。")
        print(f"      请手动播放 {OUT_WAV} 确认确实是直播声音（这一步需要你听）。")
        rc = 0
    else:
        print("\n结论: 有问题，见上面统计。")
        if dec.stderr_tail:
            print(f"      ffmpeg stderr: {dec.stderr_tail}")
        rc = 1
    print("=" * 72)
    return rc


if __name__ == "__main__":
    sys.exit(main())
