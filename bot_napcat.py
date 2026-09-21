import sys

import nonebot
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.drivers import Timeout
from nonebot.log import default_filter, default_format
from nonebot.log import logger as nb_logger
from nonebot.drivers import aiohttp as nb_aiohttp

# nonebot 只会把 .env 读进内部 Config（键还会小写化），不会写入 os.environ。
# 这里手动加载，让插件能从 os.environ 读 OOPZ_* 凭证（load_dotenv 默认不覆盖已有变量）。
load_dotenv()

# OneBot 适配器用 Request("GET", ws_url, timeout=30.0) 建 WebSocket 连接，而 aiohttp 驱动
# 会把数值型 timeout 同时当作 ws_receive 超时。NapCat 心关心跳间隔正好也是 30s，导致
# 没有群消息时 ws.receive() 等满 30s 就误判超时 → 每 30s 断线重连一次。这里把 WS
# receive 超时拉长到 90s（与 bot.py 的 QQ 版做法一致）。
_orig_websocket = nb_aiohttp.Mixin.websocket


@asynccontextmanager
async def _patched_websocket(self, setup):
    if isinstance(setup.timeout, float) or setup.timeout is None:
        setup.timeout = Timeout(read=90.0)
    async with _orig_websocket(self, setup) as ws:
        yield ws


nb_aiohttp.Mixin.websocket = _patched_websocket

# NapCat / 任意 OneBot v11 协议端入口。
# 正向 WebSocket：NoneBot 作为客户端连到 NapCat 的 WS 服务，
# 连接地址在 .env 的 ONEBOT_V11_WS_URLS 配置（NapCat 默认 ws://127.0.0.1:3001）。
nonebot.init()

# ---- 控制台日志瘦身：滤掉纯框架噪音，普通消息只留 hello 的一行摘要 ----
# 默认收到一条群消息会打多行（适配器事件 dump + 每匹配器 2 行 Matcher 生命周期），
# 与业务无关。这里换掉默认 handler：只滤噪音行，其余沿用 nonebot 默认等级过滤。
def _quiet_filter(record):
    msg = record.get("message") or ""
    if "Event will be handled by Matcher" in msg:
        return False  # 每个匹配器 1 行，纯框架内部信息
    if "running complete" in msg:
        return False  # 每个匹配器 1 行，纯框架内部信息
    if "[message." in msg:
        return False  # 适配器事件全文 dump（含群消息内容，摘要已由 hello 打印）
    return default_filter(record)


nb_logger.remove()
nb_logger.add(
    sys.stdout,
    level=0,
    diagnose=False,
    filter=_quiet_filter,
    format=default_format,
)
# 同时落盘（按天切）。控制台刷过去就没了，而「某轮为什么没推」这类问题只能靠回看
# 日志 —— DEPLOY.md 里一直写着日志写到 logs/ 下，但那个 sink 早就没了，文档没跟着改。
# 用同一个 _quiet_filter，所以文件里和屏幕上逐字一致（读文件 = 看屏幕，不用两套判断）。
# encoding 必须写死 utf-8：本机代码页是 936，不写就用 GBK 落盘，中文全是乱码。
nb_logger.add(
    "logs/napcat_bot_{time:YYYY-MM-DD}.log",
    level=0,
    encoding="utf-8",
    rotation="00:00",
    retention="14 days",
    enqueue=True,
    backtrace=False,
    diagnose=False,
    filter=_quiet_filter,
    format=default_format,
)

driver = nonebot.get_driver()
driver.register_adapter(Adapter)
nonebot.load_plugins("plugins_napcat")

if __name__ == "__main__":
    nonebot.run()
