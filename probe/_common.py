"""探测用共享工具：极简 HTTP GET（标准库，无需第三方依赖）。

已实测结论（勿删，后续代码依赖这些事实）：
  * api.live.bilibili.com 只需 User-Agent + Referer 即可访问，无需登录、无需 WBI 签名。
  * Invoke-RestMethod / curl.exe 在本机受限环境下因 schannel SEC_E_NO_CREDENTIALS 失败，
    Python 的 OpenSSL 栈正常 —— 因此一律走 Python。
  * 部分接口（如 second/getList）不带 buvid3 cookie 会返回 code=-352 风控。
"""

import gzip
import json
import ssl
import urllib.parse
import urllib.request

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_CTX = ssl.create_default_context()


def _request(url, referer="https://live.bilibili.com/", extra_headers=None,
             cookie=None, timeout=25):
    headers = {
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
    return urllib.request.Request(url, headers=headers)


def get_bytes(url, referer="https://live.bilibili.com/", extra_headers=None,
              cookie=None, timeout=25, max_bytes=None):
    """GET 原始字节。max_bytes 用于只取流开头做嗅探，避免拉整个流。"""
    req = _request(url, referer, extra_headers, cookie, timeout)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
        data = resp.read() if max_bytes is None else resp.read(max_bytes)
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return resp.status, dict(resp.headers), data


def get_json(url, referer="https://live.bilibili.com/", extra_headers=None,
             cookie=None, timeout=25):
    status, _headers, raw = get_bytes(url, referer, extra_headers, cookie, timeout)
    return status, json.loads(raw.decode("utf-8", "replace"))


def api(path, params=None, cookie=None, timeout=25):
    """调用 api.live.bilibili.com 并返回解析后的 JSON。"""
    base = "https://api.live.bilibili.com" + path
    if params:
        base += "?" + urllib.parse.urlencode(params)
    _status, payload = get_json(base, cookie=cookie, timeout=timeout)
    return payload


def join_stream_url(codec_entry, idx=0):
    """把 getRoomPlayInfo 返回的 codec 条目拼成可播放的完整 URL。

    *** 这里踩过两个坑，务必按下面的顺序手工拼 ***

      base_url = '/live-bvc/841048/live_..._2500.flv?'   <- 纯相对路径，结尾带 '?'
      host     = 'https://cn-jxnc-cm-01-03.bilivideo.com' <- 自带 scheme 的完整 URL
      extra    = 'expires=...&pt=web&...'                 <- 签名参数，必须原样保留

    正确顺序: host + path + '?' + base_query + '&' + extra

    错误做法（都试过，都错）:
      * 直接字符串相加 base + host + extra
        -> host 被塞进 query，变成 '/live-bvc/...?https://cdn...&expires=...'
      * urljoin(host + '/', base)
        -> 丢掉了 base 里 '?' 之后的 query，或把 extra 混进 path
    """
    info = codec_entry["url_info"][idx]
    host = info["host"]
    if not host.startswith("http"):
        host = "https://" + host

    raw_base = codec_entry["base_url"]
    path, _, base_query = raw_base.partition("?")
    path = path.lstrip("/")

    url = host.rstrip("/") + "/" + path
    query = "&".join(p for p in (base_query, info.get("extra") or "") if p)
    if query:
        url += "?" + query
    return url
