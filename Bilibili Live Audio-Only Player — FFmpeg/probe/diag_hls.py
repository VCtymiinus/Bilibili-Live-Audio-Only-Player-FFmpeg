"""诊断：房间 21144080 的流为什么播不了？

现象：所有候选（都是 HLS/fmp4 m3u8）在 ffplay 里 0.5 秒就 "I/O error" 退出。
而另一个房间（26774400）拿到的是纯音频 FLV，能正常播。

要弄清：
  1. 这个房间到底给了哪些流类型
  2. HLS 地址用 Python 直接拉能不能通
  3. ffplay 播这个 HLS 报什么错（完整 stderr）
  4. 是不是 only_audio=1 在这个房间上表现不同
"""

import gzip
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bililive.biliapi import BiliLiveClient, join_stream_url  # noqa: E402

ROOM = int(sys.argv[1]) if len(sys.argv) > 1 else 21144080
FFPLAY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "tools", "ffplay.exe")


def main():
    print("=" * 74)
    c = BiliLiveClient()
    info = c.resolve_room(ROOM)
    rid = info["room_id"]
    meta = c.room_info(rid)
    ri = meta.get("room_info") or {}
    ai = (meta.get("anchor_info") or {}).get("base_info") or {}
    print(f"房间 {ROOM} -> {rid}  live_status={info.get('live_status')}")
    print(f"  {ai.get('uname')} | {ri.get('title')}")

    cands = c.audio_streams(rid)
    print(f"\n拿到 {len(cands)} 条候选:")
    for i, s in enumerate(cands):
        print(f"  [{i}] {s.protocol:<12} {s.format:<5} {s.codec:<6} "
              f"audio={s.audio_codec:<12} host={s.url.split('/')[2][:28]}")

    # ---- 逐条用 Python 直接拉 ----
    print("\n--- Python 直接拉取（每条试 8 秒或 256KB）---")
    hdr = c.headers()
    good = []
    for i, s in enumerate(cands):
        dm_magic = b""
        t0 = time.time()
        try:
            req = urllib.request.Request(s.url, headers=hdr)
            with urllib.request.urlopen(req, timeout=20,
                                       context=ssl.create_default_context()) as r:
                data = r.read(262144)
            dt = time.time() - t0
            kind = ("m3u8" if data[:7] == b"#EXTM3U" else
                    "FLV" if data[:3] == b"FLV" else
                    data[:8].hex(" "))
            print(f"  [{i}] {dt:5.2f}s  {len(data):>7}B  类型={kind}")
            if data[:7] == b"#EXTM3U":
                # 打印播放列表前几行，看分片地址
                for line in data.decode("utf-8", "replace").splitlines()[:8]:
                    print(f"        {line}")
            good.append(i)
        except Exception as e:
            print(f"  [{i}] 失败 {type(e).__name__}: {str(e)[:60]}")

    # ---- 用 ffplay 试播，打印完整 stderr ----
    print("\n--- ffplay 试播（每条 6 秒，显示完整输出）---")
    for i, s in enumerate(cands[:2]):
        print(f"\n  === 候选 [{i}] {s.protocol}/{s.format} ===")
        hdrs = "".join(f"{k}: {v}\r\n" for k, v in hdr.items())
        env = dict(os.environ)
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "directsound"
        p = subprocess.Popen(
            [FFPLAY, "-hide_banner", "-loglevel", "info", "-nodisp", "-vn",
             "-autoexit", "-volume", "0", "-infbuf", "-tls_verify", "0",
             "-headers", hdrs, s.url],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=0x08000000, env=env)
        time.sleep(6)
        alive = p.poll() is None
        if alive:
            p.terminate()
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()
        out = p.stdout.read().decode("utf-8", "replace") if p.stdout else ""
        print(f"    存活={alive}  退出码={p.returncode}")
        for line in out.splitlines()[-18:]:
            if line.strip():
                print(f"      {line}")

    print("\n" + "=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
