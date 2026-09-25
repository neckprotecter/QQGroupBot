# oopz 群统计机器人

QQ 群机器人：群成员 @ 机器人发「**oopz**」或「**mc**」，实时查询 oopz 语音频道在线成员 / Minecraft 服务器在线玩家并回复。

提供**两套接入**：

- **NapCat 版（推荐）**：`bot_napcat.py`，走个人 QQ 号 + [NapCat](https://github.com/NapNeko/NapCatQQ)（OneBot v11），**可主动推送**，不受官方限制，含定时播报 / 进频道欢迎。
- **QQ 官方版（备选/回退）**：`bot.py`，官方机器人仅被动响应（2025-04-21 起官方停用主动推送，发起即报 `40034105`）。

> 🚀 **从零部署**：Windows 见 [DEPLOY.md](DEPLOY.md)，Linux 见 [DEPLOY-Linux.md](DEPLOY-Linux.md)。
> 后者用 systemd 挂机器人、用 Docker Compose 跑 NapCat；`.env` / 两份 `.toml` 的字段含义
> 两份文档共用 [DEPLOY.md 第 4 节](DEPLOY.md)。

## 功能

| 群内 @ | 回复 |
|--------|------|
| `@机器人 你好` | 「收到！被动回复链路已打通 🎉」——链路自检 |
| `@机器人 oopz` | 📊 oopz 语音频道在线成员报告（分域/频道 + 昵称） |
| `@机器人 mc [服名]` | 🗺️ Minecraft 在线人数 + 玩家名单。**不带服名 = 本群关联的服的总览**（每个服一小段名单）；带服名 = 该服明细，服名支持 id / 名字 / 别名 / 唯一前缀，认不出或有歧义都会明确回复 |
| `@机器人 whitelist add/remove <玩家名> [服名]`<br>`@机器人 whitelist list [服名]` | 🧾 MC 玩家白名单管理（**仅 `MC_ADMIN_QQ` 里的管理员**，且本群那条群关联的 `whitelist` 要指到配了 RCON 或群组接口的服）。服名写在**末尾**；本群只有一台白名单服时可以省，多台时必须点名 |

> 触发词可改：`.env` 的 `OOPZ_TRIGGER` / `MC_TRIGGER` / `MC_ADMIN_TRIGGER`（逗号分隔，不区分大小写）。一条消息只会被一个插件响应。
>
> MC 服务器的地址、显示名、RCON 密码在 **`mcs_servers.toml`**；**哪个 QQ 群看哪几台服**
> 在 **`mcs_audiences.toml`**（两份都不在 `.env`），见 [DEPLOY.md 4.1 / 4.2](DEPLOY.md)。
> 取数走三条通道：SLP（人数）、RCON（名单，自己的服）、**群组 HTTP 接口**（一次拿整组
> 每个子服的人数与名单，对方装 Velocity 插件时首选，见 4.1）。
>
> **MC 功能是按群开通的**：群号被写进某条 `[[audience]].groups` 才算开通，没被提到
> 就不开通（群里会明确回一句，不是静默）。关联外的服名在那个群里查不到 —— 这是刻意的隔离。
>
> **「被提到 = 开通查询」不包含推送。** 查询是群友拉（问了才答），推送是机器人自己
> 说话，所以进服提醒 / 定时播报要不要发给这个群，由那条关联自己的 `watch` /
> `report` 决定，**缺省都是关的** —— 新加一条关联不会悄悄让那个群开始收推送。

**自动功能（NapCat 版，配置见 `.env`）**

- ⏰ **oopz 定时播报**：整点对齐推送（间隔 30 分钟则在 :00/:30，间隔 60 则每小时整点），向 `NAPCAT_REPORT_GROUP` 推送独立格式的「📣 oopz 语音频道播报」——**仅当 oopz 有人在线时**，无人则静默跳过
- 👋 **进频道欢迎**：每 `NAPCAT_WELCOME_INTERVAL_SEC` 秒轮询，检测到有人进入目标域语音频道时推送趣味欢迎语（随机文案）
- 🎮 **MC 进服提醒**：每 `MC_WATCH_INTERVAL_SEC` 秒轮询 MC 服务器，有人进服时推送提醒（**推「进服」和「换服」（同组两台之间移动），不推退服**；最小推送间隔 `MC_JOIN_MIN_INTERVAL_SEC` 秒，窗口内的进服合并成一条）。**收件人和盯哪几台都由那条群关联决定**（`watch = true` + 它自己的 `targets`），一条关联挂三台服时一个周期内合成**一条**消息
- ⏰ **MC 定时播报**：整点对齐推送「📣 MC 播报」+ 各服在线名单（`report = true`）——某台服没人或不可达只是**列成一行**，不再让整条播报跳过；真的全都没人才静默跳过
- ⚠️ **MC 掉线提醒**：连续两轮探测失败才判定离线（单轮网络抖动不报），恢复时也会推一条；`MC_NOTIFY_SERVER_STATE=false` 可关。掉线只推给**关联了那台服**的群
- 🌙 **MC 夜间静默**：`MC_QUIET_HOURS`（默认 `0-9`，即 00:00–08:59）内**不推进服提醒、不做定时播报**，半夜不吵人；`09:00` 整那班照常发，支持跨午夜（`23-7`），留空 = 不静默。**掉线/恢复提醒不受影响**（夜里服务器真挂了得让人知道）。静默期间照样探测，所以 09:00 之后不会把整晚的人当成「刚进服」补报一遍

示例回复：

```
📊 oopz 语音频道在线：4 人

【奇妙小房间】
  🔊 游戏开黑（4人）
    • 甲
    • 乙
    • 丙
    • 丁
```

## 目录结构

```
oopz-bot/
├── .env                      # 凭证 + 行为类开关（勿提交公开仓库）
├── .env.example              # 配置模板 → 复制为 .env
├── mcs_servers.toml          # MC 服务器清单（含各服 RCON 密码，勿提交公开仓库）
├── mcs_servers.toml.example  # 服务器清单模板 → 复制为 mcs_servers.toml
├── mcs_audiences.toml        # MC 群关联（哪个群看哪几台；不含密码，但含真实群号）
├── mcs_audiences.toml.example # 群关联模板 → 复制为 mcs_audiences.toml
├── bot.py                    # 启动入口①：QQ 官方版（被动响应）
├── bot_napcat.py             # 启动入口②：NapCat/OneBot 版（可主动推送）
├── requirements.txt          # Python 依赖清单
├── vendor/Oopzbot-SDK/       # oopz_sdk 源码（不在 PyPI，随工程分发；requirements.txt 里默认装它）
├── tools/                    # 人手动跑的工具（机器人本体不 import 它们）
│   ├── README.md             # 三个工具的分工、用法、什么时候该跑哪个
│   ├── oopz_login.py         # 手机号+密码 → 写入 OOPZ_* 凭据
│   ├── oopz_check.py         # 独立验证 oopz 查询链路
│   └── mc_check.py           # 独立验证 MC 取数链路（`--list-targets` 看服务器 / `--list-audiences` 看群关联 / `--target` 测单台 / `--api` 只看群组接口 / `--self-test` 离线自测）
├── plugins/                  # QQ 官方版插件（bot.py 加载）
│   ├── hello.py              # @你好 → 链路自检
│   └── oopz_stats.py         # @oopz → 实时成员报告
├── plugins_napcat/           # NapCat 版插件（bot_napcat.py 加载）
│   ├── hello/                # 群消息摘要 + @我 功能引导
│   ├── oopz/                 # oopz：@查询 / 定时播报 / 进频道欢迎
│   │   ├── client.py         #   客户端单例 + 共享查询工具
│   │   ├── oopz_stats.py     #   @oopz
│   │   └── auto_reporter.py  #   定时播报 + 进频道欢迎
│   ├── mcs/                  # Minecraft：@查询 / 进服提醒 / 定时播报 / 白名单管理
│   │   ├── client.py         #   带 TTL 的快照缓存
│   │   ├── mc_stats.py       #   @mc
│   │   ├── mc_admin.py       #   @whitelist（管理 MC 玩家白名单）
│   │   └── mc_reporter.py    #   进服提醒 + 定时播报
│   └── _shared/              # 跨插件共享工具（下划线前缀 → 不会被当插件加载）
│       ├── mc.py             #   MC 取数层：SLP + 自实现 RCON + 群组接口合并 + list/白名单输出解析
│       ├── mcbridge.py       #   群组接口客户端（Velocity 插件：/health /status /whitelist 增删查）
│       ├── mcservers.py      #   服务器清单（mcs_servers.toml 解析 / 校验 / 服名解析 / 投影）
│       ├── mcaudiences.py    #   群关联（mcs_audiences.toml 解析 / 「这个群看哪几台」/ 唯一加载入口）
│       ├── mcdelta.py        #   玩家进出对账（reconcile：两份名单差成进服/退服/换服事件）
│       ├── mcrender.py       #   MC 消息文案（明细 / 总览 / 播报 / 合成推送，共用同一份）
│       ├── textlen.py        #   长度上限与截断（零依赖，供渲染层用）
│       ├── mcadmin.py        #   MC 玩家白名单命令层（解析 / 执行 / 独立验证）
│       ├── admin.py          #   管理员名单（用户级鉴权，留空 = 拒绝一切）
│       ├── push.py           #   群消息推送 + 长度上限
│       ├── schedule.py       #   整点对齐时间槽
│       ├── triggers.py       #   触发词归属（决定消息归哪个插件响应）
│       └── whitelist.py      #   群白名单（**现在只有 oopz 在用**；被拦下时打 warning）
├── deploy/linux/             # Linux 部署用的现成文件（见 DEPLOY-Linux.md）
│   ├── docker-compose.yml    #   NapCat 协议端容器
│   ├── oopz-bot.service      #   机器人本体（systemd）
│   └── mc-tunnel.service     #   MC 取数用的 ssh 隧道（可选）
├── DEPLOY.md                 # 从零部署指南（Windows）
├── DEPLOY-Linux.md           # 从零部署指南（Linux）
└── docs/
    └── chat.md               # 完整技术文档与踩坑记录
```

## 快速开始

> 下面这套命令是 **Windows** 的。Linux（systemd + Docker Compose NapCat）的环境准备、
> venv、配置权限、挂服务、验收与排查都在 [DEPLOY-Linux.md](DEPLOY-Linux.md)，那份从
> 第 1 节开始就不一样，别照抄这里的路径 —— 但**配置项和工具用法完全相同**。

**环境**：Windows / Linux，Python ≥ 3.10

**1. 安装依赖**

```bash
pip install -r requirements.txt   # oopz 默认一起装上：最后一行 oopz-sdk 指的就是本工程
                                  # vendor/ 里的 SDK（不在 PyPI），不用再单跑 pip。
                                  # ⚠️ 那条是本地相对路径，pip 按 cwd 解析 ——
                                  #    所以这条命令要在工程根目录跑
```

> **只要 MC 功能、不要 oopz**：把 `requirements.txt` 最后那一行注释掉再装即可。这是**受
> 支持的**状态 —— 机器人照常启动，oopz 那半边自动停用并明说原因（群里回一句、日志吼一声），
> MC 查询 / 白名单一字不受影响。以后想要：`pip install ./vendor/Oopzbot-SDK` 再重启。

**2. 配置 `.env`**

```powershell
copy .env.example .env
```

复制后按注释逐项填写（`.env` 含密钥，勿提交到公开仓库）；每个键的含义见 [DEPLOY.md 第 4 节](DEPLOY.md#4-配置-env)。

**3. 首次登录 oopz**（交互式，终端里输入，密码不进聊天记录）

```powershell
$env:OOPZ_LOGIN_PHONE = "你的oopz手机号"
$env:OOPZ_LOGIN_PASSWORD = "你的oopz密码"
.venv\Scripts\python.exe tools\oopz_login.py
```

> 脚本会把 OOPZ_* 四要素自动写进 `.env`。Linux 用 `OOPZ_LOGIN_PHONE=xxx OOPZ_LOGIN_PASSWORD=xxx python tools/oopz_login.py`。

**4. 验证 oopz 查询**（不启动机器人，直接查一次）

```powershell
.venv\Scripts\python.exe tools\oopz_check.py
```

**5. 验证 Minecraft 取数**（可选，用 MC 功能才需要）

先按 [DEPLOY.md 4.1 的「每台服务端的准备」](DEPLOY.md) 在 MC 服务端打开 RCON（**别漏 `broadcast-rcon-to-ops=false`**，缺省是 `true`，不改的话每次 `list` 都会广播给在线 OP 刷屏），再跑：

```powershell
.venv\Scripts\python.exe tools\mc_check.py                     # 实测所选群关联的全部目标（并发）
.venv\Scripts\python.exe tools\mc_check.py --target bingo      # 只测一台
.venv\Scripts\python.exe tools\mc_check.py --audience 社团群    # 换一条群关联的视角
.venv\Scripts\python.exe tools\mc_check.py --list-targets      # 只读：服务器清单 + 被哪些群关联
.venv\Scripts\python.exe tools\mc_check.py --list-audiences    # 只读：每条群关联覆盖哪些群、哪几台服
.venv\Scripts\python.exe tools\mc_check.py --self-test         # 只跑解析自测，不联网
.venv\Scripts\python.exe tools\mc_check.py --whitelist         # 只读地看一眼服务端白名单
.venv\Scripts\python.exe tools\mc_check.py --api               # 只看群组接口那一层（/health、/status、/whitelist）
```

每个目标一个块，块头就是结论；末尾汇总 `N/M 个目标正常`，**有目标不正常时退出码为 1**。
看到 `[OK] … 名单完整` 即成功。不完整时脚本会打印 RCON `list` 的**原始输出**和判定原因。

`--target` 的服名走和群里同一套解析（id / 名字 / 别名 / 唯一前缀）**且限定在所选群关联内**
（默认第一条），写错了会明确报错而不是悄悄测全部。不带 `--audience` 时报的服务器集合
就是第一条关联里的那几台 —— 这跟群友看到的是同一个集合。

**6. 启动机器人（QQ 官方版）**

```powershell
.venv\Scripts\python.exe bot.py
```

看到 `Bot <机器人AppID> connected` 即成功。

**7. 改用 NapCat 版（可选，可主动推送）**

安装 [NapCat](https://github.com/NapNeko/NapCatQQ) / 登录 / 开正向 WebSocket / 配 token 的完整步骤见 [DEPLOY.md 第 2 节](DEPLOY.md#2-安装-napcat协议端)。`ONEBOT_V11_WS_URLS` / `ONEBOT_V11_ACCESS_TOKEN` 配好后启动：

```powershell
.venv\Scripts\python.exe bot_napcat.py
```

看到日志出现 `Bot <QQ号> connected` 即成功（NapCat 未运行时日志会持续报连不上 3001，属正常）。

## 群内使用

在 QQ 群 @ 机器人发送 `oopz`、`mc` 或 `你好` 即可。仅响应群内 @，私聊不回复。

> NapCat 版默认**所有群**都能触发 **oopz** 查询；想限定群，在 `.env` 设 `NAPCAT_ALLOWED_GROUPS`（逗号分隔群号，留空 = 不限制）。
> **MC 查询不看这个变量**：它由 `mcs_audiences.toml` 的群关联决定（见下），一个群被哪条
> `[[audience]]` 提到才开通。`MC_ALLOWED_GROUPS` 是这套机制之前的做法，**已作废**。
> 官方版对应 `QQ_ALLOWED_GROUPS`（group_openid 列表），且**不支持 MC 功能**（无主动推送能力，自动功能都做不了）。

### 让一个群用上 MC 功能

MC 功能是**按群开通**的。群号写进 `mcs_audiences.toml` 的某条 `[[audience]].groups` 才算
开通，没被提到就不开通 —— 群里会明确回一句「🤔 本群还没有开通此功能」，不是静默。

```toml
# mcs_audiences.toml：社团群看自己的三台服，默认先看 gtnh
[[audience]]
name      = "社团群"
groups    = [123456789]
targets   = ["gtnh", "bingo", "backstabbed"]      # 顺序 = @查询 总览里的展示顺序
primary   = "gtnh"                                # 不带服名的 @查询 默认看它
whitelist = [{ target = "bingo", command = "whitelist" }]  # **一组表**，每台白名单服一项
watch     = true                                  # 本群的 groups 收进服提醒
report    = true                                  # 本群的 groups 收定时播报

[[audience]]
name      = "建筑群"                               # 组服还没搭，先占位
groups    = [987654321]
targets   = []                                    # 合法：那个群的查询会回「还没接入」
```

> ⚠️ **一个群只能出现在一条 `[[audience]]` 里**（写进两条会直接报错）。想让几个群看
> 同样的服，就把群号都写在同一条的 `groups` 里。**关联名也不能重名。**
>
> ⚠️ **关联外的服名在那个群里查不到**，这是刻意的隔离：每条的服务器视图是独立投影的。
> 反过来「新加的服忘了关联给任何群」很常见 —— 那时群里打它回「没这个服」，而
> `--list-targets` 和启动日志都会照常列出它，所以两者都会专门把它标成 ⚠️ 孤儿。
>
> ⚠️ **`watch` / `report` 不写就是 false。** 这两个开关只管「发不发」，**收件人恒为
> 本条的 `groups`**。「多久一次」这类节奏参数继续留在 `.env`（那是整台机器人的节奏，
> 不是某个群的偏好）。
>
> 这份文件**不含密码**（RCON 密码在 `mcs_servers.toml`），所以可以单独给人看 ——
> 但含真实 QQ 群号，同样不该提交。改完要**重启 bot**，然后跑
> `tools/mc_check.py --list-audiences` 确认。细节见 [DEPLOY.md 4.2](DEPLOY.md)。

### 管理 MC 玩家白名单

管理员在群里发 `@机器人 whitelist add <玩家名>` 即可把玩家加进服务端白名单，不用登控制台。另有 `whitelist remove <玩家名>`、`whitelist list`。一条群关联可以管**多台**白名单服，此时命令里要点名（`whitelist add <玩家名> <服名>`）——只有一台时可以省。

```powershell
# .env：只有列在这里的 QQ 号能用管理命令
MC_ADMIN_QQ=123456789,987654321
```

```toml
# mcs_audiences.toml：**按群**指定命令发给哪几台服、每台用什么前缀
[[audience]]
name      = "社团群"
groups    = [123456789]
targets   = ["bingo", "backstabbed", "proxy"]
# 一组表，每台白名单服一项；command 可省（缺省 "whitelist"）。整段省略 = 本群不管白名单。
whitelist = [
  { target = "bingo" },                                   # 子服：插件自带 whitelist
  { target = "proxy", command = "globalwhitelist" },       # 代理：Global Whitelist
]
```

> ⚠️ `MC_ADMIN_QQ` **留空 = 该功能对所有人关闭**——与其他「留空 = 不限制」的配置相反，这是刻意的：配错的后果是任何人都能改服务端白名单。
>
> 群里怎么点名：命令里**服名写在最后，一次只打一台**。`whitelist add Steve bingo` = 加到 `bingo`；`whitelist list` 不点名 = **一次列出本群全部白名单服**（按 `【服名】` 分块，并发查，一台上线也不影响另一台），`whitelist list bingo` 只看那一台。只有一台白名单服时可以省服名，行为与以前完全一样；**多台时不写服名会被要求点名，没有「默认那台」的回退**——猜错就是改错服务器的白名单。`add` / `remove` 一律只影响点名的那一台。
>
> ⚠️ `whitelist` 是**每条群关联各一份**（不是全局一份）：社团群的管理员碰不到建筑群的白名单。数组里每一项的 `target` 必须是**本条 `targets`** 之一，`command` **必须与插件注册的命令一字不差**：vanilla / NekoList 是 `whitelist`，Global Whitelist 是 `globalwhitelist`，ProxyWhitelist 是 `pwl`。写错的表现是每次操作都回 `Unknown command`、群里报「未生效」，而且**不报错**。多台时**逐台**都要对，一台写错只有点名它的命令才会出问题。注意群里打的触发词（`.env` 的 `MC_ADMIN_TRIGGER`）和这个前缀是两个不同的轴。
>
> ⚠️ **多写一台白名单服 = 同一个人能改的服务器多一台**。`MC_ADMIN_QQ` 鉴的是「人」、**不分服**，所以加项之前先确认这个人本来就该管那台服——这是权限扩大，而它不会出现在任何输出里。
>
> 三种「没配好」群里回的话不同，修法也不同，别混：
> 那条关联没写 `whitelist` → 「⚠️ 本群没有指定白名单服」；写了但那台
> **两条通道都没配**（既没有 `rcon` 也没有 `api`）→ 「⚠️ 白名单服 X 两条通道都没配」；
> 多台时没点名 → 「⚠️ 本群有 N 台白名单服，命令里要点名发给哪台」。服名认不出、
> 或者那台是本群的服但**没配白名单**，也各有专门文案（后者**不会**谎称「没这台服」）。
>
> 别把两个「白名单」搞混：**群关联**（`mcs_audiences.toml`，限制哪些 QQ 群能用 MC 功能、各看哪几台）和 **MC 玩家白名单**（`whitelist.json`，服务端的玩家准入表，由本功能管理）毫无关系，可以同时生效。`MC_ALLOWED_GROUPS` 是 2026-09-21 之前那套群白名单，已被群关联取代、不再生效。
>
> 本功能走**两条通道之一**：RCON（自己的服）或群组 HTTP 接口（对方的 Velocity 插件，见 [DEPLOY.md](DEPLOY.md) 4.1），所以该项的 `target` 要指到**配了其中之一**的服。走 RCON 时只读写 `whitelist.json`，**不会**替你打开 `server.properties` 里的 `white-list`；走接口时端口和拦截开关都由对方管。
>
> 名字打什么大小写都行：bot 先读一遍白名单，下发命令时改用服务端记录的那条拼写（`add vul` 服务端存的是 `Vul`，回复里会注明），判定同样忽略大小写。**重复添加和重复移除都会如实回「无需改动」，不会虚报成功。**
>
> ⚠️ 若你从正版模式切到 `online-mode=false`，**原有白名单会整体失效**（在线 UUID 与离线 UUID 对不上），得挨个重新加一遍。详见 [DEPLOY.md](DEPLOY.md) 的 F11。

## 自定义回复格式

oopz 回复由 `_build_stats_message()` 拼装。注意 oopz 查询有两个镜像副本：QQ 版在 [plugins/oopz_stats.py](plugins/oopz_stats.py)、NapCat 版在 [plugins_napcat/oopz/oopz_stats.py](plugins_napcat/oopz/oopz_stats.py)，**改格式需同步两份**（下方代码以 QQ 版行号为准）。

MC 回复由 [plugins_napcat/_shared/mcrender.py](plugins_napcat/_shared/mcrender.py) 拼装：`render_detail()`（单服明细）、`render_summary()`（总览）、`render_report()`（定时播报）、`format_events()`（进服提醒的合成消息）。**只有这一份**：三处共用同一个 `_summary_block` 和同一套进服模板，所以不会出现「群里和播报里同一个服显示成两样」。住在 `_shared/` 是有原因的 —— `tools/mc_check.py` 能**离线**逐字跑它，改文案不用起机器人。

**1. 回复文案 / 排版**（[plugins/oopz_stats.py:160-167](plugins/oopz_stats.py#L160-L167)）

```python
lines = [f"📊 oopz 语音频道在线：{total_online} 人"]   # 第一行：总人数
for area_name, channel_rows in online_by_area.items():
    lines.append(f"\n【{area_name}】")                  # 域（房间）名
    for ch_name, uids in channel_rows:
        names = [uid_name.get(uid) or uid[:8] for uid in uids]
        lines.append(f"  🔊 {ch_name}（{len(uids)}人）")  # 频道名 + 人数
        for name in names:
            lines.append(f"    • {name}")                # 每个成员一行
msg = "\n".join(lines)
```

可调的地方：
- 首行的总人数标题、域/频道前缀的 emoji（`📊` `🔊`）、缩进空格数、成员圆点 `•`。
- 当前成员**一行一个**；想改回横排逗号分隔，把内层 `for name in names:` 换成一行 `lines.append(f"    {', '.join(names)}")`。
- 昵称解析失败时回退显示 uid 前 8 位（`uid[:8]`），可在 `names` 那行调整。

**2. 触发词**：改 `.env` 的 `OOPZ_TRIGGER` / `MC_TRIGGER` / `MC_ADMIN_TRIGGER`，**不用改代码**。归属逻辑统一在 [plugins_napcat/_shared/triggers.py](plugins_napcat/_shared/triggers.py)——一条消息只会被一个插件响应，各插件都只问它，所以不存在「加个新插件就要回头改所有插件互斥条件」的问题。`locate()` 定归属并带回位置，`detect()` 是它的薄包装（行为逐字一致，自测里有断言钉住），`strip_keyword()` 负责把触发词剥掉取载荷。

> `@bot mc bingo` 里的 `bingo` 就是这么取出来的。`strip_keyword()` 剥的是该插件的**全部**触发词而不只是命中的那个，所以 `@bot 服务器 mc bingo` 一样能解析对（默认 `MC_TRIGGER` 是 `mc,我的世界,服务器`）。代价是**服名里含触发词的会被剥坏** —— 那种服名本来就没法寻址，见 [mcs_servers.toml.example](mcs_servers.toml.example) 末尾第 3 条。
> 注意触发词是**子串**匹配：把 `MC_ADMIN_TRIGGER` 改成 `白名单` 这类常用词，会让所有含该词的消息都被管理插件认领（查询插件不再应答）。管理插件认领后会回一条用法提示，不会真的静默，但仍建议选一个有辨识度的词，且别填 `add` / `remove` / `list`。

**3. 消息长度上限**（[plugins_napcat/_shared/push.py](plugins_napcat/_shared/push.py)）：`MAX_LEN = 1800`，QQ 群文本约 2000 上限，超长由 `truncate()` 截断加 `…`。oopz 与 MC 两侧共用这一处。

**4. 统计范围**（[.env](.env)）：`OOPZ_TARGET_AREAS=<你的域名>`——逗号分隔可加多个域（area_id 或域名），删除该行则统计全部已加入的域。

改完**重启对应的入口**生效（NapCat 版重启 `bot_napcat.py`，Linux 上是
`sudo systemctl restart oopz-bot`）。

## 维护与常见问题

完整排查清单见 [DEPLOY.md 常见问题](DEPLOY.md#8-常见问题已知坑)（F1~F11）与 [日常维护](DEPLOY.md#9-日常维护)。高频六条：

- **@机器人 毫无反应**：MC 这边现在**一定会回一句**（「本群还没有开通此功能」/「本群关联的服务器还没接入」），所以彻底沉默多半是 oopz 群白名单拦下的 —— 先看日志有没有「不在白名单内」的 warning，见 DEPLOY.md 第 8 节 F10。
- **新加的服 / 新加的群没生效**：两份配置各有只读诊断，先跑它们再动代码 —— `tools/mc_check.py --list-targets`（服务器清单 + 被哪些群关联）和 `--list-audiences`（每条群关联覆盖哪些群、关联哪几台服）。
- **管理命令没权限 / 说未配置 RCON**：见 DEPLOY.md 第 8 节 F11。最常见的是 `MC_ADMIN_QQ` 留空（= 功能整体关闭）或漏加自己的 QQ 号。
- **统计 / 播报突然失效**：多半是 `OOPZ_JWT_TOKEN` 过期（约 31 天），重跑 `tools/oopz_login.py` → 回填 `.env` → 重启，见 DEPLOY.md 第 8 节 F7。
- **某个群收不到 MC 推送**：先看**那条关联有没有写 `watch` / `report`**（缺省是 false，`--list-audiences` 的「推送」那一行会写出来），再看那台服在不在它的 `targets` 里。手动改过 `.env` 的 `MC_WATCH_GROUP` 的话也留个心：那两个变量已经作废，不搬进关联里就再也不会推送。逐项见 DEPLOY.md 第 8 节 F8。
- **MC 进服提醒不触发**：先跑 `tools/mc_check.py`——**名单不完整时提醒会静默暂停**（刻意设计，避免基于残缺名单误报）。脚本会打印 RCON `list` 原文与判定原因，见 DEPLOY.md 第 8 节 F8。
- **部分域不出现在统计里**：如「Voxel Passion 像素乐园」频道接口返回 `channels: null`，SDK 解析失败被跳过，不影响其他域（原因见 docs/chat.md §5.5）。
- **主动推送（仅 NapCat 版）**：官方版无主动推送能力；定时播报 / 进频道欢迎 / MC 全部自动功能只在 NapCat 版生效。文案位置见 [DEPLOY.md 第 9 节](DEPLOY.md#9-日常维护)。

## 详细文档

技术方案、oopz API 对照、SDK 三个已知坑 → 见 [docs/chat.md](docs/chat.md)。
