"""整点对齐的时间槽工具 + 夜间静默时段。

从 oopz/auto_reporter.py 的 _seconds_until_broadcast_slot() 抽出并参数化，
供 oopz 播报与 mc 播报共用。静默时段（quiet hours）见文件末尾。
"""
from datetime import datetime, timedelta


def seconds_until_slot(interval_min: int) -> float:
    """距下一个整点槽位的秒数：以当天 00:00 为起点，落在分钟数能被 N 整除的时刻。

    N=30 → :00 与 :30；N=60 → 每小时整点；N=15 → :00/:15/:30/:45。
    用它替代固定 sleep，避免启动时间不同导致播报时刻漂移、永远对不齐整点。
    """
    minutes = max(int(interval_min), 1)  # 夹到 >=1：0 或负数会让下面的整除炸掉
    now = datetime.now()
    next_slot = ((now.hour * 60 + now.minute) // minutes + 1) * minutes
    target = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=next_slot
    )
    return max((target - now).total_seconds(), 0.0)


# ---------------------------------------------------------------- 夜间静默

# 静默时段是**半开区间 [start, end)**：start 那一点算静默、end 那一点不算。
# 这个选择只在一个地方看得出来、但很要紧：设 0-9 时 **09:00 整那一班播报要照常发**，
# 因为它正是「夜里过去了」的第一时刻。写成闭区间就得靠 `>= end` 之类的特判来救，
# 而那种特判迟早被下一处调用点漏掉。
#
# 支持跨午夜：start > end（如 "23-7"）表示 23:00 到次日 07:00。


def parse_quiet_hours(text: str) -> tuple[int, int] | None:
    """把 "0-9" 解析成 (0, 9)；空串/纯空白返回 None（= 不静默）。

    **格式错就抛 ValueError，不返回 None**：返回 None 会把「没配」和「配错了」
    混成同一件事，而它们的处置完全相反（前者什么都不用做，后者要在启动日志里吼）。
    调用方负责接住并降级成「不静默」——静默时段没生效总比整只机器人起不来强。

    只接受一个区间、小时必须落在 0..23、且 start != end：这些都不是能力限制，
    是想让误配在启动那一刻就炸出来，而不是变成「夜里安静了但白天也不响了」。
    """
    raw = (text or "").strip()
    if not raw:
        return None

    parts = raw.split("-")
    if len(parts) != 2:
        raise ValueError(f"静默时段要写成 起-止（如 0-9 / 23-7），收到 {raw!r}")

    try:
        start, end = (int(p) for p in parts)
    except ValueError as exc:
        raise ValueError(f"静默时段的小时必须是整数，收到 {raw!r}") from exc

    for value in (start, end):
        if not 0 <= value <= 23:
            raise ValueError(f"静默时段的小时要在 0..23 之间，收到 {raw!r}")
    if start == end:
        raise ValueError(f"静默时段起止相同（{raw!r}）—— 是 0 小时还是 24 小时说不清，别猜")

    return start, end


def in_quiet_hours(window: tuple[int, int] | None, when: datetime | None = None) -> bool:
    """按**小时**判断（不看分钟）：window 为 None 时恒 False。"""
    if window is None:
        return False
    start, end = window
    hour = (when or datetime.now()).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # 跨午夜
