"""收藏（书签）的数据与持久化。

设计要点
--------
**存房间号，不存索引。** 收藏是按 room_id 唯一标识的，房间号是稳定的业务
标识（直播间改名、换主播都不影响），所以用它做 key。

**用 real room_id 而不是用户输入的那个数字。** B 站有短号：房间号 `6`
其实就是 `7734200`。如果按 `6` 存，下次按 `6` 查也能查到，但列表里会出现
同一个直播间被收藏两次（分别显示 `6` 和 `7734200`）。所以解析出真实
room_id 之后再存。

**存了 uname/title/cover 快照。** 打开软件时要立刻显示列表，不能等一轮
接口请求；而没开播的直播间拿不到当前标题，只有快照。所以快照 + 刷新时更新。

**不在这一层做网络重试/退避。** 那一套在 play.py 里已经有了；这里只在用户
点「刷新」或启动时跑一次，失败就保留旧状态并如实告诉界面。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time

# 收藏列表上限。纯粹是为了防手滑存几千条把界面卡住；
# 正常用户收藏几十个直播间就到头了。
MAX_BOOKMARKS = 200


def config_dir() -> str:
    """收藏文件的存放目录。

    放用户的 AppData 而不是程序目录：程序目录可能是只读的（解压到
    Program Files 之类），而收藏是用户数据，本来也不该跟着程序走。
    没写权限时退回程序目录，保证至少能用。
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "bililive")
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".writable")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return d
    except Exception:
        return os.path.dirname(os.path.abspath(__file__))


def store_path() -> str:
    return os.path.join(config_dir(), "bookmarks.json")


class BookmarkStore:
    """收藏列表。线程安全：界面线程和刷新线程都会碰它。"""

    def __init__(self, path: str | None = None):
        self.path = path or store_path()
        self._items: list[dict] = []
        self._lock = threading.RLock()
        self.load()

    # ---------------------------------------------------------------- 读写

    def load(self) -> None:
        """读盘。任何异常都退回空列表 —— 收藏读不出来不该让程序起不来。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items") if isinstance(data, dict) else data
            if not isinstance(items, list):
                items = []
        except Exception:
            items = []
        clean = []
        seen = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                rid = int(it.get("room_id"))
            except Exception:
                continue
            if rid <= 0 or rid in seen:
                continue
            seen.add(rid)
            clean.append({
                "room_id": rid,
                "uname": str(it.get("uname") or ""),
                "title": str(it.get("title") or ""),
                "cover": str(it.get("cover") or ""),
                # live_status: 1 开播 / 0 未开播 / -1 还没查过
                "live_status": int(it.get("live_status", -1)),
                "added_at": float(it.get("added_at") or 0),
            })
        with self._lock:
            self._items = clean

    def save(self) -> bool:
        """写盘。**先写临时文件再原子替换**，避免写到一半断电把列表写坏。

        写失败返回 False（磁盘满、只读等），由界面决定要不要提示。
        绝不能因为收藏写不进去就把整个程序弄崩。
        """
        with self._lock:
            payload = {"version": 1, "items": list(self._items)}
        try:
            d = os.path.dirname(self.path)
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".bm-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)      # 原子替换
            except Exception:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                raise
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- 查询

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def has(self, room_id: int | None) -> bool:
        if not room_id:
            return False
        with self._lock:
            return any(it["room_id"] == int(room_id) for it in self._items)

    def get(self, room_id: int | None) -> dict | None:
        if not room_id:
            return None
        with self._lock:
            for it in self._items:
                if it["room_id"] == int(room_id):
                    return dict(it)
        return None

    # ---------------------------------------------------------------- 增删

    def toggle(self, room_id: int, uname: str = "", title: str = "",
               cover: str = "", live_status: int | None = None) -> bool:
        """收藏/取消收藏。返回 True 表示**现在已收藏**。

        live_status 传 None 表示「还不知道」，存 -1 —— 不要默认成 0，
        那会把一个正在开播的直播间显示成「未开播」。
        """
        rid = int(room_id)
        ls = -1 if live_status is None else int(live_status)
        with self._lock:
            for i, it in enumerate(self._items):
                if it["room_id"] == rid:
                    del self._items[i]
                    self.save()
                    return False
            if len(self._items) >= MAX_BOOKMARKS:
                # 到上限就不加新的，但仍然把已有信息刷新一下
                return False
            self._items.append({
                "room_id": rid,
                "uname": uname or "",
                "title": title or "",
                "cover": cover or "",
                "live_status": ls,
                "added_at": time.time(),
            })
        self.save()
        return True

    def update_meta(self, room_id: int, uname: str = "", title: str = "",
                    cover: str = "", live_status: int | None = None) -> bool:
        """用最新信息更新一条收藏。返回是否真的改了（用来决定要不要重画）。"""
        rid = int(room_id)
        changed = False
        with self._lock:
            for it in self._items:
                if it["room_id"] != rid:
                    continue
                for key, val in (("uname", uname), ("title", title),
                                 ("cover", cover)):
                    if val and it.get(key) != val:
                        it[key] = val
                        changed = True
                if live_status is not None and it.get("live_status") != live_status:
                    it["live_status"] = int(live_status)
                    changed = True
                break
        if changed:
            self.save()
        return changed


def check_live(room_ids: list[int], client=None, timeout: float = 8.0,
               on_one=None, should_stop=None, skipped: list | None = None) -> dict:
    """查询一批房间的开播状态。返回 {room_id: {live_status, uname, title, cover}}。

    只查得到结果的房间，查不到的**不出现在返回里** —— 调用方据此保留旧状态，
    而不是把「网络失败」误当成「未开播」。
    这个区分很重要：断网时如果把所有收藏标成「未开播」，用户会以为主播都下播了。

    on_one(room_id, info) 每查到一个就回调一次，让界面能逐个更新，
    不用等全部查完（收藏多的时候体感差别很大）。
    should_stop() 返回 True 时提前结束 —— 用户连点刷新或关窗口时用得上。

    *** skipped ***

    提前结束时，**没轮到的房间号会追加进这个列表**。
    为什么要这个：原来提前结束就是静默放弃，那些房间会永远停在
    「状态未知」，直到用户手动点刷新 —— 收藏多、网络慢的时候看起来就是
    「最后那些永远是未知」。调用方拿到 skipped 之后可以接着查下一批，
    而不是把它们丢掉。
    """
    from .biliapi import BiliLiveClient, BiliApiError

    if client is None:
        client = BiliLiveClient(timeout=timeout)
    out = {}
    for pos, rid in enumerate(room_ids):
        if should_stop is not None:
            try:
                if should_stop():
                    # 本轮没轮到的全部交回调用方，别静默丢掉
                    if skipped is not None:
                        skipped.extend(int(x) for x in room_ids[pos:])
                    break
            except Exception:
                pass
        try:
            # room_meta 而不是 resolve_room：前者把 room_init（短号换算 +
            # 房间是否存在）和 getRoomBaseInfo（标题/主播/封面）合成一次调用。
            # 只用 resolve_room 的话拿不到标题和封面。
            info = client.room_meta(int(rid))
        except BiliApiError:
            continue            # 这个房间查不到，跳过，保留旧状态
        except Exception:
            continue
        rec = {
            "live_status": int(info["live_status"]) if info.get("live_status")
            is not None else -1,
            "uname": info.get("uname") or "",
            "title": info.get("title") or "",
            "cover": info.get("cover") or "",
        }
        out[int(rid)] = rec
        if on_one is not None:
            try:
                on_one(int(rid), rec)
            except Exception:
                pass
    return out
