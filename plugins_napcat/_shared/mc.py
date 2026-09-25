"""Minecraft 服务器取数层（不依赖 NoneBot；除了逐目标缓存 RCON 长连接外无状态）。

**逐目标**：本模块不再有任何「当前服务器」的隐式状态。目标（地址、端口、超时、
RCON 端点）由 _shared/mcservers.py 从 mcs_servers.toml 解析，每个取数函数都显式收一个
ServerTarget。RCON 长连接与命令锁按目标 id 分开存放，一台服的操作不会挡住另一台。

放在 _shared 而不是 mcs/ 的原因：mcs/__init__.py 会 import 持有 matcher 的
mc_reporter，而 mc_reporter 顶层有 get_driver()，在 nonebot.init() 之前 import
会抛 ValueError。tools/mc_check.py 必须能在不启动机器人的情况下跑，所以取数
逻辑不能住在持有 matcher 的包里。

取数设计（三条一起看才成立）：
- 在线状态与人数只信 SLP（Server List Ping）：结构化、无本地化、所有 1.7+
  服务端都支持，且不受插件影响。
- 玩家名单只信 RCON 的 list 命令：走命令通道，不受 hide-online-players 影响。
- 名单完整性 = len(names) == SLP 报的在线人数。

第三行是关键。SLP 的 players.sample 默认最多 12 个**随机**玩家、
hide-online-players=true 时直接为空、插件还能往里塞广告，绝不能当名单用。
而 RCON 的 list 文案是本地化的（中英文前缀完全不同），插件还可能整个改写格式
（EssentialsX 的 /list 根本没有冒号）。用「名字个数 vs SLP 人数」交叉校验，
就不必去解析那些文案里的数字——任何语言、任何格式改动导致的解析失败，都会
自动降级成「名单不完整」，而不是基于残缺名单推出一堆假的进服事件。

**第三条通道：群组服的 HTTP 接口**（_shared/mcbridge.py），只给「群组里那几台」用。
它的形状和上面两条都不同 —— **一次请求给出整组的数据**，所以：

- 人数和名单**出自同一份响应**，上面那条「两个独立来源交叉校验」对它不成立。
  补回来的办法是：接口型目标**能填 host/port 就填**，填了就用 SLP 再问一次人数，
  和接口的对一遍（对不上就如实报「接口数据可疑」）。不填就只能信接口。
- 所以 Snapshot 多了一个 `count_source`：人数到底是谁给的。以前这个问题不存在
  （答案永远是 SLP），现在它是「这个数字可不可信」的线索之一。
- 子服（`source` 指回某台 hub）**不发任何请求**，数据从 hub 那一份响应里切出来。
  见 fetch_snapshots。
"""
import asyncio
import contextlib
import random
import re
import socket
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field

from mcstatus import JavaServer

from . import mcbridge
from .mcbridge import BridgeAuthError, BridgeError, BridgeStatus, BridgeUnreachable
from .mcservers import ServerTarget

# SLP 失败的两种性质完全不同的原因，分开是为了**文案不把人指错方向**：
#   unreachable = 真没连上（服务器没开、端口不对、防火墙丢包）→ 该去查网络
#   unparseable = 连上了、服务端也答了，但回的不是合法状态响应。
#                 mcstatus 拿到合法 JSON 却缺 players/version 等必需字段时抛的
#                 `OSError("Received invalid status response")` 属于这一类
#                 （见 mcstatus/_protocol/java_client.py 的 _handle_status_response）。
#                 GTNH 这类大整合包**启动期间**就会这样：端口已 bind、能应答，
#                 但玩家列表还没就绪。报「不可达」会让人去查防火墙，方向全错。
SLP_UNREACHABLE = "unreachable"
SLP_UNPARSEABLE = "unparseable"

# 失败性质里的第三种：**凭证不对**（接口 401）。前两种 SLP_* 其实是一套「性质」分类
# 而不只是 SLP 的（连不上 vs 连上了但内容不对），所以接口的失败也落在它们上面；
# 唯独 401 两边都套不上 —— 它既不是网络问题也不是内容问题，下一步动作是**去要新
# token**，报成「连不上了」会让人去查防火墙和隧道，方向全错。
API_AUTH = "api-auth"

# 判据是「异常类型」而不是文案：文案是库的实现细节，会随版本变。
_CONN_ERRORS = (
    ConnectionRefusedError,  # 没开 / 端口不对
    ConnectionResetError,  # 被对端重置
    ConnectionAbortedError,
    TimeoutError,  # 防火墙丢包（握手挂住）；3.11+ 与 asyncio.TimeoutError 是同一个
    socket.gaierror,  # 域名解析不了
    EOFError,  # 连上就被关（常见于端口被非 MC 服务占用）
)


def _describe_slp_failure(exc: BaseException) -> str:
    """把 SLP 异常连同 cause 链写成一行。

    只取 `str(exc)` 会丢掉 mcstatus 特意链上的底层异常：它抛的
    `OSError("Received invalid status response")` **本身不说缺了哪个字段**，
    而 `from ValueError/KeyError` 链上的那句才说得清。排查时这行往往就是全部线索，
    丢掉它等于把「缺 players 字段」降级成「不可达」。
    """
    parts = [f"{type(exc).__name__}: {exc}"]
    cause, depth = exc.__cause__, 0
    while cause is not None and depth < 3:  # 限深：异常链理论上可以成环
        parts.append(f"{type(cause).__name__}: {cause}")
        cause = cause.__cause__
        depth += 1
    return " ← ".join(parts)

# ---------------------------------------------------------------- 目标

# 目标（host / port / 超时 / RCON 端点）来自 mcs_servers.toml，由 _shared/mcservers.py 解析。
# 本模块**不再读任何 MC_* 环境变量** —— 那些变量在配置载体切换后已失效。
# 所有取数函数都要显式收一个 ServerTarget，不再有「当前服务器」这个隐式状态。


# ---------------------------------------------------------------- 快照

@dataclass
class McSnapshot:
    """一次探测的结果。

    reachable 是「服务器是否响应」，不要用 count > 0 代替——「在线但 0 人」和
    「连不上」在文案上必须是两种，混用会导致服务器挂掉时报「当前 0 人在线」。
    """

    reachable: bool
    # 这份快照属于哪个目标（mcs_servers.toml 的 id）。逐目标缓存靠它归位，
    # 也靠它把「哪个服挂了」从一句模糊的「服务器不可达」里区分出来。
    target_id: str = ""
    count: int = 0
    max_players: int | None = None
    names: list[str] = field(default_factory=list)
    names_complete: bool = False
    names_source: str = "none"  # "rcon" | "slp-sample" | "api" | "none"
    # count 是哪条通道给的：`"slp"` 或 `"api"`。以前这个问题没有第二答案（SLP 是
    # 人数唯一来源），接口进来之后它变成了「这个数字有多可信」的线索 —— 接口型目标
    # 的人数来自对方插件自己的统计，而我们没法像 SLP 那样从外面独立验一次。
    # 代理那条尤其要看它：全群组人数是「口径」，子服人数之和是「明细」。
    count_source: str = "slp"
    latency: float | None = None
    version: str = ""
    error: str = ""  # 取数降级/失败的原因，用于日志与 mc_check 输出
    # RCON `list` 的**原始输出**。各服务端/语言的文案都不一样，解析结果对不上时
    # 唯一能看的就是它，所以由这里存下来给诊断工具打，而**不是**让调用方再发一次。
    # 两次 list 之间玩家可能进出，重发会让「打出来的原文」和「解析用的原文」
    # 不是同一份 —— 排查时最需要对齐的恰恰是这两者。
    raw_list: str = ""
    # 失败的**性质**，取值见 SLP_UNREACHABLE / SLP_UNPARSEABLE / API_AUTH / ""（没失败）。
    # 文案层必须按它分支：三者给用户的下一步动作互不相同 ——
    # 「连不上」去查网络和端口，「应答无法解析」去查服务端是不是还在启动，
    # 「凭证不对」去要新 token（查网络纯属白费功夫）。
    # 光看 reachable 分不出来（三者都是 False），所以这个字段不能靠 error 文案反推。
    error_kind: str = ""


# ---------------------------------------------------------------- RCON

class RconError(Exception):
    """RCON 相关错误基类。"""


class RconConnectError(RconError):
    """连不上：服务器没开、端口不对、防火墙挡了。"""


class RconAuthError(RconError):
    """密码错。与「连不上」分开——配置错误不该每 20 秒重试一次还刷日志。"""


class RconProtocolError(RconError):
    """包结构不对，多半是端口串到了别的服务。"""


# Source RCON 类型号。注意 EXECCOMMAND 和 AUTH_RESPONSE 都是 2，
# 只能靠 request id 和上下文区分。
_TYPE_RESPONSE_VALUE = 0
_TYPE_EXECCOMMAND = 2
_TYPE_AUTH_RESPONSE = 2
_TYPE_AUTH = 3

_MIN_PACKET_LEN = 10  # 4(id) + 4(type) + 2(尾部 \x00\x00)
_MAX_PACKET_LEN = 1 << 20  # 1 MiB：端口串到别的服务时，不校验会按荒谬长度分配内存
_READ_GRACE = 0.25  # 读完首包后再等这么久，看有没有后续分片


def _new_request_id() -> int:
    """随机非零 id：能立刻发现响应错位，而不是把上一条命令的输出当名单。"""
    return random.randint(1, 2**31 - 1)


def _pack(request_id: int, packet_type: int, payload: bytes) -> bytes:
    body = struct.pack("<ii", request_id, packet_type) + payload + b"\x00\x00"
    return struct.pack("<i", len(body)) + body


async def _read_header(reader: asyncio.StreamReader) -> int:
    (length,) = struct.unpack("<i", await reader.readexactly(4))
    if not _MIN_PACKET_LEN <= length <= _MAX_PACKET_LEN:
        raise RconProtocolError(f"包长度非法 {length}，该端口可能不是 RCON")
    return length


async def _read_body(reader: asyncio.StreamReader, length: int) -> tuple[int, int, str]:
    body = await reader.readexactly(length)
    request_id, packet_type = struct.unpack("<ii", body[:8])
    if body[-2:] != b"\x00\x00":
        raise RconProtocolError("包尾缺少 \\x00\\x00，协议不匹配")
    return request_id, packet_type, body[8:-2].decode("utf-8", errors="replace")


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    return await _read_body(reader, await _read_header(reader))


async def _authenticate(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, password: str
) -> None:
    request_id = _new_request_id()
    writer.write(_pack(request_id, _TYPE_AUTH, password.encode("utf-8")))
    await writer.drain()

    # 认证失败时 vanilla 会先多发一个空的 RESPONSE_VALUE，所以不能只读一个包
    for _ in range(4):
        resp_id, packet_type, _payload = await _read_packet(reader)
        if packet_type != _TYPE_AUTH_RESPONSE:
            continue
        if resp_id == -1:
            raise RconAuthError("RCON 密码错误（服务端返回认证失败）")
        if resp_id != request_id:
            raise RconProtocolError(f"认证响应 id 不匹配：{resp_id} != {request_id}")
        return
    raise RconProtocolError("认证阶段未收到 AUTH_RESPONSE")


async def _execute(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, command: str) -> str:
    request_id = _new_request_id()
    writer.write(_pack(request_id, _TYPE_EXECCOMMAND, command.encode("utf-8")))
    await writer.drain()

    resp_id, _packet_type, payload = await _read_packet(reader)
    if resp_id != request_id:
        raise RconProtocolError(f"响应 id 不匹配：{resp_id} != {request_id}")
    chunks = [payload]

    # 继续收后续分片，直到短超时说明没有更多数据为止。
    # 不用 Source 引擎那套 type-0 哨兵包——Minecraft 根本不回显 type-0，照抄会挂死。
    # list 的输出远小于 4096 字节（约 230 名玩家才溢出），真被截断也有
    # len(names) == 人数 的交叉校验兜底，所以不值得为极端情况加终止包复杂度。
    while True:
        try:
            length = await asyncio.wait_for(_read_header(reader), _READ_GRACE)
        except (TimeoutError, asyncio.IncompleteReadError):
            break
        rid, _packet_type, payload = await _read_body(reader, length)
        if rid == request_id:
            chunks.append(payload)
    return "".join(chunks)


# 长连接复用。MC 服务端对**每条** RCON 连接都会打两行 INFO 日志
# （`Thread RCON Client ... started` / `... shutting down`），一轮一条连接
# 就能把服务端控制台刷满，把真正的 join/leave 和报错埋掉。复用后只剩 bot
# 启动/重连时各一组。实测闲置 120s 连接仍存活，而轮询间隔只有 10s。
_conns: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
_cmd_locks: dict[str, asyncio.Lock] = {}

_Conn = tuple[asyncio.StreamReader, asyncio.StreamWriter]


def _lock(target_id: str) -> asyncio.Lock:
    """逐目标的命令串行锁：RCON 是单条有序流，并发写会把两条命令的响应串在一起。

    锁按目标分开，两个子服就可以同时跑命令 —— 白名单操作不该被另一台的探测挡住。
    """
    lock = _cmd_locks.get(target_id)
    if lock is None:
        lock = _cmd_locks[target_id] = asyncio.Lock()
    return lock


async def _discard(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def _close(target_id: str) -> None:
    """丢掉该目标缓存的连接（幂等）。"""
    conn = _conns.pop(target_id, None)
    if conn is not None:
        await _discard(conn[1])


async def close_all() -> None:
    """关掉所有目标的连接（RCON 长连接 + 接口那条 HTTP 连接池）。进程收尾用。"""
    for target_id in list(_conns):
        await _close(target_id)
    await mcbridge.close_all()


async def prune(keep_ids: set[str]) -> list[str]:
    """关掉配置里已经消失的目标的连接，返回被剪掉的 id。

    子服从 mcs_servers.toml 里删掉后连接会一直挂着，而 MC 服务端**每条** RCON 连接
    都会打两行 INFO 日志，留着就是持续刷对方控制台。锁不剪：它们没有对端资源，
    而且可能正被 await 持有，剪掉会造出两把并存的锁。
    """
    stale = [tid for tid in _conns if tid not in keep_ids]
    for target_id in stale:
        await _close(target_id)
    return stale


async def _open(target: ServerTarget) -> _Conn:
    """建连 + 认证。失败时抛 RconConnectError / RconAuthError，并关掉半开的连接。

    连接与认证各自带 timeout：主机被防火墙丢包（而不是回 RST）时 TCP 握手会一直
    挂着，没超时就把整轮探测拖死；而裸的 TimeoutError 消息为空，报出来是
    「TimeoutError: 」，看不出是哪一步、也看不出超时值。这里翻译成能读的文案。
    """
    spec = target.rcon
    if spec is None or not spec.enabled:  # 调用方应先查 rcon_enabled，这里是兜底
        raise RconConnectError(f"{target.name} 未配置 RCON 密码")
    try:
        async with asyncio.timeout(target.rcon_timeout):
            # 用 rcon_host 而不是 target.host：SLP 和 RCON 可以不在一个地址上
            # （RCON 走隧道/内网、SLP 走公网端口，见 RconSpec.host）
            reader, writer = await asyncio.open_connection(target.rcon_host, spec.port)
    except asyncio.TimeoutError as exc:
        raise RconConnectError(
            f"连接 RCON {target.rcon_addr} 超时（{target.rcon_timeout:g}s，"
            f"服务端在跑吗 / 端口对不对？）"
        ) from exc
    except OSError as exc:
        raise RconConnectError(
            f"连接 RCON {target.rcon_addr} 失败：{exc}"
        ) from exc

    try:
        async with asyncio.timeout(target.rcon_timeout):
            await _authenticate(reader, writer, spec.password)
    except asyncio.TimeoutError as exc:
        await _discard(writer)
        raise RconConnectError(
            f"RCON 认证超时（{target.rcon_timeout:g}s，该端口可能不是 RCON 服务）"
        ) from exc
    except BaseException:
        await _discard(writer)
        raise
    return reader, writer


async def _run(target: ServerTarget, conn: _Conn, command: str) -> str:
    async with asyncio.timeout(target.rcon_timeout):
        return await _execute(*conn, command)


async def rcon_command(target: ServerTarget, command: str) -> str:
    """对指定目标执行一条 RCON 命令，返回输出文本。连接跨调用复用。

    服务端重启、或它自己回收了闲置连接时，缓存的那条会失效。此时丢弃重连一次
    再重试，调用方无感。但**全新连接都失败就不重试**——那是真故障（没开、端口
    不对、密码错），重试只会让每轮探测多耗一个 rcon_timeout。

    超时算在「连接失效」里：复用的连接被服务端半开丢弃时，写可能成功而读一直
    没有响应，表现为超时而非 EOF。全新连接超时则不重试（`not reused` 挡住）。
    """
    if not target.rcon_enabled:
        raise RconConnectError(f"{target.name} 未配置 RCON 密码，无法执行命令")

    async with _lock(target.id):
        conn = _conns.get(target.id)
        reused = conn is not None
        if conn is None:
            conn = _conns[target.id] = await _open(target)
        try:
            return await _run(target, conn, command)
        except RconAuthError:
            await _close(target.id)
            raise  # 密码错，重连也还是错
        # asyncio.TimeoutError 在 3.11+ 与内置 TimeoutError（OSError 子类）同一
        # 个类，3.10 下却是独立的，两个都列上才跨版本都对
        except (RconError, OSError, EOFError, asyncio.TimeoutError):
            # IncompleteReadError 是 EOFError 的子类：对端关了连接
            await _close(target.id)
            if not reused:
                raise
            conn = _conns[target.id] = await _open(target)  # 复用的连接废了，重连一次
            return await _run(target, conn, command)


# ---------------------------------------------------------------- list 解析

_COLOR_CODE = re.compile("§.")
# 名字之间的分隔符。除了逗号，Minecraft 的列表还会在**最后两项之间**用「 and 」
# （ComponentUtils.formatList 的行为：`A, B and C`）。漏掉它会把最后两个人粘成一个
# token——白名单校验会因此误报「未生效」，进服提醒会因条数对不上而静默暂停。
# 玩家名只可能是 [A-Za-z0-9_.]，不含空格，所以「 and 」不会误切。
# 其它语言的连接词（如「和」）没处理：那种情况下解析出的条数会偏少，两级调用方
# 都会安全降级（白名单回「未能验证」、进服提醒静默暂停），不会误报。
_NAME_SPLIT = re.compile("[,，、]| and ")


def _split_names(tail: str) -> list[str]:
    """按逗号拆开名字段，去掉 § 颜色码与空白，丢掉空段。"""
    names: list[str] = []
    for raw in _NAME_SPLIT.split(tail):
        name = _COLOR_CODE.sub("", raw).strip()
        if name:
            names.append(name)
    return names


def parse_list_names(payload: str, expected: int | None = None) -> list[str]:
    """从 RCON `list` 的输出里取玩家名。

    只取名字，不解析数字——人数一律以 SLP 为准（见模块 docstring）。

    **切分位置有歧义**：前缀是本地化的服务端文案（结尾一个冒号），但玩家显示名
    里也可能带冒号（`[VIP]: Alice`、昵称插件等）。固定按第一个还是最后一个冒号
    切都会在某些服上切错，所以这里每个冒号位置都试一遍，优先返回条数正好等于
    `expected`（也就是 SLP 报的在线人数）的那个。人数本来就免费可得，用它消歧
    比猜格式可靠。

    拿不到确定的名单就返回空列表（找不到冒号 / EssentialsX 之类改写过的格式），
    调用方随后会因 len(names) != 人数 判定名单不完整并降级，不会误报。
    """
    text = (payload or "").strip()
    if not text:
        return []

    cuts = [i for i, ch in enumerate(text) if ch in ":："]
    if not cuts:
        return []

    fallback: list[str] | None = None
    for cut in cuts:
        names = _split_names(text[cut + 1 :])
        if expected is not None and len(names) == expected:
            return names
        if fallback is None:
            fallback = names
    return fallback or []


def _normalize_reply(text: str) -> str:
    """把一句回执压成可比对的形状：去 § 颜色码、把连续空白压成一个空格、忽略大小写。"""
    return " ".join(_COLOR_CODE.sub("", text).split()).casefold()


# 「白名单是空的」在服务端用的是**另一个翻译键**（vanilla 的 commands.whitelist.none），
# 那句话**整句没有冒号**，于是会被下面「第一个冒号切前缀」的规则误判成「格式不认识」。
# 实测（2026-09-21，Bingo 26.2）：
#     There are no whitelisted players
#
# 代价不只是查不到：add / remove 的**前置读**也拿不到结果，mcadmin 会在那里提前
# return，命令根本发不出去 —— 白名单为空（每台新服的初始状态）时，管理员没法从群里
# 加第一个人。
#
# 所以这里认一组「已知的空名单哨兵句」，**必须整句相等**（见 _normalize_reply）。
# 刻意**不**做成「没冒号就算空名单」：命令前缀写错时的回执同样没冒号，实测 Bingo 26.2
# 的 `nosuchcmd list` 回的是
#     Unknown or incomplete command. See below for errornosuchcmd list<--[HERE]
# 那样就会把「前缀写错」误报成「白名单是空的」，比原本的「未能解析」更难查。
#
# 匹配不上时行为完全不变（照旧 None、照旧如实报「未能解析」），所以加哨兵只可能把被
# 误伤的正经回执救回来，不会让任何现有判定退化。换了语言/插件的服若报「未能解析」，
# 先跑 `mc_check.py --whitelist` 看原文，把那句**原样**加进这里即可。
_EMPTY_WHITELIST_REPLIES = frozenset(
    _normalize_reply(reply)
    for reply in (
        "There are no whitelisted players",  # vanilla en_us：commands.whitelist.none
    )
)


def parse_whitelist_names(payload: str) -> list[str] | None:
    """从 RCON `whitelist list` 的输出里取全部白名单玩家名。

    返回三态，调用方必须区分开：
      · 列表（可以是空的）—— 解析成功；空列表 = 确实没人在白名单里
      · None             —— 判不出来（输出为空，或没有冒号说明格式被改写过）

    和 parse_list_names 同一条原则：不解析本地化前缀文案。先在**第一个冒号**处切掉
    前缀（与该函数拿不到期望人数时的兜底路径一致），再按逗号拆、去 § 颜色码、丢空段。

    不能图省事直接按逗号切整段：`There are 2 whitelisted players: Alice, Bob` 的第一段
    会连着前缀文案一起变成 `There are 2 whitelisted players: Alice`，跟 `Alice` 比不上。

    **空名单是个例外，走 _EMPTY_WHITELIST_REPLIES 整句比对**：那句回执没有冒号，
    不特判就会掉进「判不出来」。这条必须排在冒号判断**之前** —— 排在后面等于永远
    到不了（没有冒号的分支已经 return None 了）。
    """
    text = (payload or "").strip()
    if not text:
        return None
    if _normalize_reply(text) in _EMPTY_WHITELIST_REPLIES:
        return []  # 确定是「没人」，不是「看不懂」—— 两者对调用方含义相反
    cut = next((i for i, ch in enumerate(text) if ch in ":："), None)
    if cut is None:
        return None  # 没有冒号：可能是 EssentialsX 之类改写过的格式，不猜
    return _split_names(text[cut + 1 :])


# ---------------------------------------------------------------- 取数

@dataclass
class _SlpProbe:
    """一次 SLP 探测的原始结果。

    单独抽出来是因为**接口型目标也要用它**：它不走 fetch_snapshot 那整条流程（那是
    「SLP 定人数 + RCON 取名单」的流程），但需要 SLP 给接口报的人数做一次独立交叉
    校验 —— 那是接口型目标唯一能验接口有没有算错的办法（见模块 docstring）。
    """

    ok: bool
    count: int = 0
    max_players: int | None = None
    latency: float | None = None
    version: str = ""
    # 已经滤掉无名条目的 sample，fetch_snapshot 的降级路径直接用它
    sample: list[str] = field(default_factory=list)
    error: str = ""
    error_kind: str = ""


async def _slp_probe(target: ServerTarget) -> _SlpProbe:
    """对目标做一次 SLP。**不抛异常**，失败体现在返回值里。

    失败分两类（见 SLP_UNREACHABLE / SLP_UNPARSEABLE）：连不上 vs 答了但答不对。
    两类给用户的下一步动作完全相反，所以这里连**文案**都分开给好，调用方不必再拼。
    """
    try:
        # 不用 lookup()/async_lookup()：那会走 dnspython 的 SRV 查询，对
        # 127.0.0.1 纯属浪费，还引入「链式写法要 await 两次」的坑。
        # tries 默认是 3，显式传 1 —— 消抖交给上层的失败计数器，语义更清楚，
        # 也避免 20 秒轮询被重试拖到节奏漂移。
        server = JavaServer(target.host, target.port, timeout=target.timeout)
        status = await server.async_status(tries=1)
    except Exception as exc:
        # 连不上 vs 答了但答不对，两种失败的排查方向相反，别混成一句话。
        kind = SLP_UNREACHABLE if isinstance(exc, _CONN_ERRORS) else SLP_UNPARSEABLE
        label = "SLP 连不上" if kind == SLP_UNREACHABLE else "SLP 应答无法解析"
        return _SlpProbe(
            ok=False, error=f"{label}（{_describe_slp_failure(exc)}）", error_kind=kind
        )

    players = status.players
    return _SlpProbe(
        ok=True,
        count=int(players.online or 0),
        max_players=players.max,
        latency=getattr(status, "latency", None),
        version=getattr(getattr(status, "version", None), "name", "") or "",
        sample=[p.name for p in (players.sample or []) if getattr(p, "name", "")],
    )

async def fetch_snapshot(target: ServerTarget) -> McSnapshot:
    """探测一个**自己取数**的目标（SLP 型 / RCON 型），返回快照。任何失败都体现在返回值里。

    接口型目标（`api`）和挂在接口上的子服（`source`）不走这里 —— 它们的数据来自
    一份共享的接口响应，得由 fetch_snapshots 统一调度（见那里的同源合并）。
    """
    # 1) SLP：在线状态与人数的唯一来源（对这条通道而言）
    probe = await _slp_probe(target)
    if not probe.ok:
        # SLP 不通就直接判不可达：即便 RCON 还能应答，也不把它当作在线信号，
        # 因为 SLP 是基线锚点（拿不到人数就没法校验名单完整性）。
        return McSnapshot(
            target_id=target.id,
            reachable=False,
            error=probe.error,
            error_kind=probe.error_kind,
        )

    count = probe.count
    snap = McSnapshot(
        target_id=target.id,
        reachable=True,
        count=count,
        max_players=probe.max_players,
        latency=probe.latency,
        version=probe.version,
    )

    # 2) RCON 取完整名单。代理层不出名单（Velocity 无原生 glist），
    #    它的 SLP 只给全群组总人数 —— 所以直接跳过 RCON，别去发注定没意义的命令。
    rcon_error = ""
    if not target.serves_names:
        rcon_error = f"{target.name} 是代理，不出分服名单"
    elif target.rcon_enabled:
        try:
            snap.raw_list = await rcon_command(target, "list")
            # 传 expected=count 消解冒号切分位置的歧义（见 parse_list_names）
            snap.names = parse_list_names(snap.raw_list, expected=count)
            # 命令成功就算 rcon 来源，即便结果是空列表（0 人在线时本就为空）
            snap.names_source = "rcon"
        except RconAuthError as exc:
            rcon_error = str(exc)
        except RconError as exc:
            rcon_error = str(exc)
        except Exception as exc:
            rcon_error = f"{type(exc).__name__}: {exc}"
    else:
        rcon_error = "该目标没配 rcon.password（mcs_servers.toml）"

    # 3) RCON 没给出完整名单时，退一步看 SLP 的 sample。
    #    sample 顺序随机、上限约 12 条，但「条数 == 在线人数」时它就是完整名单
    #    （≤12 人的私服很常见）。有这条降级路径，没开 RCON 的服也能用进服提醒。
    #    代理不走这条：它的 sample 是**某一个后端**的随机样本，当成「全群组名单」
    #    是错的，而它本来也不出名单。
    if target.serves_names and len(snap.names) != count:
        if len(probe.sample) == count:
            snap.names = list(probe.sample)
            snap.names_source = "slp-sample"
        else:
            snap.names = []
            snap.names_source = "none"

    # 代理恒为 False：它拿不到分服名单，这不是「不完整」而是「不适用」。
    # 强制 False 是为了让所有查 names_complete 的地方（进服对账、mc_check 结论）
    # 自动跳过代理，不必每处都记得先判 kind。
    snap.names_complete = target.serves_names and len(snap.names) == count

    if not target.serves_names:
        # 代理没有名单不是故障，别写 error —— 判代理是否正常只看 reachable
        snap.error = ""
    elif not snap.names_complete:
        reason = rcon_error or f"SLP sample 只有 {len(probe.sample)} 条"
        snap.error = f"名单不完整（人数 {count}，拿到 {len(snap.names)}）：{reason}"
    elif rcon_error:
        snap.error = f"已降级用 SLP sample 取名单：{rcon_error}"

    return snap


# ------------------------------------------------- 接口型目标与它的子服（同源合并）

def _bridge_kind(exc: BaseException) -> str:
    """把接口的失败翻译成 error_kind —— 文案层按它分支，三类的动作互不相同。"""
    if isinstance(exc, BridgeAuthError):
        return API_AUTH  # 去要新 token
    if isinstance(exc, BridgeUnreachable):
        return SLP_UNREACHABLE  # 查网络 / 隧道 / 对方服务
    return SLP_UNPARSEABLE  # 对方的插件版本、路径，或者我们解析过时了


def _join_error(*parts: str) -> str:
    return "；".join(p for p in parts if p)


def _api_snapshot(
    target: ServerTarget,
    *,
    api_count: int,
    names: list[str],
    slp: _SlpProbe | None,
    note: str = "",
    exclude: int = 0,
) -> McSnapshot:
    """把「接口给的人数/名单」与「（可选的）SLP 复查」合成一份快照。

    填了 host 的目标会走 SLP 复查，**人数以 SLP 为准**：SLP 是这套取数里的基线锚点，
    也是接口型目标唯一能从外部独立验一次的数字（接口的人数来自对方插件自己的统计，
    它说什么就是什么）。接口只提供名单。两者对不上时如实报出来 —— 那正是「对方插件
    算错了」唯一会被发现的地方。

    SLP 复查没做成（多半是那台没有对外游戏端口、或端口变了）不算失败：退回用接口的
    人数，但把这件事写进 error，mc_check 一看就知道这个数字为什么没有第二来源。

    接口这条路**没有 SLP sample 那条降级**：子服的名单是代理给的（权威且完整），
    不完整就是对方的问题，用随机的 sample 去补只会把「对方少报了人」掩盖掉。

    `exclude` 是**中转服**上的人数，只对代理那条非零（见 `_api_hub_snapshot`）：
    扣减发生在**来源选定之后**，因为接口和 SLP 报的都是「连着代理的有几个」，
    两边都把中转服的人算在内 —— 只在接口那条分支上扣，SLP 一成功就把人带回来了。
    """
    count, count_source = api_count, "api"
    latency: float | None = None
    max_players: int | None = None
    version = ""
    error = note

    if slp is not None:
        if slp.ok:
            count, count_source = slp.count, "slp"
            latency, max_players, version = slp.latency, slp.max_players, slp.version
            if slp.count != api_count:
                # 对照用的是**扣减前**的原始数字：这里问的是「对方插件算错了吗」，
                # 拿我们自己的口径去修，会把唯一能发现对方算错的地方抹掉。
                error = _join_error(
                    error,
                    f"接口与 SLP 的人数对不上（接口 {api_count}，SLP {slp.count}）"
                    f"—— 接口那份数据可疑，已按 SLP 报",
                )
        else:
            error = _join_error(error, f"SLP 复查没做成（{slp.error}），人数只能用接口报的")

    # 名单长度对的是**原始**人数，所以这一句必须排在扣减之前。扣到负数是不可能的
    # （中转服的人都算在全群组里），但对方数据错乱时宁可报 0，也不要打出一个负数。
    complete = len(names) == count
    count = max(0, count - exclude)
    snap = McSnapshot(
        target_id=target.id,
        reachable=True,
        count=count,
        count_source=count_source,
        max_players=max_players,
        names=names,
        # 名单是接口给的（哪怕这次是空的 —— 0 人在线时本就为空，和 RCON 那条同一条
        # 约定：通道成功了就算这个来源）。代理不出分服名单，恒 "none"。
        names_source="api" if target.serves_names else "none",
        latency=latency,
        version=version,
    )
    snap.names_complete = target.serves_names and complete
    if not target.serves_names:
        # 代理的名单不是「不完整」而是「不适用」（同 fetch_snapshot），所以只说名单的
        # 那半句丢掉。**但 error 不能整个清空** —— 人数对不上正是从代理这条口径上
        # 看出来的（全群组总数就是它报的），清了等于把唯一能发现对方算错的地方抹掉。
        snap.error = error
    elif not snap.names_complete:
        snap.error = _join_error(error, f"名单不完整（人数 {count}，拿到 {len(names)}）")
    else:
        snap.error = error
    return snap


def _transit_count(hub: ServerTarget, status: BridgeStatus) -> tuple[int, str]:
    """数出中转服上的人，连同给诊断看的一句话。返回 `(人数, 说明)`。

    名字对不上时**不说成故障**：对方改 `velocity.toml` 的服名、或者那台中转服这次没起，
    都会走到这里。但也不能不出声 —— 扣不掉的表现是「人数偏大」，而偏大在群里看不出
    异样（总览那句「全群组 N 人」本来就是唯一的口径）。所以备注进快照的 error，
    mc_check --api 会把它打出来。
    """
    excluded = 0
    counted: list[str] = []
    unknown: list[str] = []
    for name in hub.transit:
        entry = status.find(name)
        if entry is None:
            unknown.append(name)
            continue
        excluded += entry.online
        # 0 人的中转服不进备注：扣了 0 个人这件事本身没什么好说的，说了只是噪音
        # （中转服上没人的时候才是常态）。
        if entry.online:
            counted.append(f"{name} {entry.online} 人")
    notes: list[str] = []
    if counted:
        notes.append(f"已扣掉中转服 {'、'.join(counted)}")
    if unknown:
        notes.append(
            f"transit 里写的 {'、'.join(unknown)} 不在接口的子服列表里"
            f"（接口里有：{'、'.join(status.names) or '一台都没有'}）——"
            f"名字要与对方 velocity.toml 里的服名逐字相同，否则一个人都扣不掉"
        )
    return excluded, "；".join(notes)


def _api_hub_snapshot(hub: ServerTarget, status: BridgeStatus, slp: _SlpProbe | None) -> McSnapshot:
    """hub 自己的快照。代理报的是**全群组**人数（口径），不进分服合计 —— 见 serves_names。

    「全群组」要减掉中转服上的人（`transit`，limbo 那种）：那个字段回答的是「连着代理
    的有几个」，而群里想知道的是「有几个在玩」。不减的后果是一个自相矛盾的总览 ——
    上面写「全群组 2 人」、下面几台子服加起来 1 人，而差额站在 limbo 里，谁都不知道。
    """
    excluded, note = _transit_count(hub, status)
    return _api_snapshot(
        hub, api_count=status.proxy_online, names=[], slp=slp, note=note, exclude=excluded
    )


def _api_sub_snapshot(
    sub: ServerTarget, hub: ServerTarget, status: BridgeStatus, slp: _SlpProbe | None
) -> McSnapshot:
    """子服的快照：从 hub 那份响应里切出自己那一行。"""
    entry = status.find(sub.source_name)
    if entry is None:
        # 找不到是**配置错**，不是「这台挂了」：接口里根本没有这个键。
        # 两种可能都要说出来，否则人会去查一台好着的子服为什么掉线。
        candidates = "、".join(status.names) or "一台都没有"
        return McSnapshot(
            target_id=sub.id,
            reachable=False,
            error=f"{hub.name} 的接口里没有叫 {sub.source_name!r} 的子服（接口里有："
            f"{candidates}）—— source_key 要填对方 velocity.toml 里的服名；"
            f"若名字没错，则是那台没在对方的服务端列表里注册",
            error_kind=SLP_UNPARSEABLE,
        )
    return _api_snapshot(sub, api_count=entry.online, names=list(entry.players), slp=slp)


@dataclass
class _HubResult:
    """一台 hub 的探测结果：它自己的快照 + 那份**整组共用**的接口响应。

    `status is None` = 接口这次没拿到（error 是原因）。子服必须区分这两种情况：
    接口没拿到时子服一个字都报不出来，得说清是「来源取不到」而不是「你挂了」。
    """

    snapshot: McSnapshot
    status: BridgeStatus | None
    error: BaseException | None


async def _fetch_api_group(hub: ServerTarget) -> _HubResult:
    """探一台 hub：接口响应（整组共用）+ 它自己的快照。**不抛异常。**

    SLP 复查与接口请求**并发**发出：两者互不依赖，串起来只会让一轮变慢
    （round_budget 的耗时上界就是按「同一轮里并发」算的）。
    """
    slp_task = (
        asyncio.ensure_future(_slp_probe(hub)) if hub.host else None
    )
    try:
        status = await mcbridge.fetch_status(hub.api, hub.timeout)
    except BridgeError as exc:
        if slp_task is not None:
            slp_task.cancel()
            with contextlib.suppress(BaseException):
                await slp_task
        # 接口没了不等于代理没了：SLP 还答得出人数就照常报。代理是群组的门面，
        # 把它报成「不可达」会让群里以为整个群组挂了，而玩家其实玩得好好的。
        # 名单本来也不从它取，所以这条降级损失的只是「分服那一份」。
        snap = await fetch_snapshot(hub)
        if snap.reachable:
            # 这条降级只剩 SLP，也就看不到 servers[] —— 中转服那几个人这一次扣不掉，
            # 报出去的是「连着代理的有几个」。不写出来的话它只表现为人数比平时大
            # 一两个，而那是谁也看不出来的（代理那一行本来就没有第二个数字可对照）。
            tail = "；这次看不到子服列表，中转服上的人没能扣掉" if hub.transit else ""
            snap.error = f"接口取数失败（{exc}）{tail}"
        return _HubResult(snapshot=snap, status=None, error=exc)
    slp = await slp_task if slp_task is not None else None
    return _HubResult(
        snapshot=_api_hub_snapshot(hub, status, slp),
        status=status,
        error=None,
    )


async def _fetch_api_one(target: ServerTarget, groups: dict[str, asyncio.Task]) -> McSnapshot:
    """取一个接口型目标（或它的子服）的快照。`groups` 是 hub.id → 已在飞的任务。"""
    if target.is_api:
        return (await groups[target.id]).snapshot

    hub = target.hub
    if hub is None:
        # 正常到不了：_check_sources 在解析期就会把 hub 回填好（配了 source 却没有
        # hub 的目标根本加载不出来）。真到了说明目标不是解析出来的（手工构造），
        # 这时**必须报错而不是悄悄去问一个猜出来的地址**。
        return McSnapshot(
            target_id=target.id,
            reachable=False,
            error=f"{target.name} 配了 source = {target.source!r}，但没有解析出对应的 hub",
            error_kind=SLP_UNPARSEABLE,
        )

    # 子服自己不发取数请求，但**填了 host 就能顺手做一次 SLP 复查** —— 和 hub 同一条
    # 理由（人数是接口给的，只有 SLP 能独立验一次）。和 hub 的任务并发等。
    slp_task = asyncio.ensure_future(_slp_probe(target)) if target.host else None
    result = await groups[hub.id]
    slp = await slp_task if slp_task is not None else None

    if result.status is None:
        return McSnapshot(
            target_id=target.id,
            reachable=False,
            error=f"数据来源 {hub.name} 的接口这次没取到：{result.error}",
            error_kind=_bridge_kind(result.error),
        )
    return _api_sub_snapshot(target, hub, result.status, slp)


async def _safe_api_fetch(target: ServerTarget, groups: dict[str, asyncio.Task]) -> McSnapshot:
    """给 _fetch_api_one 兜底：接口这条路也不能让异常逃出去。

    上层（client.get_snapshots）的契约是「fetch_snapshots 必然每个目标都返回一份快照，
    不会漏目标」——一个目标漏掉会让汇总悄悄少一个服。所以未知异常也翻译成失败快照。
    """
    try:
        return await _fetch_api_one(target, groups)
    except Exception as exc:
        return McSnapshot(
            target_id=target.id,
            reachable=False,
            error=f"探测 {target.name} 时内部出错：{type(exc).__name__}: {exc}",
            error_kind=SLP_UNPARSEABLE,
        )


async def fetch_snapshots(targets: Sequence[ServerTarget]) -> list[McSnapshot]:
    """并发探测多个目标，返回与 targets **同序**的快照列表。

    并发而非逐个：一轮的耗时取 max 而不是求和，加子服不会让轮询变慢
    （tools/mc_check.py 的「一轮耗时预算」就是按这个前提算的）。

    **同源合并**：接口型目标（hub）和它的子服共用**一份** `/status` 响应 ——
    按 hub 归堆，每台 hub 只发一次请求，子服从那响应里切自己那一行。子服自己
    **不发任何请求**（它们多半根本没有对外端口）。所以这里先把每台 hub 的任务
    起起来，子服 await 的是那个已在飞的任务，而不是各查一次。

    每台 hub 的任务都在本批**目标自己**的 hub 里找，不需要拿着一份全量表查表：
    子服身上带着 hub 对象（ServerTarget.hub）。于是「只探测 bingo 一个」也能正常
    工作 —— 它会自己去问 szu 的接口。这一点是必要的：缓存是按目标过期的，完全
    可能只有子服过期而 hub 还在缓存里（见 client.get_snapshots）。

    所有失败都体现在返回值里，不抛异常 —— 一台挂掉不影响其余目标的名单。
    """
    if not targets:
        return []
    groups: dict[str, ServerTarget] = {}
    for target in targets:
        if target.is_api:
            groups.setdefault(target.id, target)
        elif target.source and target.hub is not None:
            groups.setdefault(target.hub.id, target.hub)
    # 先起任务再 gather：子服要 await 的是「已经在飞的那一次请求」，
    # 若把 fetch 写成普通协程，子服 await 它时才会开始跑，而**每个子服各 await
    # 一次同一个协程对象**是未定义行为（第二个会拿到 None 或直接报错）。
    tasks = {
        hub_id: asyncio.ensure_future(_fetch_api_group(hub))
        for hub_id, hub in groups.items()
    }
    try:
        return list(
            await asyncio.gather(
                *(
                    _safe_api_fetch(t, tasks)
                    if (t.is_api or (t.source and t.hub is not None))
                    else fetch_snapshot(t)
                    for t in targets
                )
            )
        )
    finally:
        # gather 正常返回时它们都已经结束（cancel 是空操作）；异常路径下才真取消，
        # 免得留下还在飞的请求。
        for task in tasks.values():
            task.cancel()
