"""哔哩哔哩直播 API 客户端 —— 只取「纯音频流」。

本文件中的所有端点与字段顺序都经过实测，注释里标了踩过的坑，改代码前先读。

核心事实（2026-10 实测）:
  1. `only_audio=1` 确实生效。返回的 FLV 里 audio tag 有、video tag 为 0，
     实测一帧视频都没有 —— 所以不需要 ffmpeg 抽音轨。
  2. 流地址约 1 小时过期（extra 里的 expires 字段），必须定时换新。
  3. URL 拼接是坑：base_url 是纯相对路径且结尾带 '?'，host 是自带 scheme 的
     完整 URL，extra 是不带前导 '&' 的签名参数。必须按
     host + path + '?' + base_query + '&' + extra 的顺序拼，否则域名会跑进 query。
  4. 房间号可能只是「短号」，真实 room_id 要经 room_init 换。
  5. 不需要登录、不需要 WBI 签名，只要 UA + Referer。
"""

from __future__ import annotations

import gzip
import json
import ssl
import time
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field

API_HOST = "https://api.live.bilibili.com"
LIVE_REFERER = "https://live.bilibili.com/"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_SSL_CTX = ssl.create_default_context()

# ---------------------------------------------------------------------------
# 直连 vs 走系统代理 —— 首播延迟里最大的一项，实测差 25 倍
#
# 实测（同机同请求 room_init）:
#     urllib 默认（走系统代理 http://127.0.0.1:7890）   5.138s
#     urllib 禁用代理（直连）                          0.200s
#
# 原因：urllib 会自动读系统代理设置。装了 Clash / v2ray 之类常驻工具后
# （注册表 ProxyEnable=1、ProxyServer=127.0.0.1:7890），每个 API 请求都要
# 绕一圈本地代理，固定多花约 5 秒。而 B 站 API 在国内本来就能直连。
#
# 策略：优先直连，失败再回退系统代理。
# 既拿到直连速度，又不让「必须走代理才能上网」的用户直接不可用。
# ---------------------------------------------------------------------------
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_PROXY_OPENER = None          # 懒加载；默认 opener 即走系统代理

# 直连连续失败这么多次后，本轮不再尝试直连，免得每个请求都白等一轮
_DIRECT_GIVE_UP_AFTER = 2
_direct_fail_streak = 0


def reset_direct_state() -> None:
    """恢复「优先直连」的尝试。重新连接时调用。"""
    global _direct_fail_streak
    _direct_fail_streak = 0


def _get_proxy_opener():
    global _PROXY_OPENER
    if _PROXY_OPENER is None:
        _PROXY_OPENER = urllib.request.build_opener()
    return _PROXY_OPENER


def system_proxy_in_use() -> str:
    """返回系统代理地址（没有则空串）。仅用于诊断输出。"""
    try:
        p = urllib.request.getproxies()
        return p.get("https") or p.get("http") or ""
    except Exception:
        return ""


class BiliApiError(RuntimeError):
    """接口返回非 0 code，或房间不可播放。"""


@dataclass
class AudioStream:
    """一条可播放的纯音频流。"""

    url: str
    protocol: str          # http_stream / http_hls
    format: str            # flv / ts / fmp4
    codec: str             # avc（B 站沿用这个字段名，即便内容是纯音频）
    current_qn: int
    accept_qn: list[int] = field(default_factory=list)
    expires_at: float = 0.0    # unix 时间戳；0 表示没解析出来
    audio_codec: str = ""
    room_id: int = 0

    @property
    def seconds_left(self) -> float:
        return self.expires_at - time.time() if self.expires_at else float("inf")

    def is_fresh(self, margin: float = 300.0) -> bool:
        """留 margin 秒余量，避免正播着 URL 突然失效。"""
        return self.seconds_left > margin


class BiliLiveClient:
    """只做两件事：房间号 -> 真实 room_id；room_id -> 纯音频流地址。"""

    def __init__(self, timeout: float = 20.0, cookie: str | None = None):
        self.timeout = timeout
        self._cookie = cookie
        self._buvid: str | None = None

    # ------------------------------------------------------------ HTTP 基础

    def headers(self, referer: str = LIVE_REFERER) -> dict[str, str]:
        """请求头。取流时也要用同一套（含 Cookie），否则可能 403。"""
        h = {
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "*/*",
            # 只声明 gzip。早期写成 "gzip, deflate" 但解码时只处理 gzip，
            # 一旦服务端真返回 deflate，json.loads 会直接吃压缩字节而炸。
            # 与其补一个用不到的 deflate 分支，不如不声明它。
            "Accept-Encoding": "gzip",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        ck = self._cookie or self._buvid
        if ck:
            h["Cookie"] = ck
        return h

    def _read_response(self, resp, max_bytes: int | None):
        data = resp.read() if max_bytes is None else resp.read(max_bytes)
        enc = (resp.headers.get("Content-Encoding") or "").lower().strip()
        if enc == "gzip":
            data = gzip.decompress(data)
        elif enc == "deflate":
            # 虽然请求头不再声明 deflate，仍留一条兼容分支，
            # 免得某些中间层自作主张压缩后才暴露问题。
            try:
                data = zlib.decompress(data)
            except zlib.error:
                data = zlib.decompress(data, -zlib.MAX_WBITS)
        return resp.status, dict(resp.headers), data

    def _get(self, url: str, referer: str = LIVE_REFERER,
             max_bytes: int | None = None):
        """GET 一个 URL。

        **直连优先，失败回退系统代理。** 这是首播延迟优化里最关键的一项：
        走代理每个请求要多花约 5 秒（实测），而 B 站 API 本来就能直连。

        直连连续失败 _DIRECT_GIVE_UP_AFTER 次后，本轮不再尝试直连，
        避免每个请求都要先等一次直连超时。
        """
        global _direct_fail_streak
        req = urllib.request.Request(url, headers=self.headers(referer))

        if _direct_fail_streak < _DIRECT_GIVE_UP_AFTER:
            try:
                with _DIRECT_OPENER.open(req, timeout=self.timeout) as resp:
                    _direct_fail_streak = 0
                    return self._read_response(resp, max_bytes)
            except Exception as e:
                _direct_fail_streak += 1
                if _direct_fail_streak >= _DIRECT_GIVE_UP_AFTER:
                    px = system_proxy_in_use()
                    if px:
                        # 只提示一次，避免刷屏
                        raise RuntimeError(
                            f"直连 {url.split('/')[2]} 失败，"
                            f"已切换到系统代理 {px}。最后一次错误: "
                            f"{type(e).__name__}: {e}") from e
                    raise

        with _get_proxy_opener().open(req, timeout=self.timeout) as resp:
            return self._read_response(resp, max_bytes)

    def ensure_buvid(self) -> str | None:
        """取 buvid3。部分接口不带它会返回 -352 风控。拿到后缓存在实例上。"""
        if self._buvid:
            return self._buvid
        try:
            _s, _h, raw = self._get(
                "https://api.bilibili.com/x/frontend/finger/spi",
                referer="https://www.bilibili.com/")
            b3 = (json.loads(raw.decode("utf-8", "replace")).get("data") or {}).get("b_3")
            if b3:
                self._buvid = f"buvid3={b3}"
        except Exception:
            pass
        return self._buvid

    def _api(self, path: str, params: dict | None = None) -> dict:
        url = API_HOST + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        _s, _h, raw = self._get(url)
        return json.loads(raw.decode("utf-8", "replace"))

    # ------------------------------------------------------------ 业务接口

    def resolve_room(self, room: int) -> dict:
        """短号 -> 真实 room_id。返回 room_init 的 data。"""
        r = self._api("/room/v1/Room/room_init", {"id": room})
        if r.get("code") != 0:
            raise BiliApiError(f"room_init({room}) 失败: code={r.get('code')} "
                               f"msg={r.get('message') or r.get('msg')}")
        return r["data"]

    def room_info(self, room_id: int) -> dict:
        """房间标题、主播名、开播状态。失败只返回空 dict，不致命。

        *** 这里换过接口，原因必看 ***
        原来用的是 /xlive/web-room/v1/index/getInfoByRoom。实测（2026-10）
        该接口**一律返回 code=-352**（B 站风控），即使带了合法的 buvid3、
        带上 b_nut 时间戳也照样 -352，而其余接口（room_init、取流）都正常。
        也就是说标题和主播名一直是取不到的，界面上那块永远不显示，
        而且因为异常被吞掉，连个错都看不到。

        现在改用 /xlive/web-room/v1/index/getRoomBaseInfo：
            * 同一个请求里就带回 title 和 uname，不用再调主播接口；
            * 实测 code=0 稳定可用，不需要登录、不需要 WBI 签名。

        返回结构是 {"by_room_ids": {"<room_id>": {...}}}，仍然把那个内层
        dict 直接返回，调用方不用关心外层包装。
        """
        try:
            r = self._api("/xlive/web-room/v1/index/getRoomBaseInfo",
                          {"room_ids": str(room_id),
                           "req_biz": "web_room_componet"})
            if r.get("code") == 0:
                by = ((r.get("data") or {}).get("by_room_ids") or {})
                # 键是字符串形式的 room_id；取不到就退回第一个，容错更宽
                info = by.get(str(room_id))
                if info is None and by:
                    info = next(iter(by.values()))
                if info:
                    return info
        except Exception:
            pass
        return {}

    def audio_streams(self, room_id: int, qn: int = 10000) -> list[AudioStream]:
        """拿纯音频流。只请求 http_stream/flv —— 实测这是唯一稳定给纯音频的组合。

        返回按偏好排序的候选列表（换 CDN 重试时用得上）。
        """
        self.ensure_buvid()
        params = {
            "room_id": room_id,
            "protocol": "0,1",
            "format": "0,1,2",
            "codec": "0,1",
            "qn": str(qn),
            "platform": "web",
            "only_audio": "1",   # 关键：只要音频
            "only_video": "0",
        }
        r = self._api("/xlive/web-room/v2/index/getRoomPlayInfo", params)
        if r.get("code") != 0:
            raise BiliApiError(f"getRoomPlayInfo 失败: code={r.get('code')} "
                               f"msg={r.get('message') or r.get('msg')}")

        data = r.get("data") or {}
        playurl_info = data.get("playurl_info")
        if not playurl_info:
            raise BiliApiError("playurl_info 为空：房间未开播、已下播或无权限")

        live_status = data.get("live_status")
        out: list[AudioStream] = []
        for stream in playurl_info["playurl"]["stream"]:
            proto = stream.get("protocol_name", "")
            for fmt in stream.get("format", []):
                fname = fmt.get("format_name", "")
                for codec in fmt.get("codec", []):
                    for idx in range(len(codec.get("url_info") or [])):
                        url = join_stream_url(codec, idx)
                        out.append(AudioStream(
                            url=url,
                            protocol=proto,
                            format=fname,
                            codec=codec.get("codec_name", ""),
                            current_qn=codec.get("current_qn", 0),
                            accept_qn=list(codec.get("accept_qn") or []),
                            expires_at=parse_expires(url),
                            audio_codec=(codec.get("audio_codecs") or {}).get("base", ""),
                            room_id=room_id,
                        ))
        if not out:
            raise BiliApiError(f"没有可用流（live_status={live_status}）")

        # flv + http_stream 优先：实测纯音频且最省事
        def rank(s: AudioStream) -> tuple:
            return (0 if (s.format == "flv" and s.protocol == "http_stream") else 1,
                    0 if s.format == "flv" else 1)

        out.sort(key=rank)
        return out

    def best_audio_stream(self, room_id: int, qn: int = 10000) -> AudioStream:
        return self.audio_streams(room_id, qn)[0]

    def race_audio_stream(self, candidates: list[AudioStream],
                          per_try_timeout: float = 12.0,
                          want_bytes: int = 16384) -> AudioStream | None:
        """在**同一优先级层内**并行竞速，返回最先真正出数据的那条。

        为什么值得并行（实测某次首播）:
            串行等第一条 CDN     5.65s
            并行抢最快           1.56s

        *** 只在同一层内竞速，不能跨层 ***
        早期实现直接对全列表竞速，结果踩了坑：HLS 的 m3u8 播放列表只有
        几百字节，首包远快于 FLV；而 FLV 首包要等一大块数据。
        于是「最快」实际选中了排序更低的 HLS，把 audio_streams() 里
        按实测依据定下的 flv 优先规则整个绕过了。

        所以这里先看首选层（按 candidates 里第一条的 format/protocol 判定），
        层内没成功才降到下一层。层内竞速仍然是安全的：同为 flv 时，
        「谁先给数据」就是可靠信号。
        """
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        # 按 (format, protocol) 分层；candidates 已是排好序的，所以层的顺序
        # 天然继承原有优先级。
        tiers: list[list[AudioStream]] = []
        seen: dict[tuple, int] = {}
        for c in candidates:
            key = (c.format, c.protocol)
            if key not in seen:
                seen[key] = len(tiers)
                tiers.append([])
            tiers[seen[key]].append(c)

        for tier in tiers:
            if len(tier) == 1:
                return tier[0]
            won = self._race_one_tier(tier, per_try_timeout, want_bytes)
            if won is not None:
                return won
        return None

    def _race_one_tier(self, tier: list[AudioStream], timeout: float,
                       want_bytes: int) -> AudioStream | None:
        """在单一层内并行探测，返回最先读到数据的那条。"""
        import threading

        winner: dict = {}
        lock = threading.Lock()
        done = threading.Event()

        def probe(cand: AudioStream) -> None:
            try:
                req = urllib.request.Request(cand.url, headers=self.headers())
                with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
                    first = resp.read(want_bytes)
                if not first:
                    return
            except Exception:
                return
            with lock:
                if "cand" not in winner:      # 谁先写入谁赢
                    winner["cand"] = cand
                    done.set()

        threads = [threading.Thread(target=probe, args=(c,), daemon=True)
                   for c in tier]
        for t in threads:
            t.start()
        done.wait(timeout=timeout + 2.0)
        for t in threads:
            t.join(timeout=0.2)
        return winner.get("cand")


# ------------------------------------------------------------------ 工具函数

def join_stream_url(codec_entry: dict, idx: int = 0) -> str:
    """把 getRoomPlayInfo 的 codec 条目拼成完整可播放 URL。

    *** 顺序不能错，这是踩过的坑 ***

      base_url = '/live-bvc/841048/live_x_2500.flv?'   纯相对路径，结尾带 '?'
      host     = 'https://cn-jxnc-cm-01-03.bilivideo.com'  自带 scheme 的完整 URL
      extra    = 'expires=...&pt=web&...'              签名参数，原样保留

    正确: host + path + '?' + base_query + '&' + extra

    试过但错的写法:
      * 字符串相加 base + host + extra
        -> 域名被塞进 query: '/live-bvc/...?https://cdn...&expires=...' 直接报
           unknown url type
      * urljoin(host + '/', base)
        -> 丢掉 base 中 '?' 之后的 query，把 extra 混进 path

    注意空值也要过滤：某次实测 base_url 结尾是 '?server=...', 而 extra 为空，
    若不过滤会产生结尾 '&' 或重复 '?'。
    """
    info = codec_entry["url_info"][idx]
    host = info["host"]
    if not host.startswith("http"):
        host = "https://" + host.rstrip("/")

    path, _, base_query = codec_entry["base_url"].partition("?")
    path = path.lstrip("/")

    url = host.rstrip("/") + "/" + path
    query = "&".join(p for p in (base_query, info.get("extra") or "") if p)
    if query:
        url += "?" + query
    return url


def parse_expires(url: str) -> float:
    """从流地址的 query 里抠出 expires，判断这条 URL 还能用多久。"""
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        for key in ("expires", "deadline"):
            vals = qs.get(key)
            if vals and vals[0].isdigit():
                return float(vals[0])
    except Exception:
        pass
    return 0.0
