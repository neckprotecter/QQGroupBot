"""群消息推送（NapCat / OneBot v11）与消息长度上限。

从 oopz/auto_reporter.py 抽出：多群连推 + 防风控间隔原来在定时播报和进频道
欢迎里各写了一遍，现在收进 send_to_groups，新插件不用再抄。
"""
import asyncio

from nonebot import get_bots
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment

# 长度上限与截断在 textlen.py（零依赖），这里再导出一次：
# 渲染层要 import truncate，但不能因此把 nonebot 拖进去。调用方照旧写
# `from .._shared.push import truncate`，不必知道它住在哪。
from .textlen import MAX_LEN, truncate  # noqa: F401

# 多群/多条连续推送之间的间隔（秒），避免被吞或被风控
_PUSH_GAP = 0.5


def text_message(text: str) -> Message:
    """把纯文本包成单个 text 段。

    **不要直接传 str 给 send_group_msg**：传 str 时整条消息在协议层被当作 CQ 码
    文本解析，内容里万一出现 `[CQ:at,qq=all]` 这类串就会被执行。包成 text 段后
    NoneBot 序列化时会做 CQ 转义（`[` → `&#91;`），从结构上杜绝。

    本模块推的内容大多来自外部（MC 玩家显示名、oopz 昵称），第三方插件能往显示名里
    塞任意字符，所以这层不能省。理由与 mcs/mc_admin.py 的 _reply 完全一致。
    """
    return Message([MessageSegment.text(text)])


async def send_group(group_id: str, text: str) -> bool:
    """向指定群推送文本。bot 未连接或发送失败返回 False（不抛异常）。"""
    bots = get_bots()
    if not bots or not group_id:
        return False
    bot = next(iter(bots.values()))
    try:
        await bot.send_group_msg(group_id=int(group_id), message=text_message(text))
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
