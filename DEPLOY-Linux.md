# 从零部署指南（Linux · Docker）

本项目是 **QQ 群机器人 + oopz 语音频道在线统计**。这份是 Linux 版；Windows 版在
[DEPLOY.md](DEPLOY.md)。

Linux 上**两个服务都是容器**（NapCat 协议端 + 机器人本体），由仓库根的
`docker-compose.yml` 一起管起来。宿主上只留三样东西：代码、三份配置、两个数据卷。

**两份的分工**（免得你两边找）：

| 内容 | 在哪 |
|---|---|
| Linux 上「怎么装起来、怎么挂服务、怎么排查平台层的问题」 | **本文** |
| `.env` 各键的含义、`mcs_servers.toml` / `mcs_audiences.toml` 的字段语义、常见问题的业务侧排查（F1–F11） | [DEPLOY.md](DEPLOY.md)（内容与平台无关，Linux 同样适用） |
| 三份模板的完整注释 | 仓库根的 `*.example` |

代码本身**没有平台假设**（配置路径走 `Path(__file__)`、日志是相对 `logs/`），所以
Windows 上跑得通的写法 Linux 上也跑得通 —— 差别全在**怎么把它跑起来**：Windows 那份是
双击 `.bat`，这份是 docker compose。

**要用到的现成文件**（都在仓库里，不用手抄）：

```
Dockerfile                    # 机器人本体的镜像
docker-compose.yml            # 两个服务：napcat + bot
deploy/linux/
├─ oopz-bot.service           # 附录 A 用的（**不用 Docker 跑本体**时才要）
└─ mc-tunnel.service          # 只有「MC 服务端在**另一台**机器上」时才要，见附录 A
```

> ⚠️ 本文的命令**默认当前目录是仓库根**（`/opt/qqgroupbot`）。`docker compose` 只在
> 仓库根认得到那份 compose 与那三份配置。

---

## 1. 环境准备

- 一台 Linux（x86_64 / arm64 都行），**装得动 Docker** 的发行版（Debian 12+ / Ubuntu 22.04+ 等）
- **Docker + compose 插件**：
  ```bash
  docker --version && docker compose version     # 注意是 `docker compose`（子命令），不是 `docker-compose`
  ```
- 一个**普通 QQ号**（用作机器人本体）+ 一个目标 QQ 群
- 一个 oopz 账号（已有加入的语音域）
- （要用 MC 功能）能连到 MC 服务端；**对端在另一台机器上**时看附录 A

**先做两件一次性的准备**：

```bash
# ① 时区。播报是「整点对齐」的，主机跑 UTC 的话播报时刻会整体偏 8 小时，
#    日志时间戳也会和你的直觉对不上（DEPLOY.md 的 F5 排查会因此走偏）。
#    （容器里另有 TZ=Asia/Shanghai 兜底，但宿主的也得对。）
sudo timedatectl set-timezone Asia/Shanghai
timedatectl                                  # 确认 Local time 对

# ② 把仓库放进去（git clone 或 rsync 上来），目录用 /opt/qqgroupbot，
#    下文所有路径都以它为例。
#    目录里应当能看到 bot_napcat.py、Dockerfile、docker-compose.yml、vendor/、tools/。
sudo mkdir -p /opt/qqgroupbot && sudo chown $(id -u):$(id -g) /opt/qqgroupbot
git clone https://github.com/neckprotecter/QQGroupBot.git /opt/qqgroupbot
```

> ⚠️ **`.env` 和两份 `.toml` 不在仓库里**（它们在 `.gitignore` 中），`git clone`
> 出来是没有的 —— 见第 4 节。**密码别通过聊天工具明文发**，用你熟悉的安全渠道传。

**谁的身份跑？** 容器不跑 root，用的是一个普通账号的 uid/gid（compose 里的
`NAPCAT_UID` / `BOT_UID`），为的是卷里的文件归你、且读得到那三份 600 的配置。
**这两对变量每次 `docker compose up` 都要在环境里**：

```bash
export NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) BOT_UID=$(id -u) BOT_GID=$(id -g)
```

> 忘了 export 会走 compose 里的默认值 `1000:1000` —— 那多半不是你的 uid，
> 症状是机器人反复重启（读不到 `.env`、写不进 `./logs`）。真要懒得每次 export，
> 把这两对写进你的 `~/.bashrc`。

---

## 2. NapCat 协议端（Docker）

NapCat 是 QQ 机器人的协议实现，用个人 QQ 号模拟登录，把消息包装成 OneBot v11 接口。

```bash
cd /opt/qqgroupbot
export NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) BOT_UID=$(id -u) BOT_GID=$(id -g)
mkdir -p napcat-data/config napcat-data/QQ        # 两个卷的宿主目录，先建好，属主是你
docker compose up -d napcat
docker compose logs -f napcat                     # 首次会打印二维码（WebUI 里也能看）
```

用机器人那个 QQ **扫码登录**（手机 QQ → 扫一扫）。

**但只有第一次要扫 —— 之后必须靠「快速登录」，而且要显式开。** 镜像里
`entrypoint.sh` 的登录分支是：

```bash
if [ -n "${ACCOUNT}" ]; then gosu napcat /opt/QQ/qq --no-sandbox -q $ACCOUNT
else                          gosu napcat /opt/QQ/qq --no-sandbox
fi
```

它**不转发 `command: [...]` 里的参数**（写 `command: ["-q", "123456"]` 完全无效），
只能靠环境变量 `ACCOUNT` 传。compose 里已经接好了：`ACCOUNT=${NAPCAT_QQ:-}`，
所以只要在同目录 `.env` 里加一行就够了（号码是真实标识，别写进公开仓库）：

```
NAPCAT_QQ=你的机器人QQ号
```

> ⚠️ **不填的后果很难查**：会话其实好好存在 `./napcat-data/QQ` 里，容器也 `Up` 得漂漂亮亮，
> 但 NapCat 每次启动都**停在登录界面等扫码**，于是 3001 那个正向 WS 服务端根本没起来，
> 机器人的症状是刷屏 `ClientConnectorError: Cannot connect to host napcat:3001`。
> 日志里的原话是「没有 -q 指令指定快速登录，将使用二维码登录方式」。

登录成功后：

**开一个正向 WebSocket 服务**（机器人连它）：

1. 打开 WebUI：`http://127.0.0.1:6099/webui`
   - 服务器上没桌面的话，在**你自己电脑**上转过去：
     `ssh -L 6099:127.0.0.1:6099 <你的服务器>`，然后本地浏览器开
     <http://127.0.0.1:6099/webui>。
2. 首次登录的口令是 `napcat` —— **进门第一件事就是改掉它**（能进这个面板就能操作那个
   QQ 号，等于账号）。
3. 「网络配置」→ 新建 **WebSocket 服务端**（正向 WS）：
   - `host`: `0.0.0.0`（容器内；宿主上不发布这个口，见下）
   - `port`: `3001`
   - `messagePostFormat`: `array`
   - `enableForcePushEvent`: 开启
   - 勾选后会自动生成 **token**，复制出来，第 4 节填进 `.env`
4. 保存并启用。

> ⚠️ **只发布了 6099 一个口，而且绑死在回环**（compose 里写的是
> `127.0.0.1:6099:6099`）。别照抄网上「云服务器要在安全组放行 6099」的说法：6099 能扫码、
> 能改这个 QQ 的所有网络配置，放公网等于把账号交出去。要从外面看 WebUI 就用上面的 `ssh -L`。
>
> **3001 则连回环都不发布**：机器人自己就在同一个 compose 网络里，直接按服务名
> `napcat:3001` 连它（见第 8 节）。少一个暴露面，也少一次「端口被占」的争执。
>
> ⚠️ 两个卷都要挂（compose 里已写好）。`napcat-data/QQ` 装的就是登录会话，不挂它必然
> 每次都要重扫码。**但挂对了也不够** —— 还得照上面配 `NAPCAT_QQ`，否则会话明明在卷里，
> NapCat 也不会去用它，每次启动照样停在扫码界面。

---

## 3. 机器人本体（构建镜像）

```bash
cd /opt/qqgroupbot
export NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) BOT_UID=$(id -u) BOT_GID=$(id -g)
mkdir -p logs                       # 日志落盘目录（第 10 节有坑，别省）
docker compose build bot
```

镜像基于 `python:3.12-slim`，pip 走清华源（写在 `Dockerfile` 里，只影响构建）。

> **oopz 默认一起装上**：`requirements.txt` 里 `oopz-sdk @ file:./vendor/Oopzbot-SDK`
> 会把 `vendor/` 里那份 SDK 装上，不用再单跑一条 pip。**那一行是本地相对路径**，
> 所以 `Dockerfile` 里的 `WORKDIR` 必须是仓库根（`/app`）—— 那两行注释写了为什么。
>
> 只要 MC 功能、不要 oopz：把 `requirements.txt` 最后一行注释掉再构建（这是**受支持**的
> 状态，不是坏掉：机器人照常起、oopz 那半边自动停用并在群里明说原因，MC 查询与白名单
> 一字不受影响，见 DEPLOY.md 第 3 节）。

> **关于 playwright**：`oopz_sdk` 的依赖里会**装上 playwright 这个 Python 包**，但
> **不需要**下载浏览器 —— 那是「用账号密码开浏览器登录」那条路才要的（无桌面服务器还得
> 再配 Xvfb）。标准的凭据流程（第 5 节）走的是纯 API，不碰浏览器；本项目这条路上
> `playwright` 从头到尾**不会被 import**（它是函数内导入的）。真要用浏览器那条路时：
> ```bash
> docker compose exec bot python -m playwright install --with-deps chromium
> ```

---

## 4. 配置

**三份配置放在仓库根**（和 compose 同目录，compose 会把它们只读挂进容器）：

```bash
cd /opt/qqgroupbot
cp .env.example .env
cp mcs_servers.toml.example mcs_servers.toml          # 用 MC 功能才需要
cp mcs_audiences.toml.example mcs_audiences.toml      # 用 MC 功能才需要

chmod 600 .env mcs_servers.toml mcs_audiences.toml
```

> ⚠️ **权限不是可选项**：`.env` 里有 QQ 的 token、oopz 的 JWT 和私钥；
> `mcs_servers.toml` 里有各服 RCON 明文密码。600 + 容器用你的 uid 跑 = 同机其他用户读不到。
> **这三份文件必须存在** —— compose 会把它们挂进容器，缺一个机器人起不来。

**填什么**：每个键的含义见 [DEPLOY.md 第 4 节](DEPLOY.md)（表格是平台无关的），
两份 `.toml` 的字段语义见它们自己的 `.example` 注释与 [DEPLOY.md 4.1 / 4.2](DEPLOY.md)。
Linux 上要额外注意的只有两点：

1. **路径**：`MCS_SERVERS_TOML` / `MCS_AUDIENCES_TOML` 不用填 —— 代码是按
   `Path(__file__)` 找到仓库根那两份的，容器里的 `/app` 就是仓库根。要指到别处才填绝对路径。
2. **`OOPZ_PRIVATE_KEY` 是一行**：真实的换行写成字面 `\n`（`.env` 里不能有真换行）。
   从别的机器拷 `.env` 过来最容易在这一行出问题（多一个空格都会让 JWT 签名失败）。

**两处与 Windows/旧写法不同的地址**（机器人和 MC 服务端在同一台机器上时）：

| 键 | 填什么 | 为什么 |
|---|---|---|
| `.env` 的 `ONEBOT_V11_WS_URLS` | `'["ws://napcat:3001"]'` | 走 compose 内部网络的服务名，不是 `127.0.0.1` |
| `mcs_servers.toml` 的 `api.url` | `http://velocity:8080` | 见第 8 节 |
| `mcs_servers.toml` 的 RCON `host` | `gtnh`（端口/密码不动） | 同上 |

> ⚠️ **别写 `127.0.0.1`**：容器里的 `127.0.0.1` 指的是**它自己**，不是宿主。宿主的回环口
> 从容器里够不着（要走 `host.docker.internal`，而服务名那条路更干净）。

`.env` 里另外**多一行**（Windows 那份没有）：

```
NAPCAT_QQ=<机器人QQ号>
```

它是喂给 napcat 容器的 `ACCOUNT`（免扫码快速登录，见第 2 节），**机器人自己不读它**。
漏了的症状是每次重建容器都得重新扫码。

---

## 5. 生成 oopz 凭据

```bash
cd /opt/qqgroupbot
OOPZ_LOGIN_PHONE='你的oopz手机号' OOPZ_LOGIN_PASSWORD='你的oopz密码' \
    docker compose exec bot python tools/oopz_login.py
```

> 等机器人容器起来之后再跑。**没起来也可以**：先 `docker compose run --rm bot
> python tools/oopz_login.py`（一次性容器，不依赖 bot 在跑）。但要先有 `.env`（第 4 节），
> 脚本会往它里面写。

脚本会把 `OOPZ_DEVICE_ID` / `OOPZ_PERSON_UID` / `OOPZ_JWT_TOKEN` / `OOPZ_PRIVATE_KEY`
写进 `.env`。**手机号和密码只从环境变量读、不会被保存**（所以不要把它们写进 `.env`）。

> ⚠️ **`oopz_login.py` 写的是容器里那个 `.env`** —— 它就是宿主那份（只读挂载会挡住它！）。
> 所以要么把 compose 里那条挂载的 `:ro` 临时去掉，要么**在宿主上跑这个脚本**。
> 后者更简单：宿主要求有 Python 和 oopz_sdk，不如直接把现有的四个 `OOPZ_*` 值从原来的
> 机器拷过来（它们的有效期约 31 天，见第 11 节）。**推荐直接拷。**

---

## 6. 启动前先验一遍（不起机器人）

工具都在这份工程里，说明见 [tools/README.md](tools/README.md)。容器里解释器就是 `python`：

```bash
cd /opt/qqgroupbot
export NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) BOT_UID=$(id -u) BOT_GID=$(id -g)

# 离线自测：447 条断言，不联网、不读配置 —— 先证明镜像里那套依赖是齐的
docker compose run --rm bot python tools/mc_check.py --self-test

# MC 那边（用 MC 功能才需要）：
docker compose run --rm bot python tools/mc_check.py --list-audiences  # 只读：每条群关联看哪几台
docker compose run --rm bot python tools/mc_check.py --list-targets    # 只读：服务器清单解析成什么
docker compose run --rm bot python tools/mc_check.py                   # 实机探测（会真的连服务器）
docker compose run --rm bot python tools/mc_check.py --api             # 只看群组接口那一层
```

> 用 `run --rm bot` 而不是 `exec`：这时机器人还没起，`exec` 没有容器可进。
> 起来了之后两者都行（`exec` 更快）。

**这两步都过了再往下走**：凭据/配置的问题在这里会明说，而挂上 compose 之后它们只会
变成容器反复重启、日志里一串难懂的报错。

> `--list-targets` / `--api` 这些**必须在容器里跑**：`velocity` 这种服务名只有容器网络
> 里的 DNS 认识，在你 ssh 登录的宿主 shell 里是解析不了的。

---

## 7. 启动与日常命令

```bash
cd /opt/qqgroupbot
export NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) BOT_UID=$(id -u) BOT_GID=$(id -g)

docker compose up -d --build        # 起 / 更新（改了代码或依赖必须带 --build）
docker compose ps                   # 两个都该是 Up
docker compose logs -f bot          # 跟日志（Ctrl-C 只是退出跟，不影响容器）
docker compose restart bot          # 改完配置重启它（配置是启动时读一次）
docker compose down                 # 停（加 -v 会连卷一起删 —— **别加**，那会丢登录会话）
```

日志出现 `Bot <QQ号> connected` 就是连上 NapCat 了。

日志有**两处**，都留：

- `docker compose logs bot` —— 控制台那一路（推荐，带 `--since 1h` / `-f` 都好用）
- `logs/napcat_bot_YYYY-MM-DD.log` —— 按天落盘，保留 14 天（**在宿主上直接 `tail -f`**）

> ⚠️ **`logs/` 目录必须在，而且容器那个 uid 要写得进去**。写不进去时 loguru 的
> `add()` 会在**导入期**直接抛错 → 容器**反复重启**。`./logs` 挂载和 `BOT_UID` 就是为这个。
>
> 顺带说清一件容易搞错的事：**认 cwd 的不止 `logs/`，`.env` 也认**。
> `nonebot.init()` 是按 cwd 找 `.env` 的（`DRIVER` / `ONEBOT_V11_WS_URLS` /
> `ONEBOT_V11_ACCESS_TOKEN` 都从那儿读）—— cwd 不对时它会**拿着一个空的 WS 地址列表
> 正常启动、然后永远不连 NapCat**。容器里由 `Dockerfile` 的 `WORKDIR /app` +
> compose 挂的 `./.env:/app/.env` 保证；**附录 A 那种宿主写法要自己保证 `WorkingDirectory`**。
> 两份 `.toml` 才是真不认 cwd 的（走 `Path(__file__)`）。

改动配置之后**一定要重启**（`.env` 与两份 `.toml` 都是启动时读一次）—— 直接用它：

```bash
./restart.sh              # 改的是 .env / 两份 .toml
./restart.sh --build      # 改了 .py 或 requirements.txt
```

> 当然也可以自己敲 `docker compose restart bot`，但**别忘了上面那行 `export`**：
> compose 里是 `${BOT_UID:-1000}`，不 export 就会按 1000 重建容器，而三份配置是
> `1001` 属主、权限 `600` → 容器读不到 `/app/.env` → **机器人崩溃重启循环**。
> `restart.sh` 把这件事连同「容器里挂的到底是不是刚改的那份文件」一起做了：它比对
> 宿主 `.env` 与容器 `/app/.env` 的 **md5**，不一致就报警。为什么需要比：
> 有些编辑器保存是「写新文件再改名」，普通 `restart` 可能还挂着旧 inode —— 症状是
> **改了像没改**，而日志上完全看不出区别（配置生效了但没触发，和压根没生效，长得一样）。

---

## 8. 网络：机器人为什么要加入 `mcnet`

机器人要连的三样东西，**只有一样对外是通的**：

| 目标 | 形态 | 容器里怎么连 |
|---|---|---|
| NapCat（OneBot v11） | 同一个 compose 网络 | `ws://napcat:3001` |
| 群组接口（Velocity 插件） | 只在对端 docker 网络里 | `http://velocity:8080` |
| GTNH 的 RCON | 只在对端 docker 网络里 | `gtnh:25575` |

后两样在宿主上原本要靠 ssh 隧道把回环口转过来（旧写法）。**机器人搬进同一个 docker
网络之后隧道就不需要了** —— compose 里给 `bot` 挂了第二个网络 `mcnet`（`external: true`，
那是 MCSM 建的、已经存在的网络），于是可以直接用服务名。

> ⚠️ **服务名只有容器认识**。这是「机器人本体也进容器」的真正理由 —— 如果本体留在宿主
> （venv + systemd），`mcs_servers.toml` 里就**不能**写 `velocity:8080`，只能继续用
> `127.0.0.1:8080` 那种回环口，等于白搬。

`mcnet` 里有什么、服务名叫什么，在宿主上这样看：

```bash
docker network inspect mcnet -f '{{range .Containers}}{{.Name}} {{.IPv4Address}}{{"\n"}}{{end}}'
docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' <容器名>
```

游戏端口对玩家是通的（公网），所以 `mcs_servers.toml` 里给代理/独立服填的
`host`/`port`（公网 IP + 端口）**不用改** —— 那是「对接口报的人数做一次独立 SLP 复查」，
走公网才有意义。

---

## 9. 验收清单

按顺序做一遍，每步都有明确的预期：

1. 离线自测过了：`docker compose run --rm bot python tools/mc_check.py --self-test`
   → `447 PASS / 0 FAIL`。顺手把夜间静默那条也跑了：
   `docker compose run --rm bot python tools/mc_check.py --quiet-test` → `全部通过`
   （它要 init nonebot 读 `.env`，所以在容器里跑，不是在裸机上）。
2. `docker compose ps` → 两个容器都 `Up`；`docker compose logs bot | tail -50` 里能看到
   `Bot <QQ号> connected`。
3. 群里 `@机器人 你好` → 回「收到！被动回复链路已打通 🎉」。
4. 群里 `@机器人 oopz` → 列出语音频道在线成员（没有人也应有标题行，而不是沉默）。
5. 群里 `@机器人 mc` → 总览（用 MC 功能时）。看到 `[OK]`/正常人数即可；某台报「连不上」
   就回第 6 节的工具去查。
6. `@机器人 mc <服名>` → 单服明细。
7. 白名单（配了才做）：`@机器人 whitelist list` → 名单；再 `whitelist add <一次性假名字>`
   → **重新 list 确认真的多了一个**（回执不可信，反查才算数）→ `whitelist remove` 收尾。
8. 等一个播报周期（默认 60 分钟/整点）确认定时播报；或让人进服验进服提醒。
9. `docker compose restart` 一次，确认**不用重新扫码**、机器人自动连回来；
   宿主的 `logs/napcat_bot_$(date +%F).log` 在**继续增长**。

---

## 10. 坑

| 症状 | 原因 | 修法 |
|---|---|---|
| 容器反复重启，日志里 `PermissionError` 或 `logs/...` 相关 | `./logs` 不存在，或 `BOT_UID` 那个 uid 写不进去 —— loguru 的 `add()` 在**导入期**抛错 | `mkdir -p logs`；确认 `export BOT_UID=$(id -u)`（compose 默认值是 1000，可能不是你） |
| 容器 Up、也不报错，但群里**永远没反应** | 机器人没连上 NapCat。**`.env` 是按 cwd 找的**，cwd 不对时它会拿空 WS 列表正常启动 | 容器里 `docker compose exec bot env \| grep -i onebot` 应能看到 WS 地址；对不上就查 compose 的挂载与 `WORKDIR` |
| NapCat 容器 `Up`，机器人却刷屏 `Cannot connect to host napcat:3001` | NapCat 停在登录界面没登进去 —— 3001 那个正向 WS 服务端要**登录之后**才起，所以是 `Connection refused` 而不是握手 401 | `docker compose logs napcat \| grep -a 二维码`：还在打二维码就是没登录。`.env` 里补 `NAPCAT_QQ=<QQ号>`，再 `docker compose up -d --force-recreate napcat` |
| 同上，且环境里配过代理 | 容器里有 `HTTP_PROXY` 之类 —— nonebot 的 aiohttp driver 是 `trust_env=True`，会把代理用上去 | 去掉代理变量后 `docker compose up -d` |
| 机器人报连不上 NapCat，配置里写的是 `127.0.0.1:3001` | 容器里的 `127.0.0.1` 是**它自己** | 改回 `ws://napcat:3001` |
| `--list-targets` 之类的工具在宿主 shell 里报解析不了主机名 | 服务名只有容器网络的 DNS 认识 | 用 `docker compose exec bot python tools/...` |
| 每次重启容器都要重新扫码 | compose 少挂了 `./napcat-data/QQ` 卷（登录会话在里头） | 补上卷再 `docker compose up -d`，重新扫一次，之后就记住了 |
| WebUI 打不开 | 端口只绑了回环（这是**故意的**） | `ssh -L 6099:127.0.0.1:6099 <服务器>`，本地开 <http://127.0.0.1:6099/webui> |
| 播报时刻偏 8 小时 / 日志时间和直觉对不上 | 主机时区是 UTC | `sudo timedatectl set-timezone Asia/Shanghai`（compose 里的 `TZ` 只管容器） |
| `docker compose up` 报端口被占 | 6099 上已经有别的东西 | 改 compose 里 `:` 左边那半边 |
| 群里报「接口认证失败」 | **不是网络问题**，是 token 被对方轮换了 | 去要新 token 填进 `mcs_servers.toml`，`docker compose restart bot` |
| 群里报「数据来源 szu 的接口这次没取到」 | 接口连不上（对端重启、网络抖动，或服务名/端口写错） | 先 `docker compose exec bot python tools/mc_check.py --api` 定位 |
| 镜像构建到 `pip install` 就失败/极慢 | 网络问题 | `Dockerfile` 里已用清华源；换源或检查出网 |
| `docker compose up` 报 `network mcnet ... not found` | 对端那台没有这个网络（或换了机器/重装过） | `docker network ls` 确认名字，改 compose 里的 `mcnet` 为实际名字 |
| 群消息时有时无、大段延迟 | 机器负载 / 容器没限制资源 | 看 `docker stats`；这台机器别同时跑重活 |
| 想用 docker 组省 sudo | **`docker` 组等价于 root**（能挂宿主根目录） | 宁可 `sudo docker ...` |

---

## 11. 日常维护与备份

```bash
cd /opt/qqgroupbot
docker compose logs -f bot                                # 跟日志
docker compose logs bot --since 1h                        # 最近一小时
tail -f logs/napcat_bot_$(date +%F).log                   # 同一份日志的落盘版
docker compose restart bot                                # 改完 .env / 两份 toml 之后
```

**要备份的只有四样**（其余都能重建）：

- `.env`（凭据 + 行为开关）
- `mcs_servers.toml`（服务器清单 + RCON 密码 + 接口 token）
- `mcs_audiences.toml`（群关联）
- `napcat-data/`（网络配置 + 登录会话）—— **丢了就要重新扫码**

**更新代码**：

```bash
cd /opt/qqgroupbot
git pull
export NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) BOT_UID=$(id -u) BOT_GID=$(id -g)
docker compose up -d --build        # 依赖有变时必须 --build；只改代码其实也一样走它
```

`OOPZ_JWT_TOKEN` 约 31 天过期，症状是统计/播报**突然全部失效**（DEPLOY.md 的 F7）：
重跑第 5 节 → `docker compose restart bot`。

---

## 附录 A：不用 Docker 跑机器人本体（宿主 venv + systemd）

**什么时候用**：机器人和 MC 服务端**不在同一台机器上**（这时必须有隧道，也就用不了
`mcnet` 的服务名），或者你就是不想让本体进容器。

这时 NapCat 仍然用第 2 节那个容器，但 **3001 要发布出来**（本体在宿主上，
够不着 compose 的内部网络）—— 在 `docker-compose.yml` 的 `napcat` 上补一行：

```yaml
    ports:
      - "127.0.0.1:3001:3001"          # ← 加这行（仍然只绑回环）
      - "127.0.0.1:6099:6099"
```

然后 `docker compose up -d napcat`，其余照旧。宿主这边：

```bash
cd /opt/qqgroupbot
python3 -m venv .venv
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

> ⚠️ **这条命令必须在仓库根跑**：`requirements.txt` 最后一行是相对路径，pip 按 cwd 解析。
> `python3 -m venv` 报 `ensurepip is not available` → 装一下
> `sudo apt install python3-venv`（或 `python3.12-venv`）。

配置上的差别（就这两处，其余一字不改）：

- `.env` 的 `ONEBOT_V11_WS_URLS` 填 `'["ws://127.0.0.1:3001"]'`
- `mcs_servers.toml` 里所有地址填**宿主回环**（`127.0.0.1:8080` / `127.0.0.1:25575`），
  **不能填服务名** —— 宿主解析不了 docker 的服务名

MC 服务端在**另一台**机器上时，还要把端口转过来，交给 systemd 而不是手动 `ssh -N`：

```bash
# 先把 deploy/linux/mc-tunnel.service 里那五行改成你的实际情况（密钥 / 账号 / 端口 / -L）
sudo cp /opt/qqgroupbot/deploy/linux/mc-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mc-tunnel
systemctl status mc-tunnel      # active (running) = 隧道在
```

> ⚠️ 隧道**不是**常驻网络设施：网络抖动、对端重启、笔记本休眠之后 ssh 都会断，
> 症状是群里报「连不上 / 数据来源取不到」（**不是 401**，401 是 token 的事）。
> `Restart=always` 让它自己回来。密钥要放在运行账号的 `~/.ssh/` 下并 `chmod 600`；
> **`-i` 不能省**、**`-L` 右边写 `127.0.0.1:端口`**（那个主机名由对端解析，写 docker
> 别名必失败）—— 理由都写在单元文件的注释里。

本体挂到 systemd：

```bash
# 把 deploy/linux/oopz-bot.service 里的 User/Group/WorkingDirectory/ExecStart 改成实际情况
sudo cp /opt/qqgroupbot/deploy/linux/oopz-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now oopz-bot
journalctl -u oopz-bot -f
```

> ⚠️ **`WorkingDirectory` 必须是仓库根**。理由见第 7 节那段 —— 认 cwd 的是**两样**：
> `logs/` **和 `.env`**。写错了不只是日志落错地方，机器人还会「正常启动但永远不连
> NapCat」，两样都很难查。
