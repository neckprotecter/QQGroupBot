"""MC 玩家白名单（服务端 whitelist.json）命令层：解析 / 执行 / 独立验证。

**名词消歧**：本模块只管「MC 玩家白名单」（服务端 `white-list`）。
「群白名单」（哪些 QQ 群能 @查询）在 _shared/whitelist.py，两者毫无关系。

放在 _shared 而不是 mcs/ 的原因与 mc.py 相同：mcs/__init__.py 会 import 持有 matcher
的 mc_reporter，而 tools/mc_check.py 必须在 nonebot.init() 之前 import 本模块跑自测。

四条硬约束（改动前先读这四条）：

1. **动词是硬编码白名单** {add, remove, list}。这里刻意不提供「执行任意 RCON 命令」的
   入口——`op` / `stop` / `ban` 必须够不着。新增动词只能改代码，不能从群里传进来。
2. **玩家名正则既是格式校验也是命令注入防护**：^[A-Za-z0-9_]{1,16}$ 里不可能出现空格、
   换行、引号，拼进 RCON 命令行就无从注入第二条命令。
3. **成功判定不解析 RCON 回执**。`whitelist add` 的回执是本地化的（`Added Steve to the
   whitelist` / `已将 Steve 添加到白名单`），跟 `list` 是同一个坑——所以一律「执行完再跑
   一次 `whitelist list` 反查」，判不出来就老实回「未能验证」，绝不把不确定当成功。
4. **下命令时用服务端记录的拼写，不用用户输入的原文**。名字进白名单时服务端会换成它
   认的拼写（`add vul` → `Vul`），而「大小写算不算同一个玩家」取决于服务端版本和实现：
   vanilla 按忽略大小写的名字匹配，部分第三方白名单插件严格区分，offline-mode 下
   `Vul` / `vul` 更是两个不同 UUID 的独立记录。与其赌，不如先读一次 `whitelist list`
   照着里面的拼写下命令——赌注直接没了。见 run_whitelist_command。
"""
import re
from dataclasses import dataclass, field

from nonebot.log import logger

from .mc import parse_whitelist_names, rcon_command
from .mcservers import ServerTarget

VERBS: tuple[str, ...] = ("add", "remove", "list")

# 既是格式校验，也是注入防护（见模块 docstring 第 2 条）。
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")

# parse_command 的失败原因（机器可读，调用方据此出不同文案）
ERR_USAGE = "usage"  # 没找到子命令
ERR_LIST_ARGS = "list_args"  # list 后面跟了参数
ERR_NAME = "bad_name"  # 名字缺失或不合法


@dataclass(frozen=True)
class AdminCommand:
    verb: str  # "add" | "remove" | "list"
    player: str = ""  # 只有 add / remove 有


@dataclass(frozen=True)
class AdminResult:
    """一次命令的结果。

    ok 三态是刻意的：True 成功 / False 明确未生效 / **None 未能验证**。
    None 不能塌缩成 False——那会把「服务器格式不认识」误报成「命令失败」。
    """

    verb: str
    player: str
    ok: bool | None
    names: list[str] = field(default_factory=list)  # 仅 list 用
    detail: str = ""  # 失败原因，进日志
    server_name: str = ""  # 服务端实际记录的拼写（常与用户输入的大小写不同）
    # 实际下发给服务端的拼写（add 时就是用户输入；remove 时是名单里记着的那几条）。
    # 只在 remove 成功时用来回显——同名不同拼写可能有不止一条，删了哪几条要说清。
    targets: list[str] = field(default_factory=list)
    # 名单本来就是目标状态，没下发任何命令。add 命中 = 本来就在；remove 命中 = 本来就不在。
    # 不能让这种「无事发生」走「已添加 / 已移出」的文案：那是把没发生的事报成成功。
    noop: bool = False


def parse_command(text: str) -> tuple[AdminCommand | None, str]:
    """解析 `whitelist add <玩家名>` / `whitelist remove <玩家名>` / `whitelist list`。

    返回 (命令, 失败原因)；成功时原因为 ""。
    """
    tokens = (text or "").split()
    if not tokens:
        return None, ERR_USAGE

    # 动词只在**整词**上匹配：'list' 是 'whitelist' 的子串，用 `in` 判断会把
    # 「whitelist」本身当成 list 子命令。所以在 token 列表里找，不在这段文本里找。
    # 顺带这也让 MC_ADMIN_TRIGGER 换成别的词（如「白名单」）后照样能用。
    pos = next((i for i, t in enumerate(tokens) if t.lower() in VERBS), None)
    if pos is None:
        return None, ERR_USAGE

    verb = tokens[pos].lower()
    rest = tokens[pos + 1 :]

    if verb == "list":
        return (None, ERR_LIST_ARGS) if rest else (AdminCommand("list"), "")

    # 多出来的 token 会被 join 进来，从而过不了正则——「whitelist add Steve please」
    # 和「whitelist add Steve; stop」都落在 ERR_NAME 上。
    player = " ".join(rest)
    if not PLAYER_NAME_RE.match(player):
        return None, ERR_NAME
    return AdminCommand(verb, player), ""


def whitelist_matches(payload: str, name: str) -> list[str] | None:
    """白名单里所有**忽略大小写**等于 name 的条目，按服务端记录的拼写返回。

    返回 None = 判不出来（格式不认识）；空列表 = 名单里没有。

    比对必须是**整体**、不区分大小写的：子串匹配会让 `Steve` 命中 `Steve_2`。

    返回**全部**而不是第一条：offline-mode 下 `Vul` 和 `vul` 是两个不同的 UUID、
    两条独立记录，所以同名不同拼写的重复条目是真会出现的。只看第一条的话，
    「移除」会留下一条幽灵记录，紧接着的反查又命中它，报出假的「未生效」。
    """
    names = parse_whitelist_names(payload)
    if names is None:
        return None
    lowered = name.lower()
    return [entry for entry in names if entry.lower() == lowered]


def whitelist_lookup(payload: str, name: str) -> tuple[bool, str] | None:
    """白名单里有没有这个玩家名；有的话顺带给出**服务端记录的拼写**。

    返回 None = 判不出来（格式不认识）；否则是 (在不在, 服务端拼写)。

    拼写要单独回传：服务端写白名单前会拿名字去查玩家档案，查到就换成档案里的规范
    拼写（`add vul` → 存成 `Vul`），查不到才用原样字符串。机器人只负责把命令发过去，
    拼写是服务端定的——不回传的话，群里看到「已将 vul 添加」而名单里是 `Vul`，
    会以为加错了人。

    只关心「在不在、叫什么」的调用方用这个；需要区别对待重复条目的用 whitelist_matches。
    """
    matches = whitelist_matches(payload, name)
    if matches is None:
        return None
    return (True, matches[0]) if matches else (False, name)


def whitelist_contains(payload: str, name: str) -> bool | None:
    """白名单里有没有这个玩家名。判不出来时返回 None。"""
    found = whitelist_lookup(payload, name)
    return None if found is None else found[0]


def plan_mutation(verb: str, player: str, matches: list[str]) -> tuple[bool, list[str]]:
    """前置读之后的决策：要不要下发、下发哪几条拼写。

    返回 (是否无需改动, 要下发的目标列表)，两者互斥——noop 时列表必为空。

    抽成纯函数是为了能离线自测：run_whitelist_command 要活的 RCON，这里这张决策表
    才是「报得准不准」的真正所在。
    """
    if matches:
        # 已在名单里再 add，会在服务端写出同名不同拼写的重复条目
        return (True, []) if verb == "add" else (False, matches)
    # remove 一个本来就不在的名字：无事发生，但**不能**报「已移出」
    return (False, [player]) if verb == "add" else (True, [])


def settle(
    verb: str, player: str, targets: list[str], after: list[str]
) -> tuple[bool, str]:
    """反查之后的判定：成败如何，以及该回显的「服务端拼写」是什么。"""
    ok = bool(after) if verb == "add" else not after
    if after:
        return ok, after[0]  # add 成功后的规范拼写 / remove 失败后残留的拼写
    if verb == "remove" and targets:
        return ok, targets[0]  # 实际删掉的那条，用来回显服务端拼写
    return ok, player


async def run_whitelist_command(cmd: AdminCommand, target: ServerTarget) -> AdminResult:
    """对指定目标执行命令并独立验证。

    RCON 故障（RconError 子类）照常抛出，由调用方转成用户文案——在这里塞进字符串
    字段反而会丢掉类型，没法给出「密码错」和「连不上」不同的提示。

    **顺序是先读名单、再下命令**，不是反过来。两个理由：

    1. 大小写。见模块 docstring 第 4 条：服务端认不认大小写取决于版本和实现，所以
       照着它记录的拼写下命令（`remove vul` 实际发出去的是 `remove Vul`），不必赌。
    2. 「本来就在 / 本来就不在」要能报准。名单里没有的名字，`remove` 照样返回成功但
       什么也没删；不做前置判断就会把「没发生的事」报成「已移出」。

    代价是每次变更最多 3 个 RCON 往返（前置读 + 执行 + 反查），最坏 3×rcon_timeout；
    已经是目标状态时只花 1 个。这是管理员手动触发的低频操作，不在轮询里，值得。
    并发仍然安全：mc.rcon_command 在该目标的锁上串行，两个管理员不会把响应串在一起。

    白名单只发往**一个**目标（mcs_servers.toml 的 [whitelist].target）——代理层白名单是
    网络级的一份，逐子服各改一遍只会让几份名单漂移。
    """
    if cmd.verb == "list":
        names = parse_whitelist_names(await rcon_command(target, "whitelist list"))
        return AdminResult(
            verb="list",
            player="",
            ok=names is not None,
            names=names or [],
            detail="" if names is not None else "whitelist list 输出无法解析",
        )

    matches = whitelist_matches(await rcon_command(target, "whitelist list"), cmd.player)
    if matches is None:
        return AdminResult(
            verb=cmd.verb, player=cmd.player, ok=None, detail="whitelist list 输出无法解析"
        )
    noop, spellings = plan_mutation(cmd.verb, cmd.player, matches)
    if noop:
        return AdminResult(
            verb=cmd.verb,
            player=cmd.player,
            ok=True,
            noop=True,
            # server_name 显式给：默认的空串会让「服务端记录为 」这种半截后缀跑出来
            server_name=matches[0] if matches else cmd.player,
        )

    raw = ""
    # 循环变量叫 spelling 不叫 target：本函数的形参 target 是服务器目标，
    # 而这里遍历的是**玩家名的拼写**（同一玩家在服务端可能有不止一条记录）。
    for spelling in spellings:
        raw = await rcon_command(target, f"whitelist {cmd.verb} {spelling}")
        # 回执只记 debug：它的文案是本地化的，不足以判定成败（见 docstring 第 3 条）
        logger.debug("RCON whitelist {} {} 回执: {}", cmd.verb, spelling, raw)

    after = whitelist_matches(await rcon_command(target, "whitelist list"), cmd.player)
    if after is None:
        return AdminResult(
            verb=cmd.verb, player=cmd.player, ok=None, detail="whitelist list 输出无法解析"
        )
    ok, server_name = settle(cmd.verb, cmd.player, spellings, after)
    return AdminResult(
        verb=cmd.verb,
        player=cmd.player,
        ok=ok,
        detail="" if ok else f"回执: {raw[:200]!r}",
        server_name=server_name,
        targets=spellings,
    )
