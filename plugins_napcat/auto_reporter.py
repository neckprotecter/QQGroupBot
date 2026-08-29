"""定时播报 + 进频道欢迎（NapCat / OneBot v11 版）。

复用 plugins_napcat/oopz_stats.py 的 Oopz REST 客户端单例与查询函数，
走 NapCat 主动推送（send_group_msg），不受官方主动推送停用限制。

.env 配置：
  NAPCAT_REPORT_GROUP=<群号>          定时播报目标群（逗号分隔多群，留空 = 不启用）
  NAPCAT_REPORT_INTERVAL_MIN=30       播报间隔（分钟）；整点对齐（落在 :00 / :N / :2N… 时刻），仅在 Oopz 有人在线时推送，无人时静默跳过
  NAPCAT_WELCOME_GROUP=<群号>         欢迎消息目标群（逗号分隔多群，留空 = 不启用）
  NAPCAT_WELCOME_INTERVAL_SEC=15      进频道轮询间隔（秒）
  NAPCAT_WELCOME_CHANNELS=频道名,...   可选：只欢迎这些频道（逗号分隔），留空 = 统计范围内全部
"""
import asyncio
import os
import random
from datetime import datetime, timedelta

from nonebot import get_bots, get_driver
from nonebot.log import logger

from .oopz_stats import (
    _MAX_LEN,
    _TARGET_AREAS,
    _channel_name_map,
    _get_client,
    _reset_client,
)

driver = get_driver()

_REPORT_GROUPS = [
    s.strip()
    for s in os.environ.get("NAPCAT_REPORT_GROUP", "").split(",")
    if s.strip()
]
_REPORT_INTERVAL_MIN = int(os.environ.get("NAPCAT_REPORT_INTERVAL_MIN", "30"))
_WELCOME_GROUPS = [
    s.strip()
    for s in os.environ.get("NAPCAT_WELCOME_GROUP", "").split(",")
    if s.strip()
]
_WELCOME_INTERVAL_SEC = float(os.environ.get("NAPCAT_WELCOME_INTERVAL_SEC", "15"))
_WELCOME_CHANNELS = {
    s.strip()
    for s in os.environ.get("NAPCAT_WELCOME_CHANNELS", "").split(",")
    if s.strip()
}

# 每次查询整体超时（秒），与 oopz_stats 保持一致
_QUERY_TIMEOUT = 20

# 进频道欢迎文案（随机挑一条，避免每次都一个样；少用感叹号）
_WELCOME_TEMPLATES = [
    "🎮 {name} 加入 OOPZ「{channel}」频道，来开黑吗？",
    "🕹️ {name} 已经在 OOPZ「{channel}」就位。",
    "🎧 {name} 戴上耳机钻进了 OOPZ「{channel}」。",
    "🚀 {name} 空降到 OOPZ「{channel}」频道。",
]


async def _send(group_id: str, text: str) -> bool:
    """向指定群推送文本。bot 未连接或发送失败返回 False（不抛异常）。"""
    bots = get_bots()
    if not bots or not group_id:
        return False
    bot = next(iter(bots.values()))
    try:
        await bot.send_group_msg(group_id=int(group_id), message=text)
        return True
    except Exception as exc:
        logger.error("推送群 {} 消息失败: {}", group_id, exc)
        return False


# ---------------------------------------------------------------- 定时播报

async def _build_broadcast_message() -> str | None:
    """生成定时播报的独立文案（与 @统计 的回复格式区分开）。

    带时间头、每频道一行紧凑列出成员名，结尾一句俏皮话；
    无人在线时返回 None（定时播报只在有人时推送，无人静默跳过）。
    """
    bot = await _get_client()
    if bot is None:
        return "📣 Oopz 语音频道播报：凭据未配置，请先运行 tools/oopz_login.py 然后重启机器人。"

    try:
        async with asyncio.timeout(_QUERY_TIMEOUT):
            joined = await bot.areas.get_joined_areas()
    except Exception as exc:
        _reset_client()
        logger.error("Oopz 语音频道播报：查询域列表失败: {}", exc)
        return "📣 Oopz 语音频道播报：Oopz 查询失败，请稍后再试。"

    areas = list(joined or [])
    if _TARGET_AREAS:
        wanted = set(_TARGET_AREAS)
        areas = [a for a in areas if a.area_id in wanted or a.name in wanted]

    online_by_area: dict[str, list[tuple[str, list[str]]]] = {}
    total_online = 0
    all_uids: list[str] = []

    for a in areas:
        try:
            async with asyncio.timeout(_QUERY_TIMEOUT):
                result = await bot.channels.get_voice_channel_members(area=a.area_id)
            name_map = await _channel_name_map(bot, a.area_id)
        except Exception as exc:
            logger.warning("Oopz 语音频道播报：拉取域 {} 成员失败: {}", a.name, exc)
            continue

        for ch_id, members in (result.channel_members or {}).items():
            humans = [m for m in members if not getattr(m, "is_bot", False)]
            if not humans:
                continue
            ch_name = name_map.get(ch_id) or ch_id[:8]
            uids = [m.uid for m in humans]
            online_by_area.setdefault(a.name, []).append((ch_name, uids))
            total_online += len(uids)
            all_uids.extend(uids)

    uid_name: dict[str, str] = {}
    if all_uids:
        try:
            async with asyncio.timeout(_QUERY_TIMEOUT):
                users = await bot.person.get_person_infos_batch(all_uids)
            for u in users or []:
                name = getattr(u, "name", "")
                if name:
                    uid_name[getattr(u, "uid", "")] = name
        except Exception as exc:
            logger.warning("Oopz 语音频道播报：批量查询昵称失败: {}", exc)

    if total_online == 0:
        return None  # 无人在线：不推送（定时播报只在有人时有意义）

    now = datetime.now().strftime("%H:%M")
    lines = [f"📣 Oopz 语音频道播报 · {now}", "━━━━━━━━━━", f"现在有 {total_online} 位小伙伴在 Oopz 挂着"]
    for area_name, channel_rows in online_by_area.items():
        lines.append(f"\n【{area_name}】")
        for ch_name, uids in channel_rows:
            lines.append(f"🔊 {ch_name}（{len(uids)}人）")
            for uid in uids:
                lines.append(f"  • {uid_name.get(uid) or uid[:8]}")
    lines.append("\n想一起玩的，直接进频道找他们～")

    msg = "\n".join(lines)
    if len(msg) > _MAX_LEN:
        msg = msg[:_MAX_LEN - 1] + "…"
    return msg


def _seconds_until_broadcast_slot() -> float:
    """距下一个整点槽位的秒数：以当天 00:00 为起点，落在分钟数能被 N 整除的时刻。

    N=30 → :00 与 :30；N=60 → 每小时整点；N=15 → :00/:15/:30/:45。
    用它替代固定 sleep，避免启动时间不同导致播报时刻漂移、永远对不齐整点。
    """
    now = datetime.now()
    minutes = _REPORT_INTERVAL_MIN
    next_slot = ((now.hour * 60 + now.minute) // minutes + 1) * minutes
    target = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=next_slot
    )
    return max((target - now).total_seconds(), 0.0)


async def _report_loop() -> None:
    if not _REPORT_GROUPS:
        logger.info("未配置 NAPCAT_REPORT_GROUP，定时播报未启用")
        return
    # 等 bot 连上 NapCat、Oopz 客户端就绪
    await asyncio.sleep(15)
    while True:
        try:
            # 睡到下一个整点槽位；每次从当前时刻重新对齐，不累积漂移
            delay = _seconds_until_broadcast_slot()
            if delay:
                await asyncio.sleep(delay)
            msg = await _build_broadcast_message()
            if msg is None:
                logger.info("定时播报：当前 Oopz 无人在线，本次跳过")
            else:
                for g in _REPORT_GROUPS:
                    await _send(g, msg)
                    await asyncio.sleep(0.5)  # 多群连续推送间隔，避免被吞/风控
                logger.info("定时播报已推送到 {} 个群", len(_REPORT_GROUPS))
        except Exception as exc:
            logger.error("定时播报异常: {}", exc)
            await asyncio.sleep(60)  # 出错别立刻重试，等一分钟再对齐下一次


# ---------------------------------------------------------------- 进频道欢迎

# channel 唯一键 -> 已知真人 uid 集合；首次轮询只建基线、不欢迎
_known: dict[tuple[str, str], set[str]] = {}
_initialized = False


async def _welcome_loop() -> None:
    if not _WELCOME_GROUPS:
        logger.info("未配置 NAPCAT_WELCOME_GROUP，进频道欢迎未启用")
        return
    await asyncio.sleep(10)
    while True:
        try:
            await _check_joins()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("进频道检测异常: {}", exc)
        await asyncio.sleep(_WELCOME_INTERVAL_SEC)


async def _check_joins() -> None:
    """轮询目标域语音频道成员，发现新人则推送欢迎。"""
    global _known, _initialized
    bot = await _get_client()
    if bot is None:
        return

    current: dict[tuple[str, str], set[str]] = {}
    new_entries: list[tuple[str, str]] = []  # (uid, 频道名)
    try:
        async with asyncio.timeout(_QUERY_TIMEOUT):
            joined = await bot.areas.get_joined_areas()
    except Exception as exc:
        logger.error("进频道检测：查询域列表失败: {}", exc)
        return

    areas = list(joined or [])
    if _TARGET_AREAS:
        wanted = set(_TARGET_AREAS)
        areas = [a for a in areas if a.area_id in wanted or a.name in wanted]

    for a in areas:
        try:
            async with asyncio.timeout(_QUERY_TIMEOUT):
                result = await bot.channels.get_voice_channel_members(area=a.area_id)
            name_map = await _channel_name_map(bot, a.area_id)
        except Exception as exc:
            logger.warning("进频道检测：拉取域 {} 成员失败: {}", a.name, exc)
            continue

        for ch_id, members in (result.channel_members or {}).items():
            humans = {m.uid for m in members if not getattr(m, "is_bot", False)}
            if not humans:
                continue
            ch_name = name_map.get(ch_id) or ch_id[:8]
            if _WELCOME_CHANNELS and ch_name not in _WELCOME_CHANNELS:
                continue
            key = (a.area_id, ch_id)
            current[key] = humans
            if _initialized:
                for uid in humans - _known.get(key, set()):
                    new_entries.append((uid, ch_name))

    # 只在整轮拉取成功后更新基线，避免查询失败恢复时误报
    _known = current
    _initialized = True

    if not new_entries:
        return

    # 批量解析昵称
    uid_name: dict[str, str] = {}
    try:
        async with asyncio.timeout(_QUERY_TIMEOUT):
            users = await bot.person.get_person_infos_batch([uid for uid, _ in new_entries])
        for u in users or []:
            name = getattr(u, "name", "")
            if name:
                uid_name[getattr(u, "uid", "")] = name
    except Exception as exc:
        logger.warning("进频道检测：批量查询昵称失败: {}", exc)

    for uid, ch_name in new_entries:
        display = uid_name.get(uid) or uid[:8]
        template = random.choice(_WELCOME_TEMPLATES)
        text = template.format(name=display, channel=ch_name)
        logger.info("{} 进入「{}」，已推送", display, ch_name)
        for g in _WELCOME_GROUPS:
            await _send(g, text)
            await asyncio.sleep(0.5)  # 多群/多条连续推送间隔，避免被吞/风控


@driver.on_startup
async def _start_background_tasks() -> None:
    asyncio.create_task(_report_loop())
    asyncio.create_task(_welcome_loop())
