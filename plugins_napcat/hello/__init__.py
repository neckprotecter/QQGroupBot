from nonebot import on_message
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

from .._shared.triggers import detect

# 链路自检 + 群消息摘要（NapCat / OneBot v11 版）：
# - 每条群消息打一行「【群名】 昵称: 消息」摘要，便于观察群里动态
# - 群内被 @ 且没有被任何插件认领时，回复功能引导列表
hello = on_message(priority=1, block=False)

def _oopz_available() -> bool:
    """oopz SDK 装了没。

    **在函数里 import**，而且 try 兜住：这条引导语是给「只跑 MC 功能」的人看的，
    那些人**可能特意没装 SDK**（它默认随 requirements.txt 装上，装不装是可选的）——
    hello 不能因此起不来。oopz 插件整个被删掉时这里
    也会返回 False（那时 @我 oopz 没人应答，见 DEPLOY.md 第 3 节的说明）。
    """
    try:
        from ..oopz.client import sdk_available
    except Exception:
        return False
    return sdk_available()


def feature_list() -> str:
    """功能引导列表。一行一个功能，后续扩展只需在列表里追加一行。

    oopz 那行**按实际情况加**：没装 SDK 还列着「@我 oopz」，等于指着一个用不了的
    命令（虽然它会回一句解释，但引导语不该把人往那儿引）。
    """
    lines = ["🤖 我是塔萨小助手，可用指令：\n"]
    if _oopz_available():
        lines.append("· “@我 oopz” —— 查看 oopz 在线列表\n")
    lines.append("· “@我 mc” —— 查看 Minecraft 服务器在线列表\n")
    lines.append("· “@我 whitelist add|remove <玩家名> [服名]” / “@我 whitelist list [服名]”\n")
    lines.append("  —— 管理 MC 玩家白名单（仅管理员；本群有多台白名单服时要点名）\n")
    lines.append("🔧 更多功能开发中，敬请期待")
    return "".join(lines)

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
    # 被 @ 且文本没被任何插件认领时才回复功能引导。
    # 判据走 _shared/triggers.py 的单一来源——原来写死「不含 oopz 就回复」，
    # 加了第二个插件后会变成「@我 mc」同时收到功能引导 + MC 列表两条消息。
    if event.to_me and detect(text) is None:
        await bot.send_group_msg(group_id=group_id, message=feature_list())
