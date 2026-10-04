"""收流引擎：把「房间号」变成「源源不断的 AAC 帧」。

职责:
  * 解析房间号 -> 真实 room_id -> 纯音频流地址
  * 后台线程持续读取 HTTP 流，喂给 FlvDemuxer，产出 AAC 帧
  * 按 URL 有效期（实测约 1 小时）**主动**续期，不等它断了才反应
  * 断线指数退避重连；主播下播时进入等待而不是疯狂重试

实测数据（用于设定各阈值）:
  * 纯音频流码率约 190 kbps，6 秒收 147456 字节
  * URL 有效期约 60 分钟
  * AAC-LC 48000Hz 2ch，时间戳连续
"""

from __future__ import annotations

import queue
import threading
import time
import urllib.request
import ssl
from dataclasses import dataclass

from .biliapi import AudioStream, BiliApiError, BiliLiveClient
from .flvdemux import AudioFrame, FlvDemuxer

_SSL_CTX = ssl.create_default_context()

# URL 剩余时间低于这个值就提前换新，避免正播着突然失效
RENEW_MARGIN_SECONDS = 600.0

# 断线重连退避
BACKOFF_START = 1.0
BACKOFF_MAX = 30.0

# 读流超时。实测有的 CDN 建连就要 17 秒，设太小会在慢 CDN 上永远连不上，
# 表现为无限重连。这里放到 30 秒，宁可慢也不要连不上。
READ_TIMEOUT = 30.0
CHUNK_SIZE = 16384

# 首包等待：连上后迟迟不来数据的容忍次数。超过就换下一个 CDN 候选。
FIRST_DATA_TRIES = 3


class StreamState:
    CONNECTING = "connecting"      # 正在建立连接
    PLAYING = "playing"            # 正常收流
    RENEWING = "renewing"          # 换新 URL
    OFFLINE = "offline"            # 主播未开播
    RECONNECTING = "reconnecting"  # 断了，退避重试中
    STOPPED = "stopped"


@dataclass
class EngineStats:
    state: str = StreamState.CONNECTING
    detail: str = ""
    frames: int = 0
    bytes_in: int = 0
    reconnects: int = 0
    renewals: int = 0
    started_at: float = 0.0
    playing_since: float = 0.0

    @property
    def uptime(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0


class LiveAudioEngine:
    """把直播音频变成一段可消费的 AAC 帧流。

    用法:
        eng = LiveAudioEngine(房间号)
        eng.start()
        for frame in eng.frames():    # 阻塞迭代，直到 stop()
            ...

    帧放进一个有界队列。队列满说明消费端（解码/播放）跟不上，
    此时**丢最旧的帧**而不是阻塞收流 —— 直播场景下追新比补旧重要。
    """

    def __init__(self, room: int, qn: int = 10000, queue_size: int = 256,
                 log=None, cookie: str | None = None):
        self.room = room
        self.qn = qn
        self.room_id: int | None = None
        self.stream_info: AudioStream | None = None
        # 一次接口调用会给多条跨 CDN 的候选流。实测不同 CDN 的建连速度
        # 差异极大（5.5s 到 17s），且有的会连上就 EOF，所以必须轮换重试，
        # 不能只用第一条。
        self._candidates: list[AudioStream] = []
        self._cand_idx = 0
        self.stats = EngineStats()
        self._log = log or (lambda msg: None)

        self._client = BiliLiveClient(cookie=cookie)
        self._q: queue.Queue[AudioFrame | None] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._demuxer_info = None

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.stats.started_at = time.time()
        self._thread = threading.Thread(target=self._run, name="bili-live-audio",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        # 用哨兵唤醒阻塞在 get() 上的消费者
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=timeout)

    def frames(self, timeout: float = 1.0):
        """生成器：阻塞产出 AAC 帧，直到 stop()。"""
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                continue
            if item is None:      # 哨兵
                break
            yield item

    @property
    def demuxer_info(self):
        return self._demuxer_info

    # ------------------------------------------------------------ 主循环

    def _run(self) -> None:
        backoff = BACKOFF_START
        while not self._stop.is_set():
            try:
                if self.stream_info is None or not self.stream_info.is_fresh(
                        RENEW_MARGIN_SECONDS):
                    self._acquire_stream()
                self._pump()
                # _pump 正常返回 = 流被对端正常关闭，属于正常换流
                backoff = BACKOFF_START
            except BiliApiError as e:
                # 接口层错误：可能没开播，也可能被限流
                self._set_state(StreamState.OFFLINE, str(e))
                self._log(f"接口错误: {e}")
                self.stream_info = None
                self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
            except Exception as e:
                if self._stop.is_set():
                    break
                self.stats.reconnects += 1
                self._set_state(StreamState.RECONNECTING,
                                f"{type(e).__name__}: {e}")
                self._log(f"连接异常({type(e).__name__}: {e})，{backoff:.0f}s 后重试")
                self.stream_info = None   # 强制下次重新取地址
                self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

        self._set_state(StreamState.STOPPED, "")
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _set_state(self, state: str, detail: str = "") -> None:
        self.stats.state = state
        self.stats.detail = detail

    def _acquire_stream(self) -> None:
        """解析房间并取一组新鲜的纯音频流候选。"""
        if self.room_id is None:
            self._set_state(StreamState.CONNECTING, "解析房间号")
            info = self._client.resolve_room(self.room)
            self.room_id = info["room_id"]
            self._log(f"房间 {self.room} -> room_id={self.room_id}")

        self._set_state(StreamState.RENEWING if self.stats.renewals
                        else StreamState.CONNECTING, "获取音频流地址")
        cands = self._client.audio_streams(self.room_id, self.qn)
        if self._candidates:
            self.stats.renewals += 1
        self._candidates = cands
        self._cand_idx = 0
        self.stream_info = cands[0]
        self._log(f"拿到 {len(cands)} 条候选流，首选 "
                  f"{self.stream_info.protocol}/{self.stream_info.format} "
                  f"host={self.stream_info.url.split('/')[2]} "
                  f"有效 {self.stream_info.seconds_left / 60:.1f} 分钟")

    def _rotate_candidate(self, reason: str) -> bool:
        """换下一条候选流。返回 False 表示候选已用尽。"""
        if not self._candidates:
            return False
        self._cand_idx += 1
        if self._cand_idx >= len(self._candidates):
            self._cand_idx = 0
            self._candidates = []
            self.stream_info = None
            self._log(f"所有 CDN 候选都不可用（{reason}），稍后重新取地址")
            return False
        self.stream_info = self._candidates[self._cand_idx]
        host = self.stream_info.url.split("/")[2]
        self._log(f"换用候选 #{self._cand_idx} host={host}（{reason}）")
        return True

    def _pump(self) -> None:
        """读流直到断/超期/停止。正常返回表示该换流了。"""
        assert self.stream_info is not None
        dm = FlvDemuxer()
        req = urllib.request.Request(self.stream_info.url,
                                    headers=self._client.headers())
        deadline = (self.stream_info.expires_at - RENEW_MARGIN_SECONDS
                    if self.stream_info.expires_at else None)

        got_any = False
        try:
            resp = urllib.request.urlopen(req, timeout=READ_TIMEOUT,
                                         context=_SSL_CTX)
        except Exception as e:
            # 建连失败：换下一个 CDN，而不是干等退避
            if self._rotate_candidate(f"建连失败 {type(e).__name__}"):
                return
            raise

        with resp:
            self._set_state(StreamState.PLAYING, "接收中")
            self.stats.playing_since = time.time()
            self._demuxer_info = dm.info

            while not self._stop.is_set():
                if deadline and time.time() > deadline:
                    self._log("流地址接近过期，主动续期")
                    return
                try:
                    chunk = resp.read(CHUNK_SIZE)
                except (TimeoutError, OSError):
                    if not got_any:
                        # 连上了却一直读不到数据，多半是这条 CDN 有问题
                        if self._rotate_candidate("建连后无数据"):
                            return
                    continue
                if not chunk:
                    if not got_any and self._rotate_candidate("连上即 EOF"):
                        return
                    self._log("服务端关闭了流")
                    return

                got_any = True
                self.stats.bytes_in += len(chunk)
                dm.feed(chunk)
                for frame in dm.frames():
                    self.stats.frames += 1
                    self._offer(frame)

    def _offer(self, frame: AudioFrame) -> None:
        """放入队列；满了就丢最旧的，保证追新。"""
        while True:
            try:
                self._q.put_nowait(frame)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()   # 丢最旧
                except queue.Empty:
                    return
