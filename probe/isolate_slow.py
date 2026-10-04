"""隔离：为什么 urllib 要 5 秒，而 http.client 只要 0.12 秒？

怀疑对象（逐个排除）:
  A. Accept-Encoding: gzip   —— urllib 版带了这个，裸测试没带
  B. 库本身差异              —— http.client vs urllib.request
  C. 是否走了代理            —— urllib 会读环境变量/系统代理设置
  D. redirect 处理
  E. ssl.create_default_context 的开销

这是关键：如果是代理设置导致，那所有用户都可能中招，
优化方向就完全不同了。
"""

import http.client
import json
import os
import ssl
import sys
import time
import urllib.request

HOST = "api.live.bilibili.com"
PATH = "/room/v1/Room/room_init?id=26774400"
URL = f"https://{HOST}{PATH}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def timed(label, fn, n=1):
    ts = []
    err = None
    for _ in range(n):
        t0 = time.time()
        try:
            fn()
            ts.append(time.time() - t0)
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:60]}"
            ts.append(time.time() - t0)
    avg = sum(ts) / len(ts)
    flag = "  <-- 慢!" if avg > 1.0 else ""
    print(f"  {label:<46} {avg:6.3f}s{flag}"
          + (f"   {err}" if err else ""))
    return avg


print("=" * 74)
print("环境信息")
print("=" * 74)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
          "NO_PROXY", "no_proxy", "ALL_PROXY"):
    v = os.environ.get(k)
    if v:
        print(f"  {k} = {v}")
print(f"  urllib 检测到的代理: {urllib.request.getproxies()}")
try:
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion"
                         r"\Internet Settings")
    for name in ("ProxyEnable", "ProxyServer", "ProxyOverride",
                 "AutoConfigURL"):
        try:
            val, _ = winreg.QueryValueEx(key, name)
            print(f"  注册表 {name} = {val}")
        except FileNotFoundError:
            pass
    winreg.CloseKey(key)
except Exception as e:
    print(f"  读注册表失败: {e}")

print()
print("=" * 74)
print("逐项对比（每项跑 2 次取平均）")
print("=" * 74)

CTX = ssl.create_default_context()


def httpclient_plain():
    c = http.client.HTTPSConnection(HOST, 443, timeout=25, context=CTX)
    c.request("GET", PATH, headers={"User-Agent": UA,
                                    "Referer": "https://live.bilibili.com/"})
    r = c.getresponse()
    r.read()
    c.close()


def httpclient_gzip():
    c = http.client.HTTPSConnection(HOST, 443, timeout=25, context=CTX)
    c.request("GET", PATH, headers={
        "User-Agent": UA, "Referer": "https://live.bilibili.com/",
        "Accept-Encoding": "gzip", "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9"})
    r = c.getresponse()
    r.read()
    c.close()


def urllib_minimal():
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
        r.read()


def urllib_full_headers():
    req = urllib.request.Request(URL, headers={
        "User-Agent": UA, "Referer": "https://live.bilibili.com/",
        "Accept": "*/*", "Accept-Encoding": "gzip",
        "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
        r.read()


def urllib_gzip_only():
    req = urllib.request.Request(URL, headers={
        "User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
        r.read()


def urllib_no_proxy():
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(URL, headers={
        "User-Agent": UA, "Referer": "https://live.bilibili.com/",
        "Accept": "*/*", "Accept-Encoding": "gzip"})
    with op.open(req, timeout=25) as r:
        r.read()


a = timed("A. http.client 最简", httpclient_plain, 2)
b = timed("B. http.client + 完整请求头(含gzip)", httpclient_gzip, 2)
c = timed("C. urllib 最简(只有 UA)", urllib_minimal, 2)
d = timed("D. urllib + 完整请求头", urllib_full_headers, 2)
e = timed("E. urllib 只加 gzip", urllib_gzip_only, 2)
f = timed("F. urllib + 禁用代理(ProxyHandler {})", urllib_no_proxy, 2)

print()
print("=" * 74)
print("结论")
print("=" * 74)
if b < 1.0 and d > 3.0:
    print("  http.client 快、urllib 慢 -> 差异在**库**，不是请求头")
elif b > 3.0 and a < 1.0:
    print("  加 gzip 后 http.client 也慢 -> **Accept-Encoding: gzip 是元凶**")
elif f < 1.0 and d > 3.0:
    print("  禁用代理后 urllib 变快 -> **代理设置是元凶**")
else:
    print(f"  A={a:.2f} B={b:.2f} C={c:.2f} D={d:.2f} E={e:.2f} F={f:.2f}")
    print("  按上面数值判断哪一项显著更慢")
