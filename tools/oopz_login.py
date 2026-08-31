r"""oopz 纯 API 登录：手机号 + 密码 -> 写入 .env

用法（在你的终端里运行，密码不会出现在聊天记录里）：
    $env:OOPZ_LOGIN_PHONE = "你的oopz手机号"
    $env:OOPZ_LOGIN_PASSWORD = "你的oopz密码"
    .venv\Scripts\python.exe tools\oopz_login.py

登录成功后会把 OOPZ_DEVICE_ID / OOPZ_PERSON_UID / OOPZ_JWT_TOKEN /
OOPZ_PRIVATE_KEY 写进项目根目录的 .env（密码本身不保存）。
"""
import os
import sys
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _escape_pem(pem: str) -> str:
    """把多行 PEM 压成单行（\\n 转义），方便写入 .env。"""
    return pem.strip().replace("\r\n", "\n").replace("\n", "\\n")


def main() -> int:
    phone = os.environ.get("OOPZ_LOGIN_PHONE", "").strip()
    password = os.environ.get("OOPZ_LOGIN_PASSWORD", "")
    if not phone or not password:
        print("缺少环境变量 OOPZ_LOGIN_PHONE / OOPZ_LOGIN_PASSWORD")
        print("请先设置：")
        print('    $env:OOPZ_LOGIN_PHONE = "你的oopz手机号"')
        print('    $env:OOPZ_LOGIN_PASSWORD = "你的oopz密码"')
        return 1

    from oopz_sdk.auth.password_login import login_with_password_sync

    try:
        cred = login_with_password_sync(phone, password, timeout=20)
    except Exception as exc:
        print(f"[x] oopz 登录失败: {exc!r}")
        return 1

    env_map = cred.to_env()

    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    # 移除旧的 OOPZ_ 凭据行
    prefix = ("OOPZ_DEVICE_ID=", "OOPZ_PERSON_UID=", "OOPZ_JWT_TOKEN=",
              "OOPZ_PRIVATE_KEY=", "OOPZ_APP_VERSION=")
    lines = [line for line in lines if not line.strip().startswith(prefix)]

    lines.append(f"OOPZ_DEVICE_ID={env_map['OOPZ_DEVICE_ID']}")
    lines.append(f"OOPZ_PERSON_UID={env_map['OOPZ_PERSON_UID']}")
    lines.append(f"OOPZ_JWT_TOKEN={env_map['OOPZ_JWT_TOKEN']}")
    lines.append(f"OOPZ_PRIVATE_KEY={_escape_pem(env_map['OOPZ_PRIVATE_KEY'])}")
    if env_map.get("OOPZ_APP_VERSION"):
        lines.append(f"OOPZ_APP_VERSION={env_map['OOPZ_APP_VERSION']}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("✅ oopz 登录成功，凭据已写入 .env")
    print(f"   device_id   = {cred.device_id[:10]}...")
    print(f"   person_uid  = {cred.person_uid[:10]}...")
    print(f"   jwt_token   = {cred.jwt_token[:24]}...")
    print(f"   jwt 有效期  = {cred.expires_in_seconds}s" if cred.expires_in_seconds else "   jwt 有效期 = 未知")
    print(f"   私钥        = {'已获取' if cred.private_key_pem else '缺失'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
