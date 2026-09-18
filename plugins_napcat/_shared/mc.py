"""Minecraft 服务器取数层（不依赖 NoneBot；除了缓存一条 RCON 长连接外无状态）。

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
import os
import random
import re
import struct
from dataclasses import dataclass, field
from functools import lru_cache

from mcstatus import JavaServer

# ---------------------------------------------------------------- 配置

@dataclass(frozen=True)
class McConfig:
    host: str
    port: int
    name: str
    timeout: float
    rcon_port: int
    rcon_password: str
    rcon_timeout: float

    @property
    def rcon_enabled(self) -> bool:
        """没填密码就完全不尝试 RCON，免得每轮都发一次注定失败的认证。"""
        return bool(self.rcon_password)


@lru_cache(maxsize=1)
def _cfg() -> McConfig:
    """惰性读环境变量。

    刻意不在 import 期读：tools/mc_check.py 得先把 .env 灌进 os.environ 再
    调用，import 期读会踩时序坑。改完环境变量可调 _cfg.cache_clear() 重置。
    """
    return McConfig(
        host=os.environ.get("MC_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=int(os.environ.get("MC_PORT", "25565")),
        name=os.environ.get("MC_NAME", "Minecraft 服务器").strip() or "Minecraft 服务器",
        timeout=float(os.environ.get("MC_TIMEOUT", "5")),
        rcon_port=int(os.environ.get("MC_RCON_PORT", "25575")),
        rcon_password=os.environ.get("MC_RCON_PASSWORD", "").strip(),
        rcon_timeout=float(os.environ.get("MC_RCON_TIMEOUT", "5")),
    )


# ---------------------------------------------------------------- 快照

@dataclass
class McSnapshot:
    """一次探测的结果。

    reachable 是「服务器是否响应」，不要用 count > 0 代替——「在线但 0 人」和
    「连不上」在文案上必须是两种，混用会导致服务器挂掉时报「当前 0 人在线」。
    """

    reachable: bool
    count: int = 0
    max_players: int | None = None
    names: list[str] = field(default_factory=list)
    names_complete: bool = False
    names_source: str = "none"  # "rcon" | "slp-sample" | "none"
    latency: float | None = None
    version: str = ""
    error: str = ""  # 取数降级/失败的原因，用于日志与 mc_check 输出


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
_conn: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
_cmd_lock: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    """命令串行锁：RCON 是单条有序流，并发写会把两条命令的响应串在一起。"""
    global _cmd_lock
    if _cmd_lock is None:
        _cmd_lock = asyncio.Lock()
    return _cmd_lock


async def _discard(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def _close() -> None:
    """丢掉缓存的连接（幂等）。"""
    global _conn
    conn, _conn = _conn, None
    if conn is not None:
        await _discard(conn[1])


async def _open() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """建连 + 认证。失败时抛 RconConnectError / RconAuthError，并关掉半开的连接。

    连接与认证各自带 timeout：主机被防火墙丢包（而不是回 RST）时 TCP 握手会一直
    挂着，没超时就把整轮探测拖死；而裸的 TimeoutError 消息为空，报出来是
    「TimeoutError: 」，看不出是哪一步、也看不出超时值。这里翻译成能读的文案。
    """
    cfg = _cfg()
    try:
        async with asyncio.timeout(cfg.rcon_timeout):
            reader, writer = await asyncio.open_connection(cfg.host, cfg.rcon_port)
    except asyncio.TimeoutError as exc:
        raise RconConnectError(
            f"连接 RCON {cfg.host}:{cfg.rcon_port} 超时（{cfg.rcon_timeout:g}s，"
            f"服务端在跑吗 / 端口对不对？）"
        ) from exc
    except OSError as exc:
        raise RconConnectError(
            f"连接 RCON {cfg.host}:{cfg.rcon_port} 失败：{exc}"
        ) from exc

    try:
        async with asyncio.timeout(cfg.rcon_timeout):
            await _authenticate(reader, writer, cfg.rcon_password)
    except asyncio.TimeoutError as exc:
        await _discard(writer)
        raise RconConnectError(
            f"RCON 认证超时（{cfg.rcon_timeout:g}s，该端口可能不是 RCON 服务）"
        ) from exc
    except BaseException:
        await _discard(writer)
        raise
    return reader, writer


async def _run(conn: tuple[asyncio.StreamReader, asyncio.StreamWriter], command: str) -> str:
    async with asyncio.timeout(_cfg().rcon_timeout):
        return await _execute(*conn, command)


async def rcon_command(command: str) -> str:
    """执行一条 RCON 命令，返回输出文本。连接跨调用复用。

    服务端重启、或它自己回收了闲置连接时，缓存的那条会失效。此时丢弃重连一次
    再重试，调用方无感。但**全新连接都失败就不重试**——那是真故障（没开、端口
    不对、密码错），重试只会让每轮探测多耗一个 rcon_timeout。

    超时算在「连接失效」里：复用的连接被服务端半开丢弃时，写可能成功而读一直
    没有响应，表现为超时而非 EOF。全新连接超时则不重试（`not reused` 挡住）。
    """
    global _conn
    async with _lock():
        reused = _conn is not None
        if not reused:
            _conn = await _open()
        try:
            return await _run(_conn, command)
        except RconAuthError:
            await _close()
            raise  # 密码错，重连也还是错
        # asyncio.TimeoutError 在 3.11+ 与内置 TimeoutError（OSError 子类）同一
        # 个类，3.10 下却是独立的，两个都列上才跨版本都对
        except (RconError, OSError, EOFError, asyncio.TimeoutError):
            # IncompleteReadError 是 EOFError 子类：对端关了连接
            await _close()
            if not reused:
                raise
            _conn = await _open()  # 复用的连接废了，重连一次
            return await _run(_conn, command)


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

async def fetch_snapshot() -> McSnapshot:
    """探测一次服务器，返回快照。任何失败都体现在返回值里，不抛异常。"""
    cfg = _cfg()

    # 1) SLP：在线状态与人数的唯一来源
    status = None
    slp_error = ""
    try:
        # 不用 lookup()/async_lookup()：那会走 dnspython 的 SRV 查询，对
        # 127.0.0.1 纯属浪费，还引入「链式写法要 await 两次」的坑。
        # tries 默认是 3，显式传 1 —— 消抖交给上层的失败计数器，语义更清楚，
        # 也避免 20 秒轮询被重试拖到节奏漂移。
        server = JavaServer(cfg.host, cfg.port, timeout=cfg.timeout)
        status = await server.async_status(tries=1)
    except Exception as exc:
        slp_error = f"{type(exc).__name__}: {exc}"

    if status is None:
        # SLP 不通就直接判不可达：即便 RCON 还能应答，也不把它当作在线信号，
        # 因为 SLP 是基线锚点（拿不到人数就没法校验名单完整性）。
        return McSnapshot(reachable=False, error=f"SLP 不可达（{slp_error}）")

    count = int(status.players.online or 0)
    snap = McSnapshot(
        reachable=True,
        count=count,
        max_players=status.players.max,
        latency=getattr(status, "latency", None),
        version=getattr(getattr(status, "version", None), "name", "") or "",
    )

    # 2) RCON 取完整名单
    rcon_error = ""
    if cfg.rcon_enabled:
        try:
            # 传 expected=count 消解冒号切分位置的歧义（见 parse_list_names）
            snap.names = parse_list_names(await rcon_command("list"), expected=count)
            # 命令成功就算 rcon 来源，即便结果是空列表（0 人在线时本就为空）
            snap.names_source = "rcon"
        except RconAuthError as exc:
            rcon_error = str(exc)
        except RconError as exc:
            rcon_error = str(exc)
        except Exception as exc:
            rcon_error = f"{type(exc).__name__}: {exc}"
    else:
        rcon_error = "未配置 MC_RCON_PASSWORD"

    # 3) RCON 没给出完整名单时，退一步看 SLP 的 sample。
    #    sample 顺序随机、上限约 12 条，但「条数 == 在线人数」时它就是完整名单
    #    （≤12 人的私服很常见）。有这条降级路径，没开 RCON 的服也能用进服提醒。
    if len(snap.names) != count:
        sample = [p.name for p in (status.players.sample or []) if getattr(p, "name", "")]
        if len(sample) == count:
            snap.names = sample
            snap.names_source = "slp-sample"
        else:
            snap.names = []
            snap.names_source = "none"

    snap.names_complete = len(snap.names) == count
    if not snap.names_complete:
        reason = rcon_error or f"SLP sample 只有 {len(status.players.sample or [])} 条"
        snap.error = f"名单不完整（人数 {count}，拿到 {len(snap.names)}）：{reason}"
    elif rcon_error:
        snap.error = f"已降级用 SLP sample 取名单：{rcon_error}"

    return snap
