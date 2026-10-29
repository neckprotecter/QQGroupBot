"""oopz 凭据的到期计算（纯逻辑）。

**单独成文件是为了能脱离 nonebot 直接 import**（与 _shared/schedule.py 同样的理由）：
`credential_watch.py` 顶层要 `get_driver()`，在 `nonebot.init()` 之前 import 就炸，
而这里的判定是纯算术，值得单独测。

JWT 的 payload 只有 `Uid` 和 `exp` 两个 claim，**没有 `iat`** —— 到期时刻只能信 `exp`，
"登录时间 + 31 天" 只是经验值（实测两次都吻合，但别拿它当实现依据）。
解 `exp` 不需要网络，所以 oopz 自己已经废了也照样算得出来。
"""
import base64
import json

# 提醒档位：(剩余秒数上限, 档位名)，**按上限从紧到松**排列，取第一个命中的。
# 顺序不能反：写反了 23 小时会先命中 d7 那条松的档，"明天就过期"的提醒永远出不来。
# 按需求写死 7 天 / 1 天，不做成配置：档位一多，"这次该发哪条"的判断就会变复杂，
# 而真正需要的只是"提前一点"和"再不弄就要出事"两种。
THRESHOLDS = ((86400, "d1"), (7 * 86400, "d7"))


def decode_jwt_exp(token: str | None) -> int | None:
    """解出 JWT 的 exp（Unix 秒）。**解不出返回 None，绝不抛异常**。

    不抛的理由：这条提醒是"锦上添花"的功能，凭据格式一变就让它把整只机器人
    带崩（连 MC 查询一起没）是完全不划算的交换。
    """
    if not token:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # JWT 的 base64 不带 padding
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = claims.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def pick_tier(remaining_sec: float) -> str | None:
    """按剩余秒数挑**最紧**的提醒档位；还没进任何档位返回 None。

    只返回一个档位（不是"该发的所有档"）：如果机器人离线错过了 7 天窗口，
    醒来时只剩 20 小时，发 d1 那条就已经包含了 d7 想说的信息，发两条只是噪音。

    `expired` 单独一档，不跟 `d1` 合并：区别不在"还剩多久"，而在"已经坏了" ——
    文案不同，去重粒度也不同（过期后每天提醒一次，而不是一次性），调用方分开处理。
    """
    if remaining_sec <= 0:
        return "expired"
    for limit, name in THRESHOLDS:
        if remaining_sec <= limit:
            return name
    return None
