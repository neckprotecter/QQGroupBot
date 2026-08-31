import sys

import nonebot
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from nonebot.adapters.qq import Adapter
from nonebot.drivers import Timeout
from nonebot.log import default_filter, default_format
from nonebot.log import logger as nb_logger
from nonebot.drivers import aiohttp as nb_aiohttp

# nonebot 只会把 .env 读进内部 Config（键还会小写化），不会写入 os.environ。
# 这里手动加载，让插件能从 os.environ 读 OOPZ_* 凭证（load_dotenv 默认不覆盖已有变量）。
load_dotenv()

# QQ 适配器用 Request("GET", ws_url, timeout=30.0) 建 WebSocket 连接，而 aiohttp 驱动
# 会把数值型 timeout 同时当作 ws_receive 超时。QQ 网关心跳间隔正好也是 30s，导致
# 没有群消息时 ws.receive() 等满 30s 就误判超时 → 每 30s 断线重连一次（虽能 RESUMED
# 恢复且不影响走 HTTP API 的被动回复，但日志刷屏）。这里把 WS receive 超时拉长到 90s。
_orig_websocket = nb_aiohttp.Mixin.websocket


@asynccontextmanager
async def _patched_websocket(self, setup):
    if isinstance(setup.timeout, float) or setup.timeout is None:
        setup.timeout = Timeout(read=90.0)
    async with _orig_websocket(self, setup) as ws:
        yield ws


nb_aiohttp.Mixin.websocket = _patched_websocket

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

driver = nonebot.get_driver()
driver.register_adapter(Adapter)
nonebot.load_plugins("plugins")

if __name__ == "__main__":
    nonebot.run()
