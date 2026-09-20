"""文本长度上限与截断。**刻意不依赖任何东西**（尤其不依赖 nonebot）。

原来这两样住在 push.py，但 push.py 是推送层（要 `get_bots()`，必须 import nonebot）。
渲染层 `mcrender.py` 需要 truncate，若直接 import push 就把 nonebot 拖进了纯文本模块 ——
而渲染层要能被 tools/mc_check.py 直接 import 做离线自测，那条路必须在 nonebot.init()
之前就能走通。所以把这两个零依赖的东西单独放一层，push 再导出一次给旧调用方。
"""
# 每条消息最大长度（QQ 群文本消息上限约 2000，留余量）
MAX_LEN = 1800


def truncate(text: str, limit: int = MAX_LEN) -> str:
    """超长文本截断加省略号（QQ 群消息超限会被整条拒收）。"""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
