"""@机器人 whitelist add|remove <玩家名> [服名] / whitelist list [服名]

从 QQ 群管理 MC 玩家白名单。五道闸门（顺序即代码顺序，见 handle_admin）：

  1. 群关联（mcs_audiences.toml 里有没有提到这个群）—— 没提到就不开通，如实告知
  2. 管理员 QQ 号（_shared/admin.py，MC_ADMIN_QQ）—— **留空 = 谁都不许用**
  3. 本群有没有白名单服（本条关联的 whitelist 数组，空 = 不管白名单）
  4. 命令解析（动词白名单 + 玩家名正则 + 服名位置）
  5. 「发给哪台」（ServerBook.pick_whitelist）+ 那台的通道（RCON 密码或群组接口）

第 3 与第 5 步查的都是**本群那条关联**的服务器视图，不是全局：社团群的管理员因此
碰不到建筑群的白名单目标。命令前缀（RCON 里发什么词）**逐台**随路由走，一路传进
run_whitelist_command —— 它**不能硬编码**，代理上线后群里打的还是 `whitelist`、
RCON 里发的得是 `globalwhitelist`。

**一条关联可以管多台服**（P6），所以命令里要能点名（`whitelist add Steve bingo`）。
候选只有一台时**可以省略服名** —— 那是绝大多数情况，逼人写反而容易写错。多条时
点名，且**没有「默认那台」的回退**：猜错的后果是改错服务器的白名单。
`list` 不点名 = 列出本群全部白名单服（读操作，安全）；改动命令一律一次只打一台。

触发词一旦归本插件，**这条消息就已被独占**（归属逻辑见 _shared/triggers.py）——hello 的
「detect() is None 才回功能引导」和 mc_stats 的「detect() == "mc" 才回列表」两个分支
此时都不成立。所以**从第 1 道闸门起，每个分支都必须回复**：若有分支默默 return，群里
就是彻底没反应，比加这个功能之前还糟（改之前至少还能收到 hello 的功能引导）。

第 2 步是真正的安全边界。白名单本来就是防熊孩子的，群里任何人 @机器人 都能加白
就等于没锁；第 1 步只是「这个群有没有开通」。**注意第 3 步会放大第 2 步的作用域**：
一条关联里多写一台白名单服，就等于同一个管理员多能改一台服的白名单。

名词消歧：本插件管「MC 玩家白名单」（服务端 whitelist.json）；
「群白名单」（哪些 QQ 群能 @查询）现在由 mcs_audiences.toml 的群关联承担，
两者毫无关系。oopz 那边仍在用 _shared/whitelist.py。
"""
import asyncio

from nonebot import on_message
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from .._shared import admin
from .._shared.mcaudiences import audiences_path, default_config
from .._shared.mcadmin import (
    ERR_LIST_ARGS,
    ERR_NAME,
    AdminCommand,
    AdminResult,
    WhitelistAuthError,
    WhitelistError,
    WhitelistUnreachable,
    parse_command,
    run_whitelist_command,
)
from .._shared.mcrender import NO_AUDIENCE, WhitelistListRow, render_whitelist_list
from .._shared.mcservers import (
    PICK_AMBIGUOUS,
    PICK_NEED_NAME,
    PICK_NOT_WHITELIST,
    PICK_UNKNOWN,
    ServerBook,
    ServerConfigError,
    WhitelistPick,
    WhitelistRoute,
)
from .._shared.push import truncate
from .._shared.triggers import detect, primary_keyword
from .client import _MAX_NAMES

_ADMIN_QQ_VARS = ("MC_ADMIN_QQ",)

# 与其余三个 matcher 保持一致：互斥靠 detect() 和提前 return，不靠 priority/block。
# 给这里加 block=True 会悄悄改变后续新增插件的抑制语义，而且毫无收益——hello 和
# mc_stats 本来就会立刻 return。
mc_admin = on_message(priority=1, block=False)

_DENIED = "🚫 你没有 MC 管理权限，这条命令只有管理员能用。"
# 「本群没配白名单服」和「配了但发不出去」必须**分开说**：前者要去加 whitelist 数组，
# 后者要去给那台补通道（rcon 或 api）—— 一句笼统的「不可用」会让人照着改永远改不对。
_NO_WHITELIST = "⚠️ 本群没有指定白名单服，白名单管理不可用。"

# 群回复一律纯文本：**QQ 不渲染 markdown**，`**加粗**` 会连星号一起原样打出来 ——
# 群里看到的就是「服名要写在**最后**」。要强调就把话说清楚，别用 `**`。
# 自测里有一条钉子扫这个目录的字符串（docstring / 整行注释不算），别在回复文案里写 markdown。


def _not_ready(route: WhitelistRoute) -> str:
    """这台白名单服两条通道都不通。

    文案里**不说死 RCON**：群组服那台走的是对方的 HTTP 接口，写「没配 RCON 密码」
    会让人去 mcs_servers.toml 里找一个本来就不该有的 rcon 段。
    """
    return (
        f"⚠️ 白名单服 {route.name} 两条通道都没配（既没有 rcon 也没有 api），"
        f"命令发不出去。"
    )


def _usage(err: str, book: ServerBook | None = None) -> str:
    """用法文案。

    命令词取实际配置的触发词（改了 MC_ADMIN_TRIGGER 这里跟着变），**服名与台数取
    本群那条关联的真实配置** —— 否则文案会教人打一个本群不存在的服名，而那正是
    「照着提示做还是不对」那一类。
    """
    kw = primary_keyword("mcadmin", "whitelist")
    routes = book.whitelist_routes if book else ()
    lines = ["🤖 MC 玩家白名单管理（仅管理员）："]
    if err == ERR_LIST_ARGS:
        lines.append("`list` 后面最多写一个服名。")
    elif err == ERR_NAME:
        lines.append("没给出合法的玩家名。")
    if len(routes) > 1:
        names = "、".join(r.name for r in routes)
        lines += [
            f"本群有 {len(routes)} 台白名单服，改动时要点名：{names}",
            f"· {kw} add <玩家名> <服名> —— 加入某一台的白名单",
            f"· {kw} remove <玩家名> <服名> —— 从某一台移出",
            f"· {kw} list —— 一次列出本群全部 {len(routes)} 台",
            f"· {kw} list <服名> —— 只看某一台",
        ]
    else:
        lines += [
            f"· {kw} add <玩家名> —— 加入白名单",
            f"· {kw} remove <玩家名> —— 移出白名单",
            f"· {kw} list —— 查看白名单",
        ]
    lines.append("玩家名只能用字母、数字、下划线，1~16 位。")
    return truncate("\n".join(lines))


def _pick_reply(pick: WhitelistPick) -> str:
    """「发给哪台」认不出来时的回复。六态里除 OK 外每一态都必须有话可说。

    这几条都不能省：`NEED_NAME` 不说就等于「命令没反应」；`UNKNOWN` 不列出本群
    能查的服名，就分不出「打错一个字」和「这台不归本群」；`NOT_WHITELIST` 不点明
    那台确实存在，就会让人以为配置里没这台服而去改配置 —— 全是白费功夫。
    """
    kw = primary_keyword("mcadmin", "whitelist")
    if pick.reason == PICK_NEED_NAME:
        names = "、".join(pick.candidates)
        return truncate(
            f"⚠️ 本群有 {len(pick.candidates)} 台白名单服，命令里要点名发给哪台：{names}。\n"
            f"例如 {kw} add Steve {pick.candidates[0]}；只看名单直接发 {kw} list"
        )
    if pick.reason == PICK_UNKNOWN:
        known = "、".join(pick.candidates) or "（一台都没有）"
        return truncate(
            f"⚠️ 没有叫 {pick.query} 的服。本群能查的是：{known}。\n"
            f"（id / 名字 / 别名 / 唯一前缀都认）"
        )
    if pick.reason == PICK_AMBIGUOUS:
        return truncate(f"⚠️ 服名 {pick.query} 有歧义，你是指：{'、'.join(pick.candidates)}。")
    if pick.reason == PICK_NOT_WHITELIST and pick.found is not None:
        return truncate(
            f"⚠️ {pick.found.name} 是本群关联的服，但它不是白名单服。\n"
            f"本群的白名单服只有：{'、'.join(pick.candidates)}。"
            f"（想查它用 {primary_keyword('mc', 'mc')} {pick.found.name}）"
        )
    return _NO_WHITELIST  # 到不了：routes 为空在上面已经拦掉了


def _server_pos_reply(cmd: AdminCommand, token: str) -> str:
    """服名写到了动词前面。P6 之前这里是**静默丢掉**再发往缺省那台 —— 改错服的那种静默。

    回复里把改好的整条命令写出来（而不是只说「位置不对」）：群里的人下一步就是重打
    一遍，给他一条能直接照抄的，比让他自己推敲词序少一次试错。

    文案里没有 `**`：这条以前写的是「服名要写在**最后**」，而 QQ 不渲染 markdown，
    群友看到的就是连星号一起的原文（见文件顶部说明）。
    """
    kw = primary_keyword("mcadmin", "whitelist")
    tail = f"{cmd.player} {token}".strip()
    return truncate(f"⚠️ 服名要写在最后：你写的 `{token}` 是服名。改成：{kw} {cmd.verb} {tail}")


def _build_message(
    result: AdminResult, *, route_name: str, command: str, multi: bool = False
) -> str:
    """按结果生成回复。只回显正则校验过的玩家名和服务器返回的名字，绝不回显用户原文。

    `command` 是这一台的 RCON 命令前缀（`whitelist` / `globalwhitelist`），**必须由
    调用方给**：提示文案里那句「照着这条命令去服务端查」如果写死了 `whitelist`，
    配了 Global Whitelist 的群里就会给出一个服务端不存在的命令（P6 修的 bug）。

    `multi` = 本群有多台白名单服，回复要带 `【服名】` —— 不带的话，管理员的下一步动作
    完全取决于他看到的是哪一台，而消息里没有任何东西能告诉他。
    """
    text = _build_body(result, command)
    if not multi or not route_name:
        return text
    # 单行的短回复（已添加 / 未生效）行首带标签就够；多行的名单让标签单独占一行，
    # 否则第一行会被挤成「【Bingo】📋 MC 玩家白名单（12 人）：」。
    return truncate(f"【{route_name}】\n{text}" if "\n" in text else f"【{route_name}】{text}")


def _build_body(result: AdminResult, command: str) -> str:
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
        # 两种「未能验证」必须分开说（走不到这里的是 list —— 上面那个分支已经 return）：
        #   · 前置读就没读懂 → 一条命令都没发出去，说「命令已发送」是把没发生的事报成发生了
        #   · 反查没读懂     → 命令确实发了，只是验不出来
        # 群友照着前者去服务端查会一头雾水（那边根本没有任何变化）。
        if not result.mutation_sent:
            return truncate(
                f"⚠️ 读不到服务端当前的白名单，{result.verb} 命令没有发出去："
                f"{command} {result.verb} {name}。原文已记入日志。"
            )
        return truncate(
            f"⚠️ 命令已发送（未能验证）：{command} {result.verb} {name}。请到服务端确认结果。"
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


async def _list_rows(routes: tuple[WhitelistRoute, ...]) -> tuple[WhitelistListRow, ...]:
    """并发查每一台的白名单，**每台各出一行**（失败的那台也只是它自己那一行）。

    `return_exceptions=True` 是硬要求：不写的话一台 RconAuthError 会把整批结果连坐
    丢掉，群友收到一条「命令执行失败」而那几台其实查到了 —— 是「把没发生的事报成
    发生了」的镜像，同样不许。例外项也照样转成一行，保证**一台都不静默省略**。

    锁是 per target 的（mc.rcon_command），所以这里是**真并发**：整批耗时 ≈
    max(单台 rcon_timeout)，不是求和。串行实现 + 4 台 = 管理员在群里等十几秒。
    """

    async def one(route: WhitelistRoute) -> WhitelistListRow:
        if not route.whitelist_ready:
            # 单台时这个分支在更前面就拦掉了（文案也更好）；多台时只有这一台两条通道
            # 都没配，不能因此把其它台的名单一起丢掉。
            return WhitelistListRow(route.name, None, "两条通道都没配，这台的名单读不到")
        try:
            res = await run_whitelist_command(
                AdminCommand("list"), route.target, route.command
            )
        except WhitelistAuthError as exc:  # 是 WhitelistError 子类，必须先接
            logger.warning(
                "查询 {}[{}] 白名单 {} 认证失败: {}",
                route.name,
                route.id,
                route.channel,
                exc,
            )
            return WhitelistListRow(route.name, None, f"{route.channel} 认证失败")
        except (WhitelistUnreachable, WhitelistError) as exc:
            # 异常原文只进日志，不进群：`[WinError 1225] 远程计算机拒绝网络连接。` 这类
            # 内容是给看日志的人的，群里那位既读不懂也做不了什么，只是把一行话撑长。
            logger.warning(
                "查询 {}[{}] 白名单 {} 失败: {}",
                route.name,
                route.id,
                route.channel,
                exc,
            )
            return WhitelistListRow(
                route.name, None, f"连不上 {route.name}服务器（{route.channel}）"
            )
        except Exception as exc:  # 兜底：绝不让异常逃逸成「少了一台」
            logger.error("查询 {} 的白名单失败: {}", route.name, exc)
            return WhitelistListRow(route.name, None, "查询失败，详情见机器人日志")
        if not res.ok:
            return WhitelistListRow(route.name, None, "服务器返回的名单解析不了")
        return WhitelistListRow(route.name, tuple(res.names))

    out = await asyncio.gather(*(one(r) for r in routes), return_exceptions=True)
    return tuple(
        item
        if isinstance(item, WhitelistListRow)
        else WhitelistListRow(routes[i].name, None, f"内部错误：{item!r}")
        for i, item in enumerate(out)
    )


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
    try:
        config = default_config()
    except ServerConfigError as exc:
        logger.error("读取 MC 配置失败：{}", exc)
        await _reply(bot, group_id, f"⚠️ 服务器配置读不了，白名单管理不可用：{exc}")
        return
    audience = config.for_group(group_id)
    if audience is None:
        # 与 mc_stats 同一个判断、同一条文案：这个群没写进 mcs_audiences.toml。
        # 这里也要打 warning —— 被拒绝的群在群里长得和「机器人掉线」一样。
        logger.warning(
            "群 {} 收到 MC 管理命令，但它没开通：不在 {} 的任何 [[audience]].groups 里。"
            "已开通的群：{}",
            group_id,
            audiences_path(),
            "、".join(config.known_groups) or "（一个都没有）",
        )
        await _reply(bot, group_id, NO_AUDIENCE)
        return
    if not admin.allowed_user(event.get_user_id(), "mcadmin", *_ADMIN_QQ_VARS):
        await _reply(bot, group_id, _DENIED)
        return

    # 白名单归属查的是**本群那条关联**的服务器视图：社团群的管理员碰不到建筑群的服。
    routes = audience.book.whitelist_routes
    if not routes:
        # 群里只说「本群没配」，修法留给日志 —— 群友不需要知道 mcs_audiences.toml 长什么样，
        # 而配的人需要在日志里一眼看到该往哪条关联里加什么。
        logger.warning(
            "「{}」没配白名单服：在 {} 里给这条 [[audience]] 加 "
            'whitelist = [{{ target = "<mcs_servers.toml 里的 id>", command = "whitelist" }}]',
            audience.name,
            audiences_path(),
        )
        await _reply(bot, group_id, _NO_WHITELIST)
        return
    multi = len(routes) > 1

    cmd, err = parse_command(text)
    if cmd is None:
        await _reply(bot, group_id, _usage(err, audience.book))
        return

    # 服名写到了动词前面（`whitelist bingo add Steve`）。P6 之前这里是**静默丢掉**
    # `bingo` 再把命令发往缺省那台 —— 点名了 A 服却改了 B 服，正是本项目最防的那类。
    # 判据是「这个名字在本群解析得出来」，所以「帮我 whitelist add Steve」里的「帮我」
    # 不会被误伤（它不是任何一台服的名字），而真写了服名的写法一定会被拦下来。
    for token in cmd.pre_verb:
        if audience.book.resolve(token).ok:
            await _reply(bot, group_id, _server_pos_reply(cmd, token))
            return

    # `list` 不点名 = 列出本群**全部**白名单服（读操作，安全；也是「看看都有谁」最常用的
    # 写法，不该逼人先点名）。只有一台时不走这里 —— 那条路径与 P6 之前逐字相同。
    if cmd.verb == "list" and not cmd.server and multi:
        rows = await _list_rows(routes)
        text, clipped = render_whitelist_list(rows, total_names=_MAX_NAMES)
        if clipped:
            logger.warning(
                "本群 {} 台白名单服的名单过长，回复被截断（{}）",
                len(routes),
                "、".join(r.name for r in rows),
            )
        logger.info(
            "收到@bot MC 白名单 list（本群 {} 台），来自群 {} 用户 {}；逐台结果：{}",
            len(routes),
            group_id,
            event.get_user_id(),
            "；".join(
                f"{r.name}={len(r.names) if r.names is not None else '读不到'}" for r in rows
            ),
        )
        await _reply(bot, group_id, text)
        return

    pick = audience.book.pick_whitelist(cmd.server)
    if not pick.ok:
        logger.info(
            "MC 白名单命令没认出服名：{}（{}），来自群 {} 用户 {}；本群白名单服：{}",
            cmd.server,
            pick.reason,
            group_id,
            event.get_user_id(),
            "、".join(r.name for r in routes),
        )
        await _reply(bot, group_id, _pick_reply(pick))
        return
    route = pick.route

    if not route.whitelist_ready:
        logger.warning(
            "「{}」的白名单服 {}[{}] 既没配 rcon 也没有接口，命令发不出去",
            audience.name,
            route.name,
            route.id,
        )
        await _reply(bot, group_id, _not_ready(route))
        return

    # 命令前缀随**这一台**走，不硬编码：代理上线后群里照样打 whitelist，
    # 而 RCON 里要发 globalwhitelist。日志里带上它，前缀写错时一眼能看出来。
    # 接口型目标没有前缀这个概念（路径由 mcbridge 拼死），所以那半句不打印。
    command = route.command
    logger.info(
        "收到@bot MC 白名单命令：{} {}，来自群 {} 用户 {}；{} → {}[{}]{}",
        cmd.verb,
        cmd.player or "-",
        group_id,
        event.get_user_id(),
        route.channel,
        route.name,
        route.id,
        f"，命令前缀 {command!r}" if not route.target.is_api else "",
    )
    # 多台时错误文案也带服名：同一条命令在不同服上的失败原因可能完全不同，
    # 不带服名的话「连不上」到底是哪台连不上就无从判断。
    label = f"【{route.name}】" if multi else ""
    try:
        result = await run_whitelist_command(cmd, route.target, command)
    except WhitelistAuthError as exc:  # 是 WhitelistError 子类，必须先接
        logger.warning("MC 白名单命令 {} 认证失败: {}", route.channel, exc)
        # 接口那条通道 401 的下一步动作是「去要新 token」，说成「认证失败」太笼统
        hint = "（token 可能被对方轮换了）" if route.target.is_api else ""
        await _reply(bot, group_id, f"{label}😵 {route.channel} 认证失败{hint}")
        return
    except (WhitelistUnreachable, WhitelistError) as exc:
        # 文案里点名是哪台：单台时 label 是空的，不点名群里就不知道坏的是哪台。
        # 异常原文只进日志，理由同 _list_rows。
        logger.warning("MC 白名单命令 {} 失败: {}", route.channel, exc)
        await _reply(
            bot, group_id, f"{label}😵 连不上 {route.name}服务器（{route.channel}）"
        )
        return
    except Exception as exc:  # 兜底：绝不让异常逃逸成「群里没反应」
        logger.error("MC 白名单命令异常: {}", exc)
        await _reply(bot, group_id, f"{label}😵 命令执行失败，详情见机器人日志。")
        return

    if result.ok is not True:
        # 未生效 / 未能验证时把 RCON 回执原文落到日志：文案是本地化的，只有原文
        # 能让人看出服务端到底回了什么（比如「Unknown command」= 没有 whitelist 命令）
        if result.verb != "list" and not result.mutation_sent:
            # 前置读就没读懂 → 命令一条都没发。跟「发了但没生效」排查方向相反，
            # 混成一句会让照着日志查的人去服务端找一条根本不存在的改动。
            logger.warning(
                "MC 白名单 {} {} 命令未下发（读不到服务端当前的白名单）：{}",
                result.verb,
                result.player or "-",
                result.detail,
            )
        else:
            logger.warning(
                "MC 白名单 {} {} 未生效或未能验证：{}",
                result.verb,
                result.player or "-",
                result.detail,
            )
    await _reply(
        bot,
        group_id,
        _build_message(result, route_name=route.name, command=command, multi=multi),
    )
