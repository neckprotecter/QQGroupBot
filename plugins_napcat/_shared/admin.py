"""管理员名单：决定某个 QQ 号能否执行**管理类**命令（用户级鉴权）。

与 _shared/whitelist.py 各管一维，别混：
  · whitelist.py —— 「哪个**群**」能 @查询
  · 本模块       —— 「哪个**人**」能执行管理命令
两者可同时生效；一条管理命令要求两道都过。

**留空的语义跟群白名单相反（刻意的）**：
群白名单留空 = 不限制（放行），配错最多是少个查询功能；
管理员名单留空 = **拒绝一切**（fail-closed），配错等于任何人都能改服务端白名单。
所以本模块的 docstring 把这条写在最前面——从 whitelist.py 照搬心智模型是这里唯一
容易犯的错。
"""
import os

from nonebot.log import logger


def resolve(*vars: str) -> tuple[set[str], str]:
    """按顺序取第一个非空的管理员名单，返回 (QQ 号集合, 命中的环境变量名)。

    全都为空 → 返回 (set(), "")，调用方据此把「没配」和「配了但不在名单里」分开报。
    多个变量是为了支持回退链（如将来 MC_ADMIN_QQ 留空时回退 NAPCAT_ADMIN_QQ）。
    """
    for var in vars:
        users = {s.strip() for s in os.environ.get(var, "").split(",") if s.strip()}
        if users:
            return users, var
    return set(), ""


def allowed_user(user_id: int | str, plugin: str, *vars: str) -> bool:
    """QQ 号是否在管理员名单内。**名单留空 = 一律拒绝。**

    返回 False 时必打一条 warning：管理命令「没反应」的原因全在配置里，静默失败只会
    把人推去翻代码。两种拒绝分开报——「没配」是部署遗漏，「不在名单里」是权限不足，
    排查方向完全不同。用词对齐 whitelist.py 的「…被忽略」，一条 grep 能同时捞出两类。
    """
    users, source = resolve(*vars)
    if not users:
        logger.warning(
            "管理命令被拒绝：未配置管理员名单（{} 均为空，逗号分隔 QQ 号，留空 = 关闭该功能）。"
            "用户 {} @{}",
            " / ".join(vars),
            user_id,
            plugin,
        )
        return False
    if str(user_id) in users:
        return True
    logger.warning(
        "用户 {} @{} 管理命令被忽略：不在管理员名单内。{}={}（逗号分隔 QQ 号）",
        user_id,
        plugin,
        source,
        ",".join(sorted(users)),
    )
    return False
