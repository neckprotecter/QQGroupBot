"""@机器人 whitelist add|remove <玩家名> / whitelist list —— 从 QQ 群管理 MC 玩家白名单。

四道闸门（顺序即代码顺序，见 handle_admin）：

  1. 群白名单（_shared/whitelist.py，本文件里别名 group_whitelist）
  2. 管理员 QQ 号（_shared/admin.py，MC_ADMIN_QQ）—— **留空 = 谁都不许用**
  3. RCON 可用性（MC_RCON_PASSWORD）—— 没配就没有命令通道，如实告知
  4. 命令解析（动词白名单 + 玩家名正则）—— 解析不了就回用法

触发词一旦归本插件，**这条消息就已被独占**（归属逻辑见 _shared/triggers.py）——hello 的
「detect() is None 才回功能引导」和 mc_stats 的「detect() == "mc" 才回列表」两个分支
此时都不成立。所以**从第 1 道闸门起，每个分支都必须回复**：若有分支默默 return，群里
就是彻底没反应，比加这个功能之前还糟（改之前至少还能收到 hello 的功能引导）。

第 2 步是真正的安全边界。白名单本来就是防熊孩子的，群里任何人 @机器人 都能加白
就等于没锁；第 1 步只是卫生（跟 @查询共用同一条群回退链，不单独设变量）。

名词消歧：本插件管「MC 玩家白名单」（服务端 whitelist.json）；
「群白名单」（哪些 QQ 群能 @查询）在 _shared/whitelist.py，两者毫无关系。
"""
from nonebot import on_message
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from .._shared import admin
from .._shared import whitelist as group_whitelist  # 群白名单，别名防与玩家白名单混淆
from .._shared.mc import RconAuthError, RconConnectError, RconError, _cfg
from .._shared.mcadmin import (
    ERR_LIST_ARGS,
    ERR_NAME,
    AdminResult,
    parse_command,
    run_whitelist_command,
)
from .._shared.push import truncate
from .._shared.triggers import detect, primary_keyword
from .client import _MAX_NAMES

# 两个白名单回退链。群白名单与 mc_stats 完全一致——管理命令不单独设群变量：用户级
# 鉴权才是安全边界，群级这层只是卫生。将来真要单独限制，往元组里加个变量即可。
_ALLOWED_GROUPS_VARS = ("MC_ALLOWED_GROUPS", "NAPCAT_ALLOWED_GROUPS")
_ADMIN_QQ_VARS = ("MC_ADMIN_QQ",)

# 与其余三个 matcher 保持一致：互斥靠 detect() 和提前 return，不靠 priority/block。
# 给这里加 block=True 会悄悄改变后续新增插件的抑制语义，而且毫无收益——hello 和
# mc_stats 本来就会立刻 return。
mc_admin = on_message(priority=1, block=False)

_DENIED = "🚫 你没有 MC 管理权限，这条命令只有管理员能用。"
_GROUP_DENIED = "🚫 本群未启用 MC 管理命令。"
_NO_RCON = "⚠️ 未配置 MC_RCON_PASSWORD，MC 玩家白名单管理不可用。"


def _usage(err: str) -> str:
    """用法文案。命令词取实际配置的触发词，改了 MC_ADMIN_TRIGGER 这里跟着变。"""
    kw = primary_keyword("mcadmin", "whitelist")
    lines = ["🤖 MC 玩家白名单管理（仅管理员）："]
    if err == ERR_LIST_ARGS:
        lines.append("`list` 不接受参数。")
    elif err == ERR_NAME:
        lines.append("没给出合法的玩家名。")
    lines += [
        f"· {kw} add <玩家名> —— 加入白名单",
        f"· {kw} remove <玩家名> —— 移出白名单",
        f"· {kw} list —— 查看白名单",
        "玩家名只能用字母、数字、下划线，1~16 位。",
    ]
    return truncate("\n".join(lines))


def _build_message(result: AdminResult) -> str:
    """按结果生成回复。只回显正则校验过的玩家名和服务器返回的名字，绝不回显用户原文。"""
    name = result.player

    if result.verb == "list":
        if not result.ok:
            return "⚠️ 未能解析服务器返回的白名单，原文已记入日志。"
        names = result.names
        if not names:
            return "📋 MC 玩家白名单：当前没有玩家。"
        lines = [f"📋 MC 玩家白名单（{len(names)} 人）："]
        shown = names[:_MAX_NAMES]
        lines.extend(f"  • {n}" for n in shown)
        if len(names) > len(shown):
            lines.append(f"  …还有 {len(names) - len(shown)} 人")
        return truncate("\n".join(lines))

    if result.ok is None:
        return truncate(
            f"⚠️ 命令已发送（未能验证）：whitelist {result.verb} {name}。请到服务端确认结果。"
        )
    # 服务端会把名字换成它认的拼写（`add vul` → 存成 `Vul`），拼写不一致时明说是服务端
    # 改的，否则群里看到「已将 vul 添加」而名单里是 `Vul`，会以为加错了人。
    recorded = f"（服务端记录为 {result.server_name}）" if result.server_name != name else ""

    # noop：名单本来就是目标状态，没下发任何命令。这条分支必须在「已添加 / 已移出」
    # 之前——把「无事发生」报成「已完成」是最容易让人误判白名单状态的一种错。
    if result.verb == "add":
        if result.noop:
            return f"ℹ️ {name} 已在白名单中{recorded}，无需重复添加。"
        if result.ok:
            return f"✅ 已将 {name} 添加到白名单{recorded}。"
        return f"❌ 未生效：名单里仍没有 {name}。"
    if result.noop:
        return f"ℹ️ {name} 本来就不在白名单里，无需移除。"
    if not result.ok:
        return f"❌ 未生效：名单里仍有 {name}。"
    # 同名不同拼写可能有不止一条（offline-mode 下是两个不同的 UUID），删了哪几条要说清。
    # 这种情况下列出的就是全部拼写，不再叠一句「服务端记录为」。
    if len(result.targets) > 1:
        listed = "、".join(result.targets)
        return f"✅ 已将 {name} 移出白名单（同名的 {len(result.targets)} 条都已移除：{listed}）。"
    return f"✅ 已将 {name} 移出白名单{recorded}。"


async def _reply(bot: Bot, group_id: int, text: str) -> None:
    """发一条纯文本回复。

    必须包成 Message(text 段) 而不是直接传 str：传 str 时整条消息在协议层被当作
    CQ 码文本解析，`whitelist list` 里万一出现 `[CQ:at,qq=all]` 这类内容就会被执行。
    包成 text 段后，NoneBot 序列化时会做 CQ 转义（`[` → `&#91;`），从结构上杜绝，
    也就不需要再对服务器返回的名字做「像不像玩家名」的过滤（那种过滤会静默吞掉
    名字，让列表看起来比真实的白名单短）。
    """
    try:
        await bot.send_group_msg(
            group_id=group_id, message=Message([MessageSegment.text(text)])
        )
    except Exception as exc:
        logger.error("回复 MC 白名单命令失败: {}", exc)


@mc_admin.handle()
async def handle_admin(bot: Bot, event: MessageEvent):
    group_id = getattr(event, "group_id", None)
    if not group_id:
        return  # 只响应群内 @，私聊不管
    if not event.to_me:
        return  # 必须 @ 机器人才触发
    text = event.get_plaintext().strip()
    if detect(text) != "mcadmin":
        return  # 触发词不归我，本插件全程不发声

    # —— 以下本插件已独占这条消息，每个分支都必须回复 ——
    if not group_whitelist.allowed(group_id, "mcadmin", *_ALLOWED_GROUPS_VARS):
        await _reply(bot, group_id, _GROUP_DENIED)
        return
    if not admin.allowed_user(event.get_user_id(), "mcadmin", *_ADMIN_QQ_VARS):
        await _reply(bot, group_id, _DENIED)
        return
    if not _cfg().rcon_enabled:
        await _reply(bot, group_id, _NO_RCON)
        return

    cmd, err = parse_command(text)
    if cmd is None:
        await _reply(bot, group_id, _usage(err))
        return

    logger.info(
        "收到@bot MC 白名单命令：{} {}，来自群 {} 用户 {}",
        cmd.verb,
        cmd.player or "-",
        group_id,
        event.get_user_id(),
    )
    try:
        result = await run_whitelist_command(cmd)
    except RconAuthError as exc:  # 是 RconError 子类，必须先接
        logger.warning("MC 白名单命令 RCON 认证失败: {}", exc)
        await _reply(bot, group_id, f"😵 RCON 认证失败：{exc}")
        return
    except (RconConnectError, RconError) as exc:
        logger.warning("MC 白名单命令 RCON 失败: {}", exc)
        await _reply(bot, group_id, f"😵 连不上 MC 服务器（RCON）：{exc}")
        return
    except Exception as exc:  # 兜底：绝不让异常逃逸成「群里没反应」
        logger.error("MC 白名单命令异常: {}", exc)
        await _reply(bot, group_id, "😵 命令执行失败，详情见机器人日志。")
        return

    if result.ok is not True:
        # 未生效 / 未能验证时把 RCON 回执原文落到日志：文案是本地化的，只有原文
        # 能让人看出服务端到底回了什么（比如「Unknown command」= 没有 whitelist 命令）
        logger.warning(
            "MC 白名单 {} {} 未生效或未能验证：{}",
            result.verb,
            result.player or "-",
            result.detail,
        )
    await _reply(bot, group_id, _build_message(result))
