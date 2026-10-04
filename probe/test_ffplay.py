"""测 ffplay 能否直接播 B 站的纯音频流 URL。

这是 A 方案的成败点：ffplay 自己取流、解码、出声，完全不经过管道。
用硬超时跑 12 秒，看它是否稳定播放、有无报错。
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bililive.biliapi import BiliLiveClient   # noqa: E402
from bililive.ffplay import find_ffplay       # noqa: E402

ROOM = int(sys.argv[1]) if len(sys.argv) > 1 else 26774400
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0


def main():
    ffplay = find_ffplay()
    print(f"ffplay = {ffplay}")
    if not ffplay:
        return 1

    c = BiliLiveClient()
    info = c.resolve_room(ROOM)
    rid = info["room_id"]
    print(f"房间 {ROOM} -> {rid} live_status={info.get('live_status')}")
    if info.get("live_status") != 1:
        print("!! 未开播")
        return 2

    stream = c.audio_streams(rid)[0]
    print(f"流: {stream.format}/{stream.protocol} {stream.audio_codec}")
    print(f"URL: {stream.url[:110]}...")
    print(f"剩余有效 {stream.seconds_left/60:.1f} 分钟")

    hdrs = "".join(f"{k}: {v}\r\n" for k, v in c.headers().items())

    env = dict(os.environ)
    env.setdefault("SDL_VIDEODRIVER", "dummy")
    env["SDL_AUDIODRIVER"] = env.get("SDL_AUDIODRIVER", "directsound")

    cmd = [ffplay, "-hide_banner", "-loglevel", "info", "-nostats",
           "-nodisp", "-vn", "-autoexit", "-volume", "80",
           "-infbuf", "-fflags", "nobuffer",
           "-tls_verify", "0",
           "-headers", hdrs, stream.url]
    print(f"\n启动 ffplay，跑 {SECONDS:.0f} 秒 ...")
    print("-" * 68)
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=0x08000000, env=env)
    lines: list[str] = []
    try:
        while time.time() - t0 < SECONDS:
            if proc.poll() is not None:
                print(f"!! ffplay 提前退出，code={proc.returncode} "
                      f"（{time.time()-t0:.1f}s）")
                break
            time.sleep(0.3)
    finally:
        alive = proc.poll() is None
        if alive:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
        lines = [l for l in out.splitlines() if l.strip()]

    print(f"运行 {time.time()-t0:.1f}s，存活={alive}")
    print("-" * 68)
    for l in lines[-25:]:
        print("  " + l)
    print("-" * 68)

    if not lines:
        print("ffplay 无任何输出（可能静默播放成功）")
    ok = alive or "error" not in " ".join(lines).lower()
    print("结论:", "ffplay 能稳定播放" if alive else "ffplay 未撑住")
    return 0 if alive else 1


if __name__ == "__main__":
    sys.exit(main())
