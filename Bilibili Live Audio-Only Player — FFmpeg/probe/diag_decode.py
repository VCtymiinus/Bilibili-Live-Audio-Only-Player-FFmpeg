"""隔离诊断解码段：ffmpeg 到底在抱怨什么？

前两轮的卡死都发生在「喂 ADTS 给 ffmpeg -> 读 PCM」这一段。
不再猜，直接把 ffmpeg 的 stderr 原样打出来，并分别测：
  1. 用 ADTS 喂
  2. 用裸 AAC 喂
  3. 把样本存文件再用 ffmpeg 命令行解（排除管道因素）
"""

import os
import ssl
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.biliapi import BiliLiveClient  # noqa: E402
from bililive.flvdemux import FlvDemuxer     # noqa: E402
from bililive.player import OUT_CHANNELS, OUT_RATE, find_ffmpeg, pcm_rms  # noqa: E402

ROOM = 1883358196
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_diag")
os.makedirs(TMP, exist_ok=True)


def grab_frames(seconds=4.0):
    c = BiliLiveClient()
    info = c.resolve_room(ROOM)
    for s in c.audio_streams(info["room_id"]):
        if s.format != "flv":
            continue
        dm = FlvDemuxer()
        frames = []
        try:
            req = urllib.request.Request(s.url, headers=c.headers())
            resp = urllib.request.urlopen(req, timeout=30,
                                         context=ssl.create_default_context())
        except Exception:
            continue
        t0 = time.time()
        with resp:
            while time.time() - t0 < seconds:
                chunk = resp.read(16384)
                if not chunk:
                    break
                dm.feed(chunk)
                frames.extend(dm.frames())
        if frames:
            print(f"拿到 {len(frames)} 帧, {dm.info.describe()}")
            return dm.info, frames
    return None, []


def main():
    print("=" * 70)
    print("1. 抓样本")
    info, frames = grab_frames()
    if not frames:
        print("!! 没抓到帧")
        return 1

    adts_blob = b"".join(f.adts for f in frames)
    raw_blob = b"".join(f.aac for f in frames)
    adts_path = os.path.join(TMP, "sample.aac")
    raw_path = os.path.join(TMP, "sample.raw")
    with open(adts_path, "wb") as f:
        f.write(adts_blob)
    with open(raw_path, "wb") as f:
        f.write(raw_blob)
    print(f"  ADTS {len(adts_blob)}B -> {adts_path}")
    print(f"  裸帧 {len(raw_blob)}B -> {raw_path}")
    print(f"  首帧 ADTS 头: {frames[0].adts[:7].hex(' ')}")
    print(f"  首帧裸载荷  : {frames[0].aac[:8].hex(' ')}")

    ff = find_ffmpeg()
    print(f"\n2. 命令行解码 ADTS 文件（排除管道因素）")
    out_pcm = os.path.join(TMP, "out_adts.pcm")
    cmd = [ff, "-hide_banner", "-y", "-f", "aac", "-i", adts_path,
           "-f", "s16le", "-ac", "2", "-ar", "48000", out_pcm]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    print(f"  rc={p.returncode}")
    if p.stderr.strip():
        print("  stderr:")
        for line in p.stderr.strip().splitlines()[-12:]:
            print("    " + line)
    if os.path.exists(out_pcm):
        size = os.path.getsize(out_pcm)
        with open(out_pcm, "rb") as f:
            pcm = f.read()
        print(f"  PCM {size}B  RMS={pcm_rms(pcm):.4f}")
        print(f"  理论时长 {size/(OUT_RATE*OUT_CHANNELS*2):.2f}s")

    print(f"\n3. 命令行解码裸 AAC 文件（对照）")
    out_raw = os.path.join(TMP, "out_raw.pcm")
    cmd2 = [ff, "-hide_banner", "-y", "-f", "aac", "-i", raw_path,
            "-f", "s16le", "-ac", "2", "-ar", "48000", out_raw]
    p2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=60)
    print(f"  rc={p2.returncode}")
    if p2.stderr.strip():
        for line in p2.stderr.strip().splitlines()[-6:]:
            print("    " + line)
    if os.path.exists(out_raw):
        print(f"  PCM {os.path.getsize(out_raw)}B（对比 ADTS 的结果）")

    print(f"\n4. 管道方式，看是否能读到 PCM")
    proc = subprocess.Popen(
        [ff, "-hide_banner", "-loglevel", "info", "-f", "aac", "-i", "pipe:0",
         "-f", "s16le", "-ac", "2", "-ar", "48000", "pipe:1"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # 分批喂，模拟直播
    got = bytearray()
    t0 = time.time()
    try:
        for i in range(0, len(frames), 20):
            batch = b"".join(f.adts for f in frames[i:i + 20])
            proc.stdin.write(batch)
            proc.stdin.flush()
            time.sleep(0.02)
            # 尝试非阻塞读
            import select
            r, _, _ = select.select([proc.stdout], [], [], 0.01)
            if r:
                got += os.read(proc.stdout.fileno(), 65536)
    except Exception as e:
        print(f"  写入/读取异常: {type(e).__name__}: {e}")
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        rest = proc.stdout.read()
        got += rest
    except Exception:
        pass
    proc.wait(timeout=10)
    print(f"  管道收 PCM {len(got)}B  RMS={pcm_rms(bytes(got)):.4f}  "
          f"耗时 {time.time()-t0:.1f}s")
    err = proc.stderr.read().decode("utf-8", "replace")
    if err.strip():
        print("  ffmpeg stderr:")
        for line in err.strip().splitlines()[-10:]:
            print("    " + line)
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
