"""@oopz 插件（NapCat / OneBot v11 版）：群成员 @机器人发含「oopz」的消息 → 实时拉取 Oopz 语音频道在线成员 → 回复。

Oopz 客户端单例与共享工具在 oopz/client.py，本文件只负责查询与回复文案。
走个人 QQ 号协议端（NapCat），可主动推送；Oopz 侧只用 REST 查询。
"""
import asyncio
import os

from nonebot import on_message
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

from .client import (
    _MAX_LEN,
    _QUERY_TIMEOUT,
    _channel_name_map,
    _fetch_uid_names,
    _filter_areas,
    _get_client,
    _reset_client,
)

# 可选：只允许这些群触发查询（逗号分隔群号）；留空 = 所有群都可触发
_ALLOWED_GROUPS = {
    s.strip()
    for s in os.environ.get("NAPCAT_ALLOWED_GROUPS", "").split(",")
    if s.strip()
}

stat = on_message(priority=1, block=False)


async def _build_stats_message() -> str:
    """查询 Oopz 并生成统计文本。失败/无人时返回适合直接回复的字符串。"""
    bot = await _get_client()
    if bot is None:
        return "Oopz 凭据未配置：请先在终端运行 tools/oopz_login.py，然后重启机器人。"

    try:
        async with asyncio.timeout(_QUERY_TIMEOUT):
            joined = await bot.areas.get_joined_areas()
    except Exception as exc:
        _reset_client()
        logger.error("查询 Oopz 域列表失败: {}", exc)
        return "Oopz 查询失败，请稍后再试。"

    areas = _filter_areas(joined)
    if not areas:
        return "当前 Oopz 频道暂无在线成员。"

    # area 名 -> [(频道名, [成员uid])]；只保留有真人(非bot)的频道
    online_by_area: dict[str, list[tuple[str, list[str]]]] = {}
    total_online = 0
    all_uids: list[str] = []

    for a in areas:
        try:
            async with asyncio.timeout(_QUERY_TIMEOUT):
                result = await bot.channels.get_voice_channel_members(area=a.area_id)
        except Exception as exc:
            logger.warning("拉取域 {} 在线成员失败: {}", a.name, exc)
            continue

        name_map = await _channel_name_map(bot, a.area_id)
        channel_rows: list[tuple[str, list[str]]] = []
        for ch_id, members in (result.channel_members or {}).items():
            humans = [m for m in members if not getattr(m, "is_bot", False)]
            if not humans:
                continue
            ch_name = name_map.get(ch_id) or ch_id[:8]
            uids = [m.uid for m in humans]
            channel_rows.append((ch_name, uids))
            total_online += len(uids)
            all_uids.extend(uids)
        if channel_rows:
            online_by_area[a.name] = channel_rows

    if not online_by_area:
        return "当前 Oopz 频道暂无在线成员。"

    uid_name = await _fetch_uid_names(bot, all_uids)

    lines = [f"📊 Oopz 语音频道在线：{total_online} 人"]
    for area_name, channel_rows in online_by_area.items():
        lines.append(f"\n【{area_name}】")
        for ch_name, uids in channel_rows:
            names = [uid_name.get(uid) or uid[:8] for uid in uids]
            lines.append(f"  🔊 {ch_name}（{len(uids)}人）")
            for name in names:
                lines.append(f"    • {name}")

    msg = "\n".join(lines)
    if len(msg) > _MAX_LEN:
        msg = msg[:_MAX_LEN - 1] + "…"
    return msg


@stat.handle()
async def handle_stat(bot: Bot, event: MessageEvent):
    group_id = getattr(event, "group_id", None)
    text = event.get_plaintext().strip()
    if not group_id:
        return  # 只响应群内 @，私聊不管
    if not event.to_me:
        return  # 必须 @ 机器人才触发
    if _ALLOWED_GROUPS and str(group_id) not in _ALLOWED_GROUPS:
        return  # 群白名单：不在名单内的群不响应查询
    if "oopz" not in text.lower():
        return  # 文本包含 oopz（不区分大小写）即触发
    logger.info("收到@bot oopz查询，来自群 {}", group_id)
    msg = await _build_stats_message()
    try:
        await bot.send_group_msg(group_id=group_id, message=msg)
    except Exception as exc:
        logger.error("回复oopz统计失败: {}", exc)
