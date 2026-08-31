r"""验证 oopz 凭据：列出加入的域，以及各域语音频道的在线成员。

用法：
    .venv\Scripts\python.exe tools\oopz_check.py

如果 .env 里已有 OOPZ_* 凭据就直接用；否则等价于先跑 oopz_login.py。
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 加载 .env（nonebot 启动时会自己加载，这里手动载入以便独立运行）
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def _pem_from_env() -> str:
    """.env 里存的 PEM 是单行 + \\n 转义，这里还原成多行真实换行。"""
    raw = os.environ.get("OOPZ_PRIVATE_KEY", "")
    return raw.replace("\\n", "\n").strip()


def _config_from_env():
    """绕开 OopzConfig.from_env_async()：它内部 _require_env 缺 return 返回 None，
    导致 __post_init__ 把 device_id/person_uid/jwt_token 全洗成空串（SDK bug）。"""
    from oopz_sdk import OopzConfig

    return OopzConfig(
        device_id=os.environ["OOPZ_DEVICE_ID"],
        person_uid=os.environ["OOPZ_PERSON_UID"],
        jwt_token=os.environ["OOPZ_JWT_TOKEN"],
        private_key=_pem_from_env(),
        app_version=os.environ.get("OOPZ_APP_VERSION", "").strip(),
    )


async def main() -> int:
    _load_dotenv(ROOT / ".env")
    from oopz_sdk import OopzBot

    if not all(os.environ.get(k) for k in ("OOPZ_DEVICE_ID", "OOPZ_PERSON_UID", "OOPZ_JWT_TOKEN")):
        print("[!] .env 中缺少 OOPZ_* 凭据，请先运行 tools/oopz_login.py")
        return 1

    config = _config_from_env()
    bot = OopzBot(config)
    # 只启动 REST 客户端：纯被动查询不需要 WebSocket（bot.start() 会连 ws 挂住）
    await bot.rest.start()

    try:
        print("== 已加入的域 (area) ==")
        joined = await bot.areas.get_joined_areas()
        areas = getattr(joined, "areas", None) or joined
        if not areas:
            print("    (没有加入任何域)")
            return 0
        for a in areas:
            area_id = getattr(a, "id", None) or getattr(a, "area_id", None)
            name = getattr(a, "name", None) or ""
            print(f"   - {area_id}  {name}")

        # 对每个域尝试拉语音频道在线成员
        for a in areas:
            area_id = getattr(a, "id", None) or getattr(a, "area_id", None)
            if not area_id:
                continue
            print(f"\n== 域 {area_id} 的语音频道在线成员 ==")
            try:
                result = await bot.channels.get_voice_channel_members(area=area_id)
            except Exception as exc:
                print(f"   拉取失败: {exc!r}")
                continue
            if not result.channel_members:
                print("   (没有在线成员或没有语音频道)")
            for ch_id, members in result.channel_members.items():
                names = [m.uid for m in members if not getattr(m, "is_bot", False)]
                bots = [m.uid for m in members if getattr(m, "is_bot", False)]
                print(f"   - 频道 {ch_id}: {len(names)} 人在线 {names[:10]}{'...' if len(names) > 10 else ''}")
                if bots:
                    print(f"     (含 bot {len(bots)}: {bots[:5]})")
    finally:
        await bot.rest.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
