"""mcs 插件包：MC 查询（mc_stats）/ 白名单管理（mc_admin）/ 进服提醒（mc_reporter）。

本文件额外挂一对启动/关闭钩子，把 mcs_servers.toml 的**目标汇总**打进启动日志 ——
「新加的子服为什么没生效」最常见的三种原因（id 写错、kind 不对、targets 段根本没
读到）在启动日志里一眼可见，不用等群里发条消息去试。

汇总只打一次、只在启动时：mcs_servers.toml 是 import 期之后才懒加载的，而
default_book() 带 lru_cache，进程活着的期间目标集合不会变（改了配置要重启）。
"""
from nonebot import get_driver
from nonebot.log import logger

from .._shared import mc
from .._shared.mcservers import ServerConfigError, default_book, round_budget
from . import client, mc_admin, mc_reporter, mc_stats

__all__ = ["client", "mc_admin", "mc_reporter", "mc_stats"]

driver = get_driver()


@driver.on_startup
async def _log_targets() -> None:
    try:
        book = default_book()
    except ServerConfigError as exc:
        # 不在这里抛：抛了会让整个 bot 起不来，而配置坏了应当只是 MC 相关的功能
        # 不可用（各 handler 已经会回一条带原始报错的提示）。但必须吼一声 ——
        # 否则表现就是「群里发什么都没反应」，看不出是配置问题。
        logger.error("mcs_servers.toml 读不了，所有 MC 功能不可用：{}", exc)
        return

    if not book.targets:
        logger.warning("mcs_servers.toml 里没有任何 [[targets]]，所有 MC 功能不可用")
        return

    described = "、".join(
        f"{t.name}[{t.id}]{'/' + t.group if t.group else ''}" for t in book.targets
    )
    logger.info("MC 目标 {} 个：{}", len(book.targets), described)

    primary = book.primary_target
    owner = book.whitelist_owner
    logger.info(
        "MC 主服：{}   白名单命令发往：{}（前缀 {!r}）",
        primary.name if primary else "(没配)",
        f"{owner.name}[{owner.id}]" if owner else "(没配，白名单管理不可用)",
        book.whitelist_command,
    )
    # 触发词（群里打的）与命令前缀（RCON 里发的）是两个不同的轴，代理上线后必然
    # 分叉：群里仍打 `whitelist`，RCON 里要发 `globalwhitelist`。前缀错了的表现是
    # 每次操作都回 Unknown command、群里报「未生效」，所以这里显式打出来。

    for warning in book.warnings:
        logger.warning("MC 目标配置：{}", warning)

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
    await mc.prune({t.id for t in book.targets})


@driver.on_shutdown
async def _close_rcon() -> None:
    """退出时主动断开 RCON，别把连接留给对端等服务端超时回收。"""
    await mc.close_all()
