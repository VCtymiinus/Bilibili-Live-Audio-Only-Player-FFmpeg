"""深挖：为什么每个 API 请求都要约 5 秒？

把一次请求拆成 DNS / TCP / TLS / 首字节 / 完整响应，
定位这 5 秒到底花在哪个阶段。

同时测：复用连接（keep-alive）能不能省掉这个开销？
—— 这决定优化方向是「并行请求」还是「复用连接」。
"""

import http.client
import json
import socket
import ssl
import sys
import time

HOST = "api.live.bilibili.com"
PATH = "/room/v1/Room/room_init?id=26774400"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def phase_breakdown():
    print("=" * 70)
    print("单次请求的阶段分解")
    print("=" * 70)

    # 1. DNS
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)
        dns = time.time() - t0
        ips = sorted({i[4][0] for i in infos})
        print(f"  DNS 解析        {dns:6.3f}s   {len(ips)} 个 IP: {ips[:4]}")
    except Exception as e:
        print(f"  DNS 失败: {e}")
        return

    # 2. TCP
    ip = ips[0]
    t0 = time.time()
    try:
        s = socket.create_connection((ip, 443), timeout=15)
        tcp = time.time() - t0
        print(f"  TCP 连接        {tcp:6.3f}s   -> {ip}:443")
    except Exception as e:
        print(f"  TCP 失败: {e}")
        return

    # 3. TLS
    ctx = ssl.create_default_context()
    t0 = time.time()
    try:
        ss = ctx.wrap_socket(s, server_hostname=HOST)
        tls = time.time() - t0
        print(f"  TLS 握手        {tls:6.3f}s   {ss.version()}")
    except Exception as e:
        print(f"  TLS 失败: {e}")
        s.close()
        return

    # 4. 请求到首字节
    req = (f"GET {PATH} HTTP/1.1\r\nHost: {HOST}\r\n"
           f"User-Agent: {UA}\r\nReferer: https://live.bilibili.com/\r\n"
           f"Accept: */*\r\nConnection: close\r\n\r\n")
    t0 = time.time()
    ss.sendall(req.encode())
    first = ss.recv(1)
    t_first = time.time() - t0
    print(f"  请求->首字节    {t_first:6.3f}s")

    # 5. 读完整响应
    total = first
    while True:
        b = ss.recv(65536)
        if not b:
            break
        total += b
    t_all = time.time() - t0
    print(f"  读完整响应      {t_all:6.3f}s   {len(total)} 字节")
    ss.close()

    print(f"\n  => 单次总计 {dns+tcp+tls+t_all:.2f}s"
          f"  其中网络建连 {dns+tcp+tls:.2f}s，服务端+传输 {t_all:.2f}s")
    return dns + tcp + tls, t_all


def keepalive_test():
    print("\n" + "=" * 70)
    print("连接复用测试：同一连接上发 3 个请求")
    print("=" * 70)
    ctx = ssl.create_default_context()
    t0 = time.time()
    conn = http.client.HTTPSConnection(HOST, 443, timeout=20, context=ctx)
    conn.connect()
    print(f"  建连            {time.time()-t0:6.3f}s")
    for i in range(3):
        t0 = time.time()
        conn.request("GET", PATH, headers={
            "User-Agent": UA, "Referer": "https://live.bilibili.com/",
            "Accept": "*/*", "Connection": "keep-alive"})
        r = conn.getresponse()
        body = r.read()
        dt = time.time() - t0
        try:
            code = json.loads(body.decode("utf-8", "replace")).get("code")
        except Exception:
            code = "?"
        print(f"  第{i+1}个请求      {dt:6.3f}s   code={code}  {len(body)}B")
    conn.close()


def parallel_test():
    print("\n" + "=" * 70)
    print("并行测试：3 个请求同时发出")
    print("=" * 70)
    import threading

    results = {}

    def one(i, path):
        # 故意用不同接口，模拟真实场景里的三个独立请求
        paths = ["/room/v1/Room/room_init?id=26774400",
                 "/xlive/web-room/v1/index/getInfoByRoom?room_id=26774400",
                 "/room/v1/Room/room_init?id=1"]
        t0 = time.time()
        try:
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(HOST, 443, timeout=25, context=ctx)
            conn.request("GET", paths[i], headers={
                "User-Agent": UA, "Referer": "https://live.bilibili.com/",
                "Accept": "*/*"})
            r = conn.getresponse()
            body = r.read()
            conn.close()
            results[i] = (time.time() - t0, len(body))
        except Exception as e:
            results[i] = (time.time() - t0, f"{type(e).__name__}")

    t0 = time.time()
    ths = [threading.Thread(target=one, args=(i, None)) for i in range(3)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = time.time() - t0
    for i in sorted(results):
        print(f"  请求{i+1}: {results[i][0]:6.2f}s  {results[i][1]}")
    print(f"\n  并行总耗时(墙钟) {wall:.2f}s "
          f"（串行会是 {sum(v[0] for v in results.values()):.2f}s）")
    return wall


if __name__ == "__main__":
    r = phase_breakdown()
    keepalive_test()
    parallel_test()
    print("\n" + "=" * 70)
