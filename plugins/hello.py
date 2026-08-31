from nonebot import on_message
from nonebot.log import logger
from nonebot.adapters.qq import Bot, MessageEvent

# 链路自检 + 群消息摘要（QQ 官方版，备用入口）：
# - 每条群消息打一行摘要；官方版无群名/昵称 API，用 openid 短号显示
# - 群内 @「你好」回复「收到！」，验证被动回复链路
hello = on_message(priority=1, block=False)


@hello.handle()
async def handle(bot: Bot, event: MessageEvent):
    group_id = getattr(event, "group_openid", None)
    if not group_id:
        return  # 私聊不记录、不回复
    text = event.get_plaintext().strip()
    user = event.get_user_id()
    logger.info("【{}】 {}: {}", group_id[:10], user[:10], text or "(非文本消息)")
    if text == "你好":
        # 带 msg_id = 被动回复（响应@消息），不需要主动消息权限
        await bot.post_group_messages(
            group_openid=group_id,
            msg_type=0,
            content="收到！被动回复链路已打通 🎉",
            msg_id=event.id,
        )
    # 「统计」已由 plugins/oopz_stats.py 处理，这里不重复回复
