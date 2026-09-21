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
from dataclasses import dataclass, field
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
    {"id", "name", "kind", "group", "host", "port", "rcon", "aliases", "timeout", "rcon_timeout"}
)
_RCON_KEYS = frozenset({"port", "password"})

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
    write='whitelist = { target = "bingo", command = "whitelist" }',
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
    """一个目标的 RCON 端点。密码为空 = 该目标不出名单（也不下发任何命令）。"""

    port: int
    password: str = ""

    @property
    def enabled(self) -> bool:
        """没填密码就完全不尝试 RCON，免得每轮都发一次注定失败的认证。"""
        return bool(self.password)


@dataclass(frozen=True)
class ServerTarget:
    id: str
    name: str
    kind: str
    group: str
    host: str
    port: int
    timeout: float
    rcon_timeout: float
    rcon: RconSpec | None = None
    aliases: tuple[str, ...] = ()

    @property
    def game_addr(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def rcon_addr(self) -> str:
        return f"{self.host}:{self.rcon.port}" if self.rcon else "-"

    @property
    def serves_names(self) -> bool:
        """代理层拿不到「谁在哪个子服」（Velocity 无原生 glist），所以它不出名单。

        见《群组服对接需求.md》§1：代理的 SLP 只给全群组总人数，分服名单必须
        逐个连子服 RCON。
        """
        return self.kind != KIND_PROXY

    @property
    def rcon_enabled(self) -> bool:
        return self.rcon is not None and self.rcon.enabled

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
class ServerBook:
    """一份加载完的目标表。"""

    targets: tuple[ServerTarget, ...] = ()
    # 下面三项只在**投影出来的子视图**里有意义（见 scoped）。全量表里它们恒为空：
    # 「白名单发给谁」和「本群默认看哪台」都是群关联的属性，不是服务器清单的。
    whitelist_target: str = ""
    whitelist_command: str = _DEFAULT_COMMAND
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
        whitelist_target: str = "",
        whitelist_command: str = _DEFAULT_COMMAND,
    ) -> "ServerBook":
        """按一组 id 投影出**子视图**，用于「某个 QQ 群关联的服务器」。

        返回的是另一个 ServerBook，不是新类型：resolve / targets_primary_first /
        whitelist_owner / primary_target 全部原样复用，群关联因此不需要另写一套
        名字解析 —— 两份实现一定会漂，而漂的方式是「同一台服在两个群里认得出/认不出」。

        **`_by_id` 必须重建，不能沿用 self._by_id**。get() / whitelist_owner /
        primary_target 全查它；漏了不报错，只是 whitelist_owner 返回 None，
        群里表现为「查询正常、白名单说不可用」，而 primary_target 因为退回
        targets[0] 碰巧还是对的 —— 最难发现的那种。_by_id 里**只有本视图的目标**，
        所以关联外的服名 resolve 会如实返回「未知」（这正是隔离赖以成立的东西）。

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
        return ServerBook(
            targets=tuple(picked),
            whitelist_target=whitelist_target,
            whitelist_command=whitelist_command,
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
    def whitelist_owner(self) -> ServerTarget | None:
        """白名单命令发给哪台。本视图没指定时返回 None（本群不管白名单）。

        查的是**本视图**的 _by_id，所以子视图只会命中自己关联到的那几台 ——
        社团群的管理员因此碰不到建筑群的白名单目标。
        """
        return self._by_id.get(self.whitelist_target) if self.whitelist_target else None

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
        """给 --list-targets / 启动日志用的一行一个目标概览。"""
        rows = []
        for t in self.targets:
            rcon = t.rcon_addr if t.rcon_enabled else "未配密码"
            rows.append(
                f"{t.name} [{t.id}] {t.kind} 游戏={t.game_addr} RCON={rcon} 组={t.group}"
            )
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
                password=_as_text(rcon_raw.get("password", ""), f"{where}.rcon.password"),
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

        if not target.rcon_enabled and target.kind != KIND_PROXY:
            warnings.append(
                f"{name}：没配 rcon.password，它不出玩家名单"
                f"（只能靠 SLP 样本，超过 12 人就不完整）"
            )

        by_id[target_id] = target
        targets.append(target)

    _check_port_collisions(targets, warnings)

    # 白名单归属与主服都随群关联走了（见 _MOVED_* 那两条迁移报错），全量表里留空。
    # 需要它们的场景一律走 scoped() 投影出来的子视图。
    return ServerBook(
        targets=tuple(targets),
        warnings=tuple(warnings),
        _by_id=by_id,
    )


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


def round_budget(book: ServerBook) -> tuple[float, float, float]:
    """一轮探测的耗时上界（秒）：返回 (slp, rcon, budget)。

    `budget = max(SLP 超时) + 2 × max(RCON 超时)`：
      · 目标之间是**并发**探测，所以取 max 而不是求和 —— 加子服不会让这个数变大
      · RCON 那项乘 2 是因为失败会重试一次
      · 没配 rcon 的目标不参与 RCON 那项的 max

    这是**上界**（所有目标同时卡到超时）不是典型值，只用来跟
    MC_WATCH_INTERVAL_SEC 比大小。`mc_reporter._watch_loop` 是「跑完再补睡剩余
    时间」（`sleep(max(1, 间隔-耗时))`），所以超了也**不会重叠**，只是周期被拉长。

    放在 _shared 里而不是各调用点各算一遍：mc_check 的 --list-targets 和 bot 的
    启动日志都要报这个数，两处必须一致。
    """
    slp = max((t.timeout for t in book.targets), default=0.0)
    rcon = max((t.rcon_timeout for t in book.targets if t.rcon_enabled), default=0.0)
    return slp, rcon, slp + 2 * rcon
