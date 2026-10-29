"""oopz 插件包：@oopz 在线查询 + 定时播报 / 进频道欢迎 + 凭据到期提醒。

子模块：
- client.py           oopz REST 客户端单例与共享工具
- cred.py             凭据到期计算（纯逻辑，不 import nonebot）
- oopz_stats.py       @oopz 在线列表查询（on_message matcher）
- auto_reporter.py    定时播报 + 进频道欢迎（driver.on_startup 后台任务）
- credential_watch.py 凭据到期提醒 + 运行期故障告警（driver.on_startup 后台任务）

import 子模块触发各自 matcher / 后台任务注册（oopz 包本身即插件入口）。
"""
from . import auto_reporter, client, credential_watch, oopz_stats
