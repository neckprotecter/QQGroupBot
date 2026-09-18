"""整点对齐的时间槽工具。

从 oopz/auto_reporter.py 的 _seconds_until_broadcast_slot() 抽出并参数化，
供 oopz 播报与 mc 播报共用。
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
