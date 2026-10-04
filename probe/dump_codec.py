"""原样 dump getRoomPlayInfo 的 codec 条目结构 —— 上一轮 URL 拼接拼错了，
说明字段布局与我假设的 base_url + host + extra 不一致。看清楚再写代码。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _common import api  # noqa: E402

ROOM = 22388070


def main():
    params = {"room_id": ROOM, "protocol": "0,1", "format": "0,1,2", "codec": "0,1",
              "qn": "10000", "platform": "web", "only_audio": "1", "only_video": "0"}
    r = api("/xlive/web-room/v2/index/getRoomPlayInfo", params)
    pi = r["data"]["playurl_info"]

    print("### playurl 顶层键:", list(pi["playurl"].keys()))
    print("### 顶层字段（非 stream 部分）:")
    for k, v in pi["playurl"].items():
        if k != "stream":
            print(f"    {k} = {json.dumps(v, ensure_ascii=False)[:200]}")

    stream = pi["playurl"]["stream"][0]
    print("\n### stream[0] 键:", list(stream.keys()))
    fmt = stream["format"][0]
    print("### format[0] 键:", list(fmt.keys()))
    codec = fmt["codec"][0]
    print("### codec[0] 键:", list(codec.keys()))

    print("\n### codec[0] 完整内容（url_info 里 extra 截断到 120 字符）:")
    trimmed = dict(codec)
    trimmed["url_info"] = [
        {**u, "extra": (u.get("extra") or "")[:120] + " ...[截断]"}
        for u in codec["url_info"]
    ]
    print(json.dumps(trimmed, ensure_ascii=False, indent=2))

    info = codec["url_info"][0]
    print("\n### 单个 url_info[0] 各字段实值:")
    for k, v in info.items():
        print(f"    {k:<12} = {str(v)[:120]}")
    print(f"\n### base_url = {codec['base_url']!r}")
    print(f"### host 字段是否为空 -> {not info.get('host')}")


if __name__ == "__main__":
    main()
