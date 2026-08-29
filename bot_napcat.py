import nonebot
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.drivers import Timeout
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
driver = nonebot.get_driver()
driver.register_adapter(Adapter)
nonebot.load_plugins("plugins_napcat")

if __name__ == "__main__":
    nonebot.run()
