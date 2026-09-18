"""触发词归属：决定一条 @机器人 的消息该由哪个插件响应。

原来 hello 的判据是「被 @ 且文本不含 oopz 就回复功能引导」。接入第二个插件后，
`@机器人 mc` 会被 hello 和 mc_stats 同时命中，群里收到功能引导 + MC 列表两条
消息。这里把触发词收敛成单一来源：各插件只问 detect()，新增插件只需在
_KEYWORDS 里加一项，不用回头改其它插件的互斥条件。

再加一个插件要动两处：_KEYWORDS 加一项；若它必须优先于已有的插件，再往 _ORDER
里排位置。注意三个 matcher 全是 priority=1/block=False，**优先级和 block 在它们
之间不起任何仲裁作用**——互斥完全靠各 handler「先问 detect()、不归我就 return」。
"""
import os

# 插件响应优先级：一条消息命中多个插件的触发词时，靠前的那个独占。
_ORDER = ("oopz", "mc")


def _env_keywords(var: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """读逗号分隔的触发词配置；留空则用默认值。统一转小写做不区分大小写匹配。"""
    raw = os.environ.get(var, "")
    items = tuple(s.strip().lower() for s in raw.split(",") if s.strip())
    return items or default


_KEYWORDS: dict[str, tuple[str, ...]] = {
    "oopz": _env_keywords("OOPZ_TRIGGER", ("oopz",)),
    "mc": _env_keywords("MC_TRIGGER", ("mc", "我的世界", "服务器")),
}


def detect(text: str) -> str | None:
    """返回该文本应归哪个插件处理；没有插件认领时返回 None。"""
    lowered = (text or "").lower()
    for name in _ORDER:
        if any(kw in lowered for kw in _KEYWORDS.get(name, ())):
            return name
    return None


def primary_keyword(plugin: str, default: str = "") -> str:
    """取插件的第一个触发词，用于拼「怎么用」的提示文案。

    触发词可被 .env 改，写死 "whitelist" 会让改了 MC_ADMIN_TRIGGER 的用户看到
    一条用不了的用法说明。
    """
    keywords = _KEYWORDS.get(plugin, ())
    return keywords[0] if keywords else default
