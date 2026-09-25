#!/usr/bin/env bash
# ============================================================
# 改完配置后重启机器人本体（NapCat 不动 → 不用重新扫码）
#
#     ./restart.sh            # 只重启（改的是 .env / 两份 .toml）
#     ./restart.sh --build    # 连镜像一起重建（改了 .py 或 requirements.txt）
#
# 仓库默认在 /opt/qqgroupbot（见 DEPLOY-Linux.md 第 1 节）；部署在别处就
#     REPO=/your/path ./restart.sh
#
# 为什么不直接敲 docker compose：
#   1. **必须带 BOT_UID/BOT_GID**。compose 里写的是 ${BOT_UID:-1000}，不 export 就
#      按 1000 重建容器；而 .env / 两份 .toml 是 1001 属主、权限 600 → 容器读不到
#      /app/.env → 机器人崩溃重启循环。这个坑踩过一次，查了很久。
#   2. 只动 bot。不带服务名会把 napcat 也白重启一遍（不丢登录，但没必要）。
#   3. --force-recreate 保证挂进去的 /app/.env 就是你刚改的那份：有些编辑器保存是
#      「写新文件再改名」，普通 restart 可能还挂着旧文件（见下面第 5 步）。
#
# 必须在 git 工作树里跑 —— docker compose 认的是当前目录的 compose 文件。
# ============================================================
set -euo pipefail

REPO="${REPO:-/opt/qqgroupbot}"
SERVICE=bot
BUILD=0
if [[ "${1:-}" == "--build" ]]; then
    BUILD=1
elif [[ -n "${1:-}" ]]; then
    echo "用法：$0 [--build]" >&2
    exit 2
fi

# 身份要跟那三份 600 配置的属主一致，所以别用 sudo/root 跑
if [[ "$(id -u)" == "0" ]]; then
    echo "!! 别用 sudo/root 跑：要跟配置属主（szuc）同一个身份。" >&2
    exit 1
fi

cd "$REPO"

# --- 1. 前置检查：三份配置都得在且读得到，否则容器起来也是崩 ---
for f in .env mcs_servers.toml mcs_audiences.toml; do
    if [[ ! -r "$f" ]]; then
        echo "!! 读不到 $REPO/$f —— 检查属主和权限（应为 $(id -un)，600）" >&2
        exit 1
    fi
done

# --- 2. 身份对齐文件属主（见文件头第 1 条）---
export NAPCAT_UID="$(id -u)" NAPCAT_GID="$(id -g)" \
       BOT_UID="$(id -u)"     BOT_GID="$(id -g)"

echo "== 宿主 .env 里的 MC 键（改后的值）=="
grep -E '^MC_' .env || echo "  （一个都没有？）"

# --- 3. 重建 ---
args=(-d --force-recreate)
if (( BUILD )); then
    args+=(--build)
fi
echo
echo "== docker compose up ${args[*]} $SERVICE =="
docker compose up "${args[@]}" "$SERVICE"

# --- 4. 等它连上 NapCat，顺便确认 napcat 没被动 ---
echo
echo "== 等 20s =="
sleep 20
docker compose ps

# --- 5. 最要紧的一步：容器里读到的必须和宿主这份是同一个文件 ---
# 文件名一样不算数 —— 挂载点上挂的可能还是编辑前那个 inode。这一步查的正是
# 「文件改了但容器还挂着旧的」那种静默失败：光看日志分不出「配置没生效」和
# 「配置生效了但没触发」。
echo
host_md5="$(md5sum .env | cut -d' ' -f1)"
ctr_md5="$(docker compose exec -T "$SERVICE" md5sum /app/.env 2>/dev/null \
           | cut -d' ' -f1 | tr -d '\r' || true)"
if [[ "$host_md5" == "$ctr_md5" ]]; then
    echo "== 配置一致性 OK（容器读到的 .env 与宿主同一份，md5 $host_md5）=="
else
    echo "!! .env 不一致：宿主 $host_md5 / 容器 ${ctr_md5:-读不到}" >&2
    echo "!! 再跑一次本脚本；仍旧不一致就先 docker compose down 再重来。" >&2
fi

# --- 6. 日志尾巴 ---
echo
echo "== 最后 15 行日志 =="
docker compose logs --tail 15 "$SERVICE"

echo
echo "== 完。群里 @机器人 mc 试一下 =="
