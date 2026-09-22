# 从零部署指南（Windows 11）

本项目是 **QQ 群机器人 + oopz 语音频道在线统计**。QQ 侧有两条入口，推荐使用 **NapCat 版**（可主动推送定时播报 / 进频道欢迎）；QQ 官方版（`bot.py`）仅被动回复，且官方已停用主动推送。

本文档按"干净环境从 0 部署"编写。当前工程目录已经过清理，只保留运行所需 + 部署所需文件。

> 🐧 **要部署到 Linux**：看 [DEPLOY-Linux.md](DEPLOY-Linux.md)。那份从环境准备到挂服务
> 整套都不一样（systemd 跑机器人、Docker Compose 跑 NapCat），但**第 4 节的配置项含义、
> 第 7 节的验证办法、第 8 节的业务侧排查（F5/F7/F8/F9/F11 等）两份完全通用** ——
> 本文档里 Windows 专属的只有 F1–F4（NapCat 的 DLL / 沙箱坑）和那几条 PowerShell 命令。

---

## 0. 工程文件清单

```
oopz-bot/
├─ bot.py                    # QQ 官方版入口（被动，可选）
├─ bot_napcat.py             # NapCat / OneBot v11 入口（推荐，含主动推送功能）
├─ plugins/                  # QQ 官方版插件（被动回复，@oopz）
├─ plugins_napcat/           # NapCat 版插件根目录
│  ├─ hello/                 # 群消息摘要 + 功能引导
│  ├─ oopz/                  # oopz 语音频道：@查询 / 定时播报 / 进频道欢迎
│  ├─ mcs/                   # Minecraft：@查询 / 进服提醒 / 定时播报 / 白名单管理
│  └─ _shared/               # 跨插件共享工具（下划线前缀，不会被当插件加载）
├─ tools/                    # 人手动跑的工具（机器人本体不 import 它们，详见 tools/README.md）
│  ├─ README.md              # 三个工具的分工、用法、什么时候该跑哪个
│  ├─ oopz_login.py          # 生成 oopz 平台登录凭据（device_id / jwt / 私钥）
│  ├─ oopz_check.py          # oopz 诊断脚本
│  └─ mc_check.py            # Minecraft 诊断脚本（SLP + RCON + 群组接口 + 白名单，含离线自测）
├─ requirements.txt          # Python 依赖清单
├─ vendor/Oopzbot-SDK/       # oopz_sdk 源码（不在 PyPI，随工程分发；requirements.txt 里默认装它）
├─ .env.example              # 行为类配置模板 → 复制为 .env 填写
├─ .env                      # 实际配置（含密钥，勿外泄/勿提交）
├─ mcs_servers.toml.example  # MC 服务器清单模板 → 复制为 mcs_servers.toml 填写
├─ mcs_servers.toml          # 实际清单（含各服 RCON 密码，勿外泄/勿提交）
├─ mcs_audiences.toml.example # MC 群关联模板 → 复制为 mcs_audiences.toml 填写
├─ mcs_audiences.toml        # 实际群关联（不含密码，但含真实群号，勿提交）
├─ DEPLOY.md                 # 本文档（Windows）
├─ DEPLOY-Linux.md           # 从零部署指南（Linux）
├─ deploy/linux/             # Linux 部署用的现成文件（见 DEPLOY-Linux.md）
│  ├─ docker-compose.yml     #   NapCat 协议端容器
│  ├─ oopz-bot.service       #   机器人本体（systemd）
│  └─ mc-tunnel.service      #   MC 取数用的 ssh 隧道（可选）
├─ README.md                 # 项目简介
├─ docs/chat.md              # 维护记录（含 NapCat 踩坑细节）
├─ logs/                     # 运行日志（bot 自动写入）
├─ NapCat/
│  └─ NapCat.Shell.Windows.Node/   # 协议端运行时（见第 2 节）
└─ .venv/                    # Python 虚拟环境
```

> 注意：`.env` 含 QQ AppSecret、oopz JWT 等密钥，切勿提交到公开仓库。

---

## 1. 环境准备

- Windows 11 / Windows 10，64 位
- [Python 3.12](https://www.python.org/downloads/)（64 位，安装时勾选 Add to PATH）
- 一个 **普通 QQ 号**（用作机器人本体，会登录到 NapCat）
- 一个目标 QQ 群（机器人要加入的群，群号后面用到）
- oopz 账号（已有加入的语音域）
- （可选，MC 功能用）一个 **Java 版** Minecraft 服务端，能改 `server.properties` 并重启

> 这几条与平台无关，Linux 上要的是一样的东西 —— 只是装法不同，见
> [DEPLOY-Linux.md 第 1 节](DEPLOY-Linux.md)（另有两条 Linux 特有的准备：时区必须是
> `Asia/Shanghai`，以及**不能用 root 跑机器人**，要建一个专用账号）。

---

## 2. 安装 NapCat（协议端）

NapCat 是 QQ 机器人协议实现，用个人 QQ 号模拟登录，把消息/调用包装成 OneBot v11 接口。

1. 到 NapCat 官方 GitHub Releases 下载最新版：<https://github.com/NapNeko/NapCatQQ/releases>
   - 选 **`NapCat.Shell.Windows.Node`** 发行包（自带 node.exe + QQ NT 原生库，Windows 免环境依赖）。
2. 解压到 `NapCat/NapCat.Shell.Windows.Node/`（保留 zip 中所有文件到同一目录）。
3. **先不要直接启动**，NapCat 最新版有两个已知启动坑，见第 7 节【常见问题】F1/F2，先按步骤打好补丁。
4. 启动：双击 `napcat.bat`（或命令行 `node.exe index.js`），**保持窗口常开**。
   - 首次运行会打印一个二维码，用机器人 QQ 扫码登录（二维码过期会自动刷新，重看终端新图）。
   - 登录成功后进入主界面，终端会显示控制台地址，默认 <http://127.0.0.1:6099/webui>。

### 2.1 配置「正向 WebSocket」服务

WebUI 里操作（NapCat 新版 WebUI 的「网络配置」）：

1. 打开 <http://127.0.0.1:6099/webui> → 网络配置
2. 新增一个 **正向 WebSocket**（WebSocket 服务端 / server）：
   - `host`: `127.0.0.1`
   - `port`: `3001`（bot 侧 `.env` 用同端口）
   - `messagePostFormat`: `array`
   - `enableForcePushEvent`: 开启
   - 勾选后系统会**自动生成 token**，复制保存（第 4 节填进 `.env`）
3. 保存并启用。

---

## 3. 安装 Python 依赖

```powershell
# 在工程根目录
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

> **oopz 默认就一起装上了**，不用再单跑一条 pip：`requirements.txt` 最后一行是
> `oopz-sdk @ file:./vendor/Oopzbot-SDK` —— oopz_sdk 不在 PyPI，源码随工程分发在
> `vendor/` 下。注意那一行是**本地相对路径，pip 按当前工作目录解析**（实测：从别的
> 目录 `pip install -r <这个文件>` 会找不到它），所以这条命令**必须在工程根目录执行**。

> **只要 MC 功能、不要 oopz**（只用 MC 查询 / 白名单）：把 `requirements.txt` 最后那一行
> 注释掉再装，或者装完 `pip uninstall oopz-sdk`。这是**受支持的**状态，不是坏掉 ——
> 机器人照常启动，oopz 那半边自动停用：群里 `@我 oopz` 会明确回一句「本机未安装
> oopz_sdk」，启动日志也吼一声，而 **MC 功能完全不受影响**。
> 以后想要了：`pip install ./vendor/Oopzbot-SDK`（同样在工程根目录），然后重启机器人。

> oopz_sdk 会通过其 `pyproject.toml` 自动拉取 aiohttp / cryptography / pillow / pydantic / playwright / requests 等依赖，无需手动装 —— 这一串（尤其 playwright）只为 oopz 服务，所以不要 oopz 的时候省掉它是实打实的收益。

---

## 4. 配置 .env

```powershell
copy .env.example .env
```

用编辑器打开 `.env`，逐项填写：

| 键 | 填什么 |
|---|---|
| `QQ_BOTS` | （仅 QQ 官方版 bot.py 需要）QQ 开放平台应用 AppID/AppSecret |
| `ONEBOT_V11_ACCESS_TOKEN` | 第 2.1 节 NapCat WebUI 生成的 token |
| `OOPZ_DEVICE_ID` / `OOPZ_PERSON_UID` / `OOPZ_JWT_TOKEN` / `OOPZ_PRIVATE_KEY` / `OOPZ_APP_VERSION` | 先跑第 5 节 `tools/oopz_login.py` 生成后回填 |
| `OOPZ_TARGET_AREAS` | 要统计/播报/欢迎的 oopz 域名（如 `奇妙小房间`），逗号分隔 |
| `NAPCAT_REPORT_GROUP` | 定时播报推送的 QQ 群号（**逗号分隔多群**，留空=不启用）；播报**仅在有人在线时**推送，无人静默 |
| `NAPCAT_REPORT_INTERVAL_MIN` | 播报间隔（分钟，默认 30），**整点对齐**（:00 / :N / :2N… 时刻，如 30→:00/:30） |
| `NAPCAT_WELCOME_GROUP` | 进频道欢迎推送的 QQ 群号（**逗号分隔多群**，留空=不启用） |
| `NAPCAT_WELCOME_INTERVAL_SEC` | 进频道检测轮询间隔（秒，默认 15） |
| `NAPCAT_WELCOME_CHANNELS` | 可选，只欢迎这些频道，逗号分隔；留空=统计范围内全部 |
| `OOPZ_TRIGGER` / `MC_TRIGGER` | @机器人 触发对应查询的关键词（逗号分隔，不区分大小写）；留空用默认值（`oopz` / `mc,我的世界,服务器`） |
| `MC_ADMIN_QQ` | 能用 `whitelist` 管理命令的 QQ 号（逗号分隔）。**留空 = 该功能对所有人关闭**——与其他「留空 = 不限制」相反，是刻意的 |
| `MC_ADMIN_TRIGGER` | 触发管理命令的关键词（默认 `whitelist`）。别填 `add`/`remove`/`list`，也别跟 `MC_TRIGGER` 撞词 |
| ~~`MC_WATCH_GROUP`~~ | **已作废（2026-09-21）**。改成给那条 `[[audience]]` 加 `watch = true`，收件人恒为本条的 `groups`。留着会被当残留报一条警告 |
| `MC_WATCH_INTERVAL_SEC` | 进服检测轮询间隔（秒，默认 10）。进服到被发现的延迟 = 0～本值 |
| `MC_JOIN_MIN_INTERVAL_SEC` | 进服推送最小间隔（秒，默认 15），窗口内的进服合并成一条，防刷屏。**设 0 = 进服立刻推**。最大额外延迟 ≈ 本值 + 一个轮询间隔 |
| ~~`MC_REPORT_GROUP`~~ / `MC_REPORT_INTERVAL_MIN` | 群号那半**已作废**，改成那条 `[[audience]]` 的 `report = true`；间隔（分钟，默认 60，整点对齐）留在这里 |

> ⚠️ **MC 服务器的地址 / 显示名 / 超时 / RCON 密码都不在 `.env` 里。**
> 它们在同目录的 **`mcs_servers.toml`**（一台服一段 `[[targets]]`），见 4.1 节。
> 「**哪个 QQ 群看哪几台服、白名单发给谁、默认先看哪台**」则在
> **`mcs_audiences.toml`**，见 4.2 节。两份 `.example` 各复制一份再填。
> 改完要**重启 bot**。

### 4.1 服务器清单 mcs_servers.toml

机器人现在能同时看多台服（一个代理 + 若干子服，外加不在代理后面的独立模组服），
所以「服务器在哪」这件事从 `.env` 搬到了 `mcs_servers.toml`：

```powershell
Copy-Item mcs_servers.toml.example mcs_servers.toml
```

`mcs_servers.toml` 已在 `.gitignore` 里（含各服 RCON 密码明文，别提交）。它只管
**「有哪些服」**，要填的只有两样：

1. **每台服一段 `[[targets]]`**：`id`（也是 `@查询` 里打的服名）、`name`、`kind`
   （`proxy` / `backend` / `standalone`）、`host` / `port`、`rcon = { port, password }`。
2. **`[defaults]`**：`timeout` / `rcon_timeout` 两个默认超时。

**三样取数通道**（一台目标可以有 API 或 RCON，二者互斥）：

| 通道 | 字段 | 给什么 |
|---|---|---|
| SLP | `host` / `port` | 在线人数、版本、延迟；玩家样本最多 12 个且随机，**不能当名单** |
| RCON | `rcon = { port, password }` | 完整名单，也用来改白名单 |
| 群组接口 | `api = { url, token }` | 一次响应给**整组**每个子服的人数与名单（见下一节） |

> ⚠️ **`[whitelist]` 段和 `[defaults] primary` 已经不在这里了**（2026-09-21 搬到
> `mcs_audiences.toml` 的每条 `[[audience]]`）——它们要**按 QQ 群区分**，不能再是
> 全局一份。留在本文件里会**直接报错**（报错里会说搬到哪），不会静默忽略。
> 只填了这份、没填 4.2 节那份，机器人起不来。

改完**重启 bot**。首选诊断手段是 `--list-targets` —— 它**只读配置、不联网**，
会把「读的是哪个文件、每个目标的名字、**被哪些群关联**、有哪些配置冲突」全打出来，
是排查「新加的服为什么没生效」最快的入口：

```powershell
.\.venv\Scripts\python.exe tools\mc_check.py --list-targets
```

#### 走群组接口（代理那侧装了 HTTP 插件时）

> 这一节和下一节是**两条并行的路**：装了接口的群组服走这一节，自己不装接口的服
> （独立模组服、老群组服）走下一节的 RCON。**接口型目标不需要下一节的任何配置。**

有些群组服不给 RCON，而是给一个带 token 的 HTTP 口。SZUcraft 这套就是：Velocity 插件
`szucraft-bridge`。这条路比 RCON 好 —— 一次 `GET /status` 就带**每个子服**的人数与名单，
所以 N 台子服只发 1 次请求；而且它看的是代理的连接状态，比 RCON 的 `list` 更权威。

对方那个接口一共四个端点（**对方文档已删，这里是唯一记录**）：

| 端点 | 返回 |
|---|---|
| `GET /health` | `{"ok": true}` —— **不需要 token**，所以它是「隧道通不通」的专用探针 |
| `GET /status` | `{"proxy": {"online": N}, "servers": [{"name","online","players"}]}` |
| `GET /whitelist` | `{"enabled": bool, "count": N, "entries": [...]}` |
| `POST /whitelist/add`<br>`POST /whitelist/remove` | 表单 / JSON / query 三种写法都收；`changed` 表示这次是不是真改了（add 已存在的人会返回 `ok` 但 `changed=false`） |

认证三种写法都认：`Authorization: Bearer <token>`（我们在用的）、`X-Auth-Token: <token>`、
`?token=<token>` —— 哪天 Bearer 那条被对方改了，另两种是应急口子。401 = token 不对
（去要新的），404 = 路径不对，400 = 参数不对。

写法是**一台代理配 `api`，全部子服挂到它上面（`source`）**：

```toml
[[targets]]
id    = "szu"                  # 代理
name  = "主服群"
kind  = "proxy"                # api 只能配在 proxy 上
group = "主服群"
api   = { url = "http://127.0.0.1:8080", token = "<对方给的 token>" }
host  = "127.0.0.1"            # 可选：给了就有 SLP，能给接口报的人数做独立复查
port  = 25565

[[targets]]
id         = "bingo"
name       = "Bingo"
kind       = "backend"
group      = "主服群"
source     = "szu"             # 数据去 szu 的接口里拿，自己不发任何请求
source_key = "bingo"           # 对方 velocity.toml 里的服名；不填 = 用这里的 id
```

几条硬规则（写错都在**加载期直接报错**，不会静默生效）：

- `api` 和 `rcon` 不能配在同一台目标上；
- `source` 和 `rcon` 也不能同配 —— 名单会从接口来、白名单却走本机 RCON，两处各说各话，
  于是「机器人说已添加，人还是进不去」（代理层的白名单才是进服校验那一道）；
- `api` 只能配在 `kind = "proxy"` 上，子服一律写 `source`；
- `api.url` **只填到端口**（`http://127.0.0.1:8080`），带路径会报错。

> ⚠️ `token` 是密码，和 RCON 密码一样躺在 `mcs_servers.toml` 里（该文件已 gitignore）。
> 对方轮换 token 后，群里会回「😵 接口认证失败（token 可能被对方轮换了）」——
> 去要新 token 就行，**别去查隧道和防火墙**。

> ⚠️ 地址同样是**机器人本机**能连到的地址。SZUcraft 这套对方把 8080 只发到它自己主机的
> `127.0.0.1`，所以要 ssh -L 转过来（这行是对方给的，原样抄）：
>
> ```bash
> ssh -N -p 222 -L 8080:127.0.0.1:8080 -L 25575:127.0.0.1:25575 \
>     -i ~/.ssh/id_ed25519_mcbot -o IdentitiesOnly=yes -o ServerAliveInterval=30 \
>     mcbot@203.0.113.10
> ```
>
> 隧道是**临时**的（机器人搬家成容器后直接用 `http://velocity:8080`，这一段就能撤）。
> 所以要知道它断掉时的症状：报的是「接口连不上」（不是 401）。隧道不是常驻服务，
> 机器重启/网络抖动之后要**重新拉**，否则群里会开始报取不到数据。

自测（**只看接口这一层，不碰 SLP / RCON**）：

```powershell
.\.venv\Scripts\python.exe tools\mc_check.py --api
```

它把 `/health`、`/status`（逐子服一行，并列出「接口里有、我们没挂」的那几台）和
`/whitelist` 的原文打出来。排查「对方到底有没有把那台子服接进群组」用它最快；
`source_key` 写错时的报错也会**直接列出接口里实际有的服名**，照抄即可。

#### 每台服务端的准备（`server.properties`，改完要重启该服务端）

MC 功能要拿**完整**玩家名单，靠的是 RCON 执行 `list` 命令（SLP 的玩家样本默认最多
12 个随机玩家，`hide-online-players=true` 时还直接为空）。**每一台**出名单的服都要配：

| 配置项 | 值 | 说明 |
|---|---|---|
| `enable-rcon` | `true` | 不开则只能用 SLP 降级路径（只剩人数） |
| `rcon.port` | **每台一个，别撞** | 与游戏端口独立，填进 `mcs_servers.toml` 对应 target 的 `rcon.port` |
| `rcon.password` | 强密码 | 填进 `mcs_servers.toml` 对应 target 的 `rcon.password` |
| `broadcast-rcon-to-ops` | **`false`** | **默认是 `true`**！不改的话，bot 每 `MC_WATCH_INTERVAL_SEC` 秒（默认 10）一次的 `list` 会把输出广播给所有在线 OP，聊天框持续刷屏 |
| `white-list` | `true` | 想用**白名单管理**才需要。不打开时 `whitelist add` 照样能执行、也写进 `whitelist.json`，但服务端不拦人——**bot 看不出来**，只会如实报「已添加」。DEPLOY 无法替你判断，请自行确认 |

> ⚠️ **两台服的 `host:port` 不能相同。** 这不只是「不能同时跑」：探测层面无从分辨，
> 查 A 会把正在跑的 B 的数据当成 A 报出去，界面上看起来完全正常。`--list-targets`
> 会警告，但它不是错误，所以警告要认真看。

> ⚠️ **命令前缀必须和插件注册的命令一字不差。** 4.2 节里每条 `[[audience]]` 的
> `whitelist` 数组，**每一项**的 `command` 决定 RCON 里实际发什么：vanilla / NekoList
> 是 `whitelist`，Global Whitelist 是 `globalwhitelist`，ProxyWhitelist 是 `pwl`。
> 写错的表现是每次白名单操作都回 `Unknown command`、群里报「未生效」，而且**不报错**。
>
> 注意**触发词和命令前缀是两个不同的轴**：群友在群里打的词由 `.env` 的
> `MC_ADMIN_TRIGGER` 决定（默认 `whitelist`），机器人发给服务端的词由这个字段决定。
> 今天两者恰好同名，代理上线后必然分叉。
>
> **一条关联挂了多台白名单服时，前缀是逐台各写各的**：一台写错只影响那一台，
> 而不点名的那几台始终正常 —— 只看「`list` 能出结果」是发现不了的。用
> `--whitelist` 逐台核对（它会把每一台的前缀都打出来）。
>
> **接口型目标（配了 `api` 的那台）不看 `command`**：`/whitelist` 直接收结构化数据，
> 没有命令前缀这回事。字段仍是必填的，填对方插件实际注册的命令名即可（将来要切回
> RCON 就不用再改）。

改完**重启 MC 服务端**（只有走 RCON 的服需要重启；接口那侧改的是对方的配置，重启谁
由对方决定），然后跑诊断脚本确认：

```powershell
.\.venv\Scripts\python.exe tools\mc_check.py                      # 并发探测所选关联的全部目标
.\.venv\Scripts\python.exe tools\mc_check.py --target bingo       # 只看一台
.\.venv\Scripts\python.exe tools\mc_check.py --audience 建筑群    # 换一条群关联的视角
.\.venv\Scripts\python.exe tools\mc_check.py --api                # 只看群组接口那一层
```

每个目标打一个块，块头就是结论（`[OK]` / `[!!]`），末尾汇总 `N/M 个目标正常`；
**只要有目标不正常，退出码就是 1**，可以直接在脚本里判断。

> ⚠️ **探测哪几台服现在是「按群」定的。** 不带 `--audience` 时脚本站在**第一条**
> 群关联的视角上（输出的头两行会写明是哪条），所以 `--target` 能认出的服名、
> 不带参数时探到的服务器集合，都以那条关联为限。**这正是不带参数时不再探测「全部
> 目标」的原因**：群友永远站在某条关联里，脚本必须能复现他看到的那个集合，
> 否则「脚本里跑得通、群里查不到」就查不出来了。要按全量表探测就分别跑每条关联。

`--target` 后面写的服名走**和群里同一套解析**（id / 名字 / 别名 / 唯一前缀，忽略
大小写与全角）**且限定在所选关联内**，所以脚本里跑得通的写法，那个群里的群友打出来
也一定跑得通；认不出来或前缀有歧义都会**明确报错**，不会悄悄退化成「探测全部」。
关联外的服名会被如实报成「认不出」，并在提示里点明这是隔离规则、不是配置写错。

看到块头是 `[OK] … 名单完整` 即成功。若是 `[!!] … 名单不完整`，脚本会打印 RCON
`list` 的**原始输出**和判定原因，照着排查。

> `kind = "proxy"` 的目标例外：代理**本来就不出分服名单**（Velocity 没有可用的 `list`
> 命令），它的 `名单完整` 恒为 `False` —— 这不是故障。脚本对代理只看可达性，
> 报的是全群组总人数，结论是 `[OK] 代理可达`。分服名单要逐个子服查。

> ⚠️ **RCON 是明文协议，密码可被重放**。`rcon.port` 只绑内网/本机，**绝对不要暴露公网**。

### 4.2 群关联 mcs_audiences.toml

`mcs_servers.toml` 只说「有哪些服」。机器人同时服务多个 QQ 群，而**每个群看的服不
一样**（社团群看自己的三台；建筑群自己的组服还没搭），所以「哪个群关联哪几台」是
单独一份配置：

```powershell
Copy-Item mcs_audiences.toml.example mcs_audiences.toml
```

它**不含任何密码**（RCON 密码全在 `mcs_servers.toml` 里），所以**可以单独给人看、
单独贴出来讨论** —— 前提是别把密码抄进来。但含真实 QQ 群号，同样在 `.gitignore` 里。

一条 `[[audience]]` = 一条**群关联**：

```toml
[[audience]]
name      = "社团群"                                       # 必填。日志和诊断里用它指代本条
groups    = [11111111]                                     # 必填，非空。这些群共享本关联
targets   = ["gtnh", "bingo", "backstabbed"]               # 可选，**可为空**。顺序即总览顺序
primary   = "gtnh"                                         # 可选。不带服名的 @查询默认看它
whitelist = [{ target = "bingo", command = "whitelist" }]  # 可选。**一组表**，每台白名单服一项

[[audience]]
name      = "建筑群"                                       # 服还没搭，先占位
groups    = [22222222]
targets   = []
```

**群能不能用 MC 功能，就看它有没有被这里提到** —— 被提到就算开通，没提到就不开通：
群里会明确回一句「🤔 本群还没有开通 MC 查询」并在日志里留一条 warning。
所以 `.env` 里的 `MC_ALLOWED_GROUPS` **已经不作数了**（留着会被当残留报一条警告）。

**「被提到 = 开通查询」不包含推送。** 查询是群友拉（问了才答），推送是机器人自己
说话，所以进服提醒 / 定时播报要不要发给这个群，由本条自己的两个开关决定，
**缺省都是关的**：

| 键 | 缺省 | 作用 |
|---|---|---|
| `watch` | `false` | 本条的 `groups` 收**进服提醒**（有人进服 / 掉线 / 恢复） |
| `report` | `false` | 本条的 `groups` 收**定时播报** |

两个开关只管「发不发」，**收件人恒为本条的 `groups`**，盯哪几台就是本条的 `targets`
（一条关联挂三台服时，一个周期内三台的事件**合成一条**消息）。必须是 TOML 的真布尔：
`watch = "true"` 或 `watch = 1` 都会**报错**，不会静默当真也不会静默当假。

「多久一次」这类参数**继续留在 `.env`**（`MC_WATCH_INTERVAL_SEC` 等）：那是整台机器人
的节奏，不是某个群的偏好，抄进 N 条关联只会变成「A 群改了 B 群没改」。

`targets` 写 `[]` 是**合法**的，不是「没配完」：那个群的查询会回
「🏗️ 本群关联的服务器还没接入，暂时没有可查询的内容」—— 比笼统的「本群还没有开通」
准确：开通了，只是没服可看。**这种时候也别开 `watch` / `report`**：不会有任何输出，
只会多一条警告。等 `targets` 填上，开关就自动开始生效，不用再改。

### 从旧配置搬过来（MC_WATCH_GROUP / MC_REPORT_GROUP → watch / report）

这两个 `.env` 变量已作废。**顺序不能反**，这是本次迁移唯一一个「配错不报错」的坑：

1. 先给要收推送的那条 `[[audience]]` 加上 `watch = true` / `report = true`，**重启**，
   确认推送照旧（自己进一次服；或等一个整点看播报）。
2. 再删掉 `.env` 里的 `MC_WATCH_GROUP` / `MC_REPORT_GROUP` 两行，**重启**，确认启动
   日志里那条「残留」警告消失、而推送仍然正常。

反过来做（先删变量、后加开关）会让提醒**静默消失**，而群里安安静静和「机器人挂了」
长得一模一样 —— 你会去查机器人，而问题只是少了两行配置。

改完**重启 bot**，然后跑：

```powershell
.\.venv\Scripts\python.exe tools\mc_check.py --list-audiences
```

它**只读配置、不联网**，逐条打出覆盖哪些群、关联哪几台服、主服是哪台、白名单发给谁、
**推送开了哪些**，另有一段专门列「**没被任何群关联的目标**」（孤儿）。
把 `mcs_audiences.toml.example` 末尾写的五个坑照抄过来，因为它们正是这个命令要帮你
发现的东西：

1. **一个群只能出现在一条 `[[audience]]` 里，关联名也不能重名。** 写重复会**直接报错**：
   那样「这个群查询该看哪几台」就说不清了，而取先出现的那条正是本工程一直在防的静默猜。
   关联名重名的后果更隐蔽 —— 它是日志和诊断里指代一条关联的唯一标识，两条同名会让
   排错时看到的那些行分不清是哪条。
2. **关联外的服名查不到（刻意的隔离）。** 建筑群打 `@bot mc gtnh` 回「没有叫 gtnh
   的服」，而社团群的服名在建筑群那边根本不存在。反过来，**新加的服忘了关联给任何群**
   是很常见的疏漏：群里打它回「没这个服」，而 `--list-targets` 和启动日志**都会正常
   列出它**。所以两条命令都会专门把孤儿目标标出来，看到就补 `targets`。
3. **`whitelist` 里每一台的 `command` 都必须与服务端插件注册的命令一字不差**
   （见上一节）。多台时逐台都要对。
4. **要用代理管白名单，代理得在这条的 `targets` 里**：数组里每一项的 `target` 必须是
   **本条 `targets`** 之一（不是「全量表里有就行」），否则加载时就报错，不会等到
   群里发命令才炸。代理通常不属于任何群自己看的那几台，所以群组就绪后要给每条要用
   白名单的 `[[audience]]` 都把 `"proxy"` 加进 `targets`，**并在 `whitelist` 里加一项**
   `{ target = "proxy", command = "globalwhitelist" }` —— 只进 `targets` 只是「这个群
   查得到它」，白名单那边要单独声明。
5. **多写一台白名单服 = 同一个人能改的服务器多一台。** `MC_ADMIN_QQ` 鉴的是「人」、
   **不分服**，所以 `whitelist` 每多一项，那个号能改的白名单就多一份。这是**权限扩大**，
   而命令、日志、`--list-audiences` 都只会说「发往 N 台」，不会体现这件事。加项之前
   先确认这个人本来就该管那台服。

> ⚠️ 进服提醒与定时播报**已经不看 `.env` 里的群了**（那两个变量作废）。一条关联挂
> 三台服时，这三台的事件会**合成一条**消息发进本条的 `groups`；掉线与恢复只推给
> **关联了那台服**的群。启动日志里会把「哪条关联盯哪几台」逐条写出来。

**别把两个「白名单」搞混**（这是本节最容易读错的地方）：

| 叫法 | 是什么 | 配置在哪 |
|---|---|---|
| **群关联** | 哪些 **QQ 群**能用 MC 功能、各看哪几台服 | `mcs_audiences.toml` 的 `[[audience]]` |
| **MC 玩家白名单** | 服务端 `whitelist.json`，谁能进服 | 由群里的 `whitelist` 命令管理；发给哪台、用什么前缀看该条的 `whitelist`；谁能执行看 `MC_ADMIN_QQ` |

两者毫无关系、可以同时生效：一个限制「哪个群、看哪几台」，一个限制「哪个玩家能进服」。

> `MC_ALLOWED_GROUPS` 是 2026-09-21 之前的**群白名单**机制，已被「群关联」取代。
> 它不再拦任何人 —— 拦人的是「这个群有没有被某条 `[[audience]]` 提到」。

白名单管理功能要单独验一遍（**只读**，不改服务端）：

```powershell
.\.venv\Scripts\python.exe tools\mc_check.py --whitelist
.\.venv\Scripts\python.exe tools\mc_check.py --whitelist --target bingo   # 收窄到这一台
.\.venv\Scripts\python.exe tools\mc_check.py --audience 建筑群 --whitelist
```

不带 `--audience` 时看第一条群关联；不带 `--target` 时**逐台**列出该关联的全部白名单服
（`--target <服名>` / 裸写服名收窄到一台，认不出来的服名会逐态说明原因并**退出码 2**）。
它打印每台 `whitelist list` 的原文、解析结果和**那一台的命令前缀**，结尾给一句
`N 台中 M 台正常`，有任何一台没读到就退出码 1。接口型目标打的是 `/whitelist` 的
原文（`enabled` + 名单数组），没有「解析」这一层。

看到 `[OK] 解析正常` 就说明该服的白名单输出格式能被正确判定成败；若报「判不出来」，
说明格式被插件改写过，加白命令会回「未能验证」而不是「已添加」（功能仍可用，只是
无法自动确认）。若那条关联没写 `whitelist`，它会直接说明「该群的白名单管理不可用」
并给出该补的那一行 —— 这正是群里那句「⚠️ 本群没有指定白名单服」对应的配置。

接口那条通道会额外说一件 RCON 看不见的事：`enabled=false` 意味着**对方把代理层的
白名单拦截关掉了** —— 名单读得到、也改得动，但谁都能进服。脚本会把这种目标单列出来
并**退出码 1**。

**这台服为什么没读到**（逐台分别看）：`两条通道都没配` 要去 `mcs_servers.toml`
补 `rcon` 或 `api`；`RCON 认证失败` / `接口认证失败` 是凭证不对（后者去要新 token）；
`连不上` 是端口/进程/隧道问题。

> ⚠️ `--whitelist` **不写只读之外的任何东西** —— 它只发 `list`。诊断脚本会真的改动
> 服务端而它没有任何清理逻辑，脚本中途挂掉就会把 `whitelist.json` 留在谁也不知道的
> 状态。要验写入，去群里用**一次性假名字**走完整流程（见下面的联调清单）。

**关于服务端控制台的 RCON 日志**：MC 对**每条** RCON 连接都会打两行 INFO（`Thread RCON Client /… started` / `… shutting down`）。bot 复用一条长连接，所以正常情况下只有 bot 启动时那一组；如果控制台又开始每 10 秒刷一组，说明连接在反复重连——先查服务端是否在重启、`rcon.port` 是否被别的程序占用。

> `broadcast-rcon-to-ops=false` 管的是**命令输出**会不会广播给在线 OP 的聊天框，跟上面这两行监听日志是两套机制，管不到它。

---

## 5. 生成 oopz 登录凭据

```powershell
.\.venv\Scripts\python.exe tools/oopz_login.py
```

按提示用 oopz 应用扫码/登录，脚本会输出 `device_id`、`person_uid`、`jwt_token`、`private_key` 等，**回填到 `.env` 对应项**（`OOPZ_PRIVATE_KEY` 的 PEM 多行要转成字面 `\n` 写在一行里）。

> ⚠️ **`OOPZ_JWT_TOKEN` 约 31 天过期，不会自动续**。过期后 oopz 查询**全部失败**（群里 @统计 无回复、定时播报提示「查询失败」），但机器人与 QQ 的连接不受影响，表面上一切正常，只能靠日志发现。**到期就重跑本步**：`tools/oopz_login.py` 重新生成 JWT → 回填 `.env` → 重启 bot。建议每月固定一天做续期（比如直接在 `.env` 里记下签发日期，或手机日历提醒）。

---

## 6. 启动

顺序：**先 NapCat，后 bot**（两个窗口都保持常开）。

1. NapCat：双击 `NapCat\NapCat.Shell.Windows.Node\napcat.bat`，确认已登录、WebUI 的 WS 服务已启用。
2. bot：

```powershell
.\.venv\Scripts\python.exe bot_napcat.py
```

看到日志 `Bot <QQ号> connected` 即连接成功。日志同时落到 `logs/napcat_bot_<日期>.log`
（按天切，留 14 天），内容与屏幕上逐字一致。

---

## 7. 验证

- 群里 `@机器人 oopz` → 回复 oopz 语音频道在线明细
- 定时播报：到整点槽位（间隔 30 分钟则为 :00/:30），若 oopz 有人在线则推送「📣 oopz 语音频道播报…」（无人在线时静默跳过，属正常）
- 进频道欢迎：有人进入目标域语音频道后，约 1 个轮询周期内推送趣味欢迎语
- **启动日志**：确认这几行都在，它们把两份配置的实际读法全摊开了
  （「新加的服 / 新加的群为什么没生效」看一眼就知道）：
  `MC 目标 N 个：…`、`MC 关联服务器：N 条` 及其下每条 `名字（N 个群）→ …` 和
  `推送   进服提醒、定时播报`、`MC 进服提醒：… 条群关联开了 watch = true` 及其下每条
  `「<关联名>」→ N 台：…`。有警告的话紧跟着分别是
  `MC 目标配置：` / `MC 群关联配置：` / `MC 群关联「<名字>」：` 三类。
- **MC 查询**：在**已开通**的群里 `@机器人 mc` → 回复该群关联的服务器总览（主服排最前）
  - 在该群里 `@机器人 mc <服名>` → 单服明细；打 `@机器人 mc b` 这种半截名 → 列出候选
  - **走群组接口的服**（`api` / `source`）：先离线跑 `tools\mc_check.py --api`，确认
    `/health` 通、`/status` 里有你要的那几台（`--api` 会把「接口里有、我们没挂」的也列出来）。
    它对不上时群里会报「数据来源 X 的接口这次没取到」——那是**隧道断了或 token 过期**，
    不是服务端掉线（见 4.1）
  - ⚠️ **必测跨群隔离**：在一个**没写进** `mcs_audiences.toml` 的群里发 `@机器人 mc`，
    应回「🤔 本群还没有开通 MC 查询」，且日志里有一条同等信息的 warning（见 F10）
  - 若某条关联 `targets = []`（配置里那条占位条目就是，群号是假的所以没法在群里测），
    那个群应回「🏗️ 本群关联的服务器还没接入」——**不是**「本群还没有开通」。
    两者混了说明闸门顺序错了。这条可以离线验：`--audience <该条名字> --target <任意服>`
    会说明「这条关联里没有任何服务器，无可探测」，而不是拿全量表兜底
  - 关联外的服名（在该条关联下的群里打 `@bot mc gtnh`）应回「没有叫 gtnh 的服」，
    这是刻意隔离；离线对应 `--audience <该条名字> --target gtnh`
- **MC 群关联配置**（离线，不用等群消息）：
  ```powershell
  .\.venv\Scripts\python.exe tools\mc_check.py --list-audiences
  ```
  每条关联覆盖哪些群、关联哪几台服、白名单发给谁、**收不收推送**（`推送 进服提醒、定时播报`
  / `推送 不推送（watch / report 都没开）`），以及有没有「没被任何群关联的孤儿目标」。
  一个群「查询一切正常、却什么都收不到」时，先看这行。
- **MC 进服提醒**：自己进服，最慢 `MC_WATCH_INTERVAL_SEC` + `MC_JOIN_MIN_INTERVAL_SEC` 秒内推送「🎮 X 加入了…」（默认 10+15，即 25 秒内；两人紧挨着进服会合并成一条）
  - ⚠️ **首次启动只建基线**（当时已在线的玩家不算「新进服」），所以重启后不会刷屏。若重启后立刻推出一堆存量玩家，说明基线门控有 bug。
  - **一轮探全部关联服**：那条关联 `targets` 里有几台就盯几台。往关联里的**任意一台**（不只是主服）进人，都该收到提醒——这是 P7 修掉的那个症状。
  - **合成一条消息**：`MC_JOIN_MIN_INTERVAL_SEC` 窗口内三台服依次进人 → **一条** `🎮 MC 动态` 消息分 `【服名】` 块列出，不是三条。
  - **同组换服**：在 `group` 相同的两台（`主服群` 里的 bingo / 谁是杀手）之间移动 → 一条 `🔄 X 从「<来源服>」换服过来`。跨 group 移动（GTNH ↔ bingo）**只报普通加入**，没有 `🔄`——跨组时玩家确实断线重连了，说「从哪来」是猜。
    - ⚠️ **`🔄` 要求「退出一台」和「进入另一台」落在同一轮探测里**（10 秒内走完）。切换若跨过一轮（退出、加载、再进，中间有整轮没在任何一台服上），就退化成普通加入 `⛏️ X 上线了 <服名>`——**不是 bug**，是对账层刻意的保守：跨轮的名字移动可能真的是「退了去吃饭、半小时后才进另一台」，只有同一轮看到才算同一次移动。
    - 代理还没上线的今天，两台之间移动**实际是断线重连**，报「换服」只是措辞上的近似；等真代理搭好才是无缝跳转。
  - **退服不推**：只退出、没有别人进服时，群里**不应有任何消息**（`reconcile` 会算出退服事件，是推送层刻意不发的）。注意「换服」不算退服 —— 同组两台之间移动会发 `🔄`（上一条），只有**真的走了、也没出现在别的服上**才什么都不发。
  - **掉线提醒不被限流压住**：把 `MC_JOIN_MIN_INTERVAL_SEC` 调大（如 120）重启，停掉一台服 → 掉线提醒当轮就发，不等窗口。恢复时同理。
  - 提醒只发进**开了 `watch = true`** 的那些关联的群；同一台服被两条关联共用时，两条各自收到。
  - **对照日志确认**：每推一条都有一行 `MC 进服提醒（「<关联名>」）→ N 个群：` 并附发出去的原文。
    「自己进了服但日志里没这行」= 那一轮**没在对账里看到你**（多半是探测时你还没进服、或进的不是那台），
    而不是「推了但没记」。一条都没送达会是 warning，它才是「bot 不在群里 / 没连上 NapCat」。
- **MC 定时播报**：
  - 到整点槽位，播报**列出该关联的全部服务器**，每台一个 `【服名】` 块。
  - **某台离线或没人 → 只影响它那一块**（`😵 不可达` / `目前无人`），整条播报照发。今天那种「单目标 0 人 → 整条不发」在多目标下已经不适用。
  - 整条跳过的只剩两种情况，且**日志里的原因不一样**（都写成「静默跳过」的话，就没法判断是没人还是全挂了）：
    - 「「<关联名>」关联的 N 台服现在都没人在线，本次跳过」
    - 「「<关联名>」关联的 N 台服全部探测失败，本次跳过」
  - 只挂一台代理的关联**不会**被判成「无人在线」——代理的 `count` 是全群组总人数，有人就是有人。
- **MC 玩家白名单管理**（配了 `MC_ADMIN_QQ` 才需要验）：
  1. 用**不在** `MC_ADMIN_QQ` 里的号发 `@机器人 whitelist add CodexTest` → 应回「🚫 你没有 MC 管理权限」。这一步同时验证了 hello 不会再来抢答。
  2. 用管理员号发同一条 → 应回「✅ 已将 CodexTest 添加到白名单。」
  3. `@机器人 whitelist list` → 列表里能看到 `CodexTest`。
  4. `@机器人 whitelist remove CodexTest` → 「✅ 已将 CodexTest 移出白名单。」再 `list` 确认已消失。
  - ⚠️ **务必用一次性假名字**，别拿真实玩家的名字试——这条链路会真的改服务端 `whitelist.json`。走完第 3、4 步就回到原样。
  - bot 是「先读名单 → 再下命令 → 再读名单确认」。读第一次是为了照抄服务端记录的拼写、并判断「本来就在 / 本来就不在」，读第二次是因为 `whitelist add` 的回执是本地化文案、不足为凭。所以回复慢一点是正常的：每次变更最多 3 个 RCON 往返（已经是目标状态时只花 1 个），最坏 3×`[defaults].rcon_timeout` 秒。
  - **服名写在末尾，一次只打一台**：`whitelist add CodexTest bingo`。只有一台白名单服的关联**可以省服名**，行为与以前逐字相同（上面 2~4 步就是省略写法，要跑一遍确认没回归）。
  - **多条白名单服的关联额外验这四条**（临时把该条的 `whitelist` 加成两台，验完**改回一台**）：
    1. `@机器人 whitelist list` → 按 `【服名】` 分块，两台名单都在；回复里的表头是「📋 MC 玩家白名单（N 台服）：」。一台上线一台停掉，重跑一遍，确认停掉的那台只出一行说明、另一台照常列出。
    2. `@机器人 whitelist add CodexTest`（不点名）→ 应回「⚠️ 本群有 2 台白名单服，命令里要点名发给哪台：…」。**没有「默认那台」的回退**，这是刻意的。
    3. `@机器人 whitelist bingo add CodexTest`（服名写在**动词前面**，旧版会**静默把它丢掉**再发往缺省那台）→ 应回「⚠️ 服名要写在最后…」并给出改好的整条命令。这是本次修掉的两个真 bug 之一。
    4. `@机器人 whitelist list gtnh`（本群**有**这台服、但它不是白名单服）→ 应回「GTNH 是本群关联的服，但它不是白名单服」而**不是**「没有叫 gtnh 的服」——后者是假话，会让人去改一个本来没错的地方。
  - ⚠️ **验完务必把 `whitelist` 改回一台**：多一台白名单服 = 管理员能改的服务器多一台（`MC_ADMIN_QQ` 鉴人不分服）。
- 查看日志确认无报错：

```powershell
Get-Content logs/napcat_bot.log -Encoding UTF8 -Tail 30
```

---

## 8. 常见问题（已知坑）

### F1 闪退 `bad option: --no-sandbox`
原因：NapCat `napcat/napcat.mjs` 把 `--no-sandbox` 当 node 的 `execArgv` 传给纯 node worker，node 不认该参数。
修复：编辑 `napcat/napcat.mjs`，找到 `execArgv: ["--no-sandbox"]` 处改为 `execArgv: []`（Shell 模式无需沙箱参数）。

### F2 闪退 `wrapper.node: The specified module could not be found`（缺 crypto.dll/ssl.dll）
原因：`wrapper.node`（QQ NT 原生模块）常规导入 OpenSSL 的 `crypto.dll`/`ssl.dll`，NapCat 发行包未带。
修复：
1. 确认 NapCat 内置的 QQ NT 版本（本机为 `9.9.32.50969`）。
2. 下载匹配版本的 QQ NT 安装包，例：`https://qqdl.gtimg.cn/qqfile/QQNT/9.9.32/beta/a33ab721/QQ9.9.32.50969_x64.exe`
3. 用 7-Zip 解包：`"C:\Program Files\7-Zip\7z.exe" x QQ9.9.32.50969_x64.exe -oQQ_extract`
4. 把解包后 `versions\9.9.32-50969\resources\app\` 里**缺的非系统 DLL**（本机共 48 个）复制到 NapCat 根目录。判断"缺"用 [Dependencies](https://github.com/lucasg/Dependencies) 打开 `wrapper.node` 看未满足的导入。
> 详细排查过程记录在 `docs/chat.md`。若只缺 crypto.dll/ssl.dll 两个，直接补这两个即可。

### F3 WS 连接建立后被 NapCat 以 1006 断开
原因：正向 WebSocket 服务开了鉴权，bot 没带 token。
修复：`.env` 的 `ONEBOT_V11_ACCESS_TOKEN` 必须等于 NapCat WebUI 生成的 token。

### F4 WS 每隔约 30s 超时重连（日志刷 TimeoutError）
原因：适配器 WS 接收超时默认 30s，与 NapCat 心跳 30s 赛跑。
修复：**已内置**。`bot_napcat.py` / `bot.py` 启动时用 monkeypatch 把 WS receive 超时拉到 90s，无需手动改依赖。若升级 nonebot 后失效，把 `bot_napcat.py` 顶部那段 `_patched_websocket` 保留即可（随工程自带）。

### F5 定时播报不触发
- 检查：`.env` 的 `NAPCAT_REPORT_GROUP` 已填群号、bot 在该群里、`OOPZ_TARGET_AREAS` 拼写与域名一致。
- **注意：oopz 无人在线时本就不推送**（定时播报只在有人时生效），别把「无人静默」当成故障。

### F6 进频道欢迎不触发
- 首次启动只建基线（当时已在频道的人不欢迎），之后**新进入**才触发。
- 确认 `NAPCAT_WELCOME_GROUP` 已填、轮询无报错（看日志 `auto_reporter` 行）。

### F7 统计 / 播报突然全部失效
大概率是 `OOPZ_JWT_TOKEN` 过期（约 31 天有效期，见第 5 节警告）。
- 验证：手动跑 `tools\oopz_check.py`，看查询是否报 token 相关错误；或看日志里 oopz 请求的报错。
- 修复：重跑 `tools\oopz_login.py` → 回填 `.env` → 重启 bot。

### F8 MC 进服提醒不触发
**先跑诊断脚本**，它会把每一环都摊开打出来：

```powershell
.\.venv\Scripts\python.exe tools\mc_check.py
```

- **名单完整 False** → 进服提醒会**静默暂停**（不误报，但也不推消息）。这是刻意的设计：名单残缺时（比如只拿到 12 条随机样本）推「进服」全是假的。
  - 该目标**两条通道都没配**（既没 `rcon.password` 也没接口数据源，见 4.1）：在线人数 ≤12 时 SLP 样本本身就是完整名单；超过就必须开一条通道。
  - 走接口的目标（配了 `api` 或 `source`）：名单是**对方插件**给的，我们原样报。人数和名单条数对不上就是对方那边报了人数没给全名单 —— 跑 `--api` 看接口原文。
  - 配了 RCON 仍不完整：看脚本打印的 `list` **原始输出**——可能是插件改写了 `list` 格式，或 `enable-rcon` 没生效。
- **名单完整 True 但仍不推** → 查**那个群所属的 `[[audience]]` 有没有写 `watch = true`**
  （见 4.2）。不写就是默认不收推送——这是刻意的：新加一条关联不该悄悄开始说话。
  离线确认：`--list-audiences` 每条会打一行 `推送 …`，没开就是 `不推送（watch / report 都没开）`。
  - 那个群**压根没写进 `mcs_audiences.toml`** → 它连查询都没开通，更不会有推送。
  - 写了 `watch = true` 但 `targets = []` → 加载时会带一条警告，推送对它不会有任何输出
    （不是故障，配置填好自动生效）。
  - `.env` 里的 `MC_WATCH_GROUP` / `MC_REPORT_GROUP` **已经不生效了**。留着只会在启动日志里
    报一条残留警告，不会让推送恢复——迁移顺序见 4.2 的「从旧配置搬过来」。
- **只收到一部分服的提醒**（另一台有人进服却毫无反应）→ 看启动日志里 `MC 进服提醒：… ` 那段
  逐条打出的 `「<关联名>」→ N 台：<服名>[<id>]、…`。漏掉的那台就是没写进那条关联的 `targets`。
  P7 之前进服提醒只盯主服，往别的服进人不会有任何反应；现在 `targets` 里有几台就盯几台。
- **看不出推送到底盯着哪几台** → 启动日志里这两段：
  ```
  MC 进服提醒：N 条群关联开了 watch = true
    「<关联名>」→ N 台：<服名>[<id>]、…
  ```
  一条都没开时打印的是「没有任何 `[[audience]]` 写 watch = true，未启用（.env 里的
  MC_WATCH_GROUP / MC_REPORT_GROUP 已作废）」。
- **日志噪音**：RCON 出问题时**只在状态跃迁时打一条告警**，不是每轮一条。所以日志里只有一条 warning 是正常的，别以为没报错就没问题——以 `mc_check.py` 的输出为准。

### F9 MC 名单对不上 / 频繁假进服
- 玩家显示名带队伍前缀（`§a[VIP] Alice`）时，前缀本身被保留为名字的一部分。前缀变更会被算成一次进服。
- 若确实频繁误报，可在服务端试 `list uuids`（输出形如 `Alice (uuid)`），改用 UUID 作身份基准即可。先用 `mc_check.py` 确认你那台服务端支持再改。

### F10 @机器人 查询毫无反应（群里一条回复都没有）

> 📌 **2026-09-21 起 MC 查询的「群」不再靠 `.env` 拦。** 改由 `mcs_audiences.toml`
> 的**群关联**决定（见 4.2 节），而且**不响应的群里现在会收到一句明确回复**，
> 不再是彻底沉默。所以这一节分两种情况看。

**情况一：群里收到「🤔 本群还没有开通 MC 查询」**

MC 插件认领了这条消息，但这个群没被写进任何 `[[audience]].groups`。日志里有配套 warning：

```
群 123456789 收到 mc 查询，但它没开通：不在 .../mcs_audiences.toml 的任何
[[audience]].groups 里。已开通的群：11111111、22222222
```

→ 去 `mcs_audiences.toml` 把这个群号加进对应那条 `[[audience]].groups`（想让几个群看
同样的服就都写进同一条），重启 bot。加完先跑 `--list-audiences` 确认群号认对了。

**情况二：群里收到「🏗️ 本群关联的服务器还没接入」**

这个群**开通了**，但那条 `[[audience]].targets` 是空的（配置里那条占位条目就是这种）。

→ 去 `mcs_servers.toml` 确认那些服在不在表里，再把它们的 id 填进这条关联的 `targets`
（id 用 `--list-targets` 看）。**别把群号从 `groups` 里挪走** —— 那样就从「开通了但没服」
变成「没开通」，文案跟着换一句，问题反而更难看出。

**情况三：还是彻底一条回复都没有**

**先看日志有没有这条 warning**：

```
群 123456789 @oopz 查询被忽略：不在白名单内。NAPCAT_ALLOWED_GROUPS=...（逗号分隔；留空 = 所有群都可查）
```

有 → 那是 **oopz 那边**的白名单拦下的（`NAPCAT_ALLOWED_GROUPS`）。把群号加进去，重启 bot。

没有这条 warning → 说明消息根本没被判定成「@机器人」，检查是不是 @ 到了别的号 / 只是打了触发词没 @。

> **为什么 MC 那边要专门回一句**：`mc` / `我的世界` / **`服务器`** 这几个触发词是在整条
> 消息上做子串匹配的，被 MC 认领的消息不会再有别的插件应答。原先白名单是**静默 return**，
> 于是没开通的群里打什么都像「机器人掉线」——连 hello 的功能引导也不出现。改成一个群
> **必须**收到一句回复之后，这条静默路径就没了；但代价是拒绝文案会盖掉 hello 的引导，
> 所以文案末尾附了一句指路（「其它功能可以发 @机器人 你好 看看」）。
>
> 拦人的是**群关联**（限制哪些 QQ 群能用 MC 功能），跟「MC 玩家白名单」（服务端
> `whitelist.json`）是两回事，别顺着这条去改 `MC_ADMIN_QQ`。

### F11 `@机器人 whitelist` 没反应 / 说没权限 / 说未能验证

按回复内容对号入座（**每一步的拒绝都会在日志里留一条 warning**，以日志为准）：

| 群里看到 | 原因 | 修复 |
|---|---|---|
| 「🚫 你没有 MC 管理权限」 | 你的号不在 `MC_ADMIN_QQ` 里；或该项**留空**（留空 = 功能对所有人关闭） | 把自己的 QQ 号加进去，重启 bot。日志里能区分「未配置」和「不在名单内」两种 |
| 「🤔 本群还没有开通 MC 查询」 | 该群没被写进任何 `[[audience]].groups` | 见 F10 情况一 |
| 「⚠️ 本群没有指定白名单服，白名单管理不可用。」 | 这个群开通了，但那条 `[[audience]]` 没写 `whitelist`（或那一项只写了 `command` 没写 `target`，**该项被忽略**） | 按 4.2 节给那条 `[[audience]]` 补 `whitelist = [{ target = "...", command = "..." }]`；只读地先看一遍：`mc_check.py --audience <关联名> --whitelist`。只写 `command` 的项加载时会**逐项**报一条警告，日志里搜「只写了 command」 |
| 「⚠️ 白名单服 Bingo 两条通道都没配（既没有 rcon 也没有 api），命令发不出去。」 | 该条 `whitelist` 里那一项的 `target` 指到了目标，但那台**两条通道都没配** | 在 `mcs_servers.toml` 给那台补 `rcon = { port, password }` 或 `api = { url, token }`（见 4.1）。二选一，没有降级路径。**多台时逐台检查** —— 日志和启动摘要会**逐台**报，别看到第一台正常就以为都正常 |
| 「😵 接口认证失败（token 可能被对方轮换了）」 | 接口返回 401：`api.token` 不对，或对方轮换了 token | 找对方要新的 token 填回 `mcs_servers.toml`（4.1）。⚠️ **这不是网络问题**，别去查隧道和防火墙 |
| 「😵 连不上 X服务器（接口）」 | 请求发不出去：隧道断了、对方服务没起、或 `api.url` 指向的地址不通 | 先跑 `tools\mc_check.py --api` 看 `GET /health` 通不通。隧道是临时的（4.1），机器重启/网络抖动后要重新拉 |
| 「⚠️ 本群有 N 台白名单服，命令里要点名发给哪台：…」 | 该条配了**多台**白名单服，命令里没写服名。**没有「默认那台」的回退**（猜错 = 改错服务器的白名单） | 在命令**末尾**加上服名：`whitelist add Steve bingo`。只有一台时才能省 |
| 「⚠️ 没有叫 X 的服。本群能查的是：…」 | 服名打错了，或者那台服**不在本条的 `targets` 里**（关联外隔离） | 列表里就是本群能查的全部服名（id / 名字 / 别名 / 唯一前缀都认）。要加服就补该条的 `targets`（见 F10 第 2 条） |
| 「⚠️ GTNH 是本群关联的服，但它不是白名单服。」 | 这台服本群查得到，只是没写进该条的 `whitelist` 数组 | ⚠️ **这条不是「没这台服」**，别去改 `targets`。要让它能管白名单就往 `whitelist` 里加一项 `{ target = "gtnh" }`；只想看它的在线状态用 `@机器人 mc GTNH`。加之前先想清楚「多一台白名单服 = 管理员权限多一圈」（4.2 第 5 条） |
| 「⚠️ 服名要写在最后：你写的 `X` 是服名。改成：…」 | 服名写到了动词**前面**（`whitelist bingo add Steve`） | 照回复里给出的整条命令重打一遍。⚠️ 旧版本这里是**静默把 `bingo` 丢掉**再把命令发往缺省那台的 —— 也就是说它**改了另一台服的白名单却什么都没说**。所以这条报错是要留着的，不是啰嗦 |
| 「⚠️ 服务器配置读不了：…」 | `mcs_servers.toml` / `mcs_audiences.toml` 不存在或有语法/校验错误 | 报错里带原始原因，照着改；先跑 `--list-targets`（目标）和 `--list-audiences`（群关联）看详细 |
| 「❌ 未生效：名单里仍没有 X」 | 命令发出去了（或接口调用成功了），但**重新读一遍名单还是没有它** | RCON 那台：看日志里那行 warning 的 **RCON 回执原文**——常见是服务端根本没启用 `whitelist` 命令、被权限插件接管、或本条的 `whitelist.command` 前缀写错了（见 4.2 第 3 条）。接口那台：对方回执说成功也不算数，我们一律以**重新读一遍名单**为准；仍是「未生效」就是对方插件真的没写进去，把日志里的回执发给对方。**多台时只可能是点名的那一台**（回复里的 `【服名】` 就是它），别去查其它台 |
| 「⚠️ 读不到服务端当前的白名单，add 命令没有发出去」 | 连改动前的名单都读不出来，bot **一条命令都没发**（不知道名单里有什么就不敢下手） | 跑 `tools\mc_check.py --whitelist` 看**原文**——RCON 那台多半是 `whitelist list` 的输出解析不了，照下面的「空名单哨兵」处理；接口那台是 `/whitelist` 取不到（看上面两行的认证/连不上） |
| 「⚠️ 命令已发送（未能验证）」 | 命令确实发出去了，只是**反查**（改完再读一次名单）读不懂，判不出成没成。**只有 RCON 那台会出现** —— 接口给的是结构化数组，不存在读不懂 | 去服务端 `whitelist list` 亲眼确认。⚠️ 这条**不代表命令已经生效**，只是「发了，不知道结果」 |
| 完全没有任何回复 | 消息没被 @ 到 / 不在群里 | 同 F10 |
| 回的是「未知命令」用法提示 | 命令格式不对 | 用法提示里会列出当前触发词 |

其余已知限制（都不是 bug）：

- **`white-list=false`**：`add` 会写进 `whitelist.json`、`list` 也看得到，bot 如实报「已添加」，但服务端不拦人。bot 读不到 `server.properties`，判断不了——自己去服务端确认。
- **已在名单中再 add** / **移除一个不在名单里的名字**：bot 先读名单，发现已经是目标状态就**不下发命令**，回的是「ℹ️ 已在白名单中，无需重复添加」/「ℹ️ 本来就不在白名单里，无需移除」。不会把没发生的事报成「已添加 / 已移出」。
- **名字的大小写**：服务端存的拼写常和你输入的不同（`add vul` → 存成 `Vul`，服务端拿玩家档案里的规范拼写替换）。bot 的判定忽略大小写，**下发命令时也改用服务端记录的那条拼写**，所以不用管自己打的是大写还是小写；回复里若拼写有出入会附一句「（服务端记录为 Vul）」。
- **offline-mode（`online-mode=false`）的两件事**：
  - **切模式会让现有白名单失效**。`whitelist.json` 每条都带 UUID，在线模式存的是 Mojang 正版 UUID，离线模式算的是由名字推出的离线 UUID——**两者不匹配，原有条目全部进不去**。切模式后必须挨个重新加一遍（用 bot 跑 `whitelist list` → 对每个人 `whitelist add` 即可）。
  - **同名不同大小写可能变成两条独立记录**。离线 UUID 由名字字面量算出（区分大小写），`Vul` 和 `vul` 是两个不同的人；在线模式下它们会被解析到同一个账号、只会有一条。bot 两种都认：`remove vul` 会把所有同名不同拼写的条目**一并移除**，并在回复里列出删了哪几条。
- **一条关联可以管多台白名单服，但改动命令一次只打一台**。`list` 不点名时会把本群的全部白名单服**并发**查一遍（RconError 只影响那一台，其余照常列出），名点了就只看那一台。`add` / `remove` 必须点名、只发往点名的这一台：一条命令同时改多台 = 一次误操作同时改坏几台服的白名单，收益与风险不成比例。要批量改就一条条来。
- **多台 `list` 的耗时 ≈ 最慢那一台**（并发，不是求和），但**渲染可能被截断**：3 台 × 50 个长名字正好会踩消息长度上限，此时会**从头减每台显示的名额**并留下 `…还有 N 人`，日志里另有一条 warning。被截断的只是**显示**，不是查询。
- **基岩版玩家（Floodgate/Geyser）**：白名单里带 `.` 前缀（`.Steve`），玩家名正则不允许 `.`，所以**加不进去**，得去服务端控制台手动加。
- **玩家名规则**：只接受 `字母 / 数字 / 下划线`，1~16 位（Java 版规则）。写成别的会被判非法并回用法——这条正则同时也是防注入的关键，不宜放宽。
- **空名单哨兵**：服务端对「白名单是空的」用的是**另一句文案**（vanilla 的 `commands.whitelist.none`），而且**整句没有冒号**。机器人按「第一个冒号」切前缀的解析方式会把它当成「格式不认识」，后果不只是查不到 —— `add` / `remove` 的**前置读**也拿不到结果，bot 会直接放弃、**一条命令都不发**。所以 `mc.py` 里专门认了一组哨兵句（`_EMPTY_WHITELIST_REPLIES`），**整句比对**、忽略大小写与颜色码。目前只有 en_us 那句（`There are no whitelisted players`，2026-09-21 在 Bingo 26.2 上抓的原文）。
  - **认不出来的照旧报「未能解析」**，不会误判成「空名单」——所以漏了一句只会让功能不工作，不会给出错误答案。
  - 服务端若设了别的语言（或插件改写过），跑 `tools\mc_check.py --whitelist` 看 RCON **原文**，把那句逐字加进 `plugins_napcat/_shared/mc.py` 的 `_EMPTY_WHITELIST_REPLIES` 即可。**别**改成「没冒号就算空名单」：命令前缀写错时的回执同样没冒号，那样会把「前缀写错」误报成「白名单是空的」。

---

## 9. 日常维护

> Linux 上的对应命令（`systemctl restart oopz-bot`、`journalctl -u oopz-bot -f`、
> 更新代码、备份哪四样）见 [DEPLOY-Linux.md 第 11 节](DEPLOY-Linux.md)。下文凡是写
> 「重启 bot」的地方，Linux 上就是 `sudo systemctl restart oopz-bot`。

- **改文案**：
  - oopz 播报格式在 `plugins_napcat/oopz/auto_reporter.py` 的 `_build_broadcast_message()`；进频道欢迎语在同文件顶部的 `_WELCOME_TEMPLATES`。
  - MC 播报格式在 `plugins_napcat/_shared/mcrender.py` 的 `render_report()`；进服提醒语在同文件的 `_JOIN_TEMPLATES`。**这两样都在 `_shared/` 而不是 `mc_reporter.py`**，因为 `_shared/` 里的模块不 import nonebot，能在 `nonebot.init()` 之前被 `tools/mc_check.py` 直接调用——文案因此有自测覆盖。搬回 `mc_reporter.py` 会让它失去这层保护。
  - `@mc` 的回复格式也在 `plugins_napcat/_shared/mcrender.py`：`render_detail()`（`@mc <服名>` 单服明细）、`render_summary()`（`@mc` 总览）。**查询与播报共用这一份**，改这里两边一起变，不会出现同一个服在两个地方显示成两样（`render_report` 与 `render_summary` 对同一台服渲染出的块是逐字相同的，自测钉住了这条）。
  - 「谁进服了」这件事算在哪台服、算不算换服，在 `plugins_napcat/_shared/mcdelta.py` 的 `reconcile()`：纯函数（不联网、不读配置、不看时钟），自测覆盖最厚的一块。**改它之前先看块 19 的用例**——「探测失败时基线原样冻结」「没有基线 ≠ 空基线」这两条各有专门用例。
  - 白名单命令的回复文案与用法提示在 `plugins_napcat/mcs/mc_admin.py` 的 `_build_message()` / `_usage()`；命令解析与执行在 `plugins_napcat/_shared/mcadmin.py`。
- **改触发词**：`.env` 的 `OOPZ_TRIGGER` / `MC_TRIGGER` / `MC_ADMIN_TRIGGER`（不用改代码）。归属逻辑在 `plugins_napcat/_shared/triggers.py`：`locate()` 定归属并带回位置，`detect()` 是它的薄包装，`strip_keyword()` 把触发词剥掉取载荷（`@bot 服务器 mc bingo` → `bingo`）——它剥的是该插件的**全部**触发词，所以多写几个触发词也能解析对。
- **改管理员**：`.env` 的 `MC_ADMIN_QQ`（逗号分隔 QQ 号，**留空 = 关闭该功能**）。鉴权在 `plugins_napcat/_shared/admin.py`。
- **排查 MC 取数**：`.\.venv\Scripts\python.exe tools\mc_check.py`。不带参数 = 并发探测**所选群关联**的全部目标，逐台给结论；加 `--target <服名>` 只看一台；加 `--audience <关联名>` 换一条群关联的视角（默认第一条）；加 `--list-targets` 只读配置、不联网（新加的子服没生效先跑这个）；加 `--list-audiences` 只读群关联、不联网（新加的群不响应先跑这个）；加 `--self-test` 只跑解析自测、不联网；加 `--whitelist` 只看服务端白名单，只读不改。退出码 0 正常 / 1 有目标不正常 / 2 用法错误。
- **续期 oopz JWT（每月一次）**：`OOPZ_JWT_TOKEN` 约 31 天过期。症状：@统计 无回复、播报显示「查询失败」。重跑 `tools\oopz_login.py` → 回填 `.env` → 重启 bot。
- **改间隔/目标群**：改 `.env` 后重启 bot。
- **看日志**：`logs/napcat_bot_<日期>.log`（按天切、留 14 天，内容与屏幕一致；文件是 UTF-8 写的，PowerShell 用 `-Encoding UTF8` 读）。回看「某轮为什么没推送」全靠它 ——
  ```powershell
  Get-Content logs/napcat_bot_2026-09-21.log -Encoding UTF8 | Select-String "进服提醒|定时播报"
  ```
  成功推送会打一行 `MC 进服提醒（「<关联名>」）→ N 个群：` 并附上发出去的原文；一条都没送达则打 warning。所以**「日志里没有这行」= 真的没推**，不再是「可能推了但没记」。
- **NapCat 升级/重装**：三个启动坑（F1/F2/F3）会复发，按第 8 节处理。
- **清理旧日志**：`logs/` 下文件可随时删，bot 会自动重建。
