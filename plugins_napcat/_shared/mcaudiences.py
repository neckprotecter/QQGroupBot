"""群关联：mcs_audiences.toml 的解析、校验，以及「这个群看哪几台服」的查找。

机器人同时服务多个 QQ 群，每个群看的服务器**不一样**（社团群看自己的三台，建筑群
的组服还没搭）。mcs_servers.toml 只说「有哪些服」，这个文件说「哪个群关联哪几台」。

一条 `[[audience]]` = 一条**群关联**：

    [[audience]]
    name      = "社团群"                     # 必填，日志和诊断里用它指代这条
    groups    = [123456]                     # 必填，非空。这些 QQ 群共享本关联
    targets   = ["gtnh", "bingo"]            # 可选，**可为空**。顺序即总览顺序
    primary   = "gtnh"                       # 可选，本群默认先看哪台
    whitelist = { target = "bingo", command = "whitelist" }   # 可选，省略 = 本群不管白名单
    watch     = true                         # 可选，缺省 false。本条的 groups 收进服提醒
    report    = true                         # 可选，缺省 false。本条的 groups 收定时播报

「查询」和「推送」是两个轴，别混：**被提到 = 开通查询**（群里问就答），而**推不推送**由
watch / report 决定，缺省关闭。查询是群友拉、推送是机器人自己说话，让新加进来的关联默认
安静是刻意的 —— 否则「加个群进去」会顺手变成「那个群开始每小时被刷一条」。

**为什么和 mcs_servers.toml 分成两个文件**：那个含各服 RCON 明文密码，这个不含 ——
敏感度和受众都不一样，分开之后「这份可以单独给人看」。代价是两份配置要对上，所以
加载入口只有一个：default_config() 一次读两个文件、只缓存一次（见下面的说明）。

和 mcservers.py 一样放 _shared：本模块**不得** import nonebot、不得调 get_driver()，
否则 tools/mc_check.py 在 nonebot.init() 之前 import 会抛 ValueError。

四条设计原则（前三条与 mcservers 一致，第四条是本模块特有的）：

1. **能加载的就让它加载，问题记成警告**。某条关联 targets 为空是**合法**的
   （群先建着、服还没搭），它只会让那个群的查询回一句「本群关联的服务器还没接入」。
2. **解析不出唯一目标时绝不猜** —— 这里体现在「一个群只能属于一条关联」：同一个群号
   出现在两条里会让人无从判断查询该看哪台，直接报错而不是取先出现的那个。
3. **拼错的键要报错，不能静默忽略**。特别是 `groups` 写成 `group` 这种：TOML 没有
   schema，静默的后果是那个群永远收不到 MC 回复，而日志里一条线索都没有。
4. **关联外的服名必须查不到**。ServerBook.scoped() 投影出的子视图里只有本关联的目标，
   所以社团群打建筑群的服名会如实回「没有叫 X 的服」。这是隔离赖以成立的东西，
   不能靠调用方自觉去过滤。
"""
import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .mcservers import (
    _DEFAULT_COMMAND,
    ServerBook,
    ServerConfigError,
    ServerTarget,
    _as_bool,
    _as_text,
    _reject_unknown,
    book_path,
    load_book,
)

# 各表允许出现的键。多一个都报错——见模块 docstring 第 3 条。
_TOP_KEYS = frozenset({"audience"})
_AUDIENCE_KEYS = frozenset(
    {"name", "groups", "targets", "primary", "whitelist", "watch", "report"}
)
_WHITELIST_KEYS = frozenset({"target", "command"})

# .env 里已经不作数的群相关变量：变量名 -> 它搬到哪去了。
# 后半句会**直接拼进警告**，所以必须写成「照着做就能改对」的**动作**，不能是一句现状
# 描述 —— MC_WATCH_GROUP / MC_REPORT_GROUP 的修法**不是**改 groups，而是在那条关联上
# 加一个推送开关。照抄上半句会把人指去改 groups，改完提醒照样不推，而残留警告已经因为
# 他「看过一次」被忽略掉了。
_LEGACY_GROUP_VARS: dict[str, str] = {
    "MC_ALLOWED_GROUPS": "群范围改由 mcs_audiences.toml 的 [[audience]].groups 决定",
    "MC_WATCH_GROUP": "改成给那条 [[audience]] 加 watch = true",
    "MC_REPORT_GROUP": "改成给那条 [[audience]] 加 report = true",
}

_GROUPS_EXAMPLE = "groups = [123456]（整数或字符串都行，逗号分隔写多个）"


@dataclass(frozen=True)
class Audience:
    """一条群关联：这几个 QQ 群 + 它们关联到的那些服务器。"""

    name: str
    groups: tuple[str, ...]  # 归一化成字符串，便于和事件的 group_id（int）比对
    book: ServerBook  # 本关联的服务器（全量表的投影，见 ServerBook.scoped）
    warnings: tuple[str, ...] = ()
    watch: bool = False  # 本条的 groups 收进服提醒
    report: bool = False  # 本条的 groups 收定时播报

    def owns(self, group_id: int | str) -> bool:
        return str(group_id) in self.groups

    @property
    def flags_summary(self) -> str:
        """这一条收不收推送。启动日志与 --list-audiences 共用。

        必须显眼：两个开关都没开时，这个群「查询一切正常、却什么都收不到」—— 而它的
        表现（群里安安静静）和「机器人挂了」长得一模一样，看群里是分不出来的。
        """
        on = [n for n, v in (("进服提醒", self.watch), ("定时播报", self.report)) if v]
        return "、".join(on) if on else "不推送（watch / report 都没开）"

    @property
    def whitelist_summary(self) -> str:
        """白名单归属的一句话说明。启动日志与 --list-audiences 共用。"""
        owner = self.book.whitelist_owner
        if owner is None:
            return "不提供白名单管理"
        if not owner.rcon_enabled:
            return f"白名单发往 {owner.name}[{owner.id}]，但它没配 rcon.password —— 发不出去"
        return (
            f"白名单发往 {owner.name}[{owner.id}]"
            f"（命令前缀 {self.book.whitelist_command!r}）"
        )

    def summary(self) -> str:
        """一行概览：这条关联覆盖几个群、关联了哪几台服。"""
        servers = "、".join(t.name for t in self.book.targets) or "（还没关联任何服务器）"
        return f"{self.name}（{len(self.groups)} 个群）→ {servers}"


@dataclass(frozen=True)
class McConfig:
    """两份配置合起来的样子：全量目标表 + 全部群关联。"""

    book: ServerBook  # 全量，给诊断脚本和「哪些目标没人用」的判断用
    audiences: tuple[Audience, ...]
    warnings: tuple[str, ...] = ()  # 跨文件级（孤儿目标、.env 残留）

    def for_group(self, group_id: int | str) -> Audience | None:
        """这个群属于哪条关联。没被任何关联提到 → None（调用方必须回绝并打日志）。"""
        wanted = str(group_id)
        for audience in self.audiences:
            if wanted in audience.groups:
                return audience
        return None

    @property
    def known_groups(self) -> tuple[str, ...]:
        """配置里提到过的全部群号。用于「这个群没开通」的日志里说明有哪些群开着。"""
        return tuple(g for a in self.audiences for g in a.groups)

    def flag_targets(self, flag: str) -> tuple[ServerTarget, ...]:
        """开了某个推送开关（"watch" / "report"）的关联覆盖到的**全部目标**，去重保序。

        耗时就该按它算：进服提醒一轮真正探的就是这些目标（探测是并发的，所以上界取
        max 而不是求和）。按全量表算会多算进只被别的关联引用的服 —— 那台服超时写得大
        一点，就会有**一条与轮询无关的告警永远顶着**，而告警被无视之后真超了也看不出来。

        bot 的启动日志与 tools/mc_check.py 的 --list-targets 都用这个函数：两处各算
        一遍一定会漂，而它们本来就该说同一个数。
        """
        seen: dict[str, ServerTarget] = {}
        for audience in self.audiences:
            if getattr(audience, flag):
                for target in audience.book.targets:
                    seen.setdefault(target.id, target)
        return tuple(seen.values())


def parse_audiences(text: str, book: ServerBook) -> tuple[Audience, ...]:
    """解析 mcs_audiences.toml 的**文本**，对着全量表 book 校验。

    刻意只吃文本、不碰文件系统——自测要能直接喂字符串。读文件用 load_config。
    """
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ServerConfigError(f"mcs_audiences.toml 语法错误：{exc}") from exc

    _reject_unknown(doc, _TOP_KEYS, "mcs_audiences.toml")

    raw_list = doc.get("audience", [])
    if not isinstance(raw_list, list) or not raw_list:
        # 报错而不是「当空配置跑」：空配置的后果是**所有**群都查不了 MC，而表现
        # 和「机器人挂了」一模一样。想让某个群先占位，那是 targets = [] 的事。
        raise ServerConfigError(
            "mcs_audiences.toml 里至少要有一条 [[audience]]，否则没有任何群能查 MC。"
            "想让某个群先留着，就给它写 targets = []"
        )

    known_ids = {t.id for t in book.targets}
    audiences: list[Audience] = []
    # 群号 → 已占用它的关联名。跨条重复是硬错误：同一个群落在两条关联里，
    # 「这个群查询该看哪几台」就说不清了。
    owner_of: dict[str, str] = {}
    # 已经用过的关联名。重名会让日志和 --list-audiences 里「群关联「X」」指代两条不同的
    # 东西，看日志的人无从判断说的是哪条 —— 而日志正是本项目指定的第一排查入口。
    name_seen: set[str] = set()

    for index, raw in enumerate(raw_list, start=1):
        where = f"第 {index} 条 [[audience]]"
        if not isinstance(raw, dict):
            raise ServerConfigError(f"{where} 必须是一个表")
        _reject_unknown(raw, _AUDIENCE_KEYS, where)

        name = _as_text(raw.get("name", ""), f"{where}.name", allow_empty=False)
        if name in name_seen:
            raise ServerConfigError(
                f"{where} 的名字「{name}」和前面某条重复了；关联名是日志和诊断里指代"
                f"这条关联的唯一标识，重名之后看日志分不清说的是哪条"
            )
        name_seen.add(name)
        where = f"群关联「{name}」"

        # 推送开关。缺省 false —— 见模块 docstring：查询是群友拉，推送是机器人自己说话。
        watch = _as_bool(raw.get("watch", False), f"{where}.watch")
        report = _as_bool(raw.get("report", False), f"{where}.report")

        groups_raw = raw.get("groups")
        if groups_raw is None:
            raise ServerConfigError(f"{where} 缺 groups：写它覆盖哪些 QQ 群号，例如 {_GROUPS_EXAMPLE}")
        if not isinstance(groups_raw, list) or not groups_raw:
            raise ServerConfigError(f"{where}.groups 必须是非空数组，例如 {_GROUPS_EXAMPLE}")
        groups: list[str] = []
        for item in groups_raw:
            # bool 是 int 的子类：groups = [true] 不挡掉会变成群号 "True"
            if isinstance(item, bool) or not isinstance(item, (int, str)):
                raise ServerConfigError(
                    f"{where}.groups 里只能写群号（整数或字符串），实际是 {item!r}"
                )
            group_id = str(item).strip()
            if not group_id:
                raise ServerConfigError(f"{where}.groups 里有空值")
            if group_id in groups:
                raise ServerConfigError(f"{where}.groups 里 {group_id} 重复了")
            other = owner_of.get(group_id)
            if other is not None:
                raise ServerConfigError(
                    f"群 {group_id} 同时出现在「{other}」和「{name}」两条 [[audience]] 里；"
                    f"一个群只能关联一组服务器，否则它的查询该看哪台就说不清了"
                )
            owner_of[group_id] = name
            groups.append(group_id)

        targets_raw = raw.get("targets", [])
        if not isinstance(targets_raw, list):
            raise ServerConfigError(f"{where}.targets 必须是字符串数组（先占位就写 []）")
        ids: list[str] = []
        for item in targets_raw:
            target_id = _as_text(item, f"{where}.targets", allow_empty=False)
            if target_id in ids:
                raise ServerConfigError(f"{where}.targets 里 {target_id} 重复了")
            if target_id not in known_ids:
                raise ServerConfigError(
                    f"{where}.targets 指向不存在的目标 {target_id!r}；"
                    f"mcs_servers.toml 里的 id 有：{'、'.join(sorted(known_ids))}"
                )
            ids.append(target_id)

        primary = _as_text(raw.get("primary", ""), f"{where}.primary")
        if primary:
            # 「不在本条 targets 里」而不是「不在全量表里」：把建筑群的主服写成社团群的
            # 服务器是**配错**，不是「顺便也能用」—— 那个群里根本打不出这台服的名字。
            if primary not in ids:
                raise ServerConfigError(
                    f"{where}.primary 指向 {primary!r}，但它不在本条的 targets 里；"
                    f"本条关联的服务器只有：{'、'.join(ids) or '（空）'}"
                )

        whitelist_raw = raw.get("whitelist")
        whitelist_target = ""
        whitelist_command = _DEFAULT_COMMAND
        if whitelist_raw is not None:
            if not isinstance(whitelist_raw, dict):
                raise ServerConfigError(
                    f'{where}.whitelist 必须是一个表，例如 whitelist = {{ target = "bingo" }}'
                )
            _reject_unknown(whitelist_raw, _WHITELIST_KEYS, f"{where}.whitelist")
            whitelist_target = _as_text(
                whitelist_raw.get("target", ""), f"{where}.whitelist.target"
            )
            whitelist_command = (
                _as_text(whitelist_raw.get("command", ""), f"{where}.whitelist.command")
                or _DEFAULT_COMMAND
            )
            if whitelist_target and whitelist_target not in ids:
                raise ServerConfigError(
                    f"{where}.whitelist.target 指向 {whitelist_target!r}，"
                    f"但它不在本条的 targets 里；本条关联的服务器只有：{'、'.join(ids) or '（空）'}"
                )

        warnings = _audience_warnings(
            name, ids, whitelist_raw, whitelist_target, book, watch=watch, report=report
        )
        audiences.append(
            Audience(
                name=name,
                groups=tuple(groups),
                # 投影：本关联的服务器视图。它自己重建 _by_id，所以关联外的服名
                # 在这里 resolve 不出结果（模块 docstring 第 4 条）。
                book=book.scoped(
                    ids,
                    primary=primary,
                    whitelist_target=whitelist_target,
                    whitelist_command=whitelist_command,
                ),
                warnings=tuple(warnings),
                watch=watch,
                report=report,
            )
        )

    return tuple(audiences)


def _audience_warnings(
    name: str,
    ids: list[str],
    whitelist_raw: object,
    whitelist_target: str,
    book: ServerBook,
    *,
    watch: bool = False,
    report: bool = False,
) -> list[str]:
    """只属于某条关联的警告。

    刻意**不**重复全量表里那些（端口冲突、某个目标没配 rcon 密码）：全量那份在启动
    日志和 --list-targets 里各打一次就够，每条关联再抄一遍只会把日志冲成噪音，
    而噪音正是 DEPLOY 教人看日志尾巴排查时最不想有的东西。
    """
    warnings: list[str] = []
    if not ids:
        warnings.append(
            f"「{name}」没关联任何服务器 —— 该群查询会回「本群关联的服务器还没接入」"
        )
        # 开了推送却没有服可盯：警告而不是报错。它不是「被忽略的配置键」—— 我们读懂了
        # 它，并且明确说出它为什么没有输出；而硬报错会让整台 bot 起不来，代价与收益
        # 不成比例。targets 填好之后开关自动生效，不用再改配置。
        on = "、".join(n for n, v in (("watch", watch), ("report", report)) if v)
        if on:
            warnings.append(
                f"「{name}」写了 {on} 但本条没关联任何服务器 —— 推送对它不会有任何输出；"
                f"targets 填好之后自动生效，不用改这个开关"
            )
    if whitelist_target:
        owner = book.get(whitelist_target)
        # 上面已校验过它一定在 ids 里，所以 owner 不会是 None
        if owner is not None and not owner.rcon_enabled:
            warnings.append(
                f"「{name}」的白名单服 {owner.name} 没配 rcon.password，白名单命令发不出去"
            )
    elif isinstance(whitelist_raw, dict) and "command" in whitelist_raw:
        # 写了命令前缀却没写发给谁 —— 命令前缀写错是本次排第一的坑，这个形态
        # 一眼看不出来，但它等于「白名单整个没配」。
        warnings.append(
            f"「{name}」的 whitelist 只写了 command 没写 target，本条关联不提供白名单管理"
        )
    return warnings


def _cross_warnings(book: ServerBook, audiences: tuple[Audience, ...]) -> tuple[str, ...]:
    """跨文件级警告：孤儿目标、.env 里的残留。"""
    warnings: list[str] = []

    linked = {t.id for a in audiences for t in a.book.targets}
    for target in book.targets:
        if target.id not in linked:
            # 新服加进了 mcs_servers.toml 却忘了关联给任何群：群里打它回「没有叫 X 的服」，
            # 而 --list-targets 和启动日志**都会正常列出它** —— 不吼一声就查不出来。
            warnings.append(
                f"{target.name}[{target.id}] 没被任何群关联 —— 群里打它查不到，"
                f"把它加进某条 [[audience]] 的 targets 才有人能用"
            )

    legacy = [
        f"{var}（{hint}）"
        for var, hint in _LEGACY_GROUP_VARS.items()
        if os.environ.get(var, "").strip()
    ]
    if legacy:
        warnings.append(
            f".env 里的 {'、'.join(legacy)} 已不再生效，请删掉这几行 —— "
            f"留着会让人以为它还在管，而它已经什么都不管了"
        )

    return tuple(warnings)


# 默认路径：本文件在 <root>/plugins_napcat/_shared/mcaudiences.py
_DEFAULT_AUDIENCES_PATH = Path(__file__).resolve().parents[2] / "mcs_audiences.toml"


def audiences_path() -> Path:
    """实际会读的那个 mcs_audiences.toml 路径（`MCS_AUDIENCES_TOML` 可覆盖）。

    和 mcservers.book_path() 一样，暴露出来是为了能**打给人看**：排查「新加的群
    为什么不响应」时，「你到底读的哪个文件」是第一顺位的问题。
    """
    override = os.environ.get("MCS_AUDIENCES_TOML", "").strip()
    return Path(override) if override else _DEFAULT_AUDIENCES_PATH


def load_config(
    servers_file: str | Path | None = None,
    audiences_file: str | Path | None = None,
) -> McConfig:
    """从磁盘读两份配置并合成 McConfig。

    **报错顺序**：mcs_servers.toml 的错误先报。它更基础（不知道有哪些服时，
    「某条关联指向了不存在的目标」这种报错读起来毫无意义），也更有指导性。

    只被 default_config() 和 tools/mc_check.py 调用；不缓存，所以改完文件重跑命令
    就能看到新结果。
    """
    book = load_book(servers_file if servers_file is not None else book_path())

    path = Path(audiences_file) if audiences_file is not None else audiences_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ServerConfigError(
            f"找不到 {path}。它决定「每个 QQ 群看哪几台服」，"
            f"请复制 mcs_audiences.toml.example 为 mcs_audiences.toml 再填写：\n"
            f"  群号从 .env 的 MC_ALLOWED_GROUPS 搬过来（那个变量已不作数）；\n"
            f"  mcs_servers.toml 里的 [whitelist] 段和 [defaults].primary 也搬进每条 [[audience]]。"
        ) from exc
    except OSError as exc:
        raise ServerConfigError(f"读取 {path} 失败：{exc}") from exc

    audiences = parse_audiences(text, book)
    return McConfig(
        book=book,
        audiences=audiences,
        warnings=_cross_warnings(book, audiences),
    )


@lru_cache(maxsize=1)
def default_config() -> McConfig:
    """惰性加载两份配置（可用 MCS_SERVERS_TOML / MCS_AUDIENCES_TOML 改路径）。

    **唯一的加载入口**：mcs_servers 与 mcs_audiences 是同一次读、只缓存一次，
    所以不存在「清了 book 没清 audiences」的中间态 —— 两个缓存各自记着不同时刻的
    配置，症状是「改完配置重启，群里一半新一半旧」。mcservers.py 里因此**没有**
    default_book()。

    和 mc.py 原来的 `_cfg()` 一样是懒加载、**不可以在 import 期调用**：
    tools/mc_check.py 得先把 .env 灌进 os.environ 再调用，import 期读会踩时序坑。
    改完配置可调 default_config.cache_clear() 重置。

    解析失败时抛 ServerConfigError（lru_cache 不缓存异常，所以每次调用都会重试读盘
    ——这是刻意的：配置写错了应当每次都能看到同一条报错，而不是被缓存成一个陈旧的
    成功结果；现场改好、下一条群消息就正常）。
    """
    return load_config()
