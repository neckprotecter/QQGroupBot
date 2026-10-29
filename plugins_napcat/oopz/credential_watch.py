"""oopz 凭据到期提醒 + 运行期故障告警。

做两件事，都是**私聊**（不发群，不打扰群友）：

1. **提前量提醒** —— 解 `OOPZ_JWT_TOKEN` 的 `exp`，剩 ≤7 天 / ≤1 天各发一次，
   过期后每天一次。纯本地计算，不联网。
2. **运行期故障告警** —— oopz 客户端起不来时立刻发一次（每天最多一次）。
   典型场景是 JWT **被提前吊销**：此时 `exp` 还写着"没到期"，第 1 条不会响，
   而实际功能已经全废了 —— 2026-10-26 那次故障就是这个形态。

**为什么提醒走私聊、走 QQ**：提醒的意义就在 oopz 已经坏掉时还能送达。QQ 这条链路
跟 oopz 完全独立（NapCat ↔ 腾讯的登录会话），oopz 挂了它照样通。

.env 配置：
  OOPZ_CRED_WARN_QQ=644277376,3416889191   收件人（逗号分隔，须是机器人好友），留空 = 不启用
  OOPZ_CRED_CHECK_INTERVAL_SEC=3600        检查间隔（秒），最小 60

去重状态落在 logs/ 下（见 _STATE_PATH）：没有它的话，每轮检查都会把"还剩 6 天"
重发一遍，重启容器也会从头再喊一次。状态以 `exp` 作版本，换了新凭据自动作废。
"""
import asyncio
import json
import os
import time
from datetime import datetime

from nonebot import get_driver
from nonebot.log import logger

from .._shared.push import send_private
from .client import _get_client, disabled_reason
from .cred import decode_jwt_exp, pick_tier

driver = get_driver()


def _env_list(key: str) -> list[str]:
    return [s.strip() for s in os.environ.get(key, "").split(",") if s.strip()]


_RECIPIENTS = _env_list("OOPZ_CRED_WARN_QQ")


def _interval() -> int:
    """检查间隔。配错了降级成默认值并吼一声，而不是让 import 期抛异常。

    import 期抛异常会把**整只机器人**带崩（MC 查询一起没），而这里坏的只是一个提醒。
    与 quiet.py 对误配的态度一致。
    """
    raw = os.environ.get("OOPZ_CRED_CHECK_INTERVAL_SEC", "").strip()
    if not raw:
        return 3600
    try:
        return max(int(raw), 60)
    except ValueError:
        logger.warning("OOPZ_CRED_CHECK_INTERVAL_SEC={!r} 不是整数，按 3600 秒处理", raw)
        return 3600


_STATE_PATH = os.path.join("logs", "oopz_cred_watch.state.json")


# ---------------------------------------------------------------- 去重状态

def _load_state(exp: int) -> dict:
    """读状态；`exp` 变了就当作全新的一份（旧凭据的发过的档位不再适用）。"""
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        return {"exp": exp, "sent": []}
    if state.get("exp") != exp:
        return {"exp": exp, "sent": []}
    state.setdefault("sent", [])
    return state


def _save_state(state: dict) -> None:
    """原子落盘（写临时文件再改名），避免进程被杀时留下半截 JSON。"""
    try:
        os.makedirs(os.path.dirname(_STATE_PATH) or ".", exist_ok=True)
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, _STATE_PATH)
    except OSError as exc:
        # 存不下最多是重启后重发一次提醒，不值得让检查循环停掉
        logger.warning("oopz 凭据提醒状态写不进 {}: {}", _STATE_PATH, exc)


def _dedupe_key(tier: str) -> str:
    """去重键。过期后按日期分桶 → 每天一条，直到换掉凭据。"""
    if tier == "expired":
        return f"expired:{datetime.now():%Y-%m-%d}"
    return tier


# ---------------------------------------------------------------- 文案

_RENEW_HINT = (
    "续期：本机跑 tools/oopz_login.py → 只 patch 服务器 .env 的 5 个 OOPZ_ 凭据键"
    " → ./restart.sh"
)


def _build_message(tier: str, remaining_sec: float, exp: int) -> str:
    when = datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M")
    if tier == "expired":
        return (
            f"🔴 oopz 凭据已过期（{when}）\n\n"
            "@oopz 查询和语音频道播报已经停了。MC 那侧不受影响。\n"
            + _RENEW_HINT
        )
    # 剩不到一天时说小时：`d1` 档也可能是 0.2 天（机器人在 7 天窗口里离线了一周），
    # "还有 0.2 天" 这种话读起来要心算一遍才知道还剩几小时
    if tier == "d1":
        left, head = f"还有约 {max(remaining_sec / 3600, 0):.0f} 小时到期", "⚠️"
    else:
        left, head = f"还有 {max(remaining_sec / 86400, 0):.1f} 天到期", "⏳"
    return (
        f"{head} oopz 凭据{left}（{when}）\n\n"
        "到期后 @oopz 查询和语音频道播报会停掉，MC 那侧不受影响。\n"
        + _RENEW_HINT
    )


# ---------------------------------------------------------------- 检查

async def _push_all(text: str) -> int:
    """按顺序私聊所有收件人。返回成功条数。"""
    sent = 0
    for i, uid in enumerate(_RECIPIENTS):
        if i:
            await asyncio.sleep(0.5)  # 连发多条之间留间隔，避免被吞
        if await send_private(uid, text):
            sent += 1
    return sent


async def _check_expiry() -> None:
    exp = decode_jwt_exp(os.environ.get("OOPZ_JWT_TOKEN"))
    if exp is None:
        logger.warning("oopz 凭据检查：解不出 OOPZ_JWT_TOKEN 的 exp，本次跳过")
        return

    remaining = exp - time.time()
    tier = pick_tier(remaining)
    if tier is None:
        return  # 还早，不打扰

    state = _load_state(exp)
    key = _dedupe_key(tier)
    if key in state["sent"]:
        return

    if await _push_all(_build_message(tier, remaining, exp)):
        state["sent"].append(key)
        _save_state(state)
        logger.info("oopz 凭据提醒已发送（档位 {}，exp={}）", tier, exp)
    else:
        # 不记为已发：下一轮重试。发不出去通常是 bot 还没连上 NapCat。
        logger.warning("oopz 凭据提醒发送失败（档位 {}），下一轮重试", tier)


async def _check_client() -> None:
    """oopz 客户端起不来就告警（凭据被提前吊销时 exp 检查不会响）。

    `disabled_reason()` 非空 = SDK 没装或凭据缺键，那是部署问题，auto_reporter
    启动时已经吼过，这里不重复。
    """
    if disabled_reason() is not None:
        return
    if await _get_client() is not None:
        return

    state = _load_state(decode_jwt_exp(os.environ.get("OOPZ_JWT_TOKEN")) or 0)
    key = f"initfail:{datetime.now():%Y-%m-%d}"
    if key in state["sent"]:
        return

    text = (
        "🔴 oopz 客户端初始化失败\n\n"
        "可能是凭据被提前吊销（exp 还没到但已失效），也可能只是 oopz 那边抽风。\n"
        "群里 @oopz 查询现在应该是没反应的，具体报错见 logs/ 下今天的日志。\n"
        + _RENEW_HINT
    )
    if await _push_all(text):
        state["sent"].append(key)
        _save_state(state)
        logger.warning("oopz 客户端初始化失败，已私聊告警")
    else:
        logger.warning("oopz 客户端初始化失败，但告警没发出去，下一轮重试")


async def _watch_loop() -> None:
    await asyncio.sleep(15)  # 等 bot 连上 NapCat
    interval = _interval()
    while True:
        try:
            await _check_expiry()
            await _check_client()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("oopz 凭据检查异常: {}", exc)
        await asyncio.sleep(interval)


@driver.on_startup
async def _start_credential_watch() -> None:
    if not _RECIPIENTS:
        logger.info("未配置 OOPZ_CRED_WARN_QQ，oopz 凭据到期提醒未启用")
        return
    asyncio.create_task(_watch_loop())
