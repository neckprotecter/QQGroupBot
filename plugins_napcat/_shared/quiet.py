"""夜间静默时段：读配置 + 跃迁日志，MC 与 oopz 共用。

**为什么不并进 schedule.py**：那个模块不许 import nonebot（tools/mc_check.py 在
`nonebot.init()` 之前就 import 它，多一个顶层依赖整个诊断脚本就起不来，那条约束写在
它自己的 docstring 与 tools/README 里）。本模块要用 logger，所以单独一个文件。
`parse_quiet_hours` / `in_quiet_hours` 那套纯逻辑仍在 schedule.py 里，两边共用同一份。

**语义（两个插件必须一致）**：静默只掐「发消息」，不掐「取数」。取数照常跑、基线照常
推进 —— 否则静默一结束，会把整晚发生的事当成「刚刚发生」一次性补报出来。这条在两个
插件的循环里各自实现（MC 是 `_tick_audience`，oopz 是 `_check_joins`），但判据都来自
这里，以免两个插件的静默边界慢慢长歪。
"""
import os

from nonebot.log import logger

from .schedule import in_quiet_hours, parse_quiet_hours


class QuietWindow:
    """一个插件的夜间静默时段。**在 import 期建实例**（那时读一次环境变量）。

    环境变量在进程活着的期间不会变，所以读一次就够；也因此改配置必须重启机器人
    —— 和本项目其它 `.env` 项一样。

    配错了（`MC_QUIET_HOURS=0-9-5` 这种）**不抛异常**，降级成「不静默」并在启动日志里
    吼一声：为一个手滑的 `.env` 让整只机器人起不来（连着查询、白名单一起没）代价太大。
    """

    def __init__(
        self,
        key: str,
        label: str,
        what: str,
        extra: str = "",
        default: str = "0-9",
    ) -> None:
        self.key = key
        self.label = label
        # 「不发什么」与额外的补充说明，拼日志用。两个插件掐掉的东西不一样（MC 是进服/
        # 换服提醒，oopz 是进频道欢迎），所以由调用方给，而不是在这里猜。
        self.what = what
        self.extra = extra

        raw = os.environ.get(self.key, default)
        self.raw = raw
        try:
            self.window = parse_quiet_hours(raw)
        except ValueError as exc:
            self.window = None
            logger.error(
                "{} 配置有误（{}={!r}），夜间静默已关闭：{}", label, key, raw, exc
            )
            return

        if self.window is not None:
            logger.info(
                "{} 夜间静默时段：{} 点（{}={}）—— 不发{}{}",
                label,
                self.span,
                key,
                raw,
                what,
                extra,
            )
        # 跃迁日志的状态（见 note）。None = 还没判断过，避免第一次调用就打一条
        # 「静默结束」——一个从没开始过的时段「结束」了，纯属误导。
        self._quiet: bool | None = None

    @property
    def enabled(self) -> bool:
        """配了静默时段吗（没配 / 配错 / 留空都是 False）。"""
        return self.window is not None

    @property
    def span(self) -> str:
        """给日志用的「0-9 点」；没启用时是「未启用」。"""
        return f"{self.window[0]}-{self.window[1]}" if self.window else "未启用"

    def active(self) -> bool:
        """现在是否处于静默时段，并在**跃迁那一刻**打一行日志。

        按**小时**判断，不看分钟，所以「09:00 整那一班要照常发」这条是靠半开区间
        [0, 9) 保证的（见 schedule.py 的注释），不是靠这里的特判。
        """
        quiet = in_quiet_hours(self.window)
        self.note(quiet)
        return quiet

    def note(self, quiet: bool) -> None:
        """记下当前状态，只在**跃迁**时打日志（进入/结束各一行，不会每次轮询都刷）。

        两个循环共用一个实例时，谁先看到跃迁谁打，另一处不会再打一遍。
        """
        if not self.enabled or quiet == self._quiet:
            return
        self._quiet = quiet
        if quiet:
            logger.info(
                "进入 {} 夜间静默（{} 点）：不发{}{}",
                self.label,
                self.span,
                self.what,
                self.extra,
            )
        else:
            logger.info("{} 夜间静默结束，{}恢复", self.label, self.what)
