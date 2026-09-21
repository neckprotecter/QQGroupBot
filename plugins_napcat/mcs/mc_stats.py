"""@mc 查询（NapCat / OneBot v11 版）：群内 @机器人发含触发词的消息 → 回复 MC 服务器在线快照。

两种用法：
    @机器人 mc            → 本群关联的服务器的**总览**（每个目标一小段名单）
    @机器人 mc bingo      → 该目标的**明细**

「本群看哪几台服」由 mcs_audiences.toml 决定（每条 [[audience]] 是一个群关联），
「有哪些服」由 mcs_servers.toml 决定。查不到这个群 → 明确回绝，不静默。
目标定义与名字解析见 _shared/mcservers.py，群关联见 _shared/mcaudiences.py。
快照取数与缓存见 mcs/client.py，取数细节见 _shared/mc.py。
文案拼装在 _shared/mcrender.py（与进服提醒/定时播报共用同一份格式）。
触发词归属与载荷切分见 _shared/triggers.py（与 oopz 查询互斥，不会同时回两条）。
"""
from nonebot import on_message
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

from .._shared.mcaudiences import audiences_path, default_config
from .._shared.mcrender import NO_AUDIENCE, NO_TARGETS, Row, render_detail, render_summary
from .._shared.mcservers import ServerConfigError
from .._shared.push import text_message
from .._shared.triggers import locate, primary_keyword, strip_keyword
from .client import _MAX_NAMES, get_snapshot, get_snapshots

stat = on_message(priority=1, block=False)


async def _reply(bot: Bot, group_id: int, text: str) -> None:
    """发一条纯文本回复。

    必须包成 text 段而不是直接传 str：传 str 时整条消息在协议层被当作 CQ 码文本
    解析，而**本插件的内容来自服务器**（玩家显示名、版本号、服名），第三方插件能往
    显示名里塞任意字符。理由与 mcs/mc_admin.py 的 _reply 一致。
    """
    try:
        await bot.send_group_msg(group_id=group_id, message=text_message(text))
    except Exception as exc:
        logger.error("回复MC在线列表失败: {}", exc)


@stat.handle()
async def handle_stat(bot: Bot, event: MessageEvent):
    group_id = getattr(event, "group_id", None)
    if not group_id:
        return  # 只响应群内 @，私聊不管
    if not event.to_me:
        return  # 必须 @ 机器人才触发
    text = event.get_plaintext().strip()
    hit = locate(text)
    if hit is None or hit.plugin != "mc":
        return  # 触发词归属见 _shared/triggers.py；不归我的消息本插件全程不发声

    # 触发词之后的都算载荷：`@bot mc bingo` → `bingo`。空载荷 = 看总览。
    # 触发词本身被剥掉（该插件的**全部**触发词，见 strip_keyword），
    # 所以 `@bot 服务器 mc bingo` 和 `@bot mc bingo` 等价。
    payload = strip_keyword(text, hit)

    logger.info("收到@bot mc查询（载荷 {!r}），来自群 {}", payload, group_id)

    # —— 从这里起本插件已独占这条消息，每个分支都必须回复 ——
    # 与 mcs/mc_admin.py 同一条约束：默默 return 会让群里彻底没反应，
    # 比加这个功能之前还糟（那时至少还能收到 hello 的功能引导）。
    #
    # 闸门顺序很关键：**先认领（locate），再查这个群有没有开通**。旧版是反过来的
    # （先查群白名单，不在名单里就静默 return），那时还没确定这条消息是不是自己的，
    # 所以只能闭嘴。归属定下来之后就不许再静默了。
    # 代价：没开通的群里，任何含 mc / 我的世界 / **服务器** 的 @ 消息都会收到 MC 的
    # 拒绝文案，hello 的功能引导被抑制 —— 所以拒绝文案末尾附了指路那句。
    try:
        config = default_config()
    except ServerConfigError as exc:
        logger.error("读取 MC 配置失败：{}", exc)
        await _reply(bot, group_id, f"⚠️ 服务器配置读不了：{exc}")
        return

    audience = config.for_group(group_id)
    if audience is None:
        # 「这个群没写进 mcs_audiences.toml」和「机器人掉线了」在群里长得一模一样
        # （都是没有反应），而这两种的修法完全不同 —— 所以必须打这条 warning。
        # 旧版这里打的是 _shared/whitelist.py 的「不在白名单内」，DEPLOY 的 F10 整节
        # 就是教用户 grep 它，所以这条要带同样多的信息。
        logger.warning(
            "群 {} 收到 mc 查询，但它没开通：不在 {} 的任何 [[audience]].groups 里。"
            "已开通的群：{}",
            group_id,
            audiences_path(),
            "、".join(config.known_groups) or "（一个都没有）",
        )
        await _reply(bot, group_id, NO_AUDIENCE)
        return

    # 本群关联的服务器视图：只有这几台，所以关联外的服名 resolve 不出结果。
    book = audience.book
    if not book.targets:
        # 得排在解析载荷**之前**：没有目标时「没有叫 X 的服。可选：」会拼出空候选，
        # 而那个群真正的情况是「本群关联的服务器还没接入」。
        await _reply(bot, group_id, NO_TARGETS)
        return

    if payload:
        res = book.resolve(payload)
        if not res.ok:
            # 歧义与未知必须给出**不同**的答复：前者是「你打得太短」，后者是
            # 「没这个服」。两种都不能静默。
            if res.ambiguous:
                await _reply(
                    bot,
                    group_id,
                    f"🤔 「{payload}」能对上好几个服：{'、'.join(res.candidates)}\n"
                    f"打全一点，或者直接发「{primary_keyword('mc', 'mc')}」看总览。",
                )
            else:
                names = "、".join(t.name for t in book.targets)
                await _reply(
                    bot,
                    group_id,
                    f"🤔 没有叫「{payload}」的服。可选：{names}\n"
                    f"（服名 / 别名 / 唯一前缀都认，大小写和全角无所谓）",
                )
            return
        target = res.target
        snap = await get_snapshot(target)
        await _reply(bot, group_id, render_detail(snap, target, max_names=_MAX_NAMES))
        return

    # 总览：并发取本群关联的全部目标。max_age 用默认的缓存 TTL —— 群里连点两下不该
    # 打两遍 SLP+RCON（MC 服务端会为每次探测留日志）。
    # 顺序用 targets_primary_first：[[audience]].primary 那台排最前，其余按配置顺序。
    ordered = book.targets_primary_first
    snaps = await get_snapshots(ordered)
    rows = [Row(target=t, snap=s) for t, s in zip(ordered, snaps)]
    # all_targets 传**全量**表（不是本群的视图）：代理尾注要拿它判断「本群是否关联到了
    # 该代理同组的全部子服」，详见 mcrender._group_complete。
    await _reply(
        bot,
        group_id,
        render_summary(rows, total_names=_MAX_NAMES, all_targets=config.book.targets),
    )
