"""MC 进服提醒 + 定时播报（NapCat / OneBot v11 版）。

两个后台循环，模式沿用 oopz/auto_reporter.py：@driver.on_startup 起 task，
推送走 _shared/push 的 send_to_groups，整点对齐走 _shared/schedule。

只推进服、不推退服（用户选择）。退服的人仍会被基线自然吸收，只是不产生消息 ——
「不推」是 mcrender.format_events 里那一次过滤，对账层（mcdelta）照样算得出退服。

**按群关联分桶（P7）**：每条写了 `watch = true` / `report = true` 的 [[audience]]
各自一份状态、各自探自己关联的那几台服、各自合成**一条**消息推给自己的 groups。
推送目标恒为本条的 groups —— 所以「哪个群收推送」这件事在配置里一眼可见，
不会出现「.env 里指了一个没写进 mcs_audiences.toml 的群 → 循环停摆」那种
（P7 之前就是这样，且只在日志里说一声）。那种配置现在**在结构上不可能存在**。

一轮只探一次：所有开 watch 的关联覆盖到的目标先去重、并成一批并发探完，再把快照
扇出给各关联对账。按关联逐个探会从「并发取 max」退化成「并发 + 串行叠加」，
一轮耗时上界模型（round_budget）当场失效，同一台服的取数状态也会按关联数重复打印。

.env 配置（**只剩节奏与总开关**，「哪个群收推送」在 mcs_audiences.toml 里）：
  MC_WATCH_INTERVAL_SEC=10      轮询间隔（秒），决定进服被发现的延迟
  MC_JOIN_MIN_INTERVAL_SEC=15   进服推送最小间隔（秒），窗口内的进服合并成一条；
                                设 0 = 不限流。最大额外延迟 ≈ 本值 + 一个轮询间隔
  MC_NOTIFY_SERVER_STATE=true   服务器连不上 / 恢复时是否提醒
  MC_REPORT_INTERVAL_MIN=60     播报间隔（分钟），整点对齐；无人在线时静默跳过
  MC_QUIET_HOURS=0-9            夜间静默时段（默认 0-9 = 00:00–08:59 不发**任何**
                                非故障消息）；留空 = 不静默，支持跨午夜（23-7）
  MC_WATCH_GROUP / MC_REPORT_GROUP  **已作废**（P5.6/P7 搬进关联文件里了）
"""
import asyncio
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from nonebot import get_driver
from nonebot.log import logger

from .._shared.mc import SLP_UNPARSEABLE, McSnapshot
from .._shared.mcaudiences import Audience, default_config
from .._shared.mcdelta import PlayerEvent, StatusEvent, reconcile
from .._shared.mcrender import Row, format_events, render_report
from .._shared.mcservers import ServerConfigError, ServerTarget
from .._shared.push import send_to_groups
from .._shared.quiet import QuietWindow
from .._shared.schedule import seconds_until_slot
from .client import _MAX_NAMES, get_snapshots

driver = get_driver()

_WATCH_INTERVAL_SEC = float(os.environ.get("MC_WATCH_INTERVAL_SEC", "10"))
# 限流窗口 = 进服提醒的最大额外延迟（最坏 = 本值 + 一个轮询间隔）。设 0 = 不限流，
# 进服立刻推；一轮内进服 ≥_BURST 人仍会合并成一条，不会因去掉限流而刷屏。
_JOIN_MIN_INTERVAL_SEC = float(os.environ.get("MC_JOIN_MIN_INTERVAL_SEC", "15"))
_NOTIFY_SERVER_STATE = os.environ.get("MC_NOTIFY_SERVER_STATE", "").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_REPORT_INTERVAL_MIN = int(os.environ.get("MC_REPORT_INTERVAL_MIN", "60"))

# 夜间静默：这段时间里**不发进服/换服提醒、不做定时播报**（用户 2026-09-26 要求）。
# 默认 0-9 是刻意的 —— 需求本身就是「半夜别响」，默认关掉等于没做；
# 不要静默就显式写 MC_QUIET_HOURS=（空值）。
#
# **掉线/恢复提醒不受它管**（见 _tick_audience 的 quiet 分支）：那是故障通知，
# 夜里服务器真挂了，早上才发现比吵一下贵得多。同理，探测本身一轮都不停 ——
# 静默只掐「发消息」，不掐「取数」，否则夜里掉线会被算成「刚开机时整服的人一起进服」。
# 读配置、打启动与跃迁日志这套在两个插件（MC / oopz）里是同一份实现，见 _shared/quiet.py。
# 「不发什么」和那句补充说明由这里给 —— 掉线/恢复提醒不受静默管是 MC 特有的安排
# （oopz 没有故障通知这一路），所以写在这一侧而不是共享模块里。
_QUIET = QuietWindow(
    "MC_QUIET_HOURS",
    "MC",
    "进服/换服提醒与定时播报",
    extra="；服务器掉线/恢复提醒不受影响",
)

# 连续这么多轮探测失败才宣布服务器离线（单轮网络抖动不报）
_OFFLINE_THRESHOLD = 2

# ---------------------------------------------------------------- 状态

# 三层状态，键就是语义。混在一起的后果是「某个群看到的进服」和「某台服的连通性」
# 互相污染 —— 后者尤其致命：连通性是**服务器的事实**，同一台服不可能对 A 群连着、
# 对 B 群断着。放进 audience 桶会让两条关联对同一台服给出互相矛盾的结论，
# 而两条都会推给各自的群。


@dataclass
class _TargetState:
    """一台服的连通性。**按 target 分桶，不按 audience。**"""

    fail_streak: int = 0
    down: bool | None = None  # None = 启动后还没探测过
    last_state: tuple | None = None  # 取数状态，仅用于日志去重


@dataclass
class _AudienceState:
    """一个群看到的世界的账本。**按 audience 分桶。**"""

    # target_id -> 基线（上一轮的名单）。粒度是 per-(关联, 目标)：某台服这轮名单不全
    # 时只冻结它自己，同一关联里另一台照常对账 —— per-关联一份基线做不到这件事。
    #
    # **没有单独的 _initialized**：「known 里有没有那个 target_id」就是它。留一个能
    # 和字典不一致的独立状态，迟早出现「标志说初始化过了、字典里却没有」的中间态，
    # 而它的表现正是「整服的人被报成刚进服」。
    known: dict[str, frozenset[str]] = field(default_factory=dict)
    # 限流窗口内攒下的事件。元素是**事件**不是玩家名：跨轮合并时还得知道那件事发生在
    # 哪台服、是换服还是进服、来源是哪台。只存名字，合并后的消息就会丢掉服名前缀与来源
    # —— 而「从『谁是杀手』换过来」正是这条消息最该说的事。
    pending: list[PlayerEvent | StatusEvent] = field(default_factory=list)
    # 每条关联**各自一个窗口**：消息按关联合成，A 的推送节奏不该被 B 的进服带偏。
    # 全局一份时 B 的人进服会占掉 A 的窗口，表现是「A 群的提醒时快时慢，看不出规律」。
    last_push: float | None = None


_targets: dict[str, _TargetState] = {}
_audiences: dict[str, _AudienceState] = {}
_last_cfg_error: dict[str, str] = {}  # 键 = 循环名


def _target_state(target_id: str) -> _TargetState:
    state = _targets.get(target_id)
    if state is None:
        state = _targets[target_id] = _TargetState()
    return state


def _audience_state(audience: Audience) -> _AudienceState:
    """关联的状态桶。

    键用 **`groups[0]`** 而不是关联名：群号唯一是 mcaudiences 里**已有的硬校验**，
    而关联名在同一份配置里也没有唯一性校验（P7 顺手补上了重名报错，但状态不该依赖它）。
    两条都叫「社团群」的关联会静默共用一份基线，症状是两边都在乱报进服且完全不报错。
    """
    key = audience.groups[0]
    state = _audiences.get(key)
    if state is None:
        state = _audiences[key] = _AudienceState()
    return state


def _dedupe(targets: Sequence[ServerTarget]) -> list[ServerTarget]:
    """按 id 去重、保首次出现顺序。

    一轮里同一台服被两条关联引用是常态，不去重就会探两遍（SLP + RCON 各一次，
    MC 服务端为每次探测留日志），日志里的取数状态跃迁也会按关联数重复打印。
    """
    seen: dict[str, ServerTarget] = {}
    for target in targets:
        seen.setdefault(target.id, target)
    return list(seen.values())


# ---------------------------------------------------------------- 日志

def _state_key(snap: McSnapshot, target: ServerTarget) -> tuple:
    """取数状态的可比较形式。**必须带 target**：代理和普通服对「名单不完整」的判法不同。

    代理（serves_names=False）拿不到分服名单是**设计如此，不是「不完整」** —— 见
    mc.py 那两处 `names_complete = target.serves_names and ...`：它对代理恒为 False，
    同时 snap.error 被清空（代理没名单不算故障，判它正常只看 reachable）。所以这里
    若判成 "partial"，每次启动都会打一条永远为假、原因还恒为「原因未知」的告警。

    真正读 names_complete 的三处（mcrender._footnote / mcdelta.reconcile /
    tools/mc_check.py）都先看 serves_names，没有一个把代理当成「已暂停」。
    """
    if not snap.reachable:
        return ("down",)
    if not target.serves_names:
        return ("ok", "n/a")
    if not snap.names_complete:
        return ("partial",)
    return ("ok", snap.names_source)


def _log_state_transition(snap: McSnapshot, target: ServerTarget) -> None:
    """取数状态只在跃迁时打日志。

    每 20 秒一轮的重复告警会把日志冲成噪音，而 DEPLOY.md 恰恰教用户看日志尾巴
    排查问题。这里只报「变好」「变坏」两个时刻。

    **必须带服名**：一轮现在打多台，「MC 名单来源 rcon（完整，3 人在线）」这种
    不带主语的日志在多目标下等于没打 —— 看日志的人不知道是哪台在报。
    """
    state = _target_state(target.id)
    key = _state_key(snap, target)
    if key == state.last_state:
        return
    state.last_state = key
    if key[0] == "ok":
        if target.serves_names:
            logger.info(
                "MC {} 名单来源 {}（完整，{} 人在线）", target.name, snap.names_source, snap.count
            )
        else:
            # 代理的正常态。**别看 snap.names_source** —— 它对代理恒为 "none"，
            # 而上面那句「名单来源 none（完整，N 人在线）」正是要避免的：名单的
            # 有无与「完整」与否对它都不适用（names_complete 恒 False），名册在
            # 它的子服那几台上。判据只认 serves_names 这一处。
            logger.info("MC {} 正常（{} 人在线；这台不出分服名单，名册在它的子服上）",
                        target.name, snap.count)
    elif key[0] == "partial":
        logger.warning(
            "MC {} 名单不完整，它的进服提醒已暂停：{}", target.name, snap.error or "原因未知"
        )
    else:
        logger.warning("MC {} 探测失败：{}", target.name, snap.error or "原因未知")


def _log_config_error(loop: str, text: str) -> None:
    """配置类错误只在**内容变化**时打日志。

    进服循环每 MC_WATCH_INTERVAL_SEC（默认 10）秒跑一次，而配置错误会一直持续到人
    改完重启 —— 每 10 秒刷一条 error 会把日志尾巴冲成噪音，而 DEPLOY 恰恰教人看日志
    尾巴排查问题。状态跃迁式的日志（_log_state_transition）出于同样的理由。

    键是**循环名**：两个循环各有各的配置问题，只留一份的话后打的那条会把前一条顶掉，
    表现是「修好了进服提醒的报错，定时播报的报错跟着一起消失（其实还在）」。
    """
    if text != _last_cfg_error.get(loop):
        _last_cfg_error[loop] = text
        logger.error("{}", text)


def _read_config(loop: str):
    """读配置，失败就记一条去重的 error 并返回 None。两个循环共用。"""
    try:
        config = default_config()
    except ServerConfigError as exc:
        _log_config_error(loop, f"读取 MC 配置失败，{loop}停摆：{exc}")
        return None
    # 读通了就把上次那条错误消掉：不消的话，下次真的又坏了会因为「文案一样」被吞掉
    _last_cfg_error.pop(loop, None)
    return config


# ---------------------------------------------------------------- 进服提醒

def _tick_health(
    by_id: Mapping[str, McSnapshot], targets: Sequence[ServerTarget]
) -> list[StatusEvent]:
    """连通性判定：每台服**一轮只判一次**，产出的跃迁事件供各关联取用。

    语义与单目标时代一字不差：连续 _OFFLINE_THRESHOLD 轮失败才算掉线（单轮网络抖动
    不报）；「答了但答不对」（多半是刚启动还在加载）和「连不上」分开说，因为两者
    给群里人的下一步动作完全相反 —— 一律叫「连不上了」会让人去开服，而它其实正开着。
    MC_NOTIFY_SERVER_STATE 关掉时**照样维护状态**，只是不产事件：关掉提醒不该让
    「已恢复」的判定读到一个假的 down=True。
    """
    events: list[StatusEvent] = []
    for target in targets:
        snap = by_id.get(target.id)
        state = _target_state(target.id)

        if snap is None or not snap.reachable:
            state.fail_streak += 1
            if state.fail_streak >= _OFFLINE_THRESHOLD and state.down is not True:
                state.down = True
                logger.warning("{} 连续 {} 轮探测失败", target.name, state.fail_streak)
                if _NOTIFY_SERVER_STATE:
                    why = (
                        "应答异常"
                        if snap is not None and snap.error_kind == SLP_UNPARSEABLE
                        else "连不上了"
                    )
                    events.append(
                        StatusEvent(
                            target.id, down=True, streak=state.fail_streak, why=why
                        )
                    )
            # 探测失败时**绝不动基线**。旧实现靠「提前 return」保证这件事，现在由
            # mcdelta.reconcile 规则 1 保证 —— 从「小心别写错」升级成结构上做不到。
            continue

        state.fail_streak = 0
        if state.down is True:
            state.down = False
            logger.info("{} 已恢复", target.name)
            if _NOTIFY_SERVER_STATE:
                events.append(StatusEvent(target.id, down=False, count=snap.count))
    return events


async def _tick_audience(
    audience: Audience,
    by_id: Mapping[str, McSnapshot],
    status: Sequence[StatusEvent],
    quiet: bool = False,
) -> None:
    """一条群关联：对账 → 攒事件 → 到了窗口就合成一条推给它自己的 groups。

    quiet（夜间静默）只掐**进服/换服**这一路，见下面 pending 那两行。
    """
    state = _audience_state(audience)
    # 顺序用 targets_primary_first：[[audience]].primary 那台排最前，其余按配置顺序。
    # 它同时是 reconcile 的输出顺序 → 合成消息里各段的顺序，段顺序每轮乱跳的话
    # 同一条消息看着像新的一条。
    ordered = audience.book.targets_primary_first
    my_ids = {t.id for t in audience.book.targets}

    delta = reconcile(
        state.known,
        {tid: by_id[tid] for tid in my_ids if tid in by_id},
        ordered,
    )
    # 整体替换，不做「哪些键该留」的二次判断 —— 那种判断总会在某个分支漏掉，
    # 而漏掉一次的后果正是「探测失败时把基线清空，恢复时整服的人被当成刚进服」。
    #
    # **基线在静默时段照常推进**（就是上面这一行）：夜里进服的人被吸收进基线，
    # 09:00 静默一结束不会一次性把整晚的人当成「刚进服」补报出来。
    state.known = dict(delta.baseline)
    if quiet:
        # 夜间静默：进服/换服不推。顺带把**静默开始前没发出去的那几条也丢掉**
        # （限流窗口里攒着的）—— 补发一条「三小时前有人进服」正是静默要避免的事。
        # 只留 StatusEvent（掉线/恢复），它不受静默影响。
        state.pending = [e for e in state.pending if isinstance(e, StatusEvent)]
    else:
        state.pending.extend(delta.events)
    # 连通性事件按目标过滤：一台服挂了只该提醒**关联了它**的群。
    # 不过滤的后果是没关联那台服的群也会收到它的掉线提醒，而群里的人根本进不去那台服。
    state.pending.extend(e for e in status if e.target_id in my_ids)

    if not state.pending:
        return

    now = time.monotonic()
    # 掉线与恢复**当轮强制 flush**（跳过窗口检查）：它的价值在及时，等 15 秒没意义；
    # 顺带把窗口里攒的进服一起发出去，仍然是「一个周期一条」，不会拖到下一轮。
    urgent = any(isinstance(e, StatusEvent) for e in state.pending)
    # 这个检查必须**每一轮都做**，不能只在「这一轮有人进服」时做 —— 否则「甲进服占满
    # 窗口 → 乙随后进服被攒下 → 之后没人再进服」时，乙那条会一直压着等下一个进服，
    # 没人来就永远发不出；有人来也会被拖到窗口结束、和后面的人合并成一条，
    # 看起来就是「进服提醒延迟很久」。
    if (
        not urgent
        and state.last_push is not None
        and now - state.last_push < _JOIN_MIN_INTERVAL_SEC
    ):
        logger.info(
            "MC 进服提醒限流中（「{}」）：{} 条待推送，窗口还剩 {:.0f}s",
            audience.name,
            len(state.pending),
            _JOIN_MIN_INTERVAL_SEC - (now - state.last_push),
        )
        return

    pending, state.pending = state.pending, []
    text, clipped = format_events(pending, ordered)
    if text is None:
        # 攒了一轮只有退服事件 —— 不发，也**不占掉限流窗口**（没推出去就不算推过）。
        return
    state.last_push = now
    if clipped:
        # 渲染层不许 import nonebot（见 mcrender 的模块 docstring），所以「被截断了」
        # 只能由这里说。这条警告不能省：切掉的正是最后几台服的事件，消息看着完全正常。
        logger.warning(
            "MC 进服提醒超长被截断（「{}」）：排在后面的服务器的事件可能没发出去",
            audience.name,
        )
    sent = await send_to_groups(list(audience.groups), text)
    if sent:
        # **推成功也要打一行**，把发出去的原文一起记下。不打的后果是「进了服但没推」
        # 和「推了」在日志里长得一模一样：人数变化（0→1）不是取数状态跃迁、不打日志，
        # 而这条路径原先是发完就完 —— 排查时只能靠猜。
        logger.info("MC 进服提醒（「{}」）→ {} 个群：\n{}", audience.name, sent, text)
    else:
        # send_to_groups 返回 0 且**没有** error 日志，只有一种可能：一个 bot 都没连上
        # （get_bots() 为空时它静默 return False）。发送异常那条路径自己会打 error。
        logger.warning(
            "MC 进服提醒（「{}」）一条都没发出去（该推 {} 个群）—— "
            "多半是 bot 没连上 NapCat，或它不在这些群里",
            audience.name,
            len(audience.groups),
        )


async def _tick() -> None:
    # 每轮重取配置（lru_cache 命中，成本≈0）。**必须每轮取**：default_config() 刻意
    # 不缓存异常，那个设计就是为了让「配置改好就自愈」成立；在启动时读一次并缓存下来
    # 等于把下面那层的性质吃掉，症状是「配置改好了还得重启，且没人知道为什么」。
    config = _read_config("进服提醒")
    if config is None:
        return

    watched = [a for a in config.audiences if a.watch and a.book.targets]
    if not watched:
        _log_config_error(
            "进服提醒",
            "没有任何 [[audience]] 写 watch = true，进服提醒不会推送任何东西 —— "
            "给要收提醒的那条关联加上 watch = true（改完要重启）",
        )
        return
    _last_cfg_error.pop("进服提醒", None)

    # 全部关联的目标去重后**一轮探一次**，再把快照扇出给各关联对账。
    wanted = _dedupe([t for a in watched for t in a.book.targets])
    snaps = await get_snapshots(wanted, max_age=0)
    by_id = {s.target_id: s for s in snaps}

    for target in wanted:
        snap = by_id.get(target.id)
        if snap is not None:
            _log_state_transition(snap, target)
    status = _tick_health(by_id, wanted)

    # 静默判据每一轮重取：跨过 00:00 / 09:00 那一轮就换挡，不用等重启。
    # active() 顺带在跃迁那一刻打一行日志（两个循环共用一个实例，只会打一次）。
    quiet = _QUIET.active()
    for audience in watched:
        await _tick_audience(audience, by_id, status, quiet=quiet)


async def _watch_loop() -> None:
    # **不因为「没有 watch 关联」而退出**：配置读不了必须能下一轮重试，而「没有任何
    # 关联开 watch」退出与不退出观察上没差别（都要改配置重启才生效）。
    # 空转成本 = 一次 lru_cache 命中的字典扫描。
    await asyncio.sleep(15)  # 等 bot 连上 NapCat
    while True:
        started = time.monotonic()
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("MC 进服检测异常: {}", exc)
        # 从本轮结束时刻起算剩余时间，避免探测耗时（最坏 timeout×2）让间隔漂移
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, _WATCH_INTERVAL_SEC - elapsed))


# ---------------------------------------------------------------- 定时播报

async def _report_tick() -> None:
    config = _read_config("定时播报")
    if config is None:
        return

    watched = [a for a in config.audiences if a.report and a.book.targets]
    if not watched:
        _log_config_error(
            "定时播报",
            "没有任何 [[audience]] 写 report = true，定时播报不会推送任何东西 —— "
            "给要收播报的那条关联加上 report = true（改完要重启）",
        )
        return
    _last_cfg_error.pop("定时播报", None)

    wanted = _dedupe([t for a in watched for t in a.book.targets])
    snaps = await get_snapshots(wanted, max_age=0)
    by_id = {s.target_id: s for s in snaps}

    for audience in watched:
        ordered = audience.book.targets_primary_first
        rows = [Row(t, by_id[t.id]) for t in ordered if t.id in by_id]
        # all_targets 传**全量**表（不是本条的视图）：代理尾注要拿它判断「本群是否
        # 关联到了该代理同组的全部子服」，详见 mcrender._group_complete。
        text, why = render_report(
            rows,
            total_names=_MAX_NAMES,
            all_targets=config.book.targets,
            audience_name=audience.name,
        )
        if text is None:
            logger.info("MC 定时播报：{}", why)
            continue
        sent = await send_to_groups(list(audience.groups), text)
        if sent:
            logger.info(
                "MC 定时播报（「{}」）→ {} 个群（{} 台服）",
                audience.name,
                sent,
                len(rows),
            )
        else:
            # 打 len(audience.groups) 在这里是**谎话**：一个群都没送达时它照样说
            # 「已推送到 1 个群」。发送异常自己会打 error，走到这儿就是 bot 没连上。
            logger.warning(
                "MC 定时播报（「{}」）一条都没发出去（该推 {} 个群）—— "
                "多半是 bot 没连上 NapCat，或它不在这些群里",
                audience.name,
                len(audience.groups),
            )


async def _report_loop() -> None:
    await asyncio.sleep(15)  # 等 bot 连上 NapCat、MC 端口可达
    while True:
        try:
            # 睡到下一个整点槽位；每次从当前时刻重新对齐，不累积漂移
            delay = seconds_until_slot(_REPORT_INTERVAL_MIN)
            if delay:
                await asyncio.sleep(delay)
            # 静默判据用**醒来后的时刻**（= 槽位时刻）：09:00 那一班要照常发，
            # 而 0-9 是半开区间，hour==9 不算静默。
            if _QUIET.active():
                # 每小时打一行（整晚最多 9 行）。这条不能省成「只在跃迁时打」：
                # 群里安静了一整晚之后，「为什么 3 点没播报」全靠日志尾巴回答。
                logger.info("MC 定时播报：夜间静默时段（{}），本次跳过", _QUIET.span)
                continue
            await _report_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("MC 定时播报异常: {}", exc)
            await asyncio.sleep(60)  # 出错别立刻重试，等一分钟再对齐下一次


@driver.on_startup
async def _start_background_tasks() -> None:
    asyncio.create_task(_watch_loop())
    asyncio.create_task(_report_loop())
