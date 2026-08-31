from nonebot import on_message
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

# 链路自检 + 群消息摘要（NapCat / OneBot v11 版）：
# - 每条群消息打一行「【群名】 昵称: 消息」摘要，便于观察群里动态
# - 群内被 @ 且不是「oopz」查询时，回复功能引导列表（oopz 查询由 oopz_stats 处理）
hello = on_message(priority=1, block=False)

# 功能引导列表：一行一个功能，后续扩展只需在列表里追加一行
_FEATURE_LIST = (
    "🤖 我是塔萨小助手，可用指令：\n"
    "· “@我 oopz” —— 查看 oopz 在线列表\n"
    "🔧 更多功能开发中，敬请期待"
)

# 群名缓存：get_group_info 每群只查一次（改群名后需重启生效）
_group_names: dict[int, str] = {}


async def _group_name(bot: Bot, group_id: int) -> str:
    if group_id not in _group_names:
        try:
            info = await bot.get_group_info(group_id=group_id)
            _group_names[group_id] = str(info.get("group_name") or group_id)
        except Exception:
            _group_names[group_id] = str(group_id)
    return _group_names[group_id]


@hello.handle()
async def handle(bot: Bot, event: MessageEvent):
    group_id = getattr(event, "group_id", None)
    if not group_id:
        return  # 私聊不记录、不回复
    text = event.get_plaintext().strip()
    sender = getattr(event, "sender", None)
    uname = (
        (sender.card or sender.nickname or str(event.get_user_id()))
        if sender
        else str(event.get_user_id())
    )
    gname = await _group_name(bot, group_id)
    logger.info("【{}】 {}: {}", gname, uname, text or "(非文本消息)")
    # 被 @ 且文本不含「oopz」时回复功能引导（oopz 查询由 oopz_stats.py 处理）
    if event.to_me and "oopz" not in text.lower():
        await bot.send_group_msg(group_id=group_id, message=_FEATURE_LIST)
