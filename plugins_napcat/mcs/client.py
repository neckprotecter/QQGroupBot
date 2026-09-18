"""MC 快照的带缓存取数封装 + 展示用常量（三个调用方共用）。

@查询 / 进服轮询 / 定时播报都会要快照。没有缓存的话三者在同一时刻会各打一次
SLP+RCON；有了缓存，并发 @查询 也只会触发一次探测，回复更快。
真正的取数逻辑（含 RCON 实现）在 plugins_napcat/_shared/mc.py，
配置项见那里的 McConfig。
"""
import asyncio
import os
import time

from .._shared.mc import McSnapshot, fetch_snapshot

# 快照缓存有效期（秒）。只用于合并同一时刻的并发请求，不影响数据新鲜度判断
_CACHE_TTL = 5

# 消息里最多列出多少个玩家名，超出的折叠成「还有 N 人」。
# 再超长由 truncate() 兜底（QQ 群消息超 1800 字会被整条拒收）
_MAX_NAMES = int(os.environ.get("MC_QUERY_MAX_NAMES", "50"))

_lock: asyncio.Lock | None = None
_cache: tuple[float, McSnapshot] | None = None


async def get_snapshot(max_age: float = _CACHE_TTL) -> McSnapshot:
    """取一次快照。max_age 内的缓存直接复用；传 0 强制重新探测。"""
    global _cache, _lock
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        now = time.monotonic()
        if _cache is not None and now - _cache[0] < max_age:
            return _cache[1]
        snap = await fetch_snapshot()
        _cache = (time.monotonic(), snap)
        return snap
