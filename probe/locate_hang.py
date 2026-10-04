"""定位卡死点：分阶段打点，每步都有超时，找出到底阻塞在哪里。

上一轮 verify_e2e 跑了 5 分钟没结束（预期 30 秒），说明有阻塞。
本脚本把每个阶段单独隔离出来测：
  A. 取流地址
  B. 收流 + 解复用
  C. 起 ffmpeg + 喂 ADTS
  D. 读 PCM
  E. waveOut 播放
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive import player as P                      # noqa: E402
from bililive.biliapi import BiliLiveClient           # noqa: E402
from bililive.flvdemux import FlvDemuxer              # noqa: E402


def stage(name):
    print(f"\n=== {name} ===  t={time.time() - T0:.2f}s", flush=True)


T0 = time.time()
ROOM = int(sys.argv[1]) if len(sys.argv) > 1 else 22388070


def main():
    stage("A. 取流地址")
    c = BiliLiveClient()
    info = c.resolve_room(ROOM)
    print(f"  room_id={info['room_id']} live_status={info.get('live_status')}",
          flush=True)
    st = c.best_audio_stream(info["room_id"])
    print(f"  {st.protocol}/{st.format} 有效 {st.seconds_left/60:.1f} 分钟",
          flush=True)

    stage("B. 收流 + 解复用（用小 chunk，模拟真实读取）")
    import urllib.request, ssl
    dm = FlvDemuxer()
    frames = []
    req = urllib.request.Request(st.url, headers=c.headers())
    t_b = time.time()
    with urllib.request.urlopen(req, timeout=15,
                               context=ssl.create_default_context()) as resp:
        while time.time() - t_b < 4.0:
            chunk = resp.read(4096)
            if not chunk:
                print("  流结束", flush=True)
                break
            dm.feed(chunk)
            frames.extend(dm.frames())
    print(f"  收到 {len(frames)} 帧, {dm.stats_bytes_in} 字节, "
          f"视频tag={dm.stats_video_tags}", flush=True)
    print(f"  流参数: {dm.info.describe()}", flush=True)
    if not frames:
        print("  !! 没拿到帧，后续无法进行", flush=True)
        return 1

    stage("C. 起 ffmpeg 并喂 ADTS")
    ff = P.find_ffmpeg()
    print(f"  ffmpeg = {ff}", flush=True)
    dec = P.FfmpegDecoder(ff)
    dec.start()
    print("  进程已启动，pid=", dec._proc.pid, flush=True)

    fed = 0
    t_c = time.time()
    for fr in frames:
        dec.feed(fr.aac, fr.adts)
        fed += 1
    print(f"  已喂 {fed} 帧 ADTS（{time.time()-t_c:.2f}s）", flush=True)

    stage("D. 读 PCM（关键：这里是否阻塞）")
    pcm = bytearray()
    t_d = time.time()
    empty_reads = 0
    while time.time() - t_d < 6.0:
        got = dec.read_pcm()
        if got:
            pcm += got
        else:
            empty_reads += 1
            if empty_reads < 3:
                print(f"  读到空（第 {empty_reads} 次），解码进程 "
                      f"poll={dec._proc.poll()}", flush=True)
            time.sleep(0.05)
        if len(pcm) > 0 and time.time() - t_d > 3.0:
            break
    print(f"  PCM 共 {len(pcm)} 字节, 空读 {empty_reads} 次", flush=True)
    if pcm:
        print(f"  RMS = {P.pcm_rms(bytes(pcm)):.4f}", flush=True)
    stderr = dec.stderr_tail
    if stderr:
        print(f"  ffmpeg stderr: {stderr}", flush=True)

    dec.close()
    print(f"\n各阶段耗时: B={t_b-T0:.1f}s C={t_c-t_b:.1f}s D={time.time()-t_d:.1f}s",
          flush=True)

    stage("E. waveOut 打开 + 播放已解出的 PCM")
    try:
        from bililive.waveout import WaveOutPlayer
        wp = WaveOutPlayer(P.OUT_RATE, P.OUT_CHANNELS)
        wp.open()
        print("  waveOut 打开成功", flush=True)
        if pcm:
            t_e = time.time()
            wp.write(bytes(pcm))
            print(f"  写入 {len(pcm)} 字节完成（{time.time()-t_e:.2f}s）", flush=True)
            wp.drain(4.0)
            print("  drain 完成", flush=True)
        wp.close()
        print("  waveOut 关闭成功", flush=True)
    except Exception as e:
        print(f"  !! waveOut 失败: {type(e).__name__}: {e}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
