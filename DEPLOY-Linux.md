# 从零部署指南（Linux）

本项目是 **QQ 群机器人 + oopz 语音频道在线统计**。这份是 Linux 版；Windows 版在
[DEPLOY.md](DEPLOY.md)。

**两份的分工**（免得你两边找）：

| 内容 | 在哪 |
|---|---|
| Linux 上「怎么装起来、怎么挂服务、怎么排查平台层的问题」 | **本文** |
| `.env` 各键的含义、`mcs_servers.toml` / `mcs_audiences.toml` 的字段语义、常见问题的业务侧排查（F1–F11） | [DEPLOY.md](DEPLOY.md)（内容与平台无关，Linux 同样适用） |
| 三份模板的完整注释 | 仓库根的 `*.example` |

代码本身**没有平台假设**（配置路径走 `Path(__file__)`、日志是相对 `logs/`），所以
Windows 上跑得通的写法 Linux 上也跑得通 —— 差别全在**怎么把它跑起来**：Windows 那份是
双击 `.bat`，这份是 systemd，而 NapCat 这边用 Docker。

**要用到的现成文件**（都在仓库里，不用手抄）：

```
deploy/linux/
├─ docker-compose.yml       # NapCat 协议端容器
├─ oopz-bot.service         # 机器人本体（systemd）
└─ mc-tunnel.service        # MC 取数用的 ssh 隧道（systemd，可选）
```

---

## 1. 环境准备

- 一台 Linux（x86_64 / arm64 都行），**用 systemd** 的发行版（Ubuntu 22.04+ / Debian 12+ 等）
- **Python ≥ 3.10**（推荐 3.12）：`python3 --version`
- **Docker + compose 插件**：
  ```bash
  docker --version && docker compose version     # 注意是 `docker compose`（子命令），不是 `docker-compose`
  ```
  没有的话按发行版装：Debian/Ubuntu 用官方脚本或 `apt install docker.io docker-compose-plugin`。
- 一个**普通 QQ号**（用作机器人本体）+ 一个目标 QQ 群
- 一个 oopz 账号（已有加入的语音域）
- （可选，MC 功能用）一个 Java 版 MC 服务端，能改 `server.properties` 并重启

**先做三件一次性的准备**：

```bash
# ① 时区。播报是「整点对齐」的，主机跑 UTC 的话播报时刻会整体偏 8 小时，
#    日志时间戳也会和你的直觉对不上（DEPLOY.md 的 F5 排查会因此走偏）。
sudo timedatectl set-timezone Asia/Shanghai
timedatectl                                  # 确认 Local time 对

# ② 一个专用账号（不要用 root 跑机器人，也别用你自己的登录账号）。
#    nologin 只挡「交互式登录」，systemd 用 User= 起服务不受影响。
sudo useradd -r -s /usr/sbin/nologin -d /opt/oopz-bot oopzbot

# ③ 把一个已有版本的仓库放进去（git clone 或 rsync 上来）
sudo mkdir -p /opt/oopz-bot && sudo chown oopzbot:oopzbot /opt/oopz-bot
#    然后把工程放进 /opt/oopz-bot/，目录里应当能看到 bot_napcat.py、requirements.txt、
#    vendor/、tools/、deploy/ 等。下文所有路径都以 /opt/oopz-bot 为例。
```

> ⚠️ **`.env` 和 `mcs_servers.toml` 不在仓库里**（它们在 `.gitignore` 中），`git clone`
> 出来是没有的 —— 见第 4 节，从 `.example` 复制并填写。**密码别通过聊天工具明文发**，
> 用你熟悉的安全渠道传。

---

## 2. NapCat 协议端（Docker）

NapCat 是 QQ 机器人的协议实现，用个人 QQ 号模拟登录，把消息包装成 OneBot v11 接口。

```bash
sudo mkdir -p /opt/napcat && sudo chown $(id -u):$(id -g) /opt/napcat
cp /opt/oopz-bot/deploy/linux/docker-compose.yml /opt/napcat/
cd /opt/napcat

export NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g)
docker compose up -d
docker compose logs -f napcat          # 首次会打印二维码（也可在 WebUI 里看）
```

用机器人那个 QQ **扫码登录**（手机 QQ → 扫一扫）。登录成功后：

**开一个正向 WebSocket 服务**（机器人连它）：

1. 打开 WebUI：`http://127.0.0.1:6099/webui`
   - 服务器上没桌面的话，在**你自己电脑**上转过去：`ssh -L 6099:127.0.0.1:6099 <你的服务器>`，
     然后本地浏览器开 <http://127.0.0.1:6099/webui>。
2. 首次登录的口令是 `napcat` —— **进门第一件事就是改掉它**（能进这个面板就能操作那个
   QQ 号，等于账号）。
3. 「网络配置」→ 新建 **WebSocket 服务端**（正向 WS）：
   - `host`: `0.0.0.0`（容器内，宿主机只会映射到回环，见下行）
   - `port`: `3001`
   - `messagePostFormat`: `array`
   - `enableForcePushEvent`: 开启
   - 勾选后会自动生成 **token**，复制出来，第 4 节填进 `.env`
4. 保存并启用。

> ⚠️ **容器那三个端口只绑回环**（compose 文件里写的是 `127.0.0.1:3001:3001` 这样）。
> 别照抄网上「云服务器要在安全组放行 6099」的说法：6099 能扫码、能改这个 QQ 的所有
> 网络配置，3001 拿到 token 就能以这个 QQ 的身份收发消息 —— 放公网等于把账号交出去。
> 要从外面看 WebUI 就用上面的 `ssh -L`。
>
> ⚠️ 两个卷都要挂（compose 里已写好）。**少挂 `./QQ` 的话每次重启容器都要重新扫码**，
> 因为登录会话就在里头。

---

## 3. Python 环境

```bash
cd /opt/oopz-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

> **oopz 默认一起装上**：`requirements.txt` 最后一行 `oopz-sdk @ file:./vendor/Oopzbot-SDK`
> 会把 `vendor/` 里那份 SDK 装上，不用再单跑一条 pip。**那一行是本地相对路径，pip 按
> 当前工作目录解析**（实测），所以必须先 `cd /opt/oopz-bot` —— 在别处
> `pip install -r /opt/oopz-bot/requirements.txt` 会报找不到 oopz_sdk。
>
> 只要 MC 功能、不要 oopz：把它注释掉再装（受支持，机器人照常起、oopz 停用并明说原因，
> 见 DEPLOY.md 第 3 节）。补装：`.venv/bin/pip install ./vendor/Oopzbot-SDK`。

> `python3 -m venv` 报 `ensurepip is not available` 之类的错 → 装一下 venv 模块：
> Debian/Ubuntu `sudo apt install python3-venv`（或对应版本号的 `python3.12-venv`）。

**关于 playwright**：`oopz_sdk` 的依赖里会**装上 playwright 这个 Python 包**，但
**不需要**下载浏览器 —— 那是「用账号密码开浏览器登录」那条路才要的（无桌面服务器还得
再配 Xvfb）。标准的凭据流程（第 5 节）走的是纯 API，不碰浏览器。真要用浏览器那条路时：

```bash
.venv/bin/python -m playwright install --with-deps chromium
```

> 国内装依赖慢的话给 pip 挂个镜像（清华 `https://pypi.tuna.tsinghua.edu.cn/simple`），
> 用 `.venv/bin/pip install -i <镜像> ...`，别改全局配置。

---

## 4. 配置

**三份配置，从模板复制**（都在仓库根）：

```bash
cd /opt/oopz-bot
cp .env.example .env
cp mcs_servers.toml.example mcs_servers.toml          # 用 MC 功能才需要
cp mcs_audiences.toml.example mcs_audiences.toml      # 用 MC 功能才需要

chmod 600 .env mcs_servers.toml
chmod 640 mcs_audiences.toml
sudo chown oopzbot:oopzbot .env mcs_servers.toml mcs_audiences.toml
```

> ⚠️ **权限不是可选项**：`.env` 里有 QQ 的 token、oopz 的 JWT 和私钥；
> `mcs_servers.toml` 里有各服 RCON 明文密码。600 + 专用账号 = 同机其他用户读不到。
> （service 单元里的 `UMask=0007` 是第二道，别只靠它。）

**填什么**：每个键的含义见 [DEPLOY.md 第 4 节](DEPLOY.md)（表格是平台无关的），
两份 `.toml` 的字段语义见它们自己的 `.example` 注释与 [DEPLOY.md 4.1 / 4.2](DEPLOY.md)。
Linux 上要额外注意的只有两点：

1. **路径**：`MCS_SERVERS_TOML` / `MCS_AUDIENCES_TOML` 不填就用仓库根的那两份。要指到
   别处就用绝对路径（`/opt/oopz-bot/xxx.toml`），别写 `~` —— systemd 不展开它。
2. **`OOPZ_PRIVATE_KEY` 是一行**：真实的换行写成字面 `\n`（`.env` 里不能有真换行）。
   从别的机器拷 `.env` 过来最容易在这一行出问题（多一个空格都会让 JWT 签名失败）。

---

## 5. 生成 oopz 凭据

```bash
cd /opt/oopz-bot
OOPZ_LOGIN_PHONE='你的oopz手机号' OOPZ_LOGIN_PASSWORD='你的oopz密码' \
    .venv/bin/python tools/oopz_login.py
```

脚本会把 `OOPZ_DEVICE_ID` / `OOPZ_PERSON_UID` / `OOPZ_JWT_TOKEN` / `OOPZ_PRIVATE_KEY`
写进 `.env`。**手机号和密码只从环境变量读、不会被保存**（所以不要把它们写进 `.env`）。

> 在服务器上跑这条命令时留意 shell 历史：前面加个空格（` OOPZ_LOGIN_PHONE=...`）可以
> 让 bash 不记它（`HISTCONTROL=ignorespace` 时）。或者干脆本地跑完把四个值拷过去。

---

## 6. 启动前先验一遍（不起机器人）

工具都在这份工程里，说明见 [tools/README.md](tools/README.md)。Linux 上解释器是
`.venv/bin/python`：

```bash
cd /opt/oopz-bot

# oopz 那边：列出加入的域、各域在线的昵称（**没装 oopz_sdk 就跳过这条** ——
# 这个工具本身依赖 SDK，装了也才有可查的东西）
.venv/bin/python tools/oopz_check.py

# MC 那边（用 MC 功能才需要）：
.venv/bin/python tools/mc_check.py --list-audiences   # 只读：每条群关联看哪几台、白名单发给谁
.venv/bin/python tools/mc_check.py --list-targets     # 只读：服务器清单解析成什么
.venv/bin/python tools/mc_check.py                    # 实机探测（会真的连服务器）
.venv/bin/python tools/mc_check.py --api              # 只看群组接口那一层（隧道通不通）
.venv/bin/python tools/mc_check.py --self-test        # 离线自测，403 条断言
```

**这两步都过了再往下走**：凭据/配置的问题在这里会明说，而挂上 systemd 之后它们只会
变成日志里一串难懂的报错。

---

## 7. 把机器人挂到 systemd

```bash
sudo cp /opt/oopz-bot/deploy/linux/oopz-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now oopz-bot
systemctl status oopz-bot
journalctl -u oopz-bot -f
```

日志出现 `Bot <QQ号> connected` 就是连上 NapCat 了。

> ⚠️ **service 里的 `WorkingDirectory` 必须是仓库根**（`/opt/oopz-bot`），因为日志是
> **相对当前工作目录**写的：`logs/napcat_bot_<日期>.log`。工作目录写成别处，日志就落到
> 别处（`/`、`/root`…），或者因为那儿不可写而在 `add()` 阶段**直接报错起不来** ——
> 而 systemd 状态**照样是 active**，表现是「服务在跑、群里没反应」，很容易查错方向。
>
> 顺带说清另外两样**不受**工作目录影响的东西，免得一起怀疑错：
> `.env` 是 `load_dotenv()` 按**调用它的那个文件**的位置找的（所以仓库放哪都读得到）；
> 两份 `.toml` 默认路径是 `Path(__file__)` 算出来的（所以 `MCS_SERVERS_TOML` 不填
> 也能读到仓库根那两份）。**只有 `logs/` 认 cwd。**

日志有**两处**，都留：控制台那路进 journald（`journalctl -u oopz-bot`），同时按天落盘到
`logs/napcat_bot_YYYY-MM-DD.log`（保留 14 天）。目录不用手建，loguru 会自己创建，但
`/opt/oopz-bot` 得对 `oopzbot` 可写 —— 这也正是第 1 节 `chown` 那一步的用处。

改动配置之后**一定要重启**（`.env` 与两份 `.toml` 都是启动时读一次）：

```bash
sudo systemctl restart oopz-bot
```

---

## 8. ssh 隧道也做成服务（MC 那侧在别的机器上时）

现在 MC 那侧（独立模组服的 RCON、群组服的 HTTP 接口）都只在**对端主机的回环**上监听，
要靠 ssh 把端口转过来。手动拉一条 `ssh -N -L ...` 的问题是**它会断**：网络抖动、对端
重启、你合上笔记本之后，隧道没了就永远没了，而症状是群里报「连不上 / 数据来源取不到」
（**不是 401** —— 401 是 token 的事），很容易往错方向查。所以交给 systemd：

```bash
# 先把 deploy/linux/mc-tunnel.service 里那五行改成你的实际情况（密钥 / 账号 / 端口 / -L）
sudo cp /opt/oopz-bot/deploy/linux/mc-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mc-tunnel
systemctl status mc-tunnel      # active (running) = 隧道在
```

密钥要放在 `oopzbot` 用户的 `~/.ssh/` 下并 `chmod 600`。**`-i` 不能省**（这个密钥不是默认
名字）、**`-L` 右边写 `127.0.0.1:端口`**（那个主机名由对端解析，写 docker 别名必失败）——
理由都写在单元文件的注释里。

**不用 MC 功能的话这一步跳过**，那份 unit 也就别装。

---

## 9. 验收清单

按顺序做一遍，每步都有明确的预期：

1. `systemctl status oopz-bot` → `active (running)`；`journalctl -u oopz-bot -n 50` 里能看到
   `Bot <QQ号> connected`。
2. 群里 `@机器人 你好` → 回「收到！被动回复链路已打通 🎉」。
3. 群里 `@机器人 oopz` → 列出语音频道在线成员（没有人也应有标题行，而不是沉默）。
4. 群里 `@机器人 mc` → 总览（用 MC 功能时）。看到 `[OK]`/正常人数即可；某台报「连不上」
   就回第 6 节的工具去查。
5. `@机器人 mc <服名>` → 单服明细。
6. 白名单（配了才做）：`@机器人 whitelist list` → 名单；再 `whitelist add <一次性假名字>`
   → **重新 list 确认真的多了一个**（回执不可信，反查才算数）→ `whitelist remove` 收尾。
7. 等一个播报周期（默认 60 分钟/整点）确认定时播报；或让人进服验进服提醒。

---

## 10. Linux 专属的坑

| 症状 | 原因 | 修法 |
|---|---|---|
| 服务 active 但日志里没有本该有的行，或另有 `logs/` 落在别处 | `logs/` 是相对 **cwd** 写的，而 `WorkingDirectory` 写错了（`.env` 和两份 toml 都不认 cwd，别怀疑它们） | 改成仓库根，`systemctl restart` |
| journald 里中文乱码 / 日志缺行，或 `UnicodeEncodeError` | systemd 的环境里 `LANG` 是空的，Python 拿 ASCII 当控制台编码 | unit 里有 `Environment=LANG=C.UTF-8`；别删 |
| 每次重启容器都要重新扫码 | compose 少挂了 `./QQ` 卷（登录会话在里头） | 补上卷再 `docker compose up -d`，重新扫一次，之后就记住了 |
| WebUI 打不开 | 端口只绑了回环（这是**故意的**） | `ssh -L 6099:127.0.0.1:6099 <服务器>`，本地开 <http://127.0.0.1:6099/webui> |
| 播报时刻偏 8 小时 / 日志时间和直觉对不上 | 主机时区是 UTC | `sudo timedatectl set-timezone Asia/Shanghai` |
| 群里报「连不上 X 服务器」/「数据来源取不到」 | 隧道断了（对端重启、网络抖动） | 装了 `mc-tunnel` 的话它会自己回来，`systemctl status mc-tunnel` 确认；没装就装 |
| 群里报「接口认证失败」 | **不是网络问题**，是 token 被对方轮换了 | 去要新 token 填进 `mcs_servers.toml`，重启 bot |
| `docker compose up` 报端口被占 | 3001 / 6099 上已经有别的东西 | 改 compose 里 `:` 左边那半边，并同步改 `.env` 的 `ONEBOT_V11_WS_URLS` |
| `python3 -m venv` 建不起来 | 发行版把 venv 拆成单独的包 | `sudo apt install python3-venv`（或 `python3.12-venv`） |
| 群消息时有时无、大段延迟 | 机器负载 / 容器没限制资源 | 看 `journalctl` 和 `docker stats`；这台机器别同时跑重活 |
| 想用 docker 组省 sudo | **`docker` 组等价于 root**（能挂宿主根目录） | 宁可 `sudo docker ...`；机器人本体不需要 docker 权限 |

---

## 11. 日常维护与备份

```bash
sudo systemctl restart oopz-bot          # 改完 .env / 两份 toml 之后
journalctl -u oopz-bot -f                # 跟日志
journalctl -u oopz-bot --since today     # 今天的
tail -f /opt/oopz-bot/logs/napcat_bot_$(date +%F).log   # 同一份日志的落盘版
```

**要备份的只有四样**（其余都能重建）：

- `.env`（凭据 + 行为开关）
- `mcs_servers.toml`（服务器清单 + RCON 密码 + 接口 token）
- `mcs_audiences.toml`（群关联）
- NapCat 的 `config/` 与 `QQ/` 两个卷（网络配置 + 登录会话）

**更新代码**：

```bash
cd /opt/oopz-bot && sudo -u oopzbot git pull
cd /opt/oopz-bot && sudo -u oopzbot .venv/bin/pip install -r requirements.txt   # 依赖有变时
sudo systemctl restart oopz-bot
```

> 两条都带 `cd /opt/oopz-bot`：`.venv/bin/pip` 是相对路径，而 `requirements.txt` 最后
> 那行 oopz_sdk **也是**（见第 3 节）—— 换个目录跑就会去别处找 `vendor/`。

`OOPZ_JWT_TOKEN` 约 31 天过期，症状是统计/播报**突然全部失效**（DEPLOY.md 的 F7）：
重跑第 5 节 → 重启。

---

## 12. 将来：机器人搬进对端的容器网络

计划里二期是把这个机器人整体搬到对端那台机器、加入它的 docker 网络 `mcnet`。到那时候
本文档里**只有两处**要改：

1. `mcs_servers.toml` 里的地址从「隧道这头的回环」改成对端的服务名，
   例如 `api.url = "http://velocity:8080"`、RCON 的 `host = "gtnh"`；
2. 第 8 节那个 `mc-tunnel` 服务**整条撤掉**（不再需要转发）。

其余（systemd 单元、NapCat、配置结构、排查表）一字不用改。
