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


def _log_watch_target(label: str, groups: list[str]) -> None:
    """进服提醒 / 定时播报**当前盯的是哪台**。

    必须明写：它们现在只看一条群关联的主服（P7 之前），而「主服」住在群关联里，
    很容易和「全量表的第一个目标」混起来 —— 盯错了不会报错，只是悄悄监控了另一台服。
    """
    if not groups:
        logger.info("MC {}：未配置目标群，未启用", label)
        return
    target, audience = mc_reporter._primary(groups)
    if target is None:
        return  # 原因 _primary 已经打过 error
    logger.info(
        "MC {}：盯的是「{}」关联的 {}[{}]（P7 会改成按群关联分别对账）",
        label,
        audience,
        target.name,
        target.id,
    )


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

    # 触发词（群里打的）与命令前缀（RCON 里发的）是两个不同的轴，代理上线后必然
    # 分叉：群里仍打 `whitelist`，RCON 里要发 `globalwhitelist`。前缀错了的表现是
    # 每次操作都回 Unknown command、群里报「未生效」，所以 whitelist_summary 里写出来了。

    _log_watch_target("进服提醒", mc_reporter._WATCH_GROUPS)
    _log_watch_target("定时播报", mc_reporter._REPORT_GROUPS)

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

    _, _, budget = round_budget(book)
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
