"""mcs 插件包：MC 查询（mc_stats）/ 白名单管理（mc_admin）/ 进服提醒（mc_reporter）。

本文件额外挂一对启动/关闭钩子，把**两份配置的加载结果**打进启动日志 —— 「新加的子服
为什么没生效」「新加的群为什么不响应」最常见的几种原因（id 写错、kind 不对、
群没写进任何 [[audience]]、某条关联 targets 是空的）在启动日志里一眼可见，
不用等群里发条消息去试。

汇总只打一次、只在启动时：两份配置都是 import 期之后才懒加载的，而 default_config()
带 lru_cache，进程活着的期间目标集合与群关联都不会变（改了配置要重启）。
"""
from nonebot import get_driver
from nonebot.log import logger

from .._shared import mc
from .._shared.mcaudiences import default_config
from .._shared.mcservers import ServerConfigError, round_budget
from . import client, mc_admin, mc_reporter, mc_stats

__all__ = ["client", "mc_admin", "mc_reporter", "mc_stats"]

driver = get_driver()


def _log_push_targets(config, flag: str, label: str) -> None:
    """进服提醒 / 定时播报**每条关联各盯哪几台**。

    必须明写：漏掉一台不会报错，只是那台服的进服提醒永远不出现，而同一个群关联的
    其它服一切正常 —— 看上去像「那台服没人上」。这是本项目最怕的那类静默失败，
    所以把「谁推给谁」在启动日志里摊开。

    一条都没开时不报错、只说怎么开：`watch` / `report` 缺省是 false（新加一条关联
    默认安静），所以「一个都没开」在配置刚加完、还没来得及开开关时是**正常状态**。
    """
    opened = [a for a in config.audiences if getattr(a, flag)]
    if not opened:
        logger.info(
            "MC {}：没有任何 [[audience]] 写 {} = true，未启用"
            "（.env 里的 MC_WATCH_GROUP / MC_REPORT_GROUP 已作废）",
            label,
            flag,
        )
        return
    logger.info("MC {}：{} 条群关联开了 {} = true", label, len(opened), flag)
    for audience in opened:
        targets = audience.book.targets
        if not targets:
            # 加载期已有一条警告（「写了开关但本条没关联任何服务器」），这里不重复说
            logger.info("  「{}」：没关联任何服务器", audience.name)
            continue
        described = "、".join(f"{t.name}[{t.id}]" for t in targets)
        logger.info("  「{}」→ {} 台：{}", audience.name, len(targets), described)


@driver.on_startup
async def _log_targets() -> None:
    try:
        config = default_config()
    except ServerConfigError as exc:
        # 不在这里抛：抛了会让整个 bot 起不来，而配置坏了应当只是 MC 相关的功能
        # 不可用（各 handler 已经会回一条带原始报错的提示）。但必须吼一声 ——
        # 否则表现就是「群里发什么都没反应」，看不出是配置问题。
        logger.error("MC 配置读不了，所有 MC 功能不可用：{}", exc)
        return

    book = config.book
    if not book.targets:
        logger.warning("mcs_servers.toml 里没有任何 [[targets]]，所有 MC 功能不可用")
        return

    described = "、".join(
        f"{t.name}[{t.id}]{'/' + t.group if t.group else ''}" for t in book.targets
    )
    logger.info("MC 目标 {} 个：{}", len(book.targets), described)

    # 群关联：哪个群看哪几台。这段是排查「新加的群为什么不响应」的第一入口 ——
    # 群不在任何一条里，查询就会回「本群还没有开通 MC 查询」。
    logger.info("MC 关联服务器：{} 条", len(config.audiences))
    for audience in config.audiences:
        logger.info("  {}", audience.summary())
        logger.info("      {}", audience.whitelist_summary)
        # 推送开关：两个都没开的关联「查询一切正常、却什么都收不到」，而它的表现
        # （群里安安静静）和「机器人挂了」一模一样。所以每条都明写一行。
        logger.info("      推送   {}", audience.flags_summary)

    # 触发词（群里打的）与命令前缀（RCON 里发的）是两个不同的轴，代理上线后必然
    # 分叉：群里仍打 `whitelist`，RCON 里要发 `globalwhitelist`。前缀错了的表现是
    # 每次操作都回 Unknown command、群里报「未生效」，所以 whitelist_summary **逐台**
    # 把前缀写出来了 —— 多台时一台写错只会影响那一台，只看「list 能出结果」发现不了。

    _log_push_targets(config, "watch", "进服提醒")
    _log_push_targets(config, "report", "定时播报")

    # 两层警告都打，但内容不重复：book.warnings 是全量的（端口冲突、某个目标没配
    # rcon 密码），config.warnings 是跨文件的（孤儿目标、.env 残留），
    # audience.warnings 只属于某条关联（该群没关联任何服务器、白名单服没密码）。
    for warning in book.warnings:
        logger.warning("MC 目标配置：{}", warning)
    for warning in config.warnings:
        logger.warning("MC 群关联配置：{}", warning)
    for audience in config.audiences:
        for warning in audience.warnings:
            logger.warning("MC 群关联「{}」：{}", audience.name, warning)

    # 预算的**基数**是「开着 watch 的关联覆盖到的目标并集」，不是全量 book。
    # 全量里完全可能有一台只被 watch = false 的关联引用的服（超时 30s），按它算的
    # 上界会永远顶着一条与轮询无关的告警 —— 告警被无视之后，真超了也看不出来。
    #
    # round_budget 的**模型**没变，也不该变：探测是 asyncio.gather 并发的，所以取
    # max 正确，与台数无关。没有 watch 关联时是空 book → (0,0,0) → 不打告警，正确。
    watched_ids = [t.id for t in config.flag_targets("watch")]
    _, _, budget = round_budget(book.scoped(watched_ids))
    interval = mc_reporter._WATCH_INTERVAL_SEC
    if budget > interval:
        # _watch_loop 是「跑完再补睡剩余时间」，所以超了**不会重叠**，只是周期被
        # 拉长。进服发现晚几秒，不堆积。别为此去调大 MC_WATCH_INTERVAL_SEC。
        logger.warning(
            "MC 一轮耗时上界 {:g}s 超过 MC_WATCH_INTERVAL_SEC={:g}s："
            "病态情况下周期被拉长到约 {:g}s（不会重叠，只是变慢）",
            budget,
            interval,
            budget + 1,
        )

    # 目标从配置里删掉后，它的 RCON 长连接会一直挂着，而 MC 服务端每条连接都会打
    # 日志 —— 留着就是持续刷对方控制台。启动时这里必然是空转（还没连过），
    # 但它维护的是「只保留配置里还在的目标」这条不变量，放在启动点最自然。
    #
    # 传的是**所有群关联的并集**而不是某个视图：将来按群分别对账（P7）时，一台服若
    # 只被另一条关联引用着，用单个视图裁会把它正在用的连接误剪掉。
    await mc.prune({t.id for a in config.audiences for t in a.book.targets})


@driver.on_shutdown
async def _close_rcon() -> None:
    """退出时主动断开 RCON，别把连接留给对端等服务端超时回收。"""
    await mc.close_all()
