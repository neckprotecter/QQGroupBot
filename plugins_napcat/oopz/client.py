"""oopz REST 客户端单例与共享工具（NapCat / OneBot v11 版）。

从原 plugins_napcat/oopz_stats.py 抽出，供 @oopz 查询（oopz_stats）与
定时播报 / 进频道欢迎（auto_reporter）共用，避免两处各写一份：
- oopz REST 客户端单例（懒加载、复用连接；失败自动重建）
- 域过滤、频道名映射、批量昵称解析
- 公共常量
"""
import asyncio
import os

from nonebot.log import logger

from oopz_sdk import OopzBot

# 可选：限定统计范围。逗号分隔的 area_id 或域名，缺省时统计全部已加入的域。
# 例：OOPZ_TARGET_AREAS=奇妙小房间
_TARGET_AREAS = [
    s.strip()
    for s in os.environ.get("OOPZ_TARGET_AREAS", "").split(",")
    if s.strip()
]

# 每条消息最大长度（QQ 群文本消息上限约 2000，留余量）
_MAX_LEN = 1800
# 单次 oopz 查询整体超时（秒），避免把回复/播报拖过时限
_QUERY_TIMEOUT = 20

# oopz REST 客户端单例（懒加载，复用连接；失败自动重建）
_oopz_bot: OopzBot | None = None
_oopz_lock: asyncio.Lock | None = None


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
    """取 oopz 客户端（懒初始化）。缺少凭据返回 None。"""
    global _oopz_bot, _oopz_lock
    if _oopz_bot is not None:
        return _oopz_bot
    if not all(os.environ.get(k) for k in ("OOPZ_DEVICE_ID", "OOPZ_PERSON_UID", "OOPZ_JWT_TOKEN")):
        return None
    if _oopz_lock is None:
        _oopz_lock = asyncio.Lock()
    async with _oopz_lock:
        if _oopz_bot is not None:
            return _oopz_bot
        try:
            bot = OopzBot(_config_from_env())
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
