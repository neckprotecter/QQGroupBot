"""MC 快照的带缓存取数封装 + 展示用常量（三个调用方共用）。

@查询 / 进服轮询 / 定时播报都会要快照。没有缓存的话三者在同一时刻会各打一次
SLP+RCON；有了缓存，并发 @查询 也只会触发一次探测，回复更快。

缓存**按目标分开**：一个目标的探测结果不会顶掉另一个的，某个子服卡住也不影响
其余目标命中缓存。真正的取数逻辑（含 RCON 实现）在 plugins_napcat/_shared/mc.py，
目标定义在 _shared/mcservers.py。
"""
import asyncio
import os
import time
from collections.abc import Sequence

from .._shared.mc import McSnapshot, fetch_snapshots
from .._shared.mcservers import ServerTarget

# 快照缓存有效期（秒）。只用于合并同一时刻的并发请求，不影响数据新鲜度判断
_CACHE_TTL = 5

# 消息里最多列出多少个玩家名，超出的折叠成「还有 N 人」。
# 再超长由 truncate() 兜底（QQ 群消息超 1800 字会被整条拒收）
_MAX_NAMES = int(os.environ.get("MC_QUERY_MAX_NAMES", "50"))

_lock: asyncio.Lock | None = None
# target_id -> (写入时刻, 快照)
_cache: dict[str, tuple[float, McSnapshot]] = {}


async def get_snapshots(
    targets: Sequence[ServerTarget], max_age: float = _CACHE_TTL
) -> list[McSnapshot]:
    """并发取多个目标的快照，返回与 targets **同序**。max_age 内的缓存直接复用。

    锁覆盖整个取数阶段是刻意的：宁可让并发的第二个调用稍等，也不要对同一批
    目标打两遍 SLP+RCON（MC 服务端会为每次探测留下日志）。这与改动前单目标的
    行为一致 —— 当时也是一把锁罩住整个 fetch_snapshot。

    传 max_age=0 强制重新探测（进服轮询与定时播报都用 0：它们要的是此刻的真实
    状态，缓存里的几秒前结果会让进服事件错位）。
    """
    global _lock
    if not targets:
        return []
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:
        now = time.monotonic()
        stale = [
            t
            for t in targets
            if (entry := _cache.get(t.id)) is None or now - entry[0] >= max_age
        ]
        if stale:
            stamped = time.monotonic()
            for snap in await fetch_snapshots(stale):
                _cache[snap.target_id] = (stamped, snap)
        # 取数后必然每个目标都有缓存：fetch_snapshot 吞掉所有异常并返回不可达快照，
        # 不会漏目标。真漏了说明 fetch_snapshots 的「同序」契约破了，宁可炸也不要
        # 静默少返回一个目标（少一个目标会让汇总悄悄少一个服）。
        return [_cache[t.id][1] for t in targets]


async def get_snapshot(target: ServerTarget, max_age: float = _CACHE_TTL) -> McSnapshot:
    """取单个目标的快照。max_age 内的缓存直接复用；传 0 强制重新探测。"""
    return (await get_snapshots([target], max_age))[0]
