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
    names_source: str = "none"  # "rcon" | "slp-sample" | "none"
    latency: float | None = None
    version: str = ""
    error: str = ""  # 取数降级/失败的原因，用于日志与 mc_check 输出
    # RCON `list` 的**原始输出**。各服务端/语言的文案都不一样，解析结果对不上时
    # 唯一能看的就是它，所以由这里存下来给诊断工具打，而**不是**让调用方再发一次。
    # 两次 list 之间玩家可能进出，重发会让「打出来的原文」和「解析用的原文」
    # 不是同一份 —— 排查时最需要对齐的恰恰是这两者。
    raw_list: str = ""
    # 失败的**性质**，取值见 SLP_UNREACHABLE / SLP_UNPARSEABLE / ""（没失败）。
    # 文案层必须按它分支：两者给用户的下一步动作完全相反 ——
    # 「连不上」去查网络和端口，「应答无法解析」去查服务端是不是还在启动。
    # 光看 reachable 分不出来（两者都是 False），所以这个字段不能靠 error 文案反推。
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
    """关掉所有目标的连接。进程收尾用。"""
    for target_id in list(_conns):
        await _close(target_id)


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
            reader, writer = await asyncio.open_connection(target.host, spec.port)
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


def parse_whitelist_names(payload: str) -> list[str] | None:
    """从 RCON `whitelist list` 的输出里取全部白名单玩家名。

    返回三态，调用方必须区分开：
      · 列表（可以是空的）—— 解析成功；空列表 = 确实没人在白名单里
      · None             —— 判不出来（输出为空，或没有冒号说明格式被改写过）

    和 parse_list_names 同一条原则：不解析本地化前缀文案。先在**第一个冒号**处切掉
    前缀（与该函数拿不到期望人数时的兜底路径一致），再按逗号拆、去 § 颜色码、丢空段。

    不能图省事直接按逗号切整段：`There are 2 whitelisted players: Alice, Bob` 的第一段
    会连着前缀文案一起变成 `There are 2 whitelisted players: Alice`，跟 `Alice` 比不上。
    """
    text = (payload or "").strip()
    if not text:
        return None
    cut = next((i for i, ch in enumerate(text) if ch in ":："), None)
    if cut is None:
        return None  # 没有冒号：可能是 EssentialsX 之类改写过的格式，不猜
    return _split_names(text[cut + 1 :])


# ---------------------------------------------------------------- 取数

async def fetch_snapshot(target: ServerTarget) -> McSnapshot:
    """探测一个目标，返回快照。任何失败都体现在返回值里，不抛异常。"""
    # 1) SLP：在线状态与人数的唯一来源
    status = None
    slp_error = ""
    slp_kind = SLP_UNREACHABLE
    try:
        # 不用 lookup()/async_lookup()：那会走 dnspython 的 SRV 查询，对
        # 127.0.0.1 纯属浪费，还引入「链式写法要 await 两次」的坑。
        # tries 默认是 3，显式传 1 —— 消抖交给上层的失败计数器，语义更清楚，
        # 也避免 20 秒轮询被重试拖到节奏漂移。
        server = JavaServer(target.host, target.port, timeout=target.timeout)
        status = await server.async_status(tries=1)
    except Exception as exc:
        slp_error = _describe_slp_failure(exc)
        # 连不上 vs 答了但答不对，两种失败的排查方向相反，别混成一句话。
        if not isinstance(exc, _CONN_ERRORS):
            slp_kind = SLP_UNPARSEABLE

    if status is None:
        # SLP 不通就直接判不可达：即便 RCON 还能应答，也不把它当作在线信号，
        # 因为 SLP 是基线锚点（拿不到人数就没法校验名单完整性）。
        label = "SLP 连不上" if slp_kind == SLP_UNREACHABLE else "SLP 应答无法解析"
        return McSnapshot(
            target_id=target.id,
            reachable=False,
            error=f"{label}（{slp_error}）",
            error_kind=slp_kind,
        )

    count = int(status.players.online or 0)
    snap = McSnapshot(
        target_id=target.id,
        reachable=True,
        count=count,
        max_players=status.players.max,
        latency=getattr(status, "latency", None),
        version=getattr(getattr(status, "version", None), "name", "") or "",
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
        sample = [p.name for p in (status.players.sample or []) if getattr(p, "name", "")]
        if len(sample) == count:
            snap.names = sample
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
        reason = rcon_error or f"SLP sample 只有 {len(status.players.sample or [])} 条"
        snap.error = f"名单不完整（人数 {count}，拿到 {len(snap.names)}）：{reason}"
    elif rcon_error:
        snap.error = f"已降级用 SLP sample 取名单：{rcon_error}"

    return snap


async def fetch_snapshots(targets: Sequence[ServerTarget]) -> list[McSnapshot]:
    """并发探测多个目标，返回与 targets **同序**的快照列表。

    并发而非逐个：一轮的耗时取 max 而不是求和，加子服不会让轮询变慢
    （tools/mc_check.py 的「一轮耗时预算」就是按这个前提算的）。

    fetch_snapshot 自己吞掉所有异常并体现在返回值里，所以 gather 不会因单个目标
    失败而整体抛错 —— 一台挂掉不影响其余目标的名单。
    """
    if not targets:
        return []
    return list(await asyncio.gather(*(fetch_snapshot(t) for t in targets)))
