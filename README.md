# oopz 群统计机器人

QQ 群机器人：群成员 @ 机器人发「**oopz**」或「**mc**」，实时查询 oopz 语音频道在线成员 / Minecraft 服务器在线玩家并回复。

提供**两套接入**：

- **NapCat 版（推荐）**：`bot_napcat.py`，走个人 QQ 号 + [NapCat](https://github.com/NapNeko/NapCatQQ)（OneBot v11），**可主动推送**，不受官方限制，含定时播报 / 进频道欢迎。
- **QQ 官方版（备选/回退）**：`bot.py`，官方机器人仅被动响应（2025-04-21 起官方停用主动推送，发起即报 `40034105`）。

> 🚀 **从零部署**（全新 Windows 环境搭建）：见 [DEPLOY.md](DEPLOY.md)。

## 功能

| 群内 @ | 回复 |
|--------|------|
| `@机器人 你好` | 「收到！被动回复链路已打通 🎉」——链路自检 |
| `@机器人 oopz` | 📊 oopz 语音频道在线成员报告（分域/频道 + 昵称） |
| `@机器人 mc [服名]` | 🗺️ Minecraft 在线人数 + 玩家名单。**不带服名 = 全部服的总览**（每个服一小段名单）；带服名 = 该服明细，服名支持 id / 名字 / 别名 / 唯一前缀，认不出或有歧义都会明确回复 |
| `@机器人 whitelist add/remove <玩家名>`<br>`@机器人 whitelist list` | 🧾 MC 玩家白名单管理（**仅 `MC_ADMIN_QQ` 里的管理员**，且 `mcs_servers.toml` 的 `[whitelist]` 要指到一台配了 RCON 的服） |

> 触发词可改：`.env` 的 `OOPZ_TRIGGER` / `MC_TRIGGER` / `MC_ADMIN_TRIGGER`（逗号分隔，不区分大小写）。一条消息只会被一个插件响应。
>
> MC 服务器的地址、显示名、RCON 密码在 **`mcs_servers.toml`**（不在 `.env`），见 [DEPLOY.md 4.1](DEPLOY.md)。

**自动功能（NapCat 版，配置见 `.env`）**

- ⏰ **oopz 定时播报**：整点对齐推送（间隔 30 分钟则在 :00/:30，间隔 60 则每小时整点），向 `NAPCAT_REPORT_GROUP` 推送独立格式的「📣 oopz 语音频道播报」——**仅当 oopz 有人在线时**，无人则静默跳过
- 👋 **进频道欢迎**：每 `NAPCAT_WELCOME_INTERVAL_SEC` 秒轮询，检测到有人进入目标域语音频道时推送趣味欢迎语（随机文案）
- 🎮 **MC 进服提醒**：每 `MC_WATCH_INTERVAL_SEC` 秒轮询 MC 服务器，有人进服时推送提醒（**只推进服，不推退服**；最小推送间隔 `MC_JOIN_MIN_INTERVAL_SEC` 秒，窗口内的进服合并成一条）
- ⏰ **MC 定时播报**：整点对齐推送「📣 在线播报」+ 玩家名单——**无人在线时静默跳过**
- ⚠️ **MC 掉线提醒**：连续两轮探测失败才判定离线（单轮网络抖动不报），恢复时也会推一条；`MC_NOTIFY_SERVER_STATE=false` 可关

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
├── mcs_servers.toml.example  # 清单模板 → 复制为 mcs_servers.toml
├── bot.py                    # 启动入口①：QQ 官方版（被动响应）
├── bot_napcat.py             # 启动入口②：NapCat/OneBot 版（可主动推送）
├── requirements.txt          # Python 依赖清单
├── vendor/Oopzbot-SDK/       # oopz_sdk 源码（不在 PyPI，随工程分发）
├── tools/
│   ├── oopz_login.py         # 手机号+密码 → 写入 OOPZ_* 凭据
│   ├── oopz_check.py         # 独立验证 oopz 查询链路
│   └── mc_check.py           # 独立验证 MC 取数链路（`--list-targets` 看配置 / `--target` 测单台 / `--self-test` 离线自测）
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
│       ├── mc.py             #   MC 取数层：SLP + 自实现 RCON + list/白名单输出解析
│       ├── mcservers.py      #   服务器清单（mcs_servers.toml 解析 / 校验 / 服名解析）
│       ├── mcrender.py       #   MC 消息文案（明细 / 总览 / 播报，查询与播报共用同一份）
│       ├── textlen.py        #   长度上限与截断（零依赖，供渲染层用）
│       ├── mcadmin.py        #   MC 玩家白名单命令层（解析 / 执行 / 独立验证）
│       ├── admin.py          #   管理员名单（用户级鉴权，留空 = 拒绝一切）
│       ├── push.py           #   群消息推送 + 长度上限
│       ├── schedule.py       #   整点对齐时间槽
│       ├── triggers.py       #   触发词归属（决定消息归哪个插件响应）
│       └── whitelist.py      #   群白名单（被拦下时打 warning，避免静默失效）
├── DEPLOY.md                 # 从零部署指南
└── docs/
    └── chat.md               # 完整技术文档与踩坑记录
```

## 快速开始

**环境**：Windows / Linux，Python ≥ 3.10

**1. 安装依赖**

```bash
pip install -r requirements.txt
pip install ./vendor/Oopzbot-SDK   # oopz_sdk 不在 PyPI，从本地 vendor 安装
```

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

先按 [DEPLOY.md 第 4.1 节](DEPLOY.md#41-minecraft-服务端准备rcon) 在 MC 服务端打开 RCON（**别漏 `broadcast-rcon-to-ops=false`**），再跑：

```powershell
.venv\Scripts\python.exe tools\mc_check.py                     # 实测全部目标（并发）
.venv\Scripts\python.exe tools\mc_check.py --target bingo      # 只测一台
.venv\Scripts\python.exe tools\mc_check.py --self-test         # 只跑解析自测，不联网
.venv\Scripts\python.exe tools\mc_check.py --whitelist         # 只读地看一眼服务端白名单
```

每个目标一个块，块头就是结论；末尾汇总 `N/M 个目标正常`，**有目标不正常时退出码为 1**。
看到 `[OK] … 名单完整` 即成功。不完整时脚本会打印 RCON `list` 的**原始输出**和判定原因。
`--target` 的服名走和群里同一套解析（id / 名字 / 别名 / 唯一前缀），写错了会明确报错而不是
悄悄测全部。

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

> NapCat 版默认**所有群**都能触发查询；想限定群，在 `.env` 设 `NAPCAT_ALLOWED_GROUPS`（逗号分隔群号，留空 = 不限制）。MC 查询用 `MC_ALLOWED_GROUPS`，留空时回退到 `NAPCAT_ALLOWED_GROUPS`。
> 官方版对应 `QQ_ALLOWED_GROUPS`（group_openid 列表），且**不支持 MC 功能**（无主动推送能力，自动功能都做不了）。

### 管理 MC 玩家白名单

管理员在群里发 `@机器人 whitelist add <玩家名>` 即可把玩家加进服务端白名单，不用登控制台。另有 `whitelist remove <玩家名>`、`whitelist list`。

```powershell
# .env：只有列在这里的 QQ 号能用管理命令
MC_ADMIN_QQ=123456789,987654321
```

```toml
# mcs_servers.toml：命令发给哪台服、用什么前缀
[whitelist]
target  = "bingo"          # 某个 [[targets]] 的 id
command = "whitelist"      # RCON 里实际发的命令前缀
```

> ⚠️ `MC_ADMIN_QQ` **留空 = 该功能对所有人关闭**——与其他「留空 = 不限制」的配置相反，这是刻意的：配错的后果是任何人都能改服务端白名单。
>
> ⚠️ `[whitelist].command` **必须与插件注册的命令一字不差**：vanilla 是 `whitelist`，Global Whitelist 是 `globalwhitelist`，ProxyWhitelist 是 `pwl`。写错的表现是每次操作都回 `Unknown command`、群里报「未生效」，而且不报错。注意群里打的触发词（`.env` 的 `MC_ADMIN_TRIGGER`）和这个前缀是两个不同的轴。
>
> 别把两个「白名单」搞混：**群白名单**（`MC_ALLOWED_GROUPS`）限制的是**哪个群**能 @查询；**MC 玩家白名单**（`whitelist.json`）是服务端的玩家准入表，由本功能管理。两者毫无关系，可以同时生效。
>
> 本功能走 RCON，所以 `[whitelist].target` 要指到一台配了 `rcon.password` 的服；且只读写 `whitelist.json`，**不会**替你打开 `server.properties` 里的 `white-list`。
>
> 名字打什么大小写都行：bot 先读一遍白名单，下发命令时改用服务端记录的那条拼写（`add vul` 服务端存的是 `Vul`，回复里会注明），判定同样忽略大小写。**重复添加和重复移除都会如实回「无需改动」，不会虚报成功。**
>
> ⚠️ 若你从正版模式切到 `online-mode=false`，**原有白名单会整体失效**（在线 UUID 与离线 UUID 对不上），得挨个重新加一遍。详见 [DEPLOY.md](DEPLOY.md) 的 F11。

## 自定义回复格式

oopz 回复由 `_build_stats_message()` 拼装。注意 oopz 查询有两个镜像副本：QQ 版在 [plugins/oopz_stats.py](plugins/oopz_stats.py)、NapCat 版在 [plugins_napcat/oopz/oopz_stats.py](plugins_napcat/oopz/oopz_stats.py)，**改格式需同步两份**（下方代码以 QQ 版行号为准）。

MC 回复由 [plugins_napcat/_shared/mcrender.py](plugins_napcat/_shared/mcrender.py) 的 `render_detail()`（单服明细）/ `render_summary()`（总览）拼装。**只有这一份**：`@查询`、进服提醒、定时播报都从这里取格式，所以不会出现「群里和播报里同一个服显示成两样」。

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

改完**重启 `bot.py`** 生效。

## 维护与常见问题

完整排查清单见 [DEPLOY.md 常见问题](DEPLOY.md#8-常见问题已知坑)（F1~F11）与 [日常维护](DEPLOY.md#9-日常维护)。高频六条：

- **@机器人 毫无反应**：先看日志有没有「不在白名单内」的 warning——群白名单拦下时群里的表现和「机器人掉线」完全一样，见 DEPLOY.md 第 8 节 F10。
- **管理命令没权限 / 说未配置 RCON**：见 DEPLOY.md 第 8 节 F11。最常见的是 `MC_ADMIN_QQ` 留空（= 功能整体关闭）或漏加自己的 QQ 号。
- **统计 / 播报突然失效**：多半是 `OOPZ_JWT_TOKEN` 过期（约 31 天），重跑 `tools/oopz_login.py` → 回填 `.env` → 重启，见 DEPLOY.md 第 8 节 F7。
- **MC 进服提醒不触发**：先跑 `tools/mc_check.py`——**名单不完整时提醒会静默暂停**（刻意设计，避免基于残缺名单误报）。脚本会打印 RCON `list` 原文与判定原因，见 DEPLOY.md 第 8 节 F8。
- **部分域不出现在统计里**：如「Voxel Passion 像素乐园」频道接口返回 `channels: null`，SDK 解析失败被跳过，不影响其他域（原因见 docs/chat.md §5.5）。
- **主动推送（仅 NapCat 版）**：官方版无主动推送能力；定时播报 / 进频道欢迎 / MC 全部自动功能只在 NapCat 版生效。文案位置见 [DEPLOY.md 第 9 节](DEPLOY.md#9-日常维护)。

## 详细文档

技术方案、oopz API 对照、SDK 三个已知坑 → 见 [docs/chat.md](docs/chat.md)。
