"""MC 消息渲染：纯文本拼装，**不 import nonebot、不 import mcs/**。

`@查询`（mcs/mc_stats.py）、进服提醒与定时播报（mcs/mc_reporter.py）共用同一份格式，
所以它必须住在这里，而不是任一调用方里 —— 否则「总览」和「播报」的排版会各自漂移，
同一个服在两个地方显示成两种样子。

**这三处确实是同一个渲染层**：`render_summary` 与 `render_report` 共用 `_summary_block`
（同一台服在两处逐字一样），进服提醒也复用 `_JOIN_TEMPLATES`。以前播报是 mc_reporter
自己拼的，两套排版各写各的；P7 把它接了过来。

不许 import nonebot 是硬约束：tools/mc_check.py 直接 import 这个模块做离线自测，
那条路必须在 nonebot.init() 之前就能走通。长度上限因此来自 textlen.py 而不是 push.py。
**这条约束有个直接后果**：渲染层打不了日志，所以「被截断了」这类必须让人知道的事
只能**返回给调用方**去说（见 format_events 的第二个返回值）。
"""
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from .mc import API_AUTH, SLP_UNPARSEABLE, McSnapshot
from .mcdelta import KIND_JOIN, KIND_LEAVE, KIND_SWITCH, PlayerEvent, StatusEvent
from .mcservers import ServerTarget
from .textlen import MAX_LEN, truncate

# 下面两条是**群内会说出口**的拒绝文案，放在这里是因为 mc_stats 与 mc_admin 都会用到，
# 而两份副本一定会漂。措辞只说「本群」：群友不知道 mcs_audiences.toml 里那些名字是什么，
# 所以拒绝文案一律不带关联名。

# 「本群没关联任何服务器」（关联到了，但 targets 是空的）。mc_stats 在解析载荷**之前**
# 就短路返回，render_summary 收到空 rows 是同一件事 —— 两处必须是同一句话。
NO_TARGETS = "🏗️ 本群关联的服务器还没接入，暂时没有可查询的内容。"

# 「本群压根没写进 mcs_audiences.toml」。末尾那句指路很重要：没开通的群里，任何含
# mc / 我的世界 / 服务器 的 @ 消息都会被 MC 插件认领（触发词子串匹配），hello 的
# 功能引导因此被抑制，得在这里把人接回去。
NO_AUDIENCE = "🤔 本群还没有开通 MC 查询。其它功能可以发「@机器人 你好」看看。"

# 跨 group 之间的分隔线（2026-09-23 用户要求：模组服 GTNH 和群组服分开）。
# 用最朴素的 ASCII 连字符加空格，不用「┄」「┈」「─」那类制表/box-drawing 虚线：
# 那几个码点在部分安卓 QQ 的字体里是豆腐块，而这条线唯一的作用就是分区。
# 长度对齐播报里那条 ━ 实线（10 个全角宽 ≈ 20 个半角）。
_GROUP_SEP = ("- " * 10).strip()


@dataclass(frozen=True)
class Row:
    """一个目标 + 它的快照。渲染只需要这两样，所以不必依赖 ServerBook。"""

    target: ServerTarget
    snap: McSnapshot


def name_budget(total_names: int, n_targets: int) -> int:
    """多目标时**每个目标**分到几个名单额。

    直接对每个目标都用满 `MC_QUERY_MAX_NAMES` 会让 4 台服打出 200 行，
    truncate() 切的是尾部 —— 切掉的正是最后几台服和脚注。
    下限 5 是保底：目标再多也至少让人看见几个名字，否则等于「有名单但不显示」。
    """
    return max(5, total_names // max(1, n_targets))


def render_detail(snap: McSnapshot, target: ServerTarget, *, max_names: int) -> str:
    """单服明细（`@bot mc <服名>`）。"""
    name = target.name

    if not snap.reachable:
        # 「连不上」和「答了但答不对」对群里的人是两个完全不同的动作指示：
        # 前者叫人来开服 / 检查端口，后者多半是服务端还在加载（GTNH 那类大整合包
        # 启动要几分钟，期间端口已 bind、能应答，但玩家列表还没就绪）。
        # 一律说「连不上了」会让人白等或白查防火墙。
        if snap.error_kind == API_AUTH:
            # 401 说成「连不上了」会让人去查网络和隧道，而真正要做的是**去要新 token**
            return f"⚠️ {name} 的数据接口不认我们的凭证了。\n{snap.error}"
        if snap.error_kind == SLP_UNPARSEABLE:
            return f"😵 {name} 应答异常（刚启动的话可能还在加载）。\n{snap.error}"
        return f"😵 {name} 连不上了。\n{snap.error}"

    head = f"🗺️ {name} 在线：{snap.count}"
    if snap.max_players:
        head += f"/{snap.max_players}"
    if snap.latency is not None:
        head += f"　延迟 {snap.latency:.0f}ms"

    lines = [head]
    if snap.version:
        lines.append(f"　版本 {snap.version}")

    # 代理报的是全群组总人数，没有分服名单。套明细模板会打出「在线名单（N 人）：」
    # 然后一个人名都不列 —— 看着像把名单弄丢了，而它本来就没有。
    if not target.serves_names:
        lines.append("\n这是代理，报的是全群组总人数，不出分服名单。")
        lines.append("想看名单请 @机器人 mc <子服名>，或去掉服名看总览。")
        return truncate("\n".join(lines))

    if snap.count == 0:
        lines.append("\n现在没人，来当第一个？")
        return truncate("\n".join(lines))

    shown = snap.names[:max_names]
    lines.append(f"\n在线名单（{snap.count} 人）：")
    lines.extend(f"  • {n}" for n in shown)
    if len(snap.names) > len(shown):
        lines.append(f"  …还有 {len(snap.names) - len(shown)} 人")

    if not snap.names_complete:
        lines.append(f"\n⚠️ 名单可能不完整，仅供参考（{snap.error}）")

    return truncate("\n".join(lines))


def _summary_block(row: Row, budget: int) -> list[str]:
    """总览里一个目标的几行。"""
    snap, target = row.snap, row.target
    label = f"【{target.name}】"

    if not snap.reachable:
        # 凭证不对不是「不可达」—— 服务好得很，是我们进不去。总览里也要分开
        if snap.error_kind == API_AUTH:
            return [f"{label}⚠️ 接口凭证失效"]
        return [f"{label}😵 不可达"]

    tail = f"　延迟 {snap.latency:.0f}ms" if snap.latency is not None else ""
    if not target.serves_names:
        # 单独标出「全群组」：它和下面的分服数字不是一个口径，混着看会以为重了。
        # 2026-09-23 去掉句尾的「（代理，不出分服名单）」—— 总览这一行下面紧跟着就是
        # 各子服的数字，「全群组」三个字已经说明它不在那个口径里了，括号那半句只是噪音。
        # 明细视图（render_detail，见本文件 :87）里那句解释**保留**：那边是整份模板套上去，
        # 没有上面这些兄弟行当上下文，读的人需要被明说一次。
        return [f"{label}全群组 {snap.count} 人{tail}"]

    body = snap.count
    if snap.max_players:
        body = f"{snap.count}/{snap.max_players}"
    if snap.count == 0:
        # 0 人就是 0 人，不再补一句「目前无人」—— 数字已经说完了（用户原话：冗余）。
        # 这里仍然**提前返回**，走不到下面的名单与「⚠️ 名单不完整」：一台空的服没有
        # 名字可列，那两句在这个上下文里只会变成噪音。
        return [f"{label}在线 {body}{tail}"]

    lines = [f"{label}在线 {body}{tail}"]
    shown = snap.names[:budget]
    lines.extend(f"  • {n}" for n in shown)
    if len(snap.names) > len(shown):
        lines.append(f"  …还有 {len(snap.names) - len(shown)} 人")
    if not snap.names_complete:
        # 这一台的名字数是靠不住的，得让看的人知道 —— 它也正是进服提醒会静默暂停的那台
        lines.append("  ⚠️ 名单不完整")
    return lines


def _counting(rows: Sequence[Row]) -> list[Row]:
    """口径上可以相加的目标：出分服名单且可达的那些。

    代理**不在其内**：它报的是全群组总人数，各子服本来就被它包含了一次，
    加进合计等于算两遍。
    """
    return [r for r in rows if r.target.serves_names and r.snap.reachable]


def _group_complete(proxy: Row, rows: Sequence[Row], all_targets: Sequence[ServerTarget]) -> bool:
    """本群是否关联到了与该代理同组的**全部**非代理目标。

    代理报的是全群组总人数，「代理总数 vs 子服之和」只有在同组子服**都在这里**时
    才是有意义的对照。本群只关联到其中一部分时两者本来就对不上 —— 那是关联本身
    的必然结果，不是故障。不加这个判断，每次总览都会挂一条「可能是 ping-passthrough」
    的误导提示，把人指去查一个不存在的问题。

    `all_targets` 传的是**全量**目标表（不只是本群关联的），否则这个比较无从做起。
    """
    peers = {t.id for t in all_targets if t.serves_names and t.group == proxy.target.group}
    if not peers:
        # 代理和子服没声明同一个 group（group 缺省 = 用 id，各占一组）时无从知道谁
        # 归它管，退回「拿全量里的非代理目标当它的子服」—— 宁可偶尔误报一条，
        # 也不要因为配置少写一个 group 就把这条提示永久静默掉。
        peers = {t.id for t in all_targets if t.serves_names}
    shown = {
        r.target.id
        for r in rows
        if r.target.serves_names and r.target.group == proxy.target.group
    }
    return peers <= shown


def _footnote(rows: Sequence[Row], all_targets: Sequence[ServerTarget]) -> str:
    """尾注。**必须放在最后且不许被截掉** —— 它说的正是「上面的数对不上」。

    两种提示都只在真有话可说时出现，正常运行时这一整段是空的。
    """
    notes: list[str] = []

    incomplete = [
        r.target.name
        for r in rows
        if r.target.serves_names and r.snap.reachable and not r.snap.names_complete
    ]
    if incomplete:
        notes.append(
            f"⚠️ 名单不完整：{'、'.join(incomplete)}"
            f"（这些服的进服提醒会静默暂停，不会误报）"
        )

    counted = _counting(rows)
    # 接口型代理不参与这条对照（api is not None）。理由有两条，缺一条都不够：
    #   1. 这条对照要发现的是**ping-passthrough**（代理把自己某一台后端的人数当成
    #      全群组报出来）—— 那是 SLP 才有的毛病，接口给的是代理自己的权威计数。
    #   2. 接口的 /status 会把**全部**子服都列出来，而我们只挂关心的那几台（对方 6 台、
    #      我们挂 3 台）。于是「有人在 lobby 里」也会让合计对不上，每张总览都挂一条
    #      误导提示。真要提醒「有几台我们没挂」，那是配置问题，属于探测工具的活。
    proxies = [
        r
        for r in rows
        if not r.target.serves_names and r.snap.reachable and r.target.api is None
    ]
    # 只有一台子服时「代理总人数」和「子服之和」在两种成因下都相等，说什么都是误导
    if proxies and len(counted) >= 2:
        total = sum(r.snap.count for r in counted)
        for p in proxies:
            if p.snap.count != total and _group_complete(p, rows, all_targets):
                notes.append(
                    f"⚠️ {p.target.name} 报全群组 {p.snap.count} 人，各子服合计 {total} 人。"
                    f"有子服掉线时这正常；都在线则可能是代理开了 ping-passthrough"
                    f"（那样它报的其实是某一台后端的人数）。"
                )

    return "\n".join(notes)


def _block_lead(row: Row, prev_group: str | None) -> list[str]:
    """一个目标块**前面**的那一行：平时什么都不留，跨组时留一条虚线。

    判据是 group 变化，不是拿服名硬编码「GTNH 之后画一条」：哪几台归一组由配置说了算
    （没写 group 的各自独占一组），换个部署、加个别组的服都不用回来改这里。

    第一个目标前面什么都不出（上面紧接着就是表头），整条消息只有一组时也不会有虚线 ——
    刻意的：只挂模组服或只挂群组服的群里，那条线只是噪音。

    块之间不空行是总览与播报共同的取向（2026-09-23 总览从「块间空行」改成这样：
    六台服就是六条空行，把消息拉长了一倍）。规则与线型都只有这一份，两处各写一遍的话，
    改线型或改判据时一定会只改一边。
    """
    if prev_group is not None and row.target.group != prev_group:
        return [_GROUP_SEP]
    return []


def _assemble(rows: Sequence[Row], budget: int, all_targets: Sequence[ServerTarget]) -> str:
    counted = _counting(rows)
    total = sum(r.snap.count for r in counted)
    down = sum(1 for r in rows if not r.snap.reachable)

    head = f"🗺️ MC 在线总览：{total} 人"
    if counted:
        head += f"／{len(counted)} 台服"
    if down:
        head += f"（{down} 台探测失败）"

    # 表头与第一块之间留一条空行（与播报同一处），块与块之间不留 —— 一块一台服时，
    # 那些空行只是把消息拉长（2026-09-23 按用户要求去掉）。
    lines = [head, ""]
    prev_group: str | None = None
    for row in rows:
        lines.extend(_block_lead(row, prev_group))
        lines.extend(_summary_block(row, budget))
        prev_group = row.target.group

    note = _footnote(rows, all_targets)
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def _fit(build: Callable[[int], str], budget: int) -> str:
    """按额度渲染；超长就**逐行减少每个目标的名额**重渲染，而不是从头截断。

    从头截断切掉的是尾部 = 最后几台服 + 尾注，切完消息看着完全正常，只是悄悄少了
    「这些数对不上」那句 —— 而这句恰恰是这类消息里最该看到的。

    一次只减 1 是刻意的：按「一行大约多少字」去估算会大幅过冲（名字长短差很多，
    实测过一版估算式，10 个名额被一步打到 0，结果是**超长时一个人名都不显示**）。
    渲染只是拼字符串，多试几次不值钱，换来的是「能显示多少就显示多少」。

    总览与播报的表头和块间距都不一样，但这段循环一模一样，所以只有这一份 ——
    复制一份的后果是两边迟早只在一边修，而「超长时丢掉尾注」这种毛病在正常长度下
    根本看不见。
    """
    while budget > 0:
        text = build(budget)
        if len(text) <= MAX_LEN:
            return text
        budget -= 1
    # 名额清零还是超长（目标极多）才认输截断，此时尾注也只能让位给服名
    return truncate(build(0))


def render_summary(
    rows: Sequence[Row], *, total_names: int, all_targets: Sequence[ServerTarget]
) -> str:
    """总览（`@bot mc` 不带服名）：每个目标一行，各自带一小段名单。

    `all_targets` 是**全量**目标表（不只是本群关联的），只用于代理尾注那条判断：
    只有本群关联到了该代理同组的全部子服时，才提醒「代理总数与子服之和对不上」。
    设成必填参数是刻意的 —— 漏传会让每条总览都挂一条误导提示，而那是静默的。
    """
    if not rows:
        return NO_TARGETS
    return _fit(
        lambda budget: _assemble(rows, budget, all_targets),
        name_budget(total_names, len(rows)),
    )


def render_report(
    rows: Sequence[Row],
    *,
    total_names: int,
    all_targets: Sequence[ServerTarget],
    audience_name: str = "",
    now: datetime | None = None,
) -> tuple[str | None, str]:
    """定时播报（一条群关联一个周期一条）。返回 `(文案, 跳过原因)`。

    文案为 None = 本次不发，第二个元素是给日志用的**原因**。跳过只有两种，且
    **原因必须分开说**：全都连不上 → 去开服；确实没人在线 → 什么都不用做。
    混成一句「当前无人在线」会把前一种的人指去查错方向（沿用 mc_reporter 原有契约）。

    **「某一台没人 / 某一台不可达」不再是跳过理由** —— 它只是 _summary_block 已经能
    渲染的一行。单目标时代那条「0 人 → 整条不发」在多目标下必须拆掉：3 台服的播报里
    有 1 台没人，不代表这次播报没意义。真正没信息量的只剩「全是😵」。

    判定「有没有人」用的是 `any(count)` 而**不是** `_counting()` 的求和：代理的 count
    是全群组总人数且被 _counting 排除在外，只挂一台代理的关联会被求和误判成
    「无人在线」—— 可明明有人。
    """
    where = f"「{audience_name}」关联的" if audience_name else "本群关联的"
    if not rows:
        # 正常配置走不到这里（report 关联的 targets 为空时加载期就有警告、循环也不会
        # 收它进 watched）。留着是与 render_summary 的契约对齐，也防将来别处调它。
        return None, "本群没有关联任何服务器，本次跳过"
    reachable = [r for r in rows if r.snap.reachable]
    if not reachable:
        return None, f"{where} {len(rows)} 台服全部探测失败，本次跳过"
    if not any(r.snap.count for r in reachable):
        return None, f"{where} {len(rows)} 台服现在都没人在线，本次跳过"
    return (
        _fit(
            lambda budget: _assemble_report(rows, budget, all_targets, now),
            name_budget(total_names, len(rows)),
        ),
        "",
    )


def _assemble_report(
    rows: Sequence[Row],
    budget: int,
    all_targets: Sequence[ServerTarget],
    now: datetime | None,
) -> str:
    counted = _counting(rows)
    if counted:
        head = f"现在有 {sum(r.snap.count for r in counted)} 位小伙伴在线（{len(counted)} 台服）"
    else:
        # 本条关联里只有代理（它的人数不能与分服相加，见 _counting），没有可报的合计
        # 数字。写「0 位小伙伴在线」会是**假话** —— 代理块里明明写着有人在。
        head = "现在的在线情况"

    lines = [f"📣 MC 播报 · {(now or datetime.now()):%H:%M}", "━━━━━━━━━━", head, ""]
    # 块之间不留空行：播报常常是连续几台「在线 N 人」的一行块，逐个空行会把它拉成
    # 一屏，而这几行本该一眼扫完。跨组的虚线也不给自己加空行（见 _block_lead）。
    prev_group: str | None = None
    for row in rows:
        lines.extend(_block_lead(row, prev_group))
        lines.extend(_summary_block(row, budget))
        prev_group = row.target.group
    lines.append("")
    lines.append("想一起玩的，直接进服找他们～")

    note = _footnote(rows, all_targets)
    if note:
        # 代理总数与子服之和对不上，在播报里比在总览里更该看到（播报是主动推的，
        # 群里的人没有别的渠道去对账）
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


# ---------------------------------------------------------------- 白名单名单

@dataclass(frozen=True)
class WhitelistListRow:
    """多台白名单里的一台。

    刻意只吃**纯数据**，不吃 mcadmin.AdminResult：`_shared/mcadmin.py` 顶层
    `from nonebot.log import logger`，本模块 import 它就把 nonebot 拖进那条必须在
    `nonebot.init()` 之前跑通的路（tools/mc_check.py 的自测）—— 与 mcdelta 放
    _shared 是同一条硬约束。
    """

    name: str
    names: tuple[str, ...] | None = None  # None = 这台的名单没读到
    note: str = ""  # 没读到的原因（进块内，与「当前没有玩家」不是一回事）


def render_whitelist_list(
    rows: Sequence[WhitelistListRow], *, total_names: int
) -> tuple[str, bool]:
    """本群多台白名单服一次列出。返回 `(文案, 是否被截断)`。

    排版与总览/播报同一套（`【服名】` + 两空格缩进），因为群里 `@bot mc` 就长这样，
    同一台机器人的几种输出不该各写各的。

    走同一个 `name_budget()` 与 `_fit()`：3 台 × 50 个名字正好是 MAX_LEN 会踩线的
    规模，而 `truncate()` 切的是尾部 —— 切掉的正是最后几台的名单，消息看着完全正常。
    所以被截断必须**返回**给调用方去打 warning（渲染层不许记日志，与 format_events /
    render_report 同一约定）。
    """
    total = sum(len(r.names) for r in rows if r.names)
    head = f"📋 MC 玩家白名单（{len(rows)} 台服）：" if len(rows) > 1 else "📋 MC 玩家白名单："

    def build(budget: int) -> str:
        lines = [head]
        for row in rows:
            if row.names is None:
                # 这台读不到**不影响其余台**照常列出：部分成功必须报出来，
                # 整条回「执行失败」会把已经查到的两台一起丢掉。
                lines.append(f"【{row.name}】⚠️ {row.note or '名单没读到'}")
                continue
            lines.append(f"【{row.name}】（{len(row.names)} 人）")
            shown = row.names[:budget]
            lines.extend(f"  • {n}" for n in shown)
            if len(row.names) > len(shown):
                lines.append(f"  …还有 {len(row.names) - len(shown)} 人")
        return "\n".join(lines)

    budget = name_budget(total_names, len(rows))
    # 「有没有被截断」按理想额度先渲染一遍判断，**不**去看 _fit 的返回值：_fit 认输时
    # 返回的是 truncate 过的短文本，长度反而正常，照长度判断会一律报「没截断」。
    clipped = len(build(budget)) > MAX_LEN
    return _fit(build, budget), clipped


# ---------------------------------------------------------------- 进服提醒

# 一轮内进服人数达到这个数就合并成一条，避免刷屏
_BURST = 3

# **进服那一行只有这一份模板**，单事件与合成排版都从它来。
# 拆成「有服名」和「无服名」两套的后果是两边迟早只改一边，同一个动作在
# 群里两种说法。
#
# {where} 展开成「 Bingo」或空串（空串时服名已经写在【】块头里）——
# 因为它在句子中间，用「拼完再删掉服名」那种做法会留下多余空格。
_JOIN_TEMPLATES = [
    "🎮 {who} 加入了{where}（当前 {count} 人在线）",
    "🚪 {who} 溜进了{where}（当前 {count} 人在线）",
    "⛏️ {who} 上线了{where}（当前 {count} 人在线）",
    "🌍 {who} 出现在了{where}（当前 {count} 人在线）",
]


def _who(names: Sequence[str]) -> str:
    """一批进服玩家的称呼。1 人直呼其名、≤3 人顿号连接、再多就只报前两个 + 总数。"""
    if len(names) == 1:
        return names[0]
    if len(names) <= _BURST:
        return "、".join(names)
    return f"{names[0]}、{names[1]} 等 {len(names)} 人"


def _join_line(who: str, count: int, server_name: str = "") -> str:
    """进服那一行。server_name 留空 = 服名由【】块头承担（合成排版）。"""
    return random.choice(_JOIN_TEMPLATES).format(
        who=who, where=f" {server_name}" if server_name else "", count=count
    )


def format_events(
    events: Sequence[PlayerEvent | StatusEvent],
    targets: Sequence[ServerTarget],
    *,
    now: datetime | None = None,
) -> tuple[str | None, bool]:
    """把一轮（或几轮合并后）的事件拼成**一条**推给一个群的消息。

    返回 `(文案, 是否被截断)`。文案为 None = 什么都不发。第二个返回值是给调用方打
    日志用的 —— 渲染层不许 import nonebot（见模块 docstring），而「被截断」这件事
    必须让人知道：切掉的正是最后几台服的事件，消息看着却完全正常。

    **退服不推**（沿用既有产品决策）：leave 事件在对账层（mcdelta）照常产出，
    在这里被丢掉。哪天要开退服提醒，改的是这里，不是对账层。

    两类排版，规则只有一条：

    > 整条消息只有 1 个事件、且它不是「换服」时，逐字沿用旧文案；
    > 其余一律走【服名】块 + 两空格缩进行的合成排版。

    这样让 DEPLOY / README 引用的那三句（`🎮 X 加入了 Bingo（当前 3 人在线）`、
    `⚠️ Bingo 连不上了（已连续 2 轮探测失败）`、`✅ Bingo 已恢复，当前 5 人在线`）
    一字不改地继续成立，而它们是 99% 的情况 —— 为一个人进服这种最常见的事套一层
    排版，是拿最常见的情况去迁就最少见的。换服（switch）是本期的全新事物，没有旧
    文案要保，所以无论几个事件都走合成排版。
    """
    shown = [
        e for e in events if not (isinstance(e, PlayerEvent) and e.kind == KIND_LEAVE)
    ]
    if not shown:
        return None, False

    name_of = {t.id: t.name for t in targets}

    if len(shown) == 1:
        solo = _solo_text(shown[0], name_of)
        if solo is not None:
            return solo, False

    text = _composite(shown, targets, name_of, now)
    if len(text) > MAX_LEN:
        return truncate(text), True
    return text, False


def _name_of(target_id: str, name_of: dict[str, str]) -> str:
    """目标 id → 服名。查不到就直接拿 id 当名字 —— 见 _composite 里那段说明。"""
    return name_of.get(target_id, target_id)


def _solo_text(event: PlayerEvent | StatusEvent, name_of: dict[str, str]) -> str | None:
    """单个事件时的旧文案。返回 None = 这个事件没有旧文案，得走合成排版。"""
    name = _name_of(event.target_id, name_of)
    if isinstance(event, StatusEvent):
        if event.down:
            # 「答了但答不对」（多半是刚启动还在加载）和「连不上」分开说：
            # 一律叫「连不上了」会让人去开服，而它其实正开着。
            return f"⚠️ {name} {event.why}（已连续 {event.streak} 轮探测失败）"
        return f"✅ {name} 已恢复，当前 {event.count} 人在线"
    if event.kind == KIND_JOIN:
        return _join_line(event.player, event.count, name)
    return None  # 换服


def _composite(
    events: Sequence[PlayerEvent | StatusEvent],
    targets: Sequence[ServerTarget],
    name_of: dict[str, str],
    now: datetime | None,
) -> str:
    buckets: dict[str, list[PlayerEvent | StatusEvent]] = {}
    for event in events:
        buckets.setdefault(event.target_id, []).append(event)

    order = {t.id: i for i, t in enumerate(targets)}
    # 事件落在 targets 之外（调用方传错了目标表）时**不丢**，排在最后、直接拿 id 当服名。
    # 丢掉的话症状是「某个群少收到一台服的提醒」，而消息本身看着完全正常。
    ids = sorted(buckets, key=lambda tid: (order.get(tid, len(order)), tid))

    lines = [f"🎮 MC 动态 · {(now or datetime.now()):%H:%M}"]
    for tid in ids:
        # 只有出过事件的目标才有块 —— 摆一排「今天没人进服」的空块会把真正的那台埋掉
        lines.append(f"【{_name_of(tid, name_of)}】")
        lines.extend(_block_lines(buckets[tid], name_of))
    return "\n".join(lines)


def _block_lines(
    events: Sequence[PlayerEvent | StatusEvent], name_of: dict[str, str]
) -> list[str]:
    """一个【服名】块里的行。行序：换服 → 进服 → 状态跃迁。

    到达类在前（「谁来了」是这条消息存在的理由）。leave 排哪一行无从谈起 ——
    它在 format_events 入口就被丢掉了，从来不渲染。
    """
    lines: list[str] = []

    # 换服每条单独一行（来源可能各不相同），玩家名排序让同一份输入永远同一份输出
    for event in sorted(
        (e for e in events if isinstance(e, PlayerEvent) and e.kind == KIND_SWITCH),
        key=lambda e: e.player,
    ):
        origin = _name_of(event.origin_id, name_of)
        lines.append(f"  🔄 {event.player} 从「{origin}」换服过来（当前 {event.count} 人在线）")

    joins = [e for e in events if isinstance(e, PlayerEvent) and e.kind == KIND_JOIN]
    if joins:
        # 人数取**最后一个** join 的：限流窗口把几轮并到一起时，后者才是最新的快照。
        # 名字排序是为了输出稳定 —— 顺序每轮乱跳的话，同一条消息看着像新的一条。
        lines.append(f"  {_join_line(_who(sorted(e.player for e in joins)), joins[-1].count)}")

    for event in (e for e in events if isinstance(e, StatusEvent)):
        if event.down:
            lines.append(f"  ⚠️ {event.why}（已连续 {event.streak} 轮探测失败）")
        else:
            lines.append(f"  ✅ 已恢复（当前 {event.count} 人在线）")

    return lines
