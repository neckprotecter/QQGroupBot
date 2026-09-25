"""玩家进出对账：把「上一轮的名单」和「这一轮的快照」差成事件。

进服提醒做的事其实是两步，这个模块只管第一步：

1. **对账**（本模块）：两份名单相减，得出「谁来了 / 谁走了 / 谁从哪台换到哪台」。
   `reconcile()` 是纯函数 —— 不碰网络、不读配置、不看时钟。
2. **播报**（mc_reporter + mcrender）：决定这些事件里哪些推到哪个群、长什么样。

分开的收益是第一步**可以完全离线自测**。它是整条播报链路里唯一「算错就会天天在群里
报假消息」的地方，而它算错的样子（把存量玩家报成刚进服、探测抖一轮就把整服报成集体
离开）在群里和在日志里都看不出来 —— 只能靠钉死的用例挡。

**放 _shared 是硬约束不是风格**：mcs/mc_reporter.py 顶层有 get_driver()，
tools/mc_check.py 在 nonebot.init() 之前 import 它会抛 ValueError。凡是想自测的都得在这儿。

三条最容易写错的地方，每条都有对应用例：

- **不可用的目标本轮不参与对账**。「他不在名单里」和「我们没看到名单」是两回事。
  探测抖一轮就整服报离开、下一轮再整批报加入，真事件会被这堆假的埋掉。
  mc.py 里 `len(names) == SLP 人数` 那道闸门存在的理由就是这个，这里是它第一次真正
  当护栏用 —— 代理（`names_complete` 恒为假）也顺着这条自动落进「不参与」，不是特例。
- **没有基线 ≠ 基线是空的**。新加进某条关联的目标、或机器人刚启动时的目标，只建基线
  不产事件：存量玩家不是刚进服。判定用 `target_id in prev`，**不是** `prev[target_id]`
  真不真 —— 空集是合法基线（那台服真的没人在线），和「字典里没这个键」必须分开。
- **不唯一就不猜**。同一个名字上一轮要是同时出现在同组的两台服上，「他从哪台来」没有
  唯一答案，降级成普通进服。全工程的原则：解析不出唯一答案时绝不猜。
"""
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .mc import McSnapshot
from .mcservers import ServerTarget

KIND_JOIN = "join"
KIND_LEAVE = "leave"
KIND_SWITCH = "switch"


@dataclass(frozen=True)
class PlayerEvent:
    """一个玩家的一次进出。"""

    kind: str
    player: str
    # 到达/发生地：JOIN 落在进的那台，LEAVE 落在走的那台，SWITCH 落在**到达**的那台。
    # 统一成「这件事发生在哪台服」，对账、排序、限流都只认这一个字段 —— 文案层也因此
    # 按它分段，而「玩家最后待在哪台」正是群里该看到的位置（SWITCH 额外把 origin_id
    # 写进行里，「从「A」换到「B」」）。
    target_id: str
    # 仅 SWITCH 有值：从哪台来。JOIN / LEAVE 恒为空 —— 跨组移动会被拆成互不相干的
    # join 和 leave，「他从哪来」在那时是**猜**出来的（他可能先退了 A、去吃了饭、
    # 再进 B），而本项目不猜。文案层因此只需在 origin_id 非空时说「从「X」换到「Y」」。
    origin_id: str = ""
    # 事件发生时该目标的人数，取**到达端**本轮快照（换服时说走的那台有几个人没人
    # 关心，他在的那台才有）。**文案层从 2026-09-25 起不再渲染它**：动态消息一个数字
    # 都不报，人数只在定时播报与 @mc 查询里出现，而那两处用的是快照自己的 count。
    # 字段留着是为了让对账层的产出保持完整（同 leave 事件那条理由），改回「进服报
    # 人数」不用回来加字段。
    count: int = 0


@dataclass(frozen=True)
class StatusEvent:
    """一台服的连通性跃迁（掉线 / 恢复）。

    主体是**服务器**不是玩家，所以单独一个类型，而不是硬塞进 PlayerEvent 把
    `player=` 填成「服务器」之类的假名字。文案、排序、限流三处都要按这个区别分支
    （状态事件当轮强制 flush，不进限流窗口），类型分开比在字段上做约定可靠。
    """

    target_id: str
    down: bool  # True = 掉线，False = 恢复
    # streak / count **文案层从 2026-09-25 起都不渲染**（理由同 PlayerEvent.count）：
    # 掉线只说「连不上了」、恢复只说「已恢复」。留着是给日志和排查用的。
    streak: int = 0  # 掉线时连续失败了几轮
    count: int = 0  # 恢复时该服当前人数
    why: str = ""  # "连不上了" / "应答异常"，仅 down=True 时有意义。文案层照用它


# 待推送队列里的元素。两者混在一个列表里（而不是分成两个队列），是因为播报要按
# 周期**合成一条消息** —— 拆两个队列就得在发送时再按时间归并一次，多一份状态。
Event = PlayerEvent | StatusEvent


@dataclass(frozen=True)
class Delta:
    """一轮对账的产出。

    baseline 是**新一轮的完整基线**，调用方 `state.known = dict(delta.baseline)`
    整体替换。刻意不做「哪些键该留」的二次判断：那种判断总会在某个分支漏掉，而漏掉
    一次的后果正是「探测失败时把基线清空，恢复时整服的人被当成刚进服」。
    """

    events: tuple[PlayerEvent, ...] = ()
    baseline: Mapping[str, frozenset[str]] = field(default_factory=dict)


def reconcile(
    prev: Mapping[str, frozenset[str]],
    snaps: Mapping[str, McSnapshot],
    targets: Sequence[ServerTarget],
) -> Delta:
    """把上一轮基线 prev 与本轮快照 snaps 差成事件。

    prev 里**没有**某个 target_id 的键 = 那台服还没建过基线（新加进配置，或机器人
    刚启动）。空集表示「有基线，且当时没人在线」—— 两者语义不同，见模块 docstring。

    targets 的顺序**有意义**：它就是事件的输出顺序，而顺序不稳会让同一条消息每轮
    换位置、自测也没法逐项钉死。调用方传 `targets_primary_first`。
    """
    by_id = {t.id: t for t in targets}

    # 本轮「可用」的目标：既答了、名单又是全的。只有两种都满足才敢拿它的名单去和上
    # 一轮相减 —— 缺任何一个都会把「我们没看到」算成「他走了」。
    current: dict[str, frozenset[str]] = {}
    for target in targets:
        snap = snaps.get(target.id)
        if snap is None or not snap.reachable or not snap.names_complete:
            continue
        current[target.id] = frozenset(snap.names)

    baseline: dict[str, frozenset[str]] = {}
    for target in targets:
        if target.id in current:
            baseline[target.id] = current[target.id]
        elif target.id in prev:
            # 不可用 → **原样冻结**。这是整个模块最关键的一行：改成
            # `baseline[t.id] = frozenset()` 的后果是探测抖一轮就把整服清空，
            # 下一轮恢复时所有人被报成刚进服。
            baseline[target.id] = prev[target.id]
    # 只遍历 targets：prev 里那些**已被移出配置**的目标不进新基线。基线跟着配置走，
    # 删掉的服不该在字典里留一条永远冻结的残影（它既不会被探测，也不会被清掉）。

    # 逐目标求到达/离开，但**先不发事件** —— 换服横跨两台服，而事件是按目标顺序发
    # 的。边遍历边发就得在两台服各自的循环里判断「这件事归谁发」，很容易发两次或
    # 漏发；先算完归属，再按目标顺序统一发。
    arrivals: dict[str, list[str]] = {}
    departures: dict[str, list[str]] = {}
    for tid, names in current.items():
        before = prev.get(tid)
        if before is None:
            continue  # 没有基线：只建基线，零事件。存量玩家不是刚进服。
        for name in names - before:
            arrivals.setdefault(name, []).append(tid)
        for name in before - names:
            departures.setdefault(name, []).append(tid)

    moved: dict[tuple[str, str], str] = {}  # (到达端 tid, 玩家) -> 来源 tid
    taken: set[tuple[str, str]] = set()  # (离开端 tid, 玩家)，已被换服消费掉
    for name in sorted(set(arrivals) & set(departures)):
        dests, srcs = arrivals[name], departures[name]
        if len(dests) != 1 or len(srcs) != 1:
            # 到达端或来源端不唯一：同一个名字上一轮同时出现在同组两台服上
            # （跨服同名账号，或上一轮数据本身有残留）。不猜是哪台，退化成普通进服
            # + 各自的离开 —— 报一条来源错误的「从 X 换服过来」比不报更坏。
            continue
        dest, src = dests[0], srcs[0]
        if by_id[dest].group != by_id[src].group:
            # 跨组：他确实断线重连了，但「从哪来」是猜的（可能先退了 A 去吃饭，
            # 过一小时才进 B）。拆成互不相干的 join + leave，而 leave 不推 ——
            # 群里只看到「加入了 B」，不叙述一段我们并不知道的行程。
            continue
        moved[(dest, name)] = src
        taken.add((src, name))

    events: list[PlayerEvent] = []
    for target in targets:
        tid = target.id
        if tid not in current:
            continue
        before = prev.get(tid)
        if before is None:
            continue
        snap = snaps[tid]
        arrived = sorted(current[tid] - before)
        left = sorted(before - current[tid])
        # 块内顺序：换服 → 进服 → 离开。到达类在前（「谁来了」是这条消息存在的理
        # 由），离开放最后 —— 「小明离开了」不该压在「小明换服过来」上面。
        for name in [n for n in arrived if (tid, n) in moved]:
            events.append(
                PlayerEvent(KIND_SWITCH, name, tid, origin_id=moved[(tid, name)], count=snap.count)
            )
        for name in [n for n in arrived if (tid, n) not in moved]:
            events.append(PlayerEvent(KIND_JOIN, name, tid, count=snap.count))
        for name in [n for n in left if (tid, n) not in taken]:
            # leave 照常产出：对账层保持完整、可自测，将来要开退服提醒不用回来改这里。
            # 「不推 leave」是**文案层**的决定（mcrender.format_events），不是这里过滤掉的。
            events.append(PlayerEvent(KIND_LEAVE, name, tid, count=snap.count))

    return Delta(events=tuple(events), baseline=baseline)
