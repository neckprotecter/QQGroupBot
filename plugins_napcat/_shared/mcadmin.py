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
from collections.abc import Sequence
from dataclasses import dataclass, field

from nonebot.log import logger

from . import mcbridge
from .mc import (
    RconAuthError,
    RconConnectError,
    RconError,
    parse_whitelist_names,
    rcon_command,
)
from .mcbridge import BridgeAuthError, BridgeError, BridgeUnreachable
from .mcservers import ServerTarget

VERBS: tuple[str, ...] = ("add", "remove", "list")


# ---------------------------------------------------------------- 通道无关的失败
# 白名单有两条通道：RCON（一台一个端点）和接口（群组级，一次改整组）。**上面的逻辑
# 一模一样**（先读后写 + 反查），所以故障类型也必须是通道无关的一套 ——
# 让 run_whitelist_command 去分辨「这个异常是 RCON 的还是 HTTP 的」，等于把通道判断
# 散进业务逻辑里，而它下面只有「读名单 / 下命令」两件事。
#
# 三类（同 mc.error_kind 那套）：下一步动作不同 —— 连不上查网络、认证失败要新 token、
# 数据不对查对方的插件版本。调用方拿 `WhitelistRoute.channel` 决定文案里说「RCON」
# 还是「接口」。
class WhitelistError(Exception):
    """白名单操作失败（通道无关）。"""


class WhitelistUnreachable(WhitelistError):
    """连不上：拒绝连接、超时、隧道断了。"""


class WhitelistAuthError(WhitelistError):
    """凭证不对：RCON 密码错 / 接口 401。"""


class WhitelistDataError(WhitelistError):
    """连上了，但答的内容不是我们要的形状。"""


def _translate(exc: Exception) -> WhitelistError:
    """把两条通道各自的异常翻成上面那三类。**原文照抄**，只换类型。

    原文里带着地址和原因（`请求 http://… 超时` / `连接 RCON 127.0.0.1:25575 失败`），
    那正是排查要看的；调用方决定它进日志还是进群。
    """
    if isinstance(exc, (RconAuthError, BridgeAuthError)):
        return WhitelistAuthError(str(exc))
    if isinstance(exc, (RconConnectError, BridgeUnreachable)):
        return WhitelistUnreachable(str(exc))
    return WhitelistDataError(str(exc))


async def _read_names(target: ServerTarget, command: str) -> list[str] | None:
    """读当前白名单的名字。**判不出来返回 None**，通道故障抛 WhitelistError。

    两条通道在「空名单」上的语义**不一样**，这里必须都处理对：RCON 的 `list` 在名单
    为空时输出的是一句哨兵文案，靠 parse_whitelist_names 才能认出「是空的」而不是
    「判不出来」；接口直接给 `entries: []`，天然无歧义。
    """
    if target.is_api:
        try:
            whitelist = await mcbridge.fetch_whitelist(target.api, target.timeout)
        except BridgeError as exc:
            raise _translate(exc) from exc
        return list(whitelist.entries)
    try:
        payload = await rcon_command(target, command)
    except RconError as exc:
        raise _translate(exc) from exc
    return parse_whitelist_names(payload)


async def _send_mutation(
    target: ServerTarget, command: str, verb: str, spelling: str
) -> str:
    """下发一条变更命令，返回**回执原文**（只用于日志/排查，不用于判定成败）。"""
    if target.is_api:
        try:
            result = await mcbridge.write_whitelist(
                target.api, target.timeout, verb, spelling
            )
        except (BridgeError, ValueError) as exc:
            raise _translate(exc) from exc
        # 接口的回执是结构化的，但**它的 ok/changed 在各版本里含义不同**（见
        # BridgeWriteResult），所以这里只把原样打成一行给日志，成败照旧看反查。
        return f"ok={result.ok} changed={result.changed}"
    return await rcon_command(target, f"{command} {verb} {spelling}")

# 既是格式校验，也是注入防护（见模块 docstring 第 2 条）。
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")

# parse_command 的失败原因（机器可读，调用方据此出不同文案）
# 这里**只有**解析层真会返回的三种。「服名写到了动词前面」不在其中：那一层只看词序，
# 判断「这个 token 是不是本群一台服的名字」需要配置视图，所以它在 mc_admin 里查
# （parse_command 只把动词前的 token 原样交出去）。
ERR_USAGE = "usage"  # 没找到子命令
ERR_LIST_ARGS = "list_args"  # list 后面跟了不止一个参数
ERR_NAME = "bad_name"  # 名字缺失或不合法


@dataclass(frozen=True)
class AdminCommand:
    verb: str  # "add" | "remove" | "list"
    player: str = ""  # 只有 add / remove 有
    # 群里点名的**服名原文**（动词之后最后一个 token），空 = 没点名。
    # 它**不是** target id：这一层只做语法，认不认得出来由持有配置视图的一方做
    # （ServerBook.pick_whitelist），所以这里不校验它。
    server: str = ""
    # 动词**之前**的 token（原样、没小写）。这一层刻意不认识它们 —— 触发词就在里面，
    # 一律报错会误伤「帮我 whitelist add Steve」这种说话习惯。原样交出来是为了让有
    # ServerBook 的调用方能拦下「服名写错位置」（whitelist bingo add Steve）——
    # 那种写法在 P6 之前是**静默丢掉 bingo** 再把命令发往缺省那台。
    pre_verb: tuple[str, ...] = ()


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
    # **变更命令**（add / remove）是否真的发给服务端了。ok is None 时靠它分辨两种
    # 含义相反的情况：前置读就没读懂 → 一条命令都没发；反查没读懂 → 命令已经发出去了。
    # 两者的 ok 都是 None，都不该塌缩成 False，但**该说的话正相反** —— 拿这个字段
    # 分开说，否则「没发出去」会被报成「已发送（未能验证）」，等于把没发生的事报成发生了。
    # list 不改动任何东西，所以恒为 False（调用方在 list 分支上不看它，见 mc_admin）。
    mutation_sent: bool = False


def parse_command(text: str) -> tuple[AdminCommand | None, str]:
    """解析 `whitelist [服名] add|remove <玩家名> [服名]` / `whitelist [服名] list`。

    返回 (命令, 失败原因)；成功时原因为 ""。

    **服名写在末尾**（P6 起的 B 方案）：动词之后的 token 里，最后一个当服名，其余
    join 起来当玩家名。这个切法**无歧义**，因为玩家名正则不允许空格 —— 两个以上
    token 时第一个一定是玩家名、最后一个一定是服名，不需要靠「猜哪个像服名」。
    只有一个 token 时它是玩家名（add/remove 的服名可省，多台时由调用方要求点名）。

    动词之前的 token 一律收进 pre_verb 交给调用方：那里本来是**静默丢弃**的
    （`whitelist bingo add Steve` 里的 bingo 会被扔掉、命令照发往缺省那台），
    P6 把「发给哪台」交给它决定之后，再丢掉就等于改错服。
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
    head = tuple(tokens[:pos])

    if verb == "list":
        # list 没有玩家名，所以唯一的那个 token 就是服名（`whitelist list bingo`）。
        # 两个以上才说不清 —— 用法文案负责告诉他正确的写法。
        if len(rest) > 1:
            return None, ERR_LIST_ARGS
        return AdminCommand("list", server=rest[0] if rest else "", pre_verb=head), ""

    if not rest:
        return None, ERR_NAME
    server = rest[-1] if len(rest) >= 2 else ""
    # 多出来的 token 会被 join 进来，从而过不了正则——「whitelist add Steve please extra」
    # 和「whitelist add Steve; stop」都落在 ERR_NAME 上（后者的 `stop` 被当服名、
    # `Steve;` 过不了正则）。
    player = " ".join(rest[:-1]) if len(rest) >= 2 else rest[0]
    if not PLAYER_NAME_RE.match(player):
        return None, ERR_NAME
    return AdminCommand(verb, player, server, head), ""


def whitelist_matches_of(names: Sequence[str], name: str) -> list[str]:
    """在一份**已经解析好**的名单里找忽略大小写等于 name 的条目，按服务端拼写返回。

    比对必须是**整体**、不区分大小写的：子串匹配会让 `Steve` 命中 `Steve_2`。

    返回**全部**而不是第一条：offline-mode 下 `Vul` 和 `vul` 是两个不同的 UUID、
    两条独立记录，所以同名不同拼写的重复条目是真会出现的。只看第一条的话，
    「移除」会留下一条幽灵记录，紧接着的反查又命中它，报出假的「未生效」。
    """
    lowered = name.lower()
    return [entry for entry in names if entry.lower() == lowered]


def whitelist_matches(payload: str, name: str) -> list[str] | None:
    """同上，但输入是 RCON `list` 的**原始回执**。

    返回 None = 判不出来（格式不认识）；空列表 = 名单里没有。
    接口那条通道不需要它 —— 它给的就是一份数组（见 _read_names）。
    """
    names = parse_whitelist_names(payload)
    if names is None:
        return None
    return whitelist_matches_of(names, name)


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


async def run_whitelist_command(
    cmd: AdminCommand, target: ServerTarget, command: str = "whitelist"
) -> AdminResult:
    """对指定目标执行命令并独立验证。

    `command` 是 RCON 里实际发的命令前缀，来自**本群那条群关联**的
    `whitelist.command`（见 mcaudiences）。它必须与插件注册的命令一字不差：
    vanilla / NekoList 是 `whitelist`，Global Whitelist 是 `globalwhitelist`。
    写错的表现是每次都回 `Unknown command`、群里报「未生效」，而且**不报错** ——
    所以调用方必须把配置值传进来，不能在这里硬编码（那样这个配置键就是「被接受
    但被忽略」，比没有它更坏）。

    通道故障（WhitelistError 的三个子类，RCON 与接口共用）照常抛出，由调用方转成
    用户文案——在这里塞进字符串字段反而会丢掉类型，没法给出「凭证错」和「连不上」
    不同的提示。**走哪条通道由目标自己决定**（`target.is_api`）：群组服那台只有
    接口没有 RCON，而这套「先读后写 + 反查」的逻辑对两条通道一字不差。

    **顺序是先读名单、再下命令**，不是反过来。两个理由：

    1. 大小写。见模块 docstring 第 4 条：服务端认不认大小写取决于版本和实现，所以
       照着它记录的拼写下命令（`remove vul` 实际发出去的是 `remove Vul`），不必赌。
    2. 「本来就在 / 本来就不在」要能报准。名单里没有的名字，`remove` 照样返回成功但
       什么也没删；不做前置判断就会把「没发生的事」报成「已移出」。

    代价是每次变更最多 3 个 RCON 往返（前置读 + 执行 + 反查），最坏 3×rcon_timeout；
    已经是目标状态时只花 1 个。这是管理员手动触发的低频操作，不在轮询里，值得。
    并发仍然安全：mc.rcon_command 在该目标的锁上串行，两个管理员不会把响应串在一起。

    **每次只打一台**（P6 的 B 方案：一条群关联可以声明多台白名单服，但命令里点名
    的那一台才收命令）。一条命令同时改多台 = 一次误操作同时改坏几台服的白名单，
    收益与风险不成比例；要看多台的现状用 list（读操作，由调用方并发发起）。
    """
    # 读名单在两条通道上都是「发一条 list」：RCON 是 `whitelist list`，接口是 GET
    # /whitelist。命令前缀只对 RCON 有意义，接口那条由 mcbridge 自己拼路径。
    listed = f"{command} list"

    if cmd.verb == "list":
        names = await _read_names(target, listed)
        return AdminResult(
            verb="list",
            player="",
            ok=names is not None,
            names=names or [],
            detail="" if names is not None else f"{listed} 输出无法解析",
        )

    before = await _read_names(target, listed)
    matches = None if before is None else whitelist_matches_of(before, cmd.player)
    if matches is None:
        # 前置读就读不懂 → **一条变更命令都没发**（mutation_sent 保持默认 False）。
        # 读不到当前名单就不敢下手，这是刻意的：不知道名单里有什么，
        # 「添加」可能写出重复条目，「移除」可能删错拼写。
        return AdminResult(
            verb=cmd.verb, player=cmd.player, ok=None, detail=f"{listed} 输出无法解析"
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
        raw = await _send_mutation(target, command, cmd.verb, spelling)
        # 回执只记 debug：它的文案是本地化的，不足以判定成败（见 docstring 第 3 条）
        logger.debug("{} {} {} 回执: {}", command, cmd.verb, spelling, raw)

    after_names = await _read_names(target, listed)
    after = None if after_names is None else whitelist_matches_of(after_names, cmd.player)
    if after is None:
        # 命令**已经发出去了**（上面的循环），只是反查读不懂 —— 和前置读读不懂是
        # 两件相反的事，所以 mutation_sent 要显式给 True。
        return AdminResult(
            verb=cmd.verb,
            player=cmd.player,
            ok=None,
            detail=f"{listed} 输出无法解析",
            mutation_sent=True,
        )
    ok, server_name = settle(cmd.verb, cmd.player, spellings, after)
    return AdminResult(
        verb=cmd.verb,
        player=cmd.player,
        ok=ok,
        detail="" if ok else f"回执: {raw[:200]!r}",
        server_name=server_name,
        targets=spellings,
        mutation_sent=True,
    )
