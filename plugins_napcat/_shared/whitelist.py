"""群白名单：决定某个群能否触发 @查询。

oopz 与 MC 两个查询插件共用这一份实现。被拒绝时**打一条告警**——白名单配错是
「@机器人 毫无反应」最常见的原因，而原先的 `return` 是静默的：日志里既没有
报错也没有 trace，只能靠猜（连 hello 的功能引导也被触发词互斥挡掉了，群里一条
回复都没有，看起来就像机器人掉线）。
"""
import os

from nonebot.log import logger


def resolve(*vars: str) -> tuple[set[str], str]:
    """按顺序取第一个非空的白名单，返回 (群号集合, 命中的环境变量名)。

    全都为空 → 返回 (set(), "")，调用方据此视为「不限制」。
    多个变量是为了支持回退链（如 MC_ALLOWED_GROUPS 留空时用 NAPCAT_ALLOWED_GROUPS）。
    """
    for var in vars:
        groups = {s.strip() for s in os.environ.get(var, "").split(",") if s.strip()}
        if groups:
            return groups, var
    return set(), ""


def allowed(group_id: int | str, plugin: str, *vars: str) -> bool:
    """群是否在白名单内。留空的白名单 = 不限制，一律放行。

    调用方必须**先判完 `event.to_me` 再调**：告警只在「被 @ 了但被白名单拦下」
    时触发，普通群消息不会走到这里，所以不会刷屏。
    """
    groups, source = resolve(*vars)
    if not groups or str(group_id) in groups:
        return True
    logger.warning(
        "群 {} @{} 查询被忽略：不在白名单内。{}={}（逗号分隔；留空 = 所有群都可查）",
        group_id,
        plugin,
        source,
        ",".join(sorted(groups)),
    )
    return False
