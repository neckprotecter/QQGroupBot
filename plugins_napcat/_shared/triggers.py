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
from dataclasses import dataclass

# 插件响应优先级：一条消息命中多个插件的触发词时，靠前的那个独占。
# mcadmin 排首位：管理命令是显式指令，绝不能被查询插件吞掉——被吞掉时用户只会
# 收到一份在线列表，看起来就像命令根本没生效。
_ORDER = ("mcadmin", "oopz", "mc")


def _env_keywords(var: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """读逗号分隔的触发词配置；留空则用默认值。统一转小写做不区分大小写匹配。"""
    raw = os.environ.get(var, "")
    items = tuple(s.strip().lower() for s in raw.split(",") if s.strip())
    return items or default


_KEYWORDS: dict[str, tuple[str, ...]] = {
    "mcadmin": _env_keywords("MC_ADMIN_TRIGGER", ("whitelist",)),
    "oopz": _env_keywords("OOPZ_TRIGGER", ("oopz",)),
    "mc": _env_keywords("MC_TRIGGER", ("mc", "我的世界", "服务器")),
}


@dataclass(frozen=True)
class Hit:
    """一次触发词命中：哪个插件、命中的哪个词、在原文里的哪一段。"""

    plugin: str
    keyword: str
    start: int
    end: int


def locate(text: str) -> Hit | None:
    """定位触发词，返回 None = 没有插件认领。

    比 detect() 多的就是**位置**：`@bot mc bingo` 光知道「归 mc 管」还不够，
    查询插件得把 `bingo` 这个载荷取出来。取第一个命中的插件（按 _ORDER），
    插件内部再取第一个命中的触发词（按配置顺序）—— 与原来的 `any(...)` 同序，
    所以 detect() 可以退化成它的一层薄包装，行为逐字不变。
    """
    lowered = (text or "").lower()
    for name in _ORDER:
        for kw in _KEYWORDS.get(name, ()):
            idx = lowered.find(kw)
            if idx >= 0:
                return Hit(name, kw, idx, idx + len(kw))
    return None


def detect(text: str) -> str | None:
    """返回该文本应归哪个插件处理；没有插件认领时返回 None。"""
    hit = locate(text)
    return hit.plugin if hit else None


def _ireplace(text: str, needle: str, repl: str) -> str:
    """忽略大小写的全量替换（str.replace 不支持忽略大小写）。"""
    if not needle:
        return text
    lowered, low = text.lower(), needle.lower()
    parts: list[str] = []
    cursor = 0
    while (idx := lowered.find(low, cursor)) >= 0:
        parts.append(text[cursor:idx])
        parts.append(repl)
        cursor = idx + len(low)
    parts.append(text[cursor:])
    return "".join(parts)


def strip_keyword(text: str, hit: Hit) -> str:
    """去掉触发词，返回剩下的载荷（`@bot mc bingo` → `bingo`）。

    剥的是**该插件的全部触发词**，不只是命中的那一个，且**长的先剥**：
    MC_TRIGGER 默认是 `mc,我的世界,服务器`，群友写 `@bot 服务器 mc bingo` 时
    两个词都得去掉才剩得下 `bingo`（服名解析会去掉内部空白，所以留下空格没关系）；
    长的先剥是为了防触发词互相包含时残留半个词。

    副作用：服名里恰好含触发词的会被剥坏 —— 但那种服名**本来就没法寻址**
    （见 mcs_servers.toml.example 末尾第 3 条），不是这里引入的新限制。
    """
    out = text or ""
    for kw in sorted(_KEYWORDS.get(hit.plugin, ()), key=len, reverse=True):
        out = _ireplace(out, kw, " ")
    return out.strip()


def primary_keyword(plugin: str, default: str = "") -> str:
    """取插件的第一个触发词，用于拼「怎么用」的提示文案。

    触发词可被 .env 改，写死 "whitelist" 会让改了 MC_ADMIN_TRIGGER 的用户看到
    一条用不了的用法说明。
    """
    keywords = _KEYWORDS.get(plugin, ())
    return keywords[0] if keywords else default
