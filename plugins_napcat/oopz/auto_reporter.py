"""定时播报 + 进频道欢迎（NapCat / OneBot v11 版）。

oopz 客户端单例与共享工具在 oopz/client.py，本文件只负责播报/欢迎逻辑与推送，
走 NapCat 主动推送（send_group_msg），不受官方主动推送停用限制。

.env 配置：
  NAPCAT_REPORT_GROUP=<群号>          定时播报目标群（逗号分隔多群，留空 = 不启用）
  NAPCAT_REPORT_INTERVAL_MIN=30       播报间隔（分钟）；整点对齐（落在 :00 / :N / :2N… 时刻），仅在 oopz 有人在线时推送，无人时静默跳过
  NAPCAT_WELCOME_GROUP=<群号>         欢迎消息目标群（逗号分隔多群，留空 = 不启用）
  NAPCAT_WELCOME_INTERVAL_SEC=15      进频道轮询间隔（秒）
  NAPCAT_WELCOME_CHANNELS=频道名,...   可选：只欢迎这些频道（逗号分隔），留空 = 统计范围内全部
"""
import asyncio
import os
import random
from datetime import datetime

from nonebot import get_driver
from nonebot.log import logger

from .._shared.push import send_to_groups, truncate
from .._shared.schedule import seconds_until_slot
from .client import (
    _QUERY_TIMEOUT,
    _channel_name_map,
    _fetch_uid_names,
    _filter_areas,
    _get_client,
    _reset_client,
    disabled_reason,
    sdk_available,
    sdk_error,
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

# 进频道欢迎文案
_WELCOME_TEMPLATES = [
    "🎮 {name} 加入 oopz「{channel}」频道，来开黑吗？",
    "🕹️ {name} 已经在 oopz「{channel}」就位。",
    "🎧 {name} 戴上耳机钻进了 oopz「{channel}」。",
    "🚀 {name} 空降到 oopz「{channel}」频道。",
]


# ---------------------------------------------------------------- 定时播报

async def _build_broadcast_message() -> str | None:
    """生成定时播报的独立文案（与 @oopz 的回复格式区分开）。

    带时间头、每频道一行紧凑列出成员名，结尾一句俏皮话；
    无人在线时返回 None（定时播报只在有人时推送，无人静默跳过）。
    """
    reason = disabled_reason()
    if reason is not None:
        return f"📣 oopz 语音频道播报：{reason}。"
    bot = await _get_client()
    if bot is None:
        return "📣 oopz 语音频道播报：oopz 客户端初始化失败，详情见机器人日志。"

    try:
        async with asyncio.timeout(_QUERY_TIMEOUT):
            joined = await bot.areas.get_joined_areas()
    except Exception as exc:
        _reset_client()
        logger.error("oopz 语音频道播报：查询域列表失败: {}", exc)
        return "📣 oopz 语音频道播报：oopz 查询失败，请稍后再试。"

    areas = _filter_areas(joined)

    online_by_area: dict[str, list[tuple[str, list[str]]]] = {}
    total_online = 0
    all_uids: list[str] = []

    for a in areas:
        try:
            async with asyncio.timeout(_QUERY_TIMEOUT):
                result = await bot.channels.get_voice_channel_members(area=a.area_id)
            name_map = await _channel_name_map(bot, a.area_id)
        except Exception as exc:
            logger.warning("oopz 语音频道播报：拉取域 {} 成员失败: {}", a.name, exc)
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

    uid_name = await _fetch_uid_names(bot, all_uids)

    if total_online == 0:
        return None  # 无人在线：不推送

    now = datetime.now().strftime("%H:%M")
    lines = [f"📣 oopz 语音频道播报 · {now}", "━━━━━━━━━━", f"现在有 {total_online} 位小伙伴在 oopz 挂着"]
    for area_name, channel_rows in online_by_area.items():
        lines.append(f"\n【{area_name}】")
        for ch_name, uids in channel_rows:
            lines.append(f"🔊 {ch_name}（{len(uids)}人）")
            for uid in uids:
                lines.append(f"  • {uid_name.get(uid) or uid[:8]}")
    lines.append("\n想一起玩的，直接进频道找他们～")

    return truncate("\n".join(lines))


async def _report_loop() -> None:
    if not _REPORT_GROUPS:
        logger.info("未配置 NAPCAT_REPORT_GROUP，定时播报未启用")
        return
    # 等 bot 连上 NapCat、oopz 客户端就绪
    await asyncio.sleep(15)
    while True:
        try:
            # 睡到下一个整点槽位；每次从当前时刻重新对齐，不累积漂移
            delay = seconds_until_slot(_REPORT_INTERVAL_MIN)
            if delay:
                await asyncio.sleep(delay)
            msg = await _build_broadcast_message()
            if msg is None:
                logger.info("定时播报：当前 oopz 无人在线，本次跳过")
            else:
                await send_to_groups(_REPORT_GROUPS, msg)
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

    areas = _filter_areas(joined)

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
    uid_name = await _fetch_uid_names(bot, [uid for uid, _ in new_entries])

    for uid, ch_name in new_entries:
        display = uid_name.get(uid) or uid[:8]
        template = random.choice(_WELCOME_TEMPLATES)
        text = template.format(name=display, channel=ch_name)
        logger.info("{} 进入「{}」，已推送", display, ch_name)
        await send_to_groups(_WELCOME_GROUPS, text)


@driver.on_startup
async def _start_background_tasks() -> None:
    # 没装 SDK 时**连循环都不起**：起了也只会每轮生成一句「未安装 SDK」然后推给群，
    # 变成每 30 分钟一次的刷屏。这里吼一声就够，而且要说清「MC 功能不受影响」——
    # 否则看到 oopz 没起来会以为整只机器人有问题。
    if not sdk_available():
        logger.warning(
            "未安装 oopz_sdk（它随 requirements.txt 默认装，这台特意跳过了），"
            "oopz 的定时播报与进频道欢迎不会启动（MC 功能不受影响）。"
            "要用 oopz 就 pip install ./vendor/Oopzbot-SDK 然后重启；导入失败原因: {}",
            sdk_error(),
        )
        return
    asyncio.create_task(_report_loop())
    asyncio.create_task(_welcome_loop())
