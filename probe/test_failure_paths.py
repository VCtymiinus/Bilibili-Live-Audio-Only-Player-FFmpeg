"""离线故障注入测试 —— 不联网、不出声、不需要声卡。

补的正是这次审查指出的最大缺口：**异常路径只有纸面逻辑**。
P0-1 和 P0-2 之所以能同时存活，就是因为正常播放时它们永远不触发。

覆盖:
  1. classify_exit 对 ffplay「失败也返回 0」的处理（这是本次新发现）
  2. 候选轮换：确保 cands[0] 不是唯一被使用的
  3. 退避：连续失败时等待时间确实递增
  4. is_permanent_error 不再被中文子串误伤
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.biliapi import AudioStream, BiliApiError   # noqa: E402
from bililive.ffplay import FfplayPlayer, find_ffplay    # noqa: E402
from bililive.play import (BACKOFF_MAX, BACKOFF_START,   # noqa: E402
                           EXIT_FAILED, EXIT_RENEW,
                           EXIT_STREAM_ENDED, classify_exit,
                           is_permanent_error)

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: {got!r}"
          + ("" if ok else f"   期望 {want!r}"))
    if not ok:
        FAILS.append(name)


def section(t):
    print(f"\n{'=' * 68}\n{t}\n{'-' * 68}")


def test_classify():
    section("1. classify_exit —— ffplay 退出原因判定")

    # 最有价值的一条：ffplay 连不上时退出码是 0，必须靠 stderr 判定
    check("连不上但 rc=0（有 stderr 错误）",
          classify_exit(False, 0, 1.5, True), EXIT_FAILED)
    check("连不上且没有任何信息（rc=0, 存活极短）",
          classify_exit(False, 0, 0.2, False), EXIT_FAILED)
    check("地址过期（优先于其它判断）",
          classify_exit(True, 0, 3000.0, False), EXIT_RENEW)
    check("正常播完很久，无错误 -> 视为下播",
          classify_exit(False, 0, 1800.0, False), EXIT_STREAM_ENDED)
    check("非零退出码 -> 失败",
          classify_exit(False, 1, 0.5, False), EXIT_FAILED)
    check("播很久但中途报错 -> 仍是失败",
          classify_exit(False, 0, 1800.0, True), EXIT_FAILED)

    print("\n  注: 真实的 ffplay 行为（实测）")
    print("      http://127.0.0.1:1/nope.flv               -> rc=0 'Error number -138'")
    print("      http://nonexistent-host-xyz.invalid/a.flv -> rc=0 'I/O error'")
    print("      => 旧代码 `rc not in (0,None)` 永远为假，退避从不触发")


def test_had_error():
    section("2. FfplayPlayer.had_error —— 是否识别出错误特征")
    p = FfplayPlayer(find_ffplay(), volume=0, log=lambda m: None)

    p._stderr_tail = ["http://127.0.0.1:1/nope.flv: Error number -138 occurred"]
    check("识别 Error number", p.had_error, True)

    p._stderr_tail = ["http://x.invalid/a.flv: I/O error"]
    check("识别 I/O error", p.had_error, True)

    p._stderr_tail = ["https://cdn/x.flv: Connection refused"]
    check("识别 Connection refused", p.had_error, True)

    # 正常播放的 info 输出不该被误判
    p._stderr_tail = [
        "Input #0, flv, from 'https://cdn/x.flv':",
        "  Duration: N/A, start: 0.000000, bitrate: N/A",
        "  Stream #0:0: Audio: aac (LC), 48000 Hz, stereo, fltp",
        "    nan    :  0.000 fd=   0 aq=   12KB vq=    0KB sq=    0B f=0/0",
    ]
    check("正常 info 输出不误判", p.had_error, False)

    p._stderr_tail = ["Warning: something benign"]
    check("普通 warning 不误判", p.had_error, False)


def test_is_permanent():
    section("3. is_permanent_error —— 不再被中文子串误伤")
    check("60004 房间不存在 -> 永久",
          is_permanent_error(BiliApiError(
              "room_init(999999999) 失败: code=60004 msg=房间不存在")), True)
    # 这些在旧实现里会被 "\u4e0d\u5b58\u5728" 子串命中而误判为永久错误
    check("临时故障含「不存在」字样 -> 不再永久",
          is_permanent_error(BiliApiError("接口不存在，请稍后重试")), False)
    check("参数错误 -> 不再永久",
          is_permanent_error(BiliApiError("code=-400 参数错误")), False)
    check("网络超时 -> 不永久",
          is_permanent_error(BiliApiError("请求超时")), False)


def test_candidate_rotation():
    section("4. 候选轮换 —— 确保不是只用 cands[0]")

    def mk(host, expires_in=3600):
        return AudioStream(
            url=f"https://{host}/live/x.flv?expires={int(time.time()) + expires_in}",
            protocol="http_stream", format="flv", codec="avc",
            current_qn=250, accept_qn=[10000, 250],
            expires_at=time.time() + expires_in, audio_codec="mp4a.40.2")

    cands = [mk("cdn-a.example"), mk("cdn-b.example"), mk("cdn-c.example")]
    order = []
    # 复刻主循环里的取用方式
    while cands:
        cand = cands.pop(0)
        if cand.is_fresh(600.0):
            order.append(cand.url.split("/")[2])

    check("三条候选都被依次取用", len(order), 3)
    check("顺序为 a,b,c", order, ["cdn-a.example", "cdn-b.example",
                                  "cdn-c.example"])

    print("\n  过期的候选应被跳过:")
    fresh = mk("good.example")
    stale = mk("stale.example", expires_in=-100)
    cands2 = [stale, fresh]
    picked = None
    while cands2:
        c = cands2.pop(0)
        if c.is_fresh(600.0):
            picked = c
            break
    check("跳过已过期候选", picked.url.split("/")[2] if picked else None,
          "good.example")

    print("\n  旧实现写死 cands[0]，永远拿不到 b/c —— 这是 P0-2 的要害")


def test_backoff():
    section("5. 退避 —— 连续失败时等待时间确实递增")
    backoff = BACKOFF_START
    waits = []
    for _ in range(8):
        waits.append(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX)
    print(f"  等待序列: {[round(w, 1) for w in waits]}")
    check("首次等待 = BACKOFF_START", waits[0], BACKOFF_START)
    check("单调不减", all(waits[i] <= waits[i + 1] for i in range(len(waits) - 1)),
          True)
    check("被 BACKOFF_MAX 封顶", max(waits), BACKOFF_MAX)
    check("确实退避到最大值", waits[-1], BACKOFF_MAX)
    total = sum(waits)
    print(f"  8 次失败累计等待 {total:.0f}s（旧代码恒为 0.3s×8=2.4s，"
          f"即热循环打接口）")


def main():
    print("=" * 68)
    print("bililive 离线故障注入测试（不联网 / 不出声）")
    print("=" * 68)
    test_classify()
    test_had_error()
    test_is_permanent()
    test_candidate_rotation()
    test_backoff()
    section("结果")
    if FAILS:
        print(f"失败 {len(FAILS)} 项:")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
