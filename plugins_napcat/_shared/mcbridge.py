"""群组服 HTTP 接口客户端（Velocity 那侧的 bridge 插件，2026-09-22 对接的 SZUcraft）。

**这是第三条取数通道**，和 SLP、RCON 并列：

    SLP      在线状态与人数（谁都能答，但只有人数、没有名字的权威性）
    RCON     玩家的完整名单 + 白名单增删（命令通道，一个目标一个端点）
    HTTP     群组服**整组**的人数与分服名单 + 群组级白名单增删

它存在的唯一理由：**群组的权威数据在代理手里**，而代理那台通常只有一个内网 HTTP
口 —— 没有 SLP（子服不对外）、没有 RCON（代理的 RCON 只能改白名单、没有 `list`）。
没有这条通道，群组那 4 台子服就全是「只剩人数」。

和另外两条通道的**根本区别**：一次请求给出多台服的数据。所以「谁去请求」不归目标
自己管 —— 子服（`source` 指向某台 hub）不发任何请求，它们的数据从 hub 那一份响应里
切出来（见 mc.fetch_snapshots 的同源合并）。

**形状全部来自对方文档，且是双向校验过的**：
    GET  /status    → {"proxy":{"online":N},"servers":[{"name","online","players"}]}
    GET  /whitelist → {"enabled":bool,"entries":[...]}
    POST /whitelist/add | /whitelist/remove   （body: name=<玩家名>）
    GET  /health    → {"ok":true}（免鉴权）

字段名已经漂过一次：白名单那份从 `players` 改成了 `entries`（对方采纳了我们的建议，
因为 `players` 在 /status 里是「在线的人」、在白名单里是「允许进的人」，同名不同义）。
所以我们**两个都认**，但文档里只写 `entries` —— 现场那台可能还是旧版。

失败分三类，**因为三类的下一步动作完全不同**（和 SLP 那边分 unreachable/unparseable
是同一条原则）：
    BridgeUnreachable  连不上/超时 → 查网络、隧道、对方的服务还在不在
    BridgeAuthError    401         → token 不对或被轮换了，找对方要新的
    BridgeDataError    连上了但内容不对 → 对方的插件版本/路径不对，或者我们解析过时了
"""

from dataclasses import dataclass

import httpx

from .mcservers import ApiSpec

# 接口的四条绝对路径。**不参与 url 拼接的猜测**：mcs_servers.toml 里的 api.url 只填到
# 端口（解析期就拦住带路径的写法），路径一律由这里给死。
_PATH_STATUS = "/status"
_PATH_WHITELIST = "/whitelist"
_PATH_HEALTH = "/health"

# 回显响应正文时截断，免得一句报错把整个 HTML 错误页塞进日志/群里
_BODY_SNIPPET = 200


class BridgeError(Exception):
    """接口调用的失败基类。文案面向**运维**（会进日志，也可能进群）。"""


class BridgeUnreachable(BridgeError):
    """连不上：拒绝连接、超时、DNS、中途断开。"""


class BridgeAuthError(BridgeError):
    """鉴权失败（401）。**单独一类**：它的下一步动作是「去要新 token」，不是查网络。"""


class BridgeDataError(BridgeError):
    """连上了、也答了，但内容不是我们要的形状（非 JSON / 缺字段 / 类型不对）。"""


@dataclass
class BridgeServer:
    """接口里的一台子服。"""

    name: str
    online: int
    players: tuple[str, ...]


@dataclass
class BridgeStatus:
    """一次 /status 的结果。"""

    proxy_online: int
    servers: tuple[BridgeServer, ...]

    def find(self, name: str) -> BridgeServer | None:
        """按 `servers[].name` 找一台子服。找不到返回 None。

        **精确匹配，不猜大小写**：群组服名是对方 velocity.toml 里的键，写在我们配置的
        `source_key` 里，两边都是人手写的字面量。做大小写宽松匹配就会在「两台只差大小写」
        时悄悄选一个，而那种配置本来就该报错。找不到时的候选清单由调用方给（见 mc.py），
        比在这里猜有用。
        """
        for server in self.servers:
            if server.name == name:
                return server
        return None

    @property
    def names(self) -> tuple[str, ...]:
        """全部子服名，报错时当候选清单用。"""
        return tuple(s.name for s in self.servers)


@dataclass
class BridgeWhitelist:
    """一次白名单读取的结果。

    `enabled=False` 意味着**代理层的白名单拦截被关掉了**（对方配置里的
    `whitelist-enabled=false`）—— 接口照常工作、名单照常增删，但**谁都能进服**。
    这是「配了却不生效」那一类，调用方必须把它说出来，不能只报「已添加」。
    """

    enabled: bool
    entries: tuple[str, ...]


@dataclass
class BridgeWriteResult:
    """一次白名单增删的回执。

    ⚠️ **`ok` 在不同版本里含义不同**：老版把「已经在了」的幂等 add 报成 `ok=false`，
    新版报 `ok=true` + `changed=false`。所以**判断成败一律以重新读一遍名单为准**
    （mcadmin 的「先读后写 + 反查」本来就是这么做的），这个结果只用来补充说明。
    """

    ok: bool
    changed: bool | None
    whitelist: BridgeWhitelist | None


# ---------------------------------------------------------------- 连接管理

# 一个进程一条连接池。**不按目标分**：接口是给整组用的，同一时刻只会有少数几条
# 连接，HTTP/1.1 的 keep-alive 由 httpx 自己管。
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            # 超时一律逐请求传（每个目标有自己的 timeout），这里只兜底
            timeout=10.0,
            # 跟着对方文档走：机器人容器加入 mcnet 后直连，不涉及任何代理；
            # 但机器上真开了 HTTP_PROXY 时不该把内网请求绕出去
            trust_env=False,
        )
    return _client


async def close_all() -> None:
    """关掉连接池。进程收尾用（和 mc.close_all 一起调）。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _headers(api: ApiSpec) -> dict[str, str]:
    # 用请求头而不是 ?token=：query 会进对方的访问日志，而且更容易被顺手贴进聊天记录
    return {"Authorization": f"Bearer {api.token}"}


def _snippet(text: str) -> str:
    flat = " ".join((text or "").split())
    return flat[:_BODY_SNIPPET] + ("…" if len(flat) > _BODY_SNIPPET else "")


async def _request(
    api: ApiSpec, timeout: float, method: str, path: str, **kwargs: object
) -> dict:
    """发一次请求并返回解析好的 JSON 字典。所有失败都翻译成 BridgeError 子类。"""
    url = api.url.rstrip("/") + path
    try:
        response = await _get_client().request(
            method, url, timeout=timeout, headers=_headers(api), **kwargs
        )
    except httpx.TimeoutException as exc:
        raise BridgeUnreachable(
            f"请求 {url} 超时（{timeout:g}s，对方的 bridge 还在吗 / 隧道通吗？）"
        ) from exc
    except httpx.HTTPError as exc:
        # 连不上、DNS、连接被重置…… httpx 的异常文案已经带上了主机名，够用
        raise BridgeUnreachable(f"请求 {url} 失败：{type(exc).__name__}: {exc}") from exc

    if response.status_code == 401:
        raise BridgeAuthError(
            "接口拒绝了这个 token（401）—— token 可能被对方轮换了，去要一份新的"
            "（对方换之前会先通知，见 mcs_servers.toml.example 的 api 一节）"
        )
    if response.status_code == 404:
        raise BridgeDataError(
            f"接口没有 {path} 这个路径（404）—— 对方的 bridge 插件版本对吗？"
        )
    if response.status_code >= 400:
        raise BridgeDataError(
            f"接口返回 {response.status_code}：{_snippet(response.text)}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise BridgeDataError(
            f"接口返回的不是 JSON（Content-Type={response.headers.get('content-type')!r}）："
            f"{_snippet(response.text)}"
        ) from exc
    if not isinstance(payload, dict):
        raise BridgeDataError(f"接口返回的 JSON 顶层不是对象，而是 {type(payload).__name__}")
    return payload


# ---------------------------------------------------------------- 字段校验

def _int_field(table: dict, key: str, where: str) -> int:
    value = table.get(key)
    # bool 是 int 的子类，true 会被当成 1 —— 先挡掉
    if isinstance(value, bool) or not isinstance(value, int):
        raise BridgeDataError(f"接口的 {where}.{key} 不是整数，而是 {value!r}")
    return value


def _str_field(table: dict, key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str):
        raise BridgeDataError(f"接口的 {where}.{key} 不是字符串，而是 {value!r}")
    return value


def _name_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BridgeDataError(f"接口的 {where} 不是数组，而是 {type(value).__name__}")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise BridgeDataError(f"接口的 {where} 里有非字符串的条目：{item!r}")
        names.append(item)
    return tuple(names)


def parse_status(payload: dict) -> BridgeStatus:
    """把 /status 的响应解成 BridgeStatus。**形状不对就抛 BridgeDataError**。

    刻意严格（而不是「缺字段就当 0」）：这里的每一个字段都直接变成群里报出去的人数，
    静默取 0 会表现成「服务器上没人」，那是**假消息**。宁可报「接口数据看不懂」。
    """
    proxy = payload.get("proxy")
    if not isinstance(proxy, dict):
        raise BridgeDataError(f"接口的 /status 里 proxy 不是对象，而是 {proxy!r}")
    servers_raw = payload.get("servers")
    if not isinstance(servers_raw, list):
        raise BridgeDataError(
            f"接口的 /status 里 servers 不是数组，而是 {servers_raw!r}"
        )

    servers: list[BridgeServer] = []
    for index, raw in enumerate(servers_raw):
        where = f"servers[{index}]"
        if not isinstance(raw, dict):
            raise BridgeDataError(f"接口的 {where} 不是对象，而是 {raw!r}")
        # players 缺失按空数组处理：人数 > 0 而名单为空，会由调用方的
        # len(names) == count 判定如实报成「名单不完整」，不会误报成「没人」
        players = raw.get("players")
        servers.append(
            BridgeServer(
                name=_str_field(raw, "name", where),
                online=_int_field(raw, "online", where),
                players=_name_list([] if players is None else players, f"{where}.players"),
            )
        )
    return BridgeStatus(
        proxy_online=_int_field(proxy, "online", "proxy"),
        servers=tuple(servers),
    )


def parse_whitelist(payload: dict, where: str = "whitelist") -> BridgeWhitelist:
    """解白名单。响应里它可能内嵌在 `whitelist` 键下（增删的回执就是这样）。

    字段名叫 `entries` 或 `players` 都认 —— 名字漂过一次，见模块 docstring。
    """
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise BridgeDataError(f"接口的 {where}.enabled 不是 true/false，而是 {enabled!r}")
    raw = payload.get("entries", payload.get("players"))
    if raw is None:
        raise BridgeDataError(
            f"接口的 {where} 里既没有 entries 也没有 players —— 对方的插件改过字段名？"
        )
    return BridgeWhitelist(enabled=enabled, entries=_name_list(raw, f"{where}.entries"))


# ---------------------------------------------------------------- 四条接口

async def fetch_health(api: ApiSpec, timeout: float) -> bool:
    """探活。免鉴权，所以它通了**只能说明服务在**，说明不了 token 对不对。"""
    payload = await _request(api, timeout, "GET", _PATH_HEALTH)
    return bool(payload.get("ok"))


async def fetch_status(api: ApiSpec, timeout: float) -> BridgeStatus:
    """整组的在线情况（一次调用覆盖全部子服）。"""
    return parse_status(await _request(api, timeout, "GET", _PATH_STATUS))


async def fetch_whitelist(api: ApiSpec, timeout: float) -> BridgeWhitelist:
    """读群组白名单。"""
    return parse_whitelist(await _request(api, timeout, "GET", _PATH_WHITELIST))


async def write_whitelist(
    api: ApiSpec, timeout: float, verb: str, name: str
) -> BridgeWriteResult:
    """增删群组白名单。`verb` 只能是 add / remove。

    成败**不要看这里的 ok**（见 BridgeWriteResult 的说明）—— 调用方要重新读一遍名单。
    """
    if verb not in ("add", "remove"):
        raise ValueError(f"verb 只能是 add / remove，实际是 {verb!r}")
    payload = await _request(
        api, timeout, "POST", f"{_PATH_WHITELIST}/{verb}", data={"name": name}
    )
    ok = payload.get("ok")
    changed = payload.get("changed")
    nested = payload.get("whitelist")
    return BridgeWriteResult(
        ok=bool(ok),
        changed=changed if isinstance(changed, bool) else None,
        whitelist=parse_whitelist(nested) if isinstance(nested, dict) else None,
    )
