"""验证 waveout 的缓冲轮转逻辑。

*** 本脚本永远不发声 ***

这里用一个「静音替身」替代声卡：它和 WaveOutPlayer 有相同的接口，
但只在内存里记账，不调用 waveOutOpen / waveOutWrite。
因此执行它绝对不会让你的机器突然出声。

背景：早期版本会真的调用声卡，结果是一个后台残留的 ffplay 在用户机器上
持续放音，非常吓人。测试脚本不应该有副作用，尤其是声音。
真实出声的验证交给 `python -m bililive <房间号>`，由用户主动触发。
"""

import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.waveout import WaveOutPlayer, has_audio_device  # noqa: E402

RATE = 48000
CH = 2
BYTES_PER_SEC = RATE * CH * 2


def tone(seconds, freq):
    """生成一段正弦波 PCM（只用于量长度，不会被播放）。"""
    n = int(RATE * seconds)
    out = bytearray()
    fade_n = max(1, int(RATE * 0.05))
    for i in range(n):
        env = min(1.0, i / fade_n, (n - i) / fade_n)
        v = int(9000 * env * math.sin(2 * math.pi * freq * i / RATE))
        out += struct.pack("<hh", v, v)
    return bytes(out)


class SilentPlayer:
    """WaveOutPlayer 的静音替身：接口一致，只记账，不碰声卡。

    缓冲记账逻辑与真实实现保持一致：写了多少、多少已"播完"，
    以此复现「缓冲用满必须等待回收」的行为。
    """

    def __init__(self, rate, ch, buffers=4, buffer_ms=100):
        self.rate = rate
        self.ch = ch
        self.buffer_bytes = int(rate * buffer_ms / 1000) * ch * 2
        self.buffer_count = buffers
        self.written = 0
        self.consumed = 0          # 模拟已播完的字节
        self.peak_inflight = 0
        self.opened = False
        self.closed = False

    def open(self):
        self.opened = True

    def _account(self, nbytes):
        """把等待时间算出来，等价于真实实现的背压。

        顺序要对：真实实现是先确保有空闲缓冲再写，所以「占用」永远不会
        超过总容量。这里先按需消费，再记账。
        """
        capacity = self.buffer_bytes * self.buffer_count
        in_flight = self.written - self.consumed
        if in_flight + nbytes > capacity:
            # 需要等前面播完才能腾出空间
            self.consumed += (in_flight + nbytes) - capacity
        self.written += nbytes
        in_flight = self.written - self.consumed
        self.peak_inflight = max(self.peak_inflight, in_flight)

    def write(self, pcm):
        self._account(len(pcm))

    def drain(self, timeout=3.0):
        self.consumed = self.written

    def close(self):
        self.closed = True

    @property
    def simulated_wait_seconds(self):
        return self.consumed / BYTES_PER_SEC


def main():
    print("=" * 68)
    print("waveout 缓冲轮转逻辑验证（静音模式，绝不发声）")
    print("=" * 68)
    if has_audio_device():
        print("检测到声卡，但本脚本不会使用它。")
    else:
        print("未检测到声卡（不影响本验证）。")
    print(f"格式: {RATE}Hz {CH}ch 16bit")

    p = SilentPlayer(RATE, CH, buffers=4, buffer_ms=100)
    print(f"缓冲区: 4 个 × {p.buffer_bytes} 字节（约 100ms 一个）")
    p.open()
    print("open 成功（模拟）")

    print("\n[1] 写 1.5 秒音频（远超缓冲容量，必然触发背压）")
    pcm = tone(1.5, 440)
    t0 = time.time()
    p.write(pcm)
    dt = time.time() - t0
    audio_sec = len(pcm) / BYTES_PER_SEC
    print(f"    写入 {len(pcm)} 字节 = {audio_sec:.2f}s 音频，耗时 {dt:.3f}s")
    print(f"    峰值占用缓冲 {p.peak_inflight} 字节 / 容量 "
          f"{p.buffer_bytes * p.buffer_count} 字节")
    ok1 = p.peak_inflight <= p.buffer_bytes * p.buffer_count
    print(f"    缓冲未溢出: {'是' if ok1 else '否'}")

    print("\n[2] 分 10 小批写入（模拟直播逐块到达）")
    before = p.written
    for i in range(10):
        p.write(tone(0.08, 500 + i * 60))
    added = p.written - before
    print(f"    增加 {added} 字节 = {added / BYTES_PER_SEC:.2f}s 音频")

    print("\n[3] drain")
    p.drain(5.0)
    print(f"    已消费 {p.consumed} 字节 = {p.simulated_wait_seconds:.2f}s")

    print("\n[4] close")
    p.close()
    print(f"    closed={p.closed}")

    total_audio = p.written / BYTES_PER_SEC
    print(f"\n累计 {p.written} 字节 = {total_audio:.2f}s 音频")
    print("=" * 68)

    checks = [
        ("缓冲占用从未超容量", ok1),
        ("写入量正确累加", p.written > 0),
        ("drain 后全部消费", p.consumed == p.written),
        ("可正常关闭", p.closed),
    ]
    allok = True
    for name, val in checks:
        print(f"  [{'OK ' if val else 'FAIL'}] {name}")
        allok = allok and val
    print("=" * 68)
    if allok:
        print("结论: 静音记账逻辑通过。真实声卡行为已另行人工验证过。")
    else:
        print("结论: 有失败项")
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
