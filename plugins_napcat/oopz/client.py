"""oopz REST 客户端单例与共享工具（NapCat / OneBot v11 版）。

从原 plugins_napcat/oopz_stats.py 抽出，供 @oopz 查询（oopz_stats）与
定时播报 / 进频道欢迎（auto_reporter）共用，避免两处各写一份：
- oopz REST 客户端单例（懒加载、复用连接；失败自动重建）
- 域过滤、频道名映射、批量昵称解析
- 公共常量（消息长度上限 / 群推送在 plugins_napcat/_shared/）
"""
from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from nonebot.log import logger

if TYPE_CHECKING:
    from oopz_sdk import OopzBot

# oopz SDK **默认随 requirements.txt 装上**（`oopz-sdk @ file:./vendor/Oopzbot-SDK`），
# 但**装不装是部署的人可以选的**：只要 MC 功能、不要 oopz 时，把那行注释掉就不装它。
# 所以这里不是顶层 `from oopz_sdk import OopzBot` —— 那样缺 SDK 时
# `load_plugins("plugins_napcat")` 会在导入这棵树时就 ImportError，**连着 MC 功能一起
# 起不来**，而 MC 跟 oopz 毫无关系。
# 改成运行期探测：缺了就只停 oopz 这两处（@oopz 查询、定时播报/进频道欢迎），
# 并且**明说**（群里回一句、日志吼一声），不是静默失效 —— 静默会让「没装 SDK」
# 看起来像「凭据过期」或「机器人坏了」，排查方向全错。
try:
    import oopz_sdk
except Exception as _exc:  # ModuleNotFoundError，也可能是 SDK 自身依赖缺失
    oopz_sdk = None
    _SDK_ERROR: str | None = f"{type(_exc).__name__}: {_exc}"
else:
    _SDK_ERROR = None

# 可选：限定统计范围。逗号分隔的 area_id 或域名，缺省时统计全部已加入的域。
# 例：OOPZ_TARGET_AREAS=奇妙小房间
_TARGET_AREAS = [
    s.strip()
    for s in os.environ.get("OOPZ_TARGET_AREAS", "").split(",")
    if s.strip()
]

# 单次 oopz 查询整体超时（秒），避免把回复/播报拖过时限
_QUERY_TIMEOUT = 20

# 三条凭据缺一不可（OOPZ_PRIVATE_KEY / OOPZ_APP_VERSION 有默认值，不算门槛）
_REQUIRED_CREDENTIALS = ("OOPZ_DEVICE_ID", "OOPZ_PERSON_UID", "OOPZ_JWT_TOKEN")

# oopz REST 客户端单例（懒加载，复用连接；失败自动重建）
_oopz_bot: OopzBot | None = None
_oopz_lock: asyncio.Lock | None = None


def sdk_available() -> bool:
    """oopz SDK 装了没（它默认随依赖装上；见 DEPLOY.md 第 3 节）。**没装不影响 MC 功能**。"""
    return oopz_sdk is not None


def sdk_error() -> str | None:
    """SDK 导入失败的原始原因，**只进日志、不进群**（同 MC 那边的规矩）。"""
    return _SDK_ERROR


def disabled_reason() -> str | None:
    """oopz 功能当前不可用的原因（可直接拼进群回复）；一切正常时返回 None。

    两种情况**分开说**：没装 SDK 和装了但凭据没配。修法完全不同（前者是 pip
    install，后者是跑 tools/oopz_login.py），混成一句话会把人指到错的方向。
    返回的是**半句**（不带「oopz …」前缀、不带句号），好让调用方按自己的前缀拼。
    """
    if oopz_sdk is None:
        return (
            "本机未安装 oopz_sdk（它随 requirements.txt 默认装，这台的部署特意跳过了；"
            "要用 oopz 就 pip install ./vendor/Oopzbot-SDK 然后重启）"
        )
    if not all(os.environ.get(k) for k in _REQUIRED_CREDENTIALS):
        return "凭据未配置（先在终端运行 tools/oopz_login.py，再重启机器人）"
    return None


def _config_from_env() -> "OopzConfig":
    """绕开 OopzConfig.from_env_async()。

    SDK 的 _require_env() 校验通过后没有 return，隐式返回 None，
    导致 __post_init__ 把 device_id/person_uid/jwt_token 全洗成空串。
    """
    from oopz_sdk import OopzConfig

    return OopzConfig(
        device_id=os.environ["OOPZ_DEVICE_ID"],
        person_uid=os.environ["OOPZ_PERSON_UID"],
        jwt_token=os.environ["OOPZ_JWT_TOKEN"],
        private_key=os.environ.get("OOPZ_PRIVATE_KEY", "").replace("\\n", "\n").strip(),
        app_version=os.environ.get("OOPZ_APP_VERSION", "").strip(),
    )


async def _get_client() -> OopzBot | None:
    """取 oopz 客户端（懒初始化）。SDK 缺失或凭据不全时返回 None。"""
    global _oopz_bot, _oopz_lock
    if _oopz_bot is not None:
        return _oopz_bot
    if disabled_reason() is not None:
        return None
    if _oopz_lock is None:
        _oopz_lock = asyncio.Lock()
    async with _oopz_lock:
        if _oopz_bot is not None:
            return _oopz_bot
        try:
            # 走 oopz_sdk.OopzBot 而不是裸名：这个类**只在 SDK 装好时才存在**，
            # 而上面的 disabled_reason() 已经保证了那一点（顶层用它做注解会被
            # `from __future__ import annotations` 变成字符串，不会在此处求值）。
            bot = oopz_sdk.OopzBot(_config_from_env())
            # 只启 REST：bot.start() 会去连 WebSocket，纯查询用不到
            await bot.rest.start()
            _oopz_bot = bot
        except Exception as exc:
            logger.error("oopz 客户端初始化失败: {}", exc)
            _oopz_bot = None
    return _oopz_bot


def _reset_client() -> None:
    """查询异常时置空，下次请求自动重建。"""
    global _oopz_bot
    _oopz_bot = None


def _filter_areas(joined: list) -> list:
    """按 _TARGET_AREAS 过滤域列表；未配置范围时原样返回。"""
    if not _TARGET_AREAS:
        return list(joined or [])
    wanted = set(_TARGET_AREAS)
    return [a for a in joined or [] if a.area_id in wanted or a.name in wanted]


async def _channel_name_map(bot: OopzBot, area_id: str) -> dict[str, str]:
    """拉取域内频道 id -> 频道名 映射。失败返回空 dict。"""
    try:
        groups = await bot.areas.get_area_channels(area_id)
    except Exception as exc:
        logger.warning("拉取域 {} 频道列表失败: {}", area_id, exc)
        return {}
    name_map: dict[str, str] = {}
    for g in groups or []:
        for ch in getattr(g, "channels", None) or []:
            ch_id = getattr(ch, "channel_id", None)
            if ch_id:
                name_map[ch_id] = getattr(ch, "name", "") or ""
    return name_map


async def _fetch_uid_names(bot: OopzBot, uids: list[str]) -> dict[str, str]:
    """批量解析昵称（一次请求）。失败返回空 dict。"""
    uid_name: dict[str, str] = {}
    if not uids:
        return uid_name
    try:
        async with asyncio.timeout(_QUERY_TIMEOUT):
            users = await bot.person.get_person_infos_batch(uids)
        for u in users or []:
            name = getattr(u, "name", "")
            if name:
                uid_name[getattr(u, "uid", "")] = name
    except Exception as exc:
        logger.warning("批量查询昵称失败: {}", exc)
    return uid_name
