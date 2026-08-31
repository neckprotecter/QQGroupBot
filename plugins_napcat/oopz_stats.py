"""@统计 插件（NapCat / OneBot v11 版）：群成员 @机器人发「统计」→ 实时拉取 Oopz 语音频道在线成员 → 回复。

与 plugins/oopz_stats.py（QQ 官方版）逻辑一致，仅收发层换成 OneBot v11 API。
走个人 QQ 号协议端（NapCat），不再受官方「主动推送停用」限制，可主动推送。
Oopz 侧只用 REST 查询（不需要 WebSocket 长连接），见 _get_client()。
"""
import asyncio
import os

from nonebot import on_message
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent

from oopz_sdk import OopzBot

# 可选：限定统计范围。逗号分隔的 area_id 或域名，缺省时统计全部已加入的域。
# 例：OOPZ_TARGET_AREAS=奇妙小房间
_TARGET_AREAS = [
    s.strip()
    for s in os.environ.get("OOPZ_TARGET_AREAS", "").split(",")
    if s.strip()
]

# 可选：只允许这些群触发查询（逗号分隔群号）；留空 = 所有群都可触发
_ALLOWED_GROUPS = {
    s.strip()
    for s in os.environ.get("NAPCAT_ALLOWED_GROUPS", "").split(",")
    if s.strip()
}

# 每条消息最大长度（QQ 群文本消息上限约 2000，留余量）
_MAX_LEN = 1800
# 单次 Oopz 查询整体超时（秒），避免把回复拖过时限
_QUERY_TIMEOUT = 20

stat = on_message(priority=1, block=False)

# Oopz REST 客户端单例（懒加载，复用连接；失败自动重建）
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
    """取 Oopz 客户端（懒初始化）。缺少凭据返回 None。"""
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
            logger.error("Oopz 客户端初始化失败: {}", exc)
            _oopz_bot = None
    return _oopz_bot


def _reset_client() -> None:
    """查询异常时置空，下次请求自动重建。"""
    global _oopz_bot
    _oopz_bot = None


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


async def _build_stats_message() -> str:
    """查询 Oopz 并生成统计文本。失败/无人时返回适合直接回复的字符串。"""
    bot = await _get_client()
    if bot is None:
        return "Oopz 凭据未配置：请先在终端运行 tools/oopz_login.py，然后重启机器人。"

    try:
        async with asyncio.timeout(_QUERY_TIMEOUT):
            joined = await bot.areas.get_joined_areas()
    except Exception as exc:
        _reset_client()
        logger.error("查询 Oopz 域列表失败: {}", exc)
        return "Oopz 查询失败，请稍后再试。"

    areas = list(joined or [])
    if _TARGET_AREAS:
        wanted = set(_TARGET_AREAS)
        areas = [a for a in areas if a.area_id in wanted or a.name in wanted]
    if not areas:
        return "当前 Oopz 频道暂无在线成员。"

    # area 名 -> [(频道名, [成员uid])]；只保留有真人(非bot)的频道
    online_by_area: dict[str, list[tuple[str, list[str]]]] = {}
    total_online = 0
    all_uids: list[str] = []

    for a in areas:
        try:
            async with asyncio.timeout(_QUERY_TIMEOUT):
                result = await bot.channels.get_voice_channel_members(area=a.area_id)
        except Exception as exc:
            logger.warning("拉取域 {} 在线成员失败: {}", a.name, exc)
            continue

        name_map = await _channel_name_map(bot, a.area_id)
        channel_rows: list[tuple[str, list[str]]] = []
        for ch_id, members in (result.channel_members or {}).items():
            humans = [m for m in members if not getattr(m, "is_bot", False)]
            if not humans:
                continue
            ch_name = name_map.get(ch_id) or ch_id[:8]
            uids = [m.uid for m in humans]
            channel_rows.append((ch_name, uids))
            total_online += len(uids)
            all_uids.extend(uids)
        if channel_rows:
            online_by_area[a.name] = channel_rows

    if not online_by_area:
        return "当前 Oopz 频道暂无在线成员。"

    # 批量解析昵称（一次请求，最多 30 人/批，带缓存）
    uid_name: dict[str, str] = {}
    try:
        async with asyncio.timeout(_QUERY_TIMEOUT):
            users = await bot.person.get_person_infos_batch(all_uids)
        for u in users or []:
            name = getattr(u, "name", "")
            if name:
                uid_name[getattr(u, "uid", "")] = name
    except Exception as exc:
        logger.warning("批量查询昵称失败: {}", exc)

    lines = [f"📊 Oopz 语音频道在线：{total_online} 人"]
    for area_name, channel_rows in online_by_area.items():
        lines.append(f"\n【{area_name}】")
        for ch_name, uids in channel_rows:
            names = [uid_name.get(uid) or uid[:8] for uid in uids]
            lines.append(f"  🔊 {ch_name}（{len(uids)}人）")
            for name in names:
                lines.append(f"    • {name}")

    msg = "\n".join(lines)
    if len(msg) > _MAX_LEN:
        msg = msg[:_MAX_LEN - 1] + "…"
    return msg


@stat.handle()
async def handle_stat(bot: Bot, event: MessageEvent):
    group_id = getattr(event, "group_id", None)
    text = event.get_plaintext().strip()
    if not group_id:
        return  # 只响应群内 @，私聊不管
    if not event.to_me:
        return  # 必须 @ 机器人才触发
    if _ALLOWED_GROUPS and str(group_id) not in _ALLOWED_GROUPS:
        return  # 群白名单：不在名单内的群不响应查询
    if "oopz" not in text.lower():
        return  # 文本包含 oopz（不区分大小写）即触发
    logger.info("收到@oopz，来自群 {}", group_id)
    msg = await _build_stats_message()
    try:
        await bot.send_group_msg(group_id=group_id, message=msg)
    except Exception as exc:
        logger.error("回复oopz统计失败: {}", exc)
