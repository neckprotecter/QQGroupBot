# oopz QQ 群机器人 —— 机器人本体的镜像。
#
# NapCat（协议端）是**另一个容器**，见仓库根的 docker-compose.yml —— 两个一起才跑得起来。
#
# 一般不用手敲 docker build，都由 compose 驱动：
#     export NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) BOT_UID=$(id -u) BOT_GID=$(id -g)
#     docker compose up -d --build
# 部署步骤见 DEPLOY-Linux.md。

FROM python:3.12-slim

# 这几条都不是可选的（对应宿主部署时 unit 里那两条 Environment）：
#   LANG / PYTHONUTF8  —— 容器里 LANG 常是空的，Python 会拿 ASCII 当控制台编码，往
#                         stdout 打中文直接 UnicodeEncodeError（进 docker logs 那一路就没了）。
#   PYTHONUNBUFFERED   —— 不缓冲，日志实时出现在 `docker compose logs` 里。
#   TZ                 —— 播报是「整点对齐」的，时区错了播报时刻整体偏 8 小时。
ENV LANG=C.UTF-8 \
    PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

# ⚠️ WORKDIR 必须是仓库根，它同时管**两件**事，少一件都不行：
#
#   ① requirements.txt 最后一行 `oopz-sdk @ file:./vendor/Oopzbot-SDK` 是本地相对路径，
#      **pip 按当前工作目录解析它**（见 requirements.txt 文件头），换个 cwd 就找不到 vendor/。
#
#   ② `nonebot.init()` 找 `.env` 也是**按 cwd** 找的（nonebot/config.py 里
#      `Path(".env").is_file()`），而 DRIVER / ONEBOT_V11_WS_URLS / ONEBOT_V11_ACCESS_TOKEN
#      正是从这条路读的 —— 路径错了，机器人会「正常启动、只是永远不连 NapCat」，
#      状态看起来一切正常，很难查。compose 把 .env 挂到 /app/.env 正是为了配上这里。
#
#   日志 logs/napcat_bot_<日期>.log 同样是相对 cwd 的，由 compose 挂 ./logs 进来。
WORKDIR /app

# 依赖单独一层：只改代码时不用重装依赖。
# 国内装依赖慢，构建期走清华源（只影响构建，不影响运行）。
COPY requirements.txt ./
COPY vendor/ ./vendor/
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 配置（.env / 两份 .toml）由 .dockerignore 挡在镜像外，运行时由 compose 只读挂进来 ——
# 密钥不进镜像，镜像才能随手导出/分享。见 docker-compose.yml。
COPY . .

# 必须这样跑，不能 `python -m`：入口靠 __main__ 判断，且 load_plugins("plugins_napcat")
# 是按包名解析的。SIGTERM 由 nonebot（anyio）接住，`docker stop` 会走正常的 lifespan 钩子。
CMD ["python", "bot_napcat.py"]
