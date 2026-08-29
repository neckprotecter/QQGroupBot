from nonebot import on_message
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

# 链路自检插件（NapCat / OneBot v11 版）：对群内 @「你好」回复，验证收发链路
hello = on_message(priority=1, block=False)


@hello.handle()
async def handle(bot: Bot, event: MessageEvent):
    group_id = getattr(event, "group_id", None)
    text = event.get_plaintext().strip()
    logger.info(
        f"收到{'群' if group_id else '私聊'}消息: {text!r} "
        f"| group_id={group_id} | 用户={event.get_user_id()}"
    )
    if not group_id:
        return  # 私聊不回复，先只测群
    if text == "你好":
        await bot.send_group_msg(group_id=group_id, message="收到！链路已打通 🎉")
    # 「统计」已由 plugins_napcat/oopz_stats.py 处理，这里不重复回复
