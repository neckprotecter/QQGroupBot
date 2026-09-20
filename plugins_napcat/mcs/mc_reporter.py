"""MC 进服提醒 + 定时播报（NapCat / OneBot v11 版）。

两个后台循环，模式沿用 oopz/auto_reporter.py：@driver.on_startup 起 task，
推送走 _shared/push 的 send_to_groups，整点对齐走 _shared/schedule。

只推进服、不推退服（用户选择）。退服的人仍会被基线自然吸收，只是不产生消息。

.env 配置：
  MC_WATCH_GROUP=<群号>         进服提醒目标群（逗号分隔多群，留空 = 不启用）
  MC_WATCH_INTERVAL_SEC=10      轮询间隔（秒），决定进服被发现的延迟
  MC_JOIN_MIN_INTERVAL_SEC=15   进服推送最小间隔（秒），窗口内的进服合并成一条；
                                设 0 = 不限流。最大额外延迟 ≈ 本值 + 一个轮询间隔
  MC_NOTIFY_SERVER_STATE=true   服务器连不上 / 恢复时是否提醒
  MC_REPORT_GROUP=<群号>        定时播报目标群（逗号分隔多群，留空 = 不启用）
  MC_REPORT_INTERVAL_MIN=60     播报间隔（分钟），整点对齐；无人在线时静默跳过
"""
import asyncio
import os
import random
import time
from datetime import datetime

from nonebot import get_driver
from nonebot.log import logger

from .._shared.mc import SLP_UNPARSEABLE, McSnapshot
from .._shared.push import send_to_groups, truncate
from .._shared.schedule import seconds_until_slot
from .._shared.mcservers import ServerConfigError, ServerTarget, default_book
from .client import _MAX_NAMES, get_snapshot

driver = get_driver()

_WATCH_GROUPS = [
    s.strip() for s in os.environ.get("MC_WATCH_GROUP", "").split(",") if s.strip()
]
_WATCH_INTERVAL_SEC = float(os.environ.get("MC_WATCH_INTERVAL_SEC", "10"))
# 限流窗口 = 进服提醒的最大额外延迟（最坏 = 本值 + 一个轮询间隔）。设 0 = 不限流，
# 进服立刻推；一轮内进服 ≥_BURST 人仍会合并成一条，不会因去掉限流而刷屏。
_JOIN_MIN_INTERVAL_SEC = float(os.environ.get("MC_JOIN_MIN_INTERVAL_SEC", "15"))
_NOTIFY_SERVER_STATE = os.environ.get("MC_NOTIFY_SERVER_STATE", "").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_REPORT_GROUPS = [
    s.strip() for s in os.environ.get("MC_REPORT_GROUP", "").split(",") if s.strip()
]
_REPORT_INTERVAL_MIN = int(os.environ.get("MC_REPORT_INTERVAL_MIN", "60"))

# 一轮内进服人数达到这个数就合并成一条，避免刷屏
_BURST = 3
# 连续这么多轮探测失败才宣布服务器离线（单轮网络抖动不报）
_OFFLINE_THRESHOLD = 2

_JOIN_TEMPLATES = [
    "🎮 {who} 加入了 {server}（当前 {count} 人在线）",
    "🚪 {who} 溜进了 {server}（当前 {count} 人在线）",
    "⛏️ {who} 上线了 {server}（当前 {count} 人在线）",
    "🌍 {who} 出现在了 {server}（当前 {count} 人在线）",
]

# ---------------------------------------------------------------- 状态

_known: set[str] = set()  # 基线：上一轮的在线玩家名
_initialized = False  # 首次成功轮询只建基线，不把存量玩家当成新进服
_fail_streak = 0  # 连续探测失败轮数
_server_down: bool | None = None  # None = 启动后还没探测过
_last_state: tuple | None = None  # 取数状态，仅用于日志去重
_pending: list[str] = []  # 限流窗口内攒下的进服事件
_last_push: float | None = None


def _state_key(snap: McSnapshot) -> tuple:
    if not snap.reachable:
        return ("down",)
    if not snap.names_complete:
        return ("partial",)
    return ("ok", snap.names_source)


def _log_state_transition(snap: McSnapshot) -> None:
    """取数状态只在跃迁时打日志。

    每 20 秒一轮的重复告警会把日志冲成噪音，而 DEPLOY.md 恰恰教用户看日志尾巴
    排查问题。这里只报「变好」「变坏」两个时刻。
    """
    global _last_state
    key = _state_key(snap)
    if key == _last_state:
        return
    _last_state = key
    if key[0] == "ok":
        logger.info("MC 名单来源 {}（完整，{} 人在线）", snap.names_source, snap.count)
    elif key[0] == "partial":
        logger.warning("MC 名单不完整，进服提醒已暂停：{}", snap.error or "原因未知")
    else:
        logger.warning("MC 服务器探测失败：{}", snap.error or "原因未知")


def _primary() -> ServerTarget | None:
    """进服提醒与定时播报当前看的目标。

    **当前阶段只看「主服」**（mcs_servers.toml 的 [defaults].primary，不写就是列表
    第一个）。改成逐目标对账、多目标播报是下一步的事，届时这个函数会被替换掉。
    """
    try:
        return default_book().primary_target
    except ServerConfigError as exc:
        logger.error("读取 mcs_servers.toml 失败：{}", exc)
        return None


def _format_joins(names: list[str], count: int, server_name: str) -> str:
    if len(names) == 1:
        who = names[0]
    elif len(names) <= _BURST:
        who = "、".join(names)
    else:
        who = f"{names[0]}、{names[1]} 等 {len(names)} 人"
    return random.choice(_JOIN_TEMPLATES).format(
        who=who, server=server_name, count=count
    )


# ---------------------------------------------------------------- 进服提醒

async def _tick() -> None:
    global _known, _initialized, _fail_streak, _server_down, _pending, _last_push

    target = _primary()
    if target is None:
        return  # 配置读不了，_primary 已经打过日志；下一轮再试

    snap = await get_snapshot(target, max_age=0)
    _log_state_transition(snap)

    if not snap.reachable:
        _fail_streak += 1
        if _fail_streak >= _OFFLINE_THRESHOLD and _server_down is not True:
            _server_down = True
            logger.warning(
                "{} 连续 {} 轮探测失败", target.name, _fail_streak
            )
            if _NOTIFY_SERVER_STATE:
                # 「答了但答不对」（多半是刚启动还在加载）和「连不上」要分开说：
                # 一律叫「连不上了」会让人去开服，而它其实正开着。
                why = "应答异常" if snap.error_kind == SLP_UNPARSEABLE else "连不上了"
                await send_to_groups(
                    _WATCH_GROUPS,
                    f"⚠️ {target.name} {why}（已连续 {_fail_streak} 轮探测失败）",
                )
        return  # 探测失败时绝不动基线，否则恢复时整服的人会被当成新进服

    _fail_streak = 0
    if _server_down is True:
        _server_down = False
        logger.info("{} 已恢复", target.name)
        if _NOTIFY_SERVER_STATE:
            await send_to_groups(
                _WATCH_GROUPS,
                f"✅ {target.name} 已恢复，当前 {snap.count} 人在线",
            )

    if not snap.names_complete:
        # 名单完整性过不了就绝不更新基线：残缺名单（比如只有 12 条随机 sample）
        # 会让下一轮「换了一批人」被误判成大量进服
        return

    current = set(snap.names)
    if not _initialized:
        _known = current
        _initialized = True
        logger.info("MC 进服检测已建立基线：{} 人在线", len(current))
        return

    joined = [n for n in snap.names if n not in _known]
    _known = current
    if joined:
        _pending.extend(joined)

    if not _pending:
        return

    now = time.monotonic()
    # 限流窗口内的进服攒起来合并成一条。这个检查必须**每一轮都做**，不能只在
    # 「这一轮有人进服」时做——否则「甲进服占满窗口 → 乙随后进服被攒下 → 之后没人
    # 再进服」时，乙那条会一直压着等下一个进服，没人来就永远发不出；有人来也会被
    # 拖到窗口结束、和后面的人合并成一条，看起来就是「进服提醒延迟很久」。
    if _last_push is not None and now - _last_push < _JOIN_MIN_INTERVAL_SEC:
        if joined:
            logger.info(
                "MC 进服提醒限流中：{} 人待推送，窗口还剩 {:.0f}s",
                len(_pending),
                _JOIN_MIN_INTERVAL_SEC - (now - _last_push),
            )
        return

    names, _pending = _pending, []
    _last_push = now
    await send_to_groups(_WATCH_GROUPS, _format_joins(names, snap.count, target.name))


async def _watch_loop() -> None:
    if not _WATCH_GROUPS:
        logger.info("未配置 MC_WATCH_GROUP，进服提醒未启用")
        return
    await asyncio.sleep(15)  # 等 bot 连上 NapCat
    while True:
        started = time.monotonic()
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("MC 进服检测异常: {}", exc)
        # 从本轮结束时刻起算剩余时间，避免探测耗时（最坏 timeout×2）让间隔漂移
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, _WATCH_INTERVAL_SEC - elapsed))


# ---------------------------------------------------------------- 定时播报

async def _build_report_message() -> str | None:
    """生成播报文案。服务器不可达或无人在线时返回 None（静默跳过，与 oopz 一致）。"""
    target = _primary()
    if target is None:
        return None

    snap = await get_snapshot(target, max_age=0)
    if not snap.reachable:
        logger.info("MC 定时播报：{} 不可达，本次跳过", target.name)
        return None
    if snap.count == 0:
        return None

    head = f"现在有 {snap.count} 位小伙伴在线"
    if snap.max_players:
        head += f"（上限 {snap.max_players}）"

    lines = [f"📣 {target.name} 播报 · {datetime.now():%H:%M}", "━━━━━━━━━━", head]
    if snap.names_complete:
        shown = snap.names[:_MAX_NAMES]
        lines.extend(f"  • {n}" for n in shown)
        if len(snap.names) > len(shown):
            lines.append(f"  …还有 {len(snap.names) - len(shown)} 人")
    elif snap.names:
        lines.extend(f"  • {n}" for n in snap.names[:_MAX_NAMES])
        lines.append("（名单可能不完整）")
    else:
        lines.append("（未能取到名单）")
    lines.append("\n想一起玩的，直接进服找他们～")
    return truncate("\n".join(lines))


async def _report_loop() -> None:
    if not _REPORT_GROUPS:
        logger.info("未配置 MC_REPORT_GROUP，定时播报未启用")
        return
    await asyncio.sleep(15)  # 等 bot 连上 NapCat、MC 端口可达
    while True:
        try:
            # 睡到下一个整点槽位；每次从当前时刻重新对齐，不累积漂移
            delay = seconds_until_slot(_REPORT_INTERVAL_MIN)
            if delay:
                await asyncio.sleep(delay)
            msg = await _build_report_message()
            if msg is None:
                logger.info("MC 定时播报：当前无人在线，本次跳过")
            else:
                await send_to_groups(_REPORT_GROUPS, msg)
                logger.info("MC 定时播报已推送到 {} 个群", len(_REPORT_GROUPS))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("MC 定时播报异常: {}", exc)
            await asyncio.sleep(60)  # 出错别立刻重试，等一分钟再对齐下一次


@driver.on_startup
async def _start_background_tasks() -> None:
    asyncio.create_task(_watch_loop())
    asyncio.create_task(_report_loop())
