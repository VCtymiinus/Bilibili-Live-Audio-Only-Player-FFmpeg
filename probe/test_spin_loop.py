"""故障注入：候主流永远过期时，会不会变成零等待死循环？

针对 ISSUES.md 第 1 条。早期实现在 `picked is None` 分支直接 continue，
没有任何等待 -> 「取接口 -> 拿到同样过期的结果 -> 立刻再来」，
每秒约 2 次请求，一天 25 万次打接口。

本测试用假 client 让候选**永远处于已过期状态**，跑固定时长，
数它到底请求了多少次接口。不联网、不出声。

判据：修好后请求次数应该是个位数（节流 ≥5s），
      而不是「每秒 2 次」那种量级。
"""

import os
import sys
import threading
import time

sys.dont_write_bytecode = True      # 别生成 __pycache__ 污染目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive import play as P            # noqa: E402
from bililive.biliapi import AudioStream  # noqa: E402

FAILS = []


def check(name, got, cond, detail=""):
    ok = bool(cond)
    print(f"  [{'OK ' if ok else 'FAIL'}] {name}: {got} {detail}")
    if not ok:
        FAILS.append(name)


def make_expired(host, seconds_ago=1200):
    """构造一条**已经过期**的候选，模拟时钟偏快 20 分钟。"""
    exp = time.time() - seconds_ago
    return AudioStream(
        url=f"https://{host}/live/x.flv?expires={int(exp)}",
        protocol="http_stream", format="flv", codec="avc",
        current_qn=250, accept_qn=[10000, 250],
        expires_at=exp, audio_codec="mp4a.40.2")


class FakeClient:
    """假的 BiliLiveClient：房间永远在播，但候选永远已过期。"""

    def __init__(self):
        self.stream_calls = 0
        self.resolve_calls = 0

    def resolve_room(self, room):
        self.resolve_calls += 1
        return {"room_id": 26774400, "live_status": 1}

    def room_info(self, room_id):
        return {}

    def audio_streams(self, room_id, qn=10000):
        self.stream_calls += 1
        return [make_expired("cdn-a.example"),
                make_expired("cdn-b.example"),
                make_expired("cdn-c.example")]

    def headers(self, referer=None):
        return {"Referer": "https://live.bilibili.com/"}


def main():
    print("=" * 70)
    print("故障注入：候主流永远过期（ISSUES.md 第 1 条）")
    print("=" * 70)

    fake = FakeClient()
    real_client = P.BiliLiveClient
    P.BiliLiveClient = lambda *a, **k: fake

    # 跑 12 秒，看接口被调几次
    DURATION = 12.0
    result = {}

    def run():
        try:
            result["rc"] = P.run(26774400, 0, False)
        except SystemExit as e:
            result["rc"] = e.code
        except Exception as e:
            result["exc"] = f"{type(e).__name__}: {e}"

    print(f"\n启动主循环，跑 {DURATION:.0f} 秒 ...\n")
    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(DURATION)

    # 停掉它：主循环靠 SIGINT 或 stop 事件退出，这里直接杀线程所在进程不方便，
    # 改为把 BiliLiveClient 还原并等待自然结束 —— 实际上它不会自己停，
    # 所以我们只统计这段时间内的调用次数。
    calls = fake.stream_calls
    print(f"\n{DURATION:.0f} 秒内 audio_streams 被调用 {calls} 次")
    print(f"          resolve_room 被调用 {fake.resolve_calls} 次")

    rate = calls / DURATION
    print(f"          约 {rate:.2f} 次/秒，折合 {rate*86400:,.0f} 次/天")

    print()
    check("节流生效（不是每秒 2 次的热循环）", f"{rate:.2f} 次/秒", rate < 0.5,
          "  判据 < 0.5 次/秒")
    check("不超过节流理论上限", f"{calls} 次", calls <= DURATION / 5.0 + 2,
          f"  12 秒内应 ≤ {DURATION/5.0+2:.0f} 次（每次等 ≥5s）")

    print("\n对比：旧实现零等待 -> 约 2 次/秒 -> 172,800 次/天")
    print(f"      现在      -> {rate:.2f} 次/秒 -> {rate*86400:,.0f} 次/天")

    # 还原，避免影响后续
    P.BiliLiveClient = real_client

    print("\n" + "=" * 70)
    if FAILS:
        print(f"失败 {len(FAILS)} 项: {FAILS}")
        return 1
    print("通过：零等待死循环已被节流堵住")
    return 0


if __name__ == "__main__":
    sys.exit(main())
