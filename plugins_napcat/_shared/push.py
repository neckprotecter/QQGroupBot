"""群消息推送（NapCat / OneBot v11）与消息长度上限。

从 oopz/auto_reporter.py 抽出：多群连推 + 防风控间隔原来在定时播报和进频道
欢迎里各写了一遍，现在收进 send_to_groups，新插件不用再抄。
"""
import asyncio

from nonebot import get_bots
from nonebot.log import logger

# 每条消息最大长度（QQ 群文本消息上限约 2000，留余量）
MAX_LEN = 1800

# 多群/多条连续推送之间的间隔（秒），避免被吞或被风控
_PUSH_GAP = 0.5


async def send_group(group_id: str, text: str) -> bool:
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


async def send_to_groups(group_ids: list[str], text: str) -> int:
    """按顺序推送到多个群，群间留防风控间隔。返回成功条数。"""
    sent = 0
    for i, group_id in enumerate(group_ids):
        if i:
            await asyncio.sleep(_PUSH_GAP)
        if await send_group(group_id, text):
            sent += 1
    return sent


def truncate(text: str, limit: int = MAX_LEN) -> str:
    """超长文本截断加省略号（QQ 群消息超限会被整条拒收）。"""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
