"""多服务器目标表：mcs_servers.toml 的解析、校验与名字解析。

**只管「有哪些服务器」这一件事。** 「哪个 QQ 群看哪几台」在 _shared/mcaudiences.py
（mcs_audiences.toml），它用 `ServerBook.scoped()` 把这里的全量表投影成一个子视图。
白名单归属（发给哪台、什么命令前缀）也随群关联走，不再是全局一份。

**为什么不塞进 mc.py**：mc.py 的单服假设（一个 host/port/rcon）要逐目标化，而
「有哪些目标、叫什么」这件事本身与 MC 协议无关。把配置层和协议层
分开，前者就能在没有任何网络的情况下完整自测（tools/mc_check.py 的自测块 10/11）。

和 mc.py 一样放 _shared：本模块**不得** import nonebot、不得调 get_driver()，
否则 tools/mc_check.py 在 nonebot.init() 之前 import 会抛 ValueError。

三条设计原则：

1. **能加载的就让它加载，问题记成警告**。两个目标抢同一个端口是运行期事实
   （那两台不能同时起），不是配置语法错误；硬报错会让整个 bot 起不来，而它
   本可以在「哪台在跑就监控哪台」的模式下正常工作。警告由 --list-targets 显眼打出。
2. **解析不出唯一目标时绝不猜**。返回候选列表交给调用方回一条「你是指哪个」，
   而不是随便挑一个——在管理命令上猜错的后果是改错服务器。
3. **拼错的键要报错，不能静默忽略**。TOML 没有 schema，`rcon_password` 写成
   `rcon.password` 之外的形态会静默变成「没配 RCON」，然后表现为「名单一直不完整」，
   极难查。所以每个表都校验未知键。
"""
import os
import tomllib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

# 目标角色。决定的不多，但有一条很关键：代理不出在线名单。
KIND_PROXY = "proxy"
KIND_BACKEND = "backend"
KIND_STANDALONE = "standalone"
KINDS = (KIND_PROXY, KIND_BACKEND, KIND_STANDALONE)

# 各表允许出现的键。多一个都报错——见模块 docstring 第 3 条。
# whitelist / defaults.primary 曾经在这里，2026-09-21 搬到 mcs_audiences.toml 的
# 每条 [[audience]] 里（它们要按群区分，不能再是全局一份）。下面两条迁移报错负责
# 把「还留着旧键」这件事说清楚 —— 不能只靠 _reject_unknown，那只会说「不认识的键」。
_TOP_KEYS = frozenset({"defaults", "targets"})
_DEFAULT_KEYS = frozenset({"timeout", "rcon_timeout"})
_TARGET_KEYS = frozenset(
    {
        "id",
        "name",
        "kind",
        "group",
        "host",
        "port",
        "rcon",
        "api",
        "source",
        "source_key",
        "aliases",
        "timeout",
        "rcon_timeout",
    }
)
_RCON_KEYS = frozenset({"host", "port", "password"})
_API_KEYS = frozenset({"url", "token"})

_MOVED_WHITELIST_KEYS = frozenset({"whitelist"})
_MOVED_DEFAULT_KEYS = frozenset({"primary"})

# 这两句会被**打给群里看**（mc_stats / mc_admin 的配置读不了分支会回显原文），
# 所以要短。详细说明在 mcs_audiences.toml.example 和 DEPLOY.md。
_MOVED_HINT = (
    "mcs_servers.toml 里的 {key} 已经搬到 mcs_audiences.toml 的每条 [[audience]] 里了"
    "（它要按 QQ 群区分，不能再是全局一份）。\n"
    "把 {write} 写进对应那条 [[audience]]，然后删掉这里的 {key}。\n"
    "群号也从 .env 的 MC_ALLOWED_GROUPS 搬进 [[audience]].groups；"
    "模板见 mcs_audiences.toml.example。"
)
_MOVED_WHITELIST_HINT = _MOVED_HINT.format(
    key="[whitelist] 段",
    write='whitelist = [{ target = "bingo", command = "whitelist" }]',
)
_MOVED_PRIMARY_HINT = _MOVED_HINT.format(
    key="[defaults].primary",
    write='primary = "gtnh"',
)

_DEFAULT_TIMEOUT = 5.0
_DEFAULT_RCON_TIMEOUT = 5.0
# 「群里打 whitelist，RCON 里发什么」的命令前缀。留在这里当缺省值：
# ServerBook.scoped() 要给投影出来的子视图填一个默认命令，而那个默认值天然是
# 「vanilla 的 whitelist」。真正生效的那份在每条 [[audience]] 的 whitelist.command。
_DEFAULT_COMMAND = "whitelist"


class ServerConfigError(Exception):
    """mcs_servers.toml / mcs_audiences.toml 不合法，且不能带着它继续跑。"""


@dataclass(frozen=True)
class RconSpec:
    """一个目标的 RCON 端点。密码为空 = 该目标不出名单（也不下发任何命令）。

    `host` 留空 = 跟目标自己的 `host` 相同（绝大多数情况）。需要单独填是因为
    SLP 和 RCON 可以不在一个地址上 —— 典型场景是 RCON 只绑在内网/回环，由隧道
    转出来，而游戏的 SLP 走的是公网端口。2026-09-22 对方那台 GTNH 就是这样：
    SLP 在 `203.0.113.10:30004`，RCON 在隧道这头的 `127.0.0.1:25575`。
    """
    port: int
    host: str = ""
    password: str = ""

    @property
    def enabled(self) -> bool:
        """没填密码就完全不尝试 RCON，免得每轮都发一次注定失败的认证。"""
        return bool(self.password)


@dataclass(frozen=True)
class ApiSpec:
    """一个目标的 HTTP 接口端点 —— 群组服那侧自建的 bridge 插件（见 DEPLOY.md）。

    存在的理由：**群组的权威数据在代理手里**，而代理那台通常只有一个内网 HTTP 口，
    没有 SLP、没有 RCON。SZUcraft 那套（2026-09-22 对接）给的是：

        GET  /status    → {"proxy":{"online":N},"servers":[{"name","online","players"}]}
        GET  /whitelist → {"enabled":bool,"count":N,"entries":[...]}
        POST /whitelist/add | /whitelist/remove  （表单 / JSON / query 三选一）
        GET  /health    → {"ok":true}（免鉴权，探活专用）

    它和 RCON 是**互斥的两条通道**：都配在一台目标上，就没人说得清实际走哪条 ——
    所以解析期直接报错（见 parse_book），不做「有 api 就优先」这种静默择优。

    token 必填（这里的默认值只是为了构造方便，`parse_book` 那侧是 allow_empty=False，
    空 token 在解析期就报错）。理由：没有 token 时每条请求都是 401，等于配了一个
    「永远取不到数据」的目标，与其留到运行时不如现在就拦下。url 里的路径部分不参与
    拼接，固定用上面那四条绝对路径 —— 免得出现「url 写到 /status、再拼一次」这类错。
    """
    url: str
    token: str = ""

    @property
    def enabled(self) -> bool:
        """**解析期已保证 token 非空**，所以真跑起来时这里恒为 True。

        留着是因为 `ServerTarget.is_api` 读它（那是个语义正确的转发），删掉得同时改
        调用方。写这段注释是为了拦住「照这个 if 反推『token 可以为空』」—— 加等于白加
        的那版行为（空 token = 没配）在 2026-09-22 已经废掉，权威是 parse_book。
        """
        return bool(self.token)


@dataclass(frozen=True)
class ServerTarget:
    id: str
    name: str
    kind: str
    group: str
    timeout: float
    rcon_timeout: float
    # host/port 有默认值是因为 **接口型目标（api 段）根本没有游戏端口**：它的数据
    # 全从那一个 HTTP 口来，游戏端口没发布到公网、机器人够不着。RCON 还留着默认值
    # 之前的位置不变（谁都在用），只是不能排在无默认值的字段前面。
    host: str = ""
    port: int = 0
    rcon: RconSpec | None = None
    api: ApiSpec | None = None
    # 数据不从自己这儿取，而是「哪台目标的接口里带我的数据」。值是另一条 [[targets]]
    # 的 id，且那台必须配了 api。群组子服（lobby/bingo/…）都指向代理那条。
    source: str = ""
    # 在上面那台的响应里，用哪个键取自己（/status 的 servers[].name）。不填 = 用 id。
    source_key: str = ""
    aliases: tuple[str, ...] = ()
    # `source` 解出来的**那台目标本身**，由 _check_sources 在解析期回填；自己没有
    # source 时是 None。存对象而不是每处都拿着 id 去查表，是为了让取数层能在**任意
    # 子集**里工作：只探测 bingo 一个目标时，也得知道去问 szu 的接口（收数的人可能
    # 只关联了子服，或者缓存里只有子服过期了）。查表要有一份全量表在手，而那是
    # 配置层的东西，取数层不该依赖它。
    # repr=False：它是另一条 ServerTarget，打进日志会把整张表套着打一遍。
    # compare=False：两个目标的相等性与它无关（同一个 id 就是一个目标）。
    hub: "ServerTarget | None" = field(default=None, repr=False, compare=False)

    @property
    def source_name(self) -> str:
        """在 hub 的响应里取自己那一份用的键。

        回退只写在这一处（和 rcon_host 同一个道理）：`--list-targets` 给人看的、
        真去查表用的，都读它。群组那边的服名和本地的 id **不必相同**，所以能单独填。
        """
        return self.source_key or self.id

    @property
    def is_api(self) -> bool:
        """数据与白名单都走 HTTP 接口。"""
        return self.api is not None and self.api.enabled

    @property
    def has_own_source(self) -> bool:
        """这台目标自己就能给出「谁在线」的名单（不算 SLP 样本那条降级路）。

        代理配了接口 → 能；子服挂了 source → 能（从 hub 那台拿）；配了 RCON → 能。
        """
        return self.is_api or bool(self.source) or self.rcon_enabled

    @property
    def game_addr(self) -> str:
        """SLP 地址。接口型目标没有这个，回 `-`（和 rcon_addr 同一套写法）。"""
        return f"{self.host}:{self.port}" if self.host else "-"

    @property
    def rcon_host(self) -> str:
        """RCON 连哪台主机。`rcon.host` 没填就用目标自己的 `host`（绝大多数目标如此）。

        单独存在是因为 SLP 和 RCON 可以不在一个地址上：RCON 只绑内网/回环、由隧道
        转出来，而游戏 SLP 走公网端口。回退逻辑只写在这一处，`rcon_addr`（给人看）
        和 `mc._open()`（真去连）都读它 —— 免得两处各有一套判断。
        """
        if self.rcon is not None and self.rcon.host:
            return self.rcon.host
        return self.host

    @property
    def rcon_addr(self) -> str:
        return f"{self.rcon_host}:{self.rcon.port}" if self.rcon else "-"

    @property
    def serves_names(self) -> bool:
        """代理恒为 False —— **配上接口也是 False**。

        这个属性问的不是「拿不拿得到名单」，而是「这份快照的名单该不该按**分服口径**
        进入计数与渲染」。代理的名单是**全群组**的（所有人，不管在哪台子服），把它当
        一台服来数，`_counting()` 就会把同一批人数两遍 —— 各子服的名单里本来就有他们。
        所以即便接口明明能给出全群组的名单，这里也**故意**不用它。

        顺带纠正一条旧结论：出不了名单的理由**不是**「Velocity 拿不到谁在哪台子服」——
        那是 BungeeCord 时代的印象（把「没有内置 glist 那条命令」当成了「拿不到」）。
        Velocity 插件能直接枚举每台后端的连接玩家。真正的限制只是 SLP + RCON 那条
        通道拿不到，而接口那条能拿到，只是我们按上面的理由不用它。

        代理的 RCON 也不参与（它只服务白名单，没有 `list`），所以这里从不看 rcon_enabled。
        """
        return self.kind != KIND_PROXY

    @property
    def rcon_enabled(self) -> bool:
        return self.rcon is not None and self.rcon.enabled

    @property
    def whitelist_ready(self) -> bool:
        """这台目标能不能做白名单操作：RCON 或 HTTP 接口，通一条就行。

        和 rcon_enabled 分开是因为**问的问题不一样**。调用方真正想知道的是「能不能改
        这台服的白名单」，用 rcon_enabled 代替它，接口型目标就会被当成「没配密码」拒掉，
        群里表现为「本群没配白名单服」，而配置里明明写着 —— 又一处「配了却不生效」。
        """
        return self.rcon_enabled or self.is_api

    @property
    def transport(self) -> str:
        """靠什么取在线名单，给诊断打印用（describe / 探测失败时的排查）。

        顺序即优先级：接口 > 挂在接口上的子服 > RCON > SLP（只剩人数）。
        和谁真正生效保持一致（见 parse_book 对 api + rcon 并存的报错）。
        """
        if self.is_api:
            return "接口"
        if self.source:
            return f"接口（{self.source} 的子服）"
        if self.rcon_enabled:
            return "RCON"
        return "SLP"

    @property
    def keys(self) -> tuple[str, ...]:
        """所有能用来指代这个目标的名字（含别名）。"""
        return (self.id, self.name, *self.aliases)


@dataclass(frozen=True)
class Resolution:
    """一次名字解析的结果，三态：命中 / 歧义 / 未知。

    刻意不返回 `ServerTarget | None`：调用方需要区分「没有这个服」和「你是指哪个」，
    这两种提示对用户的下一步动作完全不同。而两种都必须有回复——触发词一旦被认领，
    就不能静默吞掉。
    """

    query: str
    target: ServerTarget | None = None
    candidates: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.target is not None

    @property
    def ambiguous(self) -> bool:
        return self.target is None and bool(self.candidates)


@dataclass(frozen=True)
class WhitelistRoute:
    """白名单的一条路由：命令发给**哪台**服、RCON 里发**什么前缀**。

    一条群关联可以有 N 条（P6）。`target` 一定是**本视图**里的目标：由 scoped() 校验，
    所以关联外的服既不会出现在候选里，也 pick 不到 —— 隔离靠投影，不靠调用方自觉
    （与 ServerBook.scoped 里 `_by_id` 必须重建是同一条原则）。

    前缀随路由走而不是随关联走：代理上线后子服用 `whitelist`、代理用 `globalwhitelist`，
    同一个群里两台的前缀就是不一样的。
    """

    target: ServerTarget
    command: str = _DEFAULT_COMMAND

    # 转发属性：调用方原先读 owner.name / owner.id / owner.rcon_enabled / owner.rcon_addr，
    # 转发之后那些行不用逐字改，「一个 → 一组」的 diff 才看得清。
    @property
    def name(self) -> str:
        return self.target.name

    @property
    def id(self) -> str:
        return self.target.id

    @property
    def rcon_enabled(self) -> bool:
        return self.target.rcon_enabled

    @property
    def whitelist_ready(self) -> bool:
        """能不能改这台的名单（RCON 或接口，通一条就行）。判定见 ServerTarget 同名属性。"""
        return self.target.whitelist_ready

    @property
    def channel(self) -> str:
        """白名单走哪条通道，给用户文案用。只有 "接口" 和 "RCON" 两种可能。

        与 `transport` 分开：那个是**诊断**用的（会把「接口（szu 的子服）」这种来源
        写全），而这里进群文案，要的是最短的那个通道名。
        """
        return "接口" if self.target.is_api else "RCON"

    @property
    def rcon_addr(self) -> str:
        return self.target.rcon_addr


# pick_whitelist 的结果码。六态互斥，且**每一态群里都必须有话可说** —— 触发词一旦被
# 认领，静默比报错更坏（见 mcs/mc_admin.py 的模块 docstring）。
PICK_OK = "ok"  # 命中一条路由
PICK_NO_ROUTE = "no_route"  # 本群压根没配白名单服
PICK_NEED_NAME = "need_name"  # 有好几台，命令里必须点名
PICK_UNKNOWN = "unknown"  # 认不出这个服名
PICK_AMBIGUOUS = "ambiguous"  # 服名有歧义
PICK_NOT_WHITELIST = "not_whitelist"  # 认得出这台服，但它不是白名单服


@dataclass(frozen=True)
class WhitelistPick:
    """一次「这条命令发给哪台」的解析结果。"""

    reason: str
    query: str = ""
    route: WhitelistRoute | None = None
    # 歧义候选 / 本群可选的服名清单 / 本群全部白名单服名，三处共用（都是给人看的服名）
    candidates: tuple[str, ...] = ()
    # PICK_NOT_WHITELIST 专用：那台确实存在、只是不在白名单路由里。有这个字段才可能
    # 说出「GTNH 是本群的服，但本群的白名单服是 Bingo」——否则只能骗人说「没这台服」。
    found: ServerTarget | None = None

    @property
    def ok(self) -> bool:
        return self.route is not None


@dataclass(frozen=True)
class ServerBook:
    """一份加载完的目标表。"""

    targets: tuple[ServerTarget, ...] = ()
    # 下面两项只在**投影出来的子视图**里有意义（见 scoped）。全量表里它们恒为空：
    # 「白名单发给谁」和「本群默认看哪台」都是群关联的属性，不是服务器清单的。
    # whitelist_routes 的顺序就是配置里的书写顺序（诊断打印按它排）。
    whitelist_routes: tuple[WhitelistRoute, ...] = ()
    # 「主服」的 id。留空 = 用 targets[0]。
    primary: str = ""
    warnings: tuple[str, ...] = ()
    _by_id: dict[str, ServerTarget] = field(default_factory=dict, repr=False)

    def get(self, target_id: str) -> ServerTarget | None:
        return self._by_id.get(target_id)

    def scoped(
        self,
        ids: Sequence[str],
        *,
        primary: str = "",
        whitelist: Sequence[WhitelistRoute] = (),
    ) -> "ServerBook":
        """按一组 id 投影出**子视图**，用于「某个 QQ 群关联的服务器」。

        返回的是另一个 ServerBook，不是新类型：resolve / targets_primary_first /
        pick_whitelist / primary_target 全部原样复用，群关联因此不需要另写一套
        名字解析 —— 两份实现一定会漂，而漂的方式是「同一台服在两个群里认得出/认不出」。

        **`_by_id` 必须重建，不能沿用 self._by_id**。get() / pick_whitelist /
        primary_target 全查它；漏了不报错，只是白名单路由一台都 pick 不出来，
        群里表现为「查询正常、白名单说本群没配」，而 primary_target 因为退回
        targets[0] 碰巧还是对的 —— 最难发现的那种。_by_id 里**只有本视图的目标**，
        所以关联外的服名 resolve 会如实返回「未知」（这正是隔离赖以成立的东西）。

        白名单路由**故意不做第二份索引**（不像 _by_id 那样建 `_whitelist_by_id`）：
        P5 那次「scoped 忘了重建索引 → 白名单静默不可用」的教训就是「多一份必须记得
        重建的东西」造成的。每群至多几台，pick_whitelist 直接线性扫 whitelist_routes。

        warnings 留空、不继承 self.warnings：全量那份（端口冲突等）在启动日志和
        --list-targets 里各打一次就够，继承过来会让每条关联都重复一遍别人的问题。
        只有某条关联才说得通的警告由 mcaudiences 生成，进 Audience.warnings。
        """
        picked: list[ServerTarget] = []
        for target_id in ids:
            target = self.get(target_id)
            if target is None:
                # 正常路径到不了这里：mcaudiences 会先对着全量表校验一遍 id。
                # 真到了说明校验被绕过了，宁可炸也不要投影出一个少了一台的视图。
                raise ServerConfigError(f"目标 {target_id!r} 不在目标表里，无法投影")
            picked.append(target)
        in_view = {t.id for t in picked}
        routes: list[WhitelistRoute] = []
        seen: set[str] = set()
        for route in whitelist:
            if route.target.id not in in_view:
                # 同样到不了：mcaudiences 已校验过 target 在本条 targets 里。
                raise ServerConfigError(
                    f"白名单路由 {route.target.id!r} 不在本次投影的目标里，无法投影"
                )
            if route.target.id in seen:
                # 重复的后果是「同一个群给一台服用两个前缀」，而命令只会按第一条发出去 ——
                # 又一处「配了却不生效」，所以在投影这一步就拦住。
                raise ServerConfigError(
                    f"白名单路由 {route.target.id!r} 重复：同一台服只能有一条白名单路由"
                )
            seen.add(route.target.id)
            routes.append(route)
        return ServerBook(
            targets=tuple(picked),
            whitelist_routes=tuple(routes),
            primary=primary,
            warnings=(),
            _by_id={t.id: t for t in picked},
        )

    @property
    def primary_target(self) -> ServerTarget | None:
        """主服：汇总里排最前，也是「只支持单目标」的场景下的默认目标。

        在**子视图**里由 `[[audience]].primary` 指定（`scoped(primary=...)` 填进来）；
        不写就是本视图的第一个。用显式键而不是约定「第一个」，是因为顺序还承担着
        展示职责（--list-targets 按配置顺序打印），让两者绑在一起会互相牵制。
        """
        if self.primary:
            found = self._by_id.get(self.primary)
            if found is not None:
                return found
        return self.targets[0] if self.targets else None

    @property
    def whitelist_targets(self) -> tuple[ServerTarget, ...]:
        """本视图的白名单服，只要目标不要前缀（诊断脚本打印用）。"""
        return tuple(r.target for r in self.whitelist_routes)

    def pick_whitelist(self, query: str = "") -> WhitelistPick:
        """「这条白名单命令发给哪台」。**白名单归属的唯一解析入口**，群消息与诊断脚本共用。

        两条规则，顺序不能反：

        1. **query 为空**：0 条路由 → NO_ROUTE；恰好 1 条 → 命中（单台可以省略服名，
           这是绝大多数情况的写法）；≥2 条 → NEED_NAME。**没有「默认那台」这种回退** ——
           多台时猜错的后果是改错服务器的白名单。
        2. **query 非空**：先在本条关联的**全部**目标里跑现成的 resolve()（NFKC + 唯一
           前缀 + 歧义三态），认出是哪台之后再看它是不是白名单路由。不能只在本视图的
           白名单路由里解析：`gtnh` 是这个群查得到的服、只是没配白名单，直接回
           「没有叫 gtnh 的服」是**假话**（群里 `@bot mc gtnh` 明明查得到它），照着这句
           去改配置名字是白费功夫。所以那种情况回 NOT_WHITELIST 并带上 found。
        """
        raw = (query or "").strip()
        routes = self.whitelist_routes
        if not raw:
            if len(routes) == 1:
                return WhitelistPick(PICK_OK, route=routes[0])
            if not routes:
                return WhitelistPick(PICK_NO_ROUTE)
            return WhitelistPick(PICK_NEED_NAME, candidates=tuple(r.name for r in routes))

        res = self.resolve(raw)
        if res.ambiguous:
            return WhitelistPick(PICK_AMBIGUOUS, query=raw, candidates=res.candidates)
        if not res.ok:
            return WhitelistPick(
                PICK_UNKNOWN, query=raw, candidates=tuple(t.name for t in self.targets)
            )
        for route in routes:  # 线性扫描：不引入第二份必须记得重建的索引
            if route.target.id == res.target.id:
                return WhitelistPick(PICK_OK, query=raw, route=route)
        return WhitelistPick(
            PICK_NOT_WHITELIST,
            query=raw,
            found=res.target,
            candidates=tuple(r.name for r in routes),
        )

    @property
    def targets_primary_first(self) -> tuple[ServerTarget, ...]:
        """主服排最前，其余保持配置顺序。

        「主服」这个键的含义就是「默认先看哪台」，所以汇总和播报里它得在最前面。
        配置顺序**不**参与这件事 —— 它只负责 --list-targets 的打印次序，两者刻意
        分开（见 mcs_audiences.toml.example 里对 primary 的说明），否则调打印顺序会
        顺手改掉群里的展示顺序。
        """
        first = self.primary_target
        if first is None:
            return self.targets
        return (first, *(t for t in self.targets if t.id != first.id))

    def resolve(self, query: str) -> Resolution:
        """把群里的一个词解析成唯一目标。

        匹配顺序（先严后宽，任何一步同时命中多个就停下报歧义，不降级到下一步）：
          1. 区分大小写的**精确** id / name / alias
          2. 归一化后（NFKC + 去空白 + 忽略大小写）精确匹配
          3. 归一化后的**唯一前缀**

        第 2 步的 NFKC 是为了全角：QQ 输入法下「ｂｉｎｇｏ」和「bingo」应当等价。
        """
        raw = (query or "").strip()
        if not raw:
            return Resolution(query=raw)

        # 1) 精确（区分大小写）
        exact = [t for t in self.targets if raw in t.keys]
        if len(exact) == 1:
            return Resolution(query=raw, target=exact[0])
        if len(exact) > 1:
            # 加载期已保证 id/name/alias 全局不撞，走到这里说明有 bug，但仍不猜
            return Resolution(query=raw, candidates=tuple(t.name for t in exact))

        needle = _normalize(raw)

        # 2) 归一化后精确
        hits = [t for t in self.targets if needle in {_normalize(k) for k in t.keys}]
        if len(hits) == 1:
            return Resolution(query=raw, target=hits[0])
        if len(hits) > 1:
            return Resolution(query=raw, candidates=tuple(t.name for t in hits))

        # 3) 归一化后的唯一前缀
        prefixed = [
            t for t in self.targets if any(_normalize(k).startswith(needle) for k in t.keys)
        ]
        if len(prefixed) == 1:
            return Resolution(query=raw, target=prefixed[0])
        if len(prefixed) > 1:
            return Resolution(query=raw, candidates=tuple(t.name for t in prefixed))

        return Resolution(query=raw)

    def describe(self) -> list[str]:
        """给 --list-targets / 启动日志用的一行一个目标概览。

        **把「数据从哪来」显式打出来**（接口 / hub 的子服 / RCON / 只剩人数）。
        接口型目标是唯一「游戏端口可有可无」的一类，光看 host:port 分不出
        「没配」和「不需要」，而这两种情况的排查方向相反。
        """
        rows = []
        for t in self.targets:
            parts = [f"{t.name} [{t.id}] {t.kind} 组={t.group}"]
            if t.is_api:
                parts.append(f"接口={t.api.url}")
                # 接口型的 host/port 是**可选**的：填了就拿 SLP 做一次人数交叉校验
                # （接口的名单和人数出自同一份 JSON，少了这层独立来源），填不填都不影响取数。
                parts.append(
                    f"游戏={t.game_addr}（仅交叉校验）" if t.host else "游戏=（无）"
                )
            elif t.source:
                parts.append(f"数据={t.source}:{t.source_name}")
                parts.append(f"游戏={t.game_addr}" if t.host else "游戏=（不可达）")
            else:
                parts.append(f"游戏={t.game_addr}")
            if t.is_api:
                parts.append("白名单=接口")
            elif t.rcon_enabled:
                parts.append(f"RCON={t.rcon_addr}")
                if t.source:
                    parts.append("（也用于白名单）")
            elif t.kind != KIND_PROXY:
                parts.append("RCON=未配密码")
            rows.append(" ".join(parts))
        return rows


def _normalize(text: str) -> str:
    """NFKC + 去内部空白 + casefold。用于「宽松但不猜」的名字比对。"""
    return unicodedata.normalize("NFKC", text).replace(" ", "").replace("　", "").casefold()


def _as_int(value: object, where: str, *, low: int = 1, high: int = 65535) -> int:
    # bool 是 int 的子类，不先挡掉的话 port=true 会被当成 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise ServerConfigError(f"{where} 必须是整数，实际是 {value!r}")
    if not low <= value <= high:
        raise ServerConfigError(f"{where} 超出范围 {low}~{high}：{value}")
    return value


def _as_float(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ServerConfigError(f"{where} 必须是数字，实际是 {value!r}")
    number = float(value)
    if number <= 0:
        raise ServerConfigError(f"{where} 必须为正数，实际是 {number:g}")
    return number


def _as_text(value: object, where: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise ServerConfigError(f"{where} 必须是字符串，实际是 {value!r}")
    text = value.strip()
    if not text and not allow_empty:
        raise ServerConfigError(f"{where} 不能为空")
    return text


def _as_bool(value: object, where: str) -> bool:
    """布尔键：只认 TOML 的真布尔，不做「字符串真值」那一套。

    `watch = "true"` 这种写法**必须报错**。静默当真会让这个群莫名收到推送，静默当假
    则等于「配置写了但没生效」—— 后者正是本工程最防的那类键（改的人以为自己改好了）。

    也**刻意不认 1 / 0**：`isinstance(True, int)` 是真的，反过来 `1` 不是 bool，只认 bool
    就把「拿整数当开关」一并挡掉。这和 groups 那边专挡 `isinstance(item, bool)` 是同一个
    坑的两面（那边是 bool 混进 int 里，这边是 int 混进 bool 里）。
    """
    if not isinstance(value, bool):
        raise ServerConfigError(f"{where} 必须是 true / false，实际是 {value!r}")
    return value


def _reject_unknown(table: dict, allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ServerConfigError(
            f"{where} 里有不认识的键：{'、'.join(unknown)}；"
            f"可用的是：{'、'.join(sorted(allowed))}"
        )


def parse_book(text: str) -> ServerBook:
    """解析 mcs_servers.toml 的**文本**，返回 ServerBook。

    刻意只吃文本、不碰文件系统——自测要能直接喂字符串。读文件用 load_book。
    """
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ServerConfigError(f"mcs_servers.toml 语法错误：{exc}") from exc

    # 旧键的存在性检查必须在 _reject_unknown **之前**：_TOP_KEYS 收窄之后，
    # [whitelist] 会先被当成「不认识的键」拦下，而那句话完全没告诉人该搬去哪 ——
    # 用户照着它改只会把段名删掉，白名单功能静默消失。
    # 按「键存在」判而不是「值非空」：空表 `[whitelist]`（或 primary = ""）同样是
    # 没搬完的痕迹，留着它只会让人以为那个键还在生效。
    if _MOVED_WHITELIST_KEYS & set(doc):
        raise ServerConfigError(_MOVED_WHITELIST_HINT)

    _reject_unknown(doc, _TOP_KEYS, "mcs_servers.toml")

    defaults = doc.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ServerConfigError("[defaults] 必须是一个表")
    if _MOVED_DEFAULT_KEYS & set(defaults):
        raise ServerConfigError(_MOVED_PRIMARY_HINT)
    _reject_unknown(defaults, _DEFAULT_KEYS, "[defaults]")
    base_timeout = _as_float(defaults.get("timeout", _DEFAULT_TIMEOUT), "[defaults].timeout")
    base_rcon_timeout = _as_float(
        defaults.get("rcon_timeout", _DEFAULT_RCON_TIMEOUT), "[defaults].rcon_timeout"
    )

    raw_targets = doc.get("targets", [])
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ServerConfigError("至少要有一个 [[targets]]，否则没有任何可监控的服务器")

    warnings: list[str] = []
    targets: list[ServerTarget] = []
    by_id: dict[str, ServerTarget] = {}
    # 名字 → 已占用的 id。别名/名字撞车会让 resolve 永远歧义，是硬错误。
    claimed: dict[str, str] = {}

    for index, raw in enumerate(raw_targets, start=1):
        where = f"第 {index} 个 [[targets]]"
        if not isinstance(raw, dict):
            raise ServerConfigError(f"{where} 必须是一个表")
        _reject_unknown(raw, _TARGET_KEYS, where)

        target_id = _as_text(raw.get("id", ""), f"{where}.id", allow_empty=False)
        if target_id in by_id:
            raise ServerConfigError(f"目标 id 重复：{target_id}")
        where = f"目标 {target_id}"

        kind = _as_text(raw.get("kind", ""), f"{where}.kind", allow_empty=False)
        if kind not in KINDS:
            raise ServerConfigError(
                f"{where}.kind 非法：{kind!r}；只能是 {'、'.join(KINDS)} 之一"
            )

        api_raw = raw.get("api")
        api: ApiSpec | None = None
        if api_raw is not None:
            if not isinstance(api_raw, dict):
                raise ServerConfigError(f"{where}.api 必须是一个表")
            _reject_unknown(api_raw, _API_KEYS, f"{where}.api")
            api = ApiSpec(
                url=_as_text(api_raw.get("url", ""), f"{where}.api.url", allow_empty=False),
                # token 不给默认值、也不许空：没有它每条请求都是 401，等于配了个
                # 「永远取不到数据」的目标。宁可现在就报错。
                token=_as_text(api_raw.get("token", ""), f"{where}.api.token", allow_empty=False),
            )
            if not api.url.startswith(("http://", "https://")):
                raise ServerConfigError(
                    f"{where}.api.url 必须以 http:// 或 https:// 开头，实际是 {api.url!r}"
                )
            # url 只填到端口。**带路径的写法必须拦下**：那一段不会参与拼接（mcbridge
            # 用死路径），也就是说 http://h:8080/status 里的 /status 被静默忽略 ——
            # 看着像配对了、其实那一段从来没生效过。这正是「配了却不生效」那一类。
            if "/" in api.url.split("://", 1)[1].rstrip("/"):
                raise ServerConfigError(
                    f"{where}.api.url 只填到端口就行（如 http://127.0.0.1:8080），不要带路径 ——"
                    f"/status、/whitelist 这些路径由机器人自己拼。实际是 {api.url!r}"
                )
            if kind != KIND_PROXY:
                raise ServerConfigError(
                    f"{where} 的 kind 是 {kind!r}，但配了 api —— 这个接口给的是整个群组的"
                    f'数据（每个子服各一行），只有 kind = "proxy" 的目标用得上它。'
                    f"子服不要配 api，改成 source = \"<代理的 id>\"。"
                )

        source = _as_text(raw.get("source", ""), f"{where}.source")
        source_key = _as_text(raw.get("source_key", ""), f"{where}.source_key")
        if source_key and not source:
            raise ServerConfigError(
                f"{where}.source_key 只有配了 source 之后才有意义（它指的是「去那台的"
                f"接口里，用哪个键取我自己」）"
            )
        if source and api is not None:
            raise ServerConfigError(
                f"{where} 同时配了 api 和 source：api 是「数据从我自己的接口取」，"
                f"source 是「数据从 {source} 的接口取」，两者只能留一个"
            )

        # host/port 在**数据不靠自己取**时是可选的：接口型目标（api）的机器只有那一个
        # 内网 HTTP 口，子服（source）压根没发布游戏端口，机器人都够不着。填了仍有用
        # ——SLP 会给人数做一次独立交叉校验（见 fetch_snapshot），所以不是「填了也白填」，
        # 但**只填 port 不填 host** 就真的是白填（SLP 要两个一起），当错误拦下。
        if api is not None or source:
            host = _as_text(raw.get("host", ""), f"{where}.host")
            port = _as_int(raw.get("port", 0), f"{where}.port") if host else 0
            if not host and "port" in raw:
                raise ServerConfigError(
                    f"{where}.port 单独出现没有意义：SLP 要 host 和 port 一起给。"
                    f"这个目标的数据本来也不靠 SLP，两个都删掉就是对的写法。"
                )
        else:
            host = _as_text(raw.get("host", ""), f"{where}.host", allow_empty=False)
            port = _as_int(raw.get("port", 0), f"{where}.port")
        # name 缺省就用 id：消息里显示的服名不该是空的
        name = _as_text(raw.get("name", target_id), f"{where}.name") or target_id
        # group 缺省用 id = 自己独占一组。同组的才会把 A→B 合并成一条「换服」。
        group = _as_text(raw.get("group", ""), f"{where}.group") or target_id
        timeout = _as_float(raw.get("timeout", base_timeout), f"{where}.timeout")
        rcon_timeout = _as_float(raw.get("rcon_timeout", base_rcon_timeout), f"{where}.rcon_timeout")

        rcon_raw = raw.get("rcon")
        rcon: RconSpec | None = None
        if rcon_raw is not None:
            if not isinstance(rcon_raw, dict):
                raise ServerConfigError(f"{where}.rcon 必须是一个表")
            _reject_unknown(rcon_raw, _RCON_KEYS, f"{where}.rcon")
            rcon = RconSpec(
                port=_as_int(rcon_raw.get("port", 0), f"{where}.rcon.port"),
                # 留空 = 跟目标的 host 相同。不用 allow_empty=False —— 空是这个字段的
                # 正常取值（"跟着目标走"），不是漏填。
                host=_as_text(rcon_raw.get("host", ""), f"{where}.rcon.host"),
                password=_as_text(rcon_raw.get("password", ""), f"{where}.rcon.password"),
            )
        if rcon is not None and api is not None:
            # 两条通道都能取名单、都能改白名单，同时配着就没有任何一处能说清实际走哪条。
            # 更坏的是两边会**各说各话**：名单按接口的报、白名单按…其实按接口（transport
            # 的优先级），于是 rcon 段静默失效，而配它的人以为自己开着一条后路。
            raise ServerConfigError(
                f"{where} 同时配了 rcon 和 api —— 这是两条互斥的通道（都能出名单、都能改"
                f"白名单），同时存在就没法判断实际走哪条。留一个：走对方接口的群组服留 "
                f"api，自己开 RCON 的服留 rcon。"
            )
        if rcon is not None and source:
            # source 和 rcon **不会各说各话到报错**，所以这条比上一条隐蔽得多：在线名单
            # 走 source（hub 的接口），而白名单命令走本机 RCON —— 群里显示的名字和加白
            # 名单时读的名字来自两个地方。真实翻车：代理那侧的白名单（GlobalWhitelist
            # 之类）才是进服校验那一道，子服自己的白名单加上去**看起来成功**，
            # 人还是进不去。取数层没法自己发现这件事（两条路各自都是自洽的），
            # 所以只能在解析期拦下。
            raise ServerConfigError(
                f"{where} 同时配了 source 和 rcon：source 是「我的在线名单从 {source} 的"
                f"接口拿」，而 rcon 会让白名单命令走「这台自己的」控制台 —— 群里的名单和"
                f"加白名单时读的那份名单来自两处，代理层才是进服校验那一道，于是会出现"
                f"「机器人说已添加，人还是进不去」。二选一：挂接口的子服把白名单也交给 "
                f'{source}（在 mcs_audiences.toml 的 whitelist 里把 target 写成 '
                f'"{source}"），或者去掉 source、这台完全走自己的 RCON。'
            )

        aliases_raw = raw.get("aliases", [])
        if not isinstance(aliases_raw, list):
            raise ServerConfigError(f"{where}.aliases 必须是字符串数组")
        aliases = tuple(
            _as_text(a, f"{where}.aliases", allow_empty=False) for a in aliases_raw
        )

        target = ServerTarget(
            id=target_id,
            name=name,
            kind=kind,
            group=group,
            host=host,
            port=port,
            timeout=timeout,
            rcon_timeout=rcon_timeout,
            rcon=rcon,
            api=api,
            source=source,
            source_key=source_key,
            aliases=aliases,
        )

        for key in target.keys:
            norm = _normalize(key)
            owner = claimed.get(norm)
            # 只查**跨目标**撞车。同一个目标自己的 id 和 name 归一化后相同是正常的
            # （id="bingo" / name="Bingo"），它们都指向它自己，不产生歧义。
            if owner is not None and owner != target_id:
                raise ServerConfigError(
                    f"{where} 的名字 {key!r} 已被目标 {owner} 占用（id / name / alias 不能重复），"
                    f"否则群里的查询永远无法确定指哪个"
                )
            claimed[norm] = target_id

        # 「这台只会显示人数」的警告。三种情况不该报：配了 RCON / 自己有接口 /
        # 挂在接口上（数据从 hub 拿）。代理也不报 —— 它出不出名单是通道决定的，
        # 没配接口的代理是**设计如此**，不是漏配（见 serves_names）。
        if not target.has_own_source and target.kind != KIND_PROXY:
            warnings.append(
                f"{name}：没配 rcon.password、也没有接口数据源，它不出玩家名单"
                f"（只能靠 SLP 样本，超过 12 人就不完整）"
            )

        by_id[target_id] = target
        targets.append(target)

    _check_sources(targets, by_id)
    _check_port_collisions(targets, warnings)

    # 白名单归属与主服都随群关联走了（见 _MOVED_* 那两条迁移报错），全量表里留空。
    # 需要它们的场景一律走 scoped() 投影出来的子视图。
    return ServerBook(
        targets=tuple(targets),
        warnings=tuple(warnings),
        _by_id=by_id,
    )


def _check_sources(targets: list[ServerTarget], by_id: dict[str, ServerTarget]) -> None:
    """`source` 必须指向一台**配了 api** 的目标。加载期能查清，所以直接报错。

    两件事只能在**所有目标都解析完**之后做：

    1. source 允许指向写在它**后面**的那条 [[targets]]。人写配置时「代理段」不一定
       在最前面，而写在循环里就会变成顺序敏感的坑 —— 调一下段落顺序就报错，报的还
       是「不存在」这种误导话。
    2. 反过来只有全表在手，才可能给出「你是不是想填 XX」这种能照着改的提示。

    「指向一台没配 api 的服」是最容易犯的错（把 source 当成「数据来自哪台服」而不是
    「哪台的接口」），所以那句话要点明接口才是来源。

    校验的同时把 hub 对象**回填**进子服（见 ServerTarget.hub）：这一步必须在全表解析
    完之后，而它恰好就在这里。回填后的对象要**同时换进 by_id** —— 它才是
    ServerBook.get() / scoped() 真正查的那份索引，漏换就会出现「全量表里的 bingo 有
    hub、投影出来的 bingo 没有」，而症状是「某个群里查 bingo 永远说不出来源」。
    """
    for index, t in enumerate(targets):
        if not t.source:
            continue
        hub = by_id.get(t.source)
        if hub is None:
            raise ServerConfigError(
                f"目标 {t.id} 的 source = {t.source!r} 不存在：它要填的是另一条 "
                f"[[targets]] 的 id（配了 api 的那台，通常就是代理那条）"
            )
        if hub.api is None:
            raise ServerConfigError(
                f"目标 {t.id} 的 source 指向 {t.source}，但那台没配 api —— "
                f"source 的意思是「去那台的接口里取我的数据」，所以那台必须有 api 段"
            )
        # 循环里查的一律是 by_id，而 hub 自己（配了 api 的那台）不可能有 source
        # （见 parse_book：api + source 并存直接报错），所以本轮的查找不受回填影响。
        bound = replace(t, hub=hub)
        targets[index] = bound
        by_id[t.id] = bound


def _check_port_collisions(targets: list[ServerTarget], warnings: list[str]) -> None:
    """两个目标抢同一个端口 —— 警告而非报错。

    这**不是**配置语法错误，是运行期事实：那两台不能同时起，后起的会绑定失败。
    硬报错会让配置根本加载不了，把整条流水线堵死；而 bot 在「哪台在跑就监控哪台」
    的模式下本来就能正常工作。

    比「不能同时起」更隐蔽的是**误报**：两台 host:port 相同时，探测层面无从分辨，
    查 A 会把正在跑的 B 的数据当成 A 报出去（2026-09-20 之前 GTNH 和 bingo 就是
    这种情况，靠版本号 1.7.10 vs 26.2 才认出来）。所以这条警告要留着 —— 它守的是
    「一台服的数据被标上另一台的名字」这个静默错误。
    """
    games: dict[str, list[str]] = {}
    rcons: dict[str, list[str]] = {}
    for t in targets:
        # 没有游戏地址的目标（接口型、挂在接口上的子服）不参与 —— 它们本来就没有
        # SLP 端点，而 game_addr 会一律退化成 "-"，不加这一句就会报出一堆
        # 「这几台都绑 -」的假冲突，把真冲突埋掉。
        if t.host:
            games.setdefault(t.game_addr, []).append(t.id)
        if t.rcon_enabled:
            rcons.setdefault(t.rcon_addr, []).append(t.id)

    for addr, ids in games.items():
        if len(ids) > 1:
            warnings.append(
                f"端口冲突：{'、'.join(ids)} 都绑 {addr} —— 它们不能同时运行，"
                f"后启动的那台会绑定失败"
            )
    for addr, ids in rcons.items():
        if len(ids) > 1:
            warnings.append(
                f"RCON 端口冲突：{'、'.join(ids)} 都用 {addr} —— 同上，不能同时运行"
            )


def load_book(path: str | Path) -> ServerBook:
    """从磁盘读并解析 mcs_servers.toml。文件不存在时抛 ServerConfigError。"""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ServerConfigError(
            f"找不到 {file}。请复制 mcs_servers.toml.example 为 mcs_servers.toml 再填写。"
        ) from exc
    except OSError as exc:
        raise ServerConfigError(f"读取 {file} 失败：{exc}") from exc
    return parse_book(text)


# 默认路径：本文件在 <root>/plugins_napcat/_shared/mcservers.py
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "mcs_servers.toml"


def book_path() -> Path:
    """实际会读的那个 mcs_servers.toml 路径（`MCS_SERVERS_TOML` 可覆盖）。

    暴露出来是为了能**打给人看**：排查「新加的子服为什么没生效」时，
    「你到底读的哪个文件」是第一顺位的问题 —— 编辑错文件（比如改了仓库根目录那份
    而部署跑的是别处的副本）看起来和"配置没生效"一模一样。

    另一份配置（mcs_audiences.toml）的路径在 mcaudiences.audiences_path()，
    同样可覆盖（`MCS_AUDIENCES_TOML`）。
    """
    override = os.environ.get("MCS_SERVERS_TOML", "").strip()
    return Path(override) if override else _DEFAULT_PATH


# 这里**刻意没有** default_book()。加载入口只有一个：
# _shared/mcaudiences.py 的 default_config()，它一次读两个文件、只缓存一次。
# 留一个带 lru_cache 的 default_book() 在旁边，就会出现「清了 book 没清 audiences」
# 的中间态（两个缓存各自记着不同时刻的配置），而症状是「改完配置重启，群里一半新
# 一半旧」。全量表本身仍然随时可用：load_book(book_path())，不缓存。


def round_budget(book: ServerBook) -> tuple[float, float, float, float]:
    """一轮探测的耗时上界（秒）：返回 (slp, rcon, api, budget)。

    **目标之间是并发探测的**，所以各项都取 max 而不是求和 —— 加子服不会让这个数变大。
    逐目标的成本：

      · SLP：配了 host/port 就有（接口型目标是可选的），花 `timeout`
      · 接口：只有 api 型目标有，花 `timeout`；它和 SLP **是串行的**（SLP 用来给
        接口报的人数做独立交叉校验，见 fetch_snapshot），所以接口型目标两份都算
      · RCON：有密码且出名单才有，花 `2 × rcon_timeout`（失败会重试一次）

    `budget = max(上面那三类之和)`。接口型目标只加一次 HTTP，**挂在它上面的子服
    （source）不额外花时间** —— 数据已经在 hub 那一份响应里了，这也是同源合并的意义。

    三项分开返回而不是只给 `budget`：打给人看的算式必须**加得起来**
    （`SLP 5s + RCON 5s ×2 + 接口 0s = 最坏 15s`）。早先只返回总和时，输出里少了
    接口那一项，于是「接口型目标一配，算式就和小计对不上」—— 这种输出会让人怀疑
    是算错了，而真相比这无聊：只是没打印出来。

    这是**上界**（所有目标同时卡到超时）不是典型值，只用来跟
    MC_WATCH_INTERVAL_SEC 比大小。`mc_reporter._watch_loop` 是「跑完再补睡剩余
    时间」（`sleep(max(1, 间隔-耗时))`），所以超了也**不会重叠**，只是周期被拉长。

    放在 _shared 里而不是各调用点各算一遍：mc_check 的 --list-targets 和 bot 的
    启动日志都要报这个数，两处必须一致。
    """
    slp = max((t.timeout for t in book.targets if t.host), default=0.0)
    # 只有会真去连 RCON 的目标才计入：代理（RCON 只服务白名单、不发 list）不参与，
    # 挂在外面的服（source）也不参与。
    rcon = max(
        (t.rcon_timeout for t in book.targets if t.rcon_enabled and t.serves_names),
        default=0.0,
    )
    api = max((t.timeout for t in book.targets if t.is_api), default=0.0)
    return slp, rcon, api, slp + 2 * rcon + api
