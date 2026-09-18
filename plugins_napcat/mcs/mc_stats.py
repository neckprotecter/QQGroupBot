"""@mc 查询（NapCat / OneBot v11 版）：群内 @机器人发含触发词的消息 → 回复 MC 服务器在线快照。

快照取数与缓存见 mcs/client.py，取数细节见 _shared/mc.py。
触发词归属见 _shared/triggers.py（与 oopz 查询互斥，不会同时回两条）。
"""
from nonebot import on_message
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

from .._shared import whitelist
from .._shared.mc import McSnapshot, _cfg
from .._shared.push import truncate
from .._shared.triggers import detect
from .client import _MAX_NAMES, get_snapshot

# MC 查询的群白名单回退链：MC_ALLOWED_GROUPS 留空时用 NAPCAT_ALLOWED_GROUPS。
# 不直接只认 NAPCAT_ALLOWED_GROUPS——那样会变成「想让 MC 能查，就必须让 oopz 也能查」。
_ALLOWED_GROUPS_VARS = ("MC_ALLOWED_GROUPS", "NAPCAT_ALLOWED_GROUPS")

stat = on_message(priority=1, block=False)


def _build_message(snap: McSnapshot) -> str:
    """按快照生成回复文案。"""
    name = _cfg().name

    if not snap.reachable:
        return f"😵 {name} 连不上了。\n{snap.error}"

    head = f"🗺️ {name} 在线：{snap.count}"
    if snap.max_players:
        head += f"/{snap.max_players}"
    if snap.latency is not None:
        head += f"　延迟 {snap.latency:.0f}ms"

    lines = [head]
    if snap.version:
        lines.append(f"　版本 {snap.version}")

    if snap.count == 0:
        lines.append("\n现在没人，来当第一个？")
        return truncate("\n".join(lines))

    shown = snap.names[:_MAX_NAMES]
    lines.append(f"\n在线名单（{snap.count} 人）：")
    lines.extend(f"  • {n}" for n in shown)
    if len(snap.names) > len(shown):
        lines.append(f"  …还有 {len(snap.names) - len(shown)} 人")

    if not snap.names_complete:
        lines.append(f"\n⚠️ 名单可能不完整，仅供参考（{snap.error}）")

    return truncate("\n".join(lines))


@stat.handle()
async def handle_stat(bot: Bot, event: MessageEvent):
    group_id = getattr(event, "group_id", None)
    if not group_id:
        return  # 只响应群内 @，私聊不管
    if not event.to_me:
        return  # 必须 @ 机器人才触发
    # 群白名单：不在名单内的群不响应查询（被拦下时会打一条 warning，便于排查）
    if not whitelist.allowed(group_id, "mc", *_ALLOWED_GROUPS_VARS):
        return
    if detect(event.get_plaintext().strip()) != "mc":
        return  # 触发词归属见 _shared/triggers.py

    logger.info("收到@bot mc查询，来自群 {}", group_id)
    snap = await get_snapshot()
    try:
        await bot.send_group_msg(group_id=group_id, message=_build_message(snap))
    except Exception as exc:
        logger.error("回复MC在线列表失败: {}", exc)
