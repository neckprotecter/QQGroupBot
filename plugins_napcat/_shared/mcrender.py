"""MC 消息渲染：纯文本拼装，**不 import nonebot、不 import mcs/**。

`@查询`（mcs/mc_stats.py）与进服提醒/定时播报（mcs/mc_reporter.py）共用同一份格式，
所以它必须住在这里，而不是任一调用方里 —— 否则「总览」和「播报」的排版会各自漂移，
同一个服在两个地方显示成两种样子。

不许 import nonebot 是硬约束：tools/mc_check.py 直接 import 这个模块做离线自测，
那条路必须在 nonebot.init() 之前就能走通。长度上限因此来自 textlen.py 而不是 push.py。
"""
from collections.abc import Sequence
from dataclasses import dataclass

from .mc import SLP_UNPARSEABLE, McSnapshot
from .mcservers import ServerTarget
from .textlen import MAX_LEN, truncate


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
        return [f"{label}😵 不可达"]

    tail = f"　延迟 {snap.latency:.0f}ms" if snap.latency is not None else ""
    if not target.serves_names:
        # 单独标出「全群组」：它和下面的分服数字不是一个口径，混着看会以为重了
        return [f"{label}全群组 {snap.count} 人{tail}（代理，不出分服名单）"]

    body = snap.count
    if snap.max_players:
        body = f"{snap.count}/{snap.max_players}"
    if snap.count == 0:
        return [f"{label}在线 {body}{tail}　目前无人"]

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


def _footnote(rows: Sequence[Row]) -> str:
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
    proxies = [r for r in rows if not r.target.serves_names and r.snap.reachable]
    # 只有一台子服时「代理总人数」和「子服之和」在两种成因下都相等，说什么都是误导
    if proxies and len(counted) >= 2:
        total = sum(r.snap.count for r in counted)
        for p in proxies:
            if p.snap.count != total:
                notes.append(
                    f"⚠️ {p.target.name} 报全群组 {p.snap.count} 人，各子服合计 {total} 人。"
                    f"有子服掉线时这正常；都在线则可能是代理开了 ping-passthrough"
                    f"（那样它报的其实是某一台后端的人数）。"
                )

    return "\n".join(notes)


def _assemble(rows: Sequence[Row], budget: int) -> str:
    counted = _counting(rows)
    total = sum(r.snap.count for r in counted)
    down = sum(1 for r in rows if not r.snap.reachable)

    head = f"🗺️ MC 在线总览：{total} 人"
    if counted:
        head += f"／{len(counted)} 台服"
    if down:
        head += f"（{down} 台探测失败）"

    lines = [head]
    for row in rows:
        lines.append("")
        lines.extend(_summary_block(row, budget))

    note = _footnote(rows)
    if note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def render_summary(rows: Sequence[Row], *, total_names: int) -> str:
    """总览（`@bot mc` 不带服名）：每个目标一行，各自带一小段名单。"""
    if not rows:
        return "⚠️ mcs_servers.toml 里没有配置任何目标。"

    budget = name_budget(total_names, len(rows))
    # 先按额度渲染；超长就**逐行减少每个目标的名额**重渲染，而不是从头截断。
    # 从头截断切掉的是尾部 = 最后几台服 + 尾注，切完消息看着完全正常，
    # 只是悄悄少了「这些数对不上」那句 —— 而这句恰恰是总览里最该看到的。
    #
    # 一次只减 1 是刻意的：按「一行大约多少字」去估算会大幅过冲（名字长短差很多，
    # 实测过一版估算式，10 个名额被一步打到 0，结果是**超长时一个人名都不显示**）。
    # 渲染只是拼字符串，多试几次不值钱，换来的是「能显示多少就显示多少」。
    while budget > 0:
        text = _assemble(rows, budget)
        if len(text) <= MAX_LEN:
            return text
        budget -= 1
    # 名额清零还是超长（目标极多）才认输截断，此时尾注也只能让位给服名
    return truncate(_assemble(rows, 0))
