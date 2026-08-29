from nonebot import on_message
from nonebot.log import logger
from nonebot.adapters.qq import Bot, MessageEvent

# 链路自检插件：打印收到的消息，对群内 @「你好」回复「收到！」，验证被动回复链路
hello = on_message(priority=1, block=False)


@hello.handle()
async def handle(bot: Bot, event: MessageEvent):
    group_id = getattr(event, "group_openid", None)
    text = event.get_plaintext().strip()
    logger.info(
        f"收到{'群' if group_id else '私聊'}消息: {text!r} "
        f"| group_openid={group_id} | 用户={event.get_user_id()}"
    )
    if not group_id:
        return  # 私聊不回复，先只测群
    if text == "你好":
        # 带 msg_id = 被动回复（响应@消息），不需要主动消息权限
        await bot.post_group_messages(
            group_openid=group_id,
            msg_type=0,
            content="收到！被动回复链路已打通 🎉",
            msg_id=event.id,
        )
    # 「统计」已由 plugins/oopz_stats.py 处理，这里不重复回复
