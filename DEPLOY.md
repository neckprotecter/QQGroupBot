# 从零部署指南（Windows 11）

本项目是 **QQ 群机器人 + oopz 语音频道在线统计**。QQ 侧有两条入口，推荐使用 **NapCat 版**（可主动推送定时播报 / 进频道欢迎）；QQ 官方版（`bot.py`）仅被动回复，且官方已停用主动推送。

本文档按"干净环境从 0 部署"编写。当前工程目录已经过清理，只保留运行所需 + 部署所需文件。

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
├─ tools/
│  ├─ oopz_login.py          # 生成 oopz 平台登录凭据（device_id / jwt / 私钥）
│  ├─ oopz_check.py          # oopz 诊断脚本
│  └─ mc_check.py            # Minecraft 诊断脚本（SLP + RCON + 白名单，含解析自测）
├─ requirements.txt          # Python 依赖清单
├─ vendor/Oopzbot-SDK/       # oopz_sdk 源码（不在 PyPI，随工程分发）
├─ .env.example              # 配置模板 → 复制为 .env 填写
├─ .env                      # 实际配置（含密钥，勿外泄/勿提交）
├─ DEPLOY.md                 # 本文档
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
pip install ./vendor/Oopzbot-SDK     # oopz_sdk 不在 PyPI，从本地 vendor 安装
```

> oopz_sdk 会通过其 `pyproject.toml` 自动拉取 aiohttp / cryptography / pillow / pydantic / playwright / requests 等依赖，无需手动装。

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
| `MC_HOST` / `MC_PORT` | MC 服务端地址。bot 与 MC 同机时填 `127.0.0.1` |
| `MC_RCON_PASSWORD` | 见 4.1 节。**留空 = 不启用 RCON**，此时名单降级用 SLP 样本（在线 ≤12 人时仍是完整名单） |
| `MC_WATCH_GROUP` | 进服提醒推送的 QQ 群号（逗号分隔多群，留空=不启用）。**只推进服，不推退服** |
| `MC_WATCH_INTERVAL_SEC` | 进服检测轮询间隔（秒，默认 10）。进服到被发现的延迟 = 0～本值 |
| `MC_JOIN_MIN_INTERVAL_SEC` | 进服推送最小间隔（秒，默认 15），窗口内的进服合并成一条，防刷屏。**设 0 = 进服立刻推**。最大额外延迟 ≈ 本值 + 一个轮询间隔 |
| `MC_REPORT_GROUP` / `MC_REPORT_INTERVAL_MIN` | MC 定时播报目标群 / 间隔（分钟，默认 60，整点对齐）。**无人在线时静默跳过**，与 oopz 播报一致 |

### 4.1 Minecraft 服务端准备（RCON）

MC 功能要拿**完整**玩家名单，靠的是 RCON 执行 `list` 命令（SLP 的玩家样本默认最多 12 个随机玩家，`hide-online-players=true` 时还直接为空）。改服务端 `server.properties`：

| 配置项 | 值 | 说明 |
|---|---|---|
| `enable-rcon` | `true` | 不开则只能用 SLP 降级路径 |
| `rcon.port` | `25575` | 与游戏端口独立，填进 `.env` 的 `MC_RCON_PORT` |
| `rcon.password` | 强密码 | 填进 `.env` 的 `MC_RCON_PASSWORD` |
| `broadcast-rcon-to-ops` | **`false`** | **默认是 `true`**！不改的话，bot 每 20 秒一次的 `list` 会把玩家名单广播给所有在线 OP，聊天框持续刷屏 |
| `white-list` | `true` | 想用**白名单管理**才需要。不打开时 `whitelist add` 照样能执行、也写进 `whitelist.json`，但服务端不拦人——**bot 看不出来**，只会如实报「已添加」。DEPLOY 无法替你判断，请自行确认 |

改完**重启 MC 服务端**，然后跑诊断脚本确认：

```powershell
.\.venv\Scripts\python.exe tools\mc_check.py
```

看到 `名单完整 True` 即成功。若为 `False`，脚本会打印 RCON `list` 的**原始输出**和判定原因，照着排查。

> ⚠️ **RCON 是明文协议，密码可被重放**。`rcon.port` 只绑内网/本机，**绝对不要暴露公网**。

**别把两个「白名单」搞混**（这是本节最容易读错的地方）：

| 叫法 | 是什么 | 配置在哪 |
|---|---|---|
| **群白名单** | 哪些 **QQ 群**能 @机器人 查询 | `MC_ALLOWED_GROUPS` / `NAPCAT_ALLOWED_GROUPS` |
| **MC 玩家白名单** | 服务端 `whitelist.json`，谁能进服 | 由群里的 `whitelist` 命令管理；谁能执行看 `MC_ADMIN_QQ` |

两者毫无关系、可以同时生效：一个限制「哪个群」，一个限制「哪个人」，另一个限制「哪个玩家能进服」。

白名单管理功能要单独验一遍（**只读**，不改服务端）：

```powershell
.\.venv\Scripts\python.exe tools\mc_check.py --whitelist
```

它打印 `whitelist list` 的原文和解析结果。看到 `[OK] 解析正常` 就说明本服的白名单输出格式能被正确判定成败；若报「判不出来」，说明格式被插件改写过，加白命令会回「未能验证」而不是「已添加」（功能仍可用，只是无法自动确认）。

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

看到日志 `Bot <QQ号> connected` 即连接成功。日志写入 `logs/napcat_bot.log`。

---

## 7. 验证

- 群里 `@机器人 oopz` → 回复 oopz 语音频道在线明细
- 定时播报：到整点槽位（间隔 30 分钟则为 :00/:30），若 oopz 有人在线则推送「📣 oopz 语音频道播报…」（无人在线时静默跳过，属正常）
- 进频道欢迎：有人进入目标域语音频道后，约 1 个轮询周期内推送趣味欢迎语
- **MC 查询**：群里 `@机器人 mc` → 回复 MC 服务器在线人数与名单
- **MC 进服提醒**：自己进服，最慢 `MC_WATCH_INTERVAL_SEC` + `MC_JOIN_MIN_INTERVAL_SEC` 秒内推送「🎮 X 加入了…」（默认 10+15，即 25 秒内；两人紧挨着进服会合并成一条）
  - ⚠️ **首次启动只建基线**（当时已在线的玩家不算「新进服」），所以重启后不会刷屏。若重启后立刻推出一堆存量玩家，说明基线门控有 bug。
- **MC 玩家白名单管理**（配了 `MC_ADMIN_QQ` 才需要验）：
  1. 用**不在** `MC_ADMIN_QQ` 里的号发 `@机器人 whitelist add CodexTest` → 应回「🚫 你没有 MC 管理权限」。这一步同时验证了 hello 不会再来抢答。
  2. 用管理员号发同一条 → 应回「✅ 已将 CodexTest 添加到白名单。」
  3. `@机器人 whitelist list` → 列表里能看到 `CodexTest`。
  4. `@机器人 whitelist remove CodexTest` → 「✅ 已将 CodexTest 移出白名单。」再 `list` 确认已消失。
  - ⚠️ **务必用一次性假名字**，别拿真实玩家的名字试——这条链路会真的改服务端 `whitelist.json`。走完第 3、4 步就回到原样。
  - bot 是「先读名单 → 再下命令 → 再读名单确认」。读第一次是为了照抄服务端记录的拼写、并判断「本来就在 / 本来就不在」，读第二次是因为 `whitelist add` 的回执是本地化文案、不足为凭。所以回复慢一点是正常的：每次变更最多 3 个 RCON 往返（已经是目标状态时只花 1 个），最坏 3×`MC_RCON_TIMEOUT` 秒。
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
  - 没配 `MC_RCON_PASSWORD`：在线人数 ≤12 时 SLP 样本本身就是完整名单；超过就必须开 RCON（见 4.1）。
  - 配了 RCON 仍不完整：看脚本打印的 `list` **原始输出**——可能是插件改写了 `list` 格式，或 `enable-rcon` 没生效。
- **名单完整 True 但仍不推** → 查 `.env` 的 `MC_WATCH_GROUP` 是否填了、bot 是否在该群。
- **日志噪音**：RCON 出问题时**只在状态跃迁时打一条告警**，不是每轮一条。所以日志里只有一条 warning 是正常的，别以为没报错就没问题——以 `mc_check.py` 的输出为准。

### F9 MC 名单对不上 / 频繁假进服
- 玩家显示名带队伍前缀（`§a[VIP] Alice`）时，前缀本身被保留为名字的一部分。前缀变更会被算成一次进服。
- 若确实频繁误报，可在服务端试 `list uuids`（输出形如 `Alice (uuid)`），改用 UUID 作身份基准即可。先用 `mc_check.py` 确认你那台服务端支持再改。

### F10 @机器人 查询毫无反应（群里一条回复都没有）
**先看日志有没有这条 warning**：

```
群 123456789 @mc 查询被忽略：不在白名单内。NAPCAT_ALLOWED_GROUPS=...（逗号分隔；留空 = 所有群都可查）
```

有 → 该群不在白名单里。把群号加进 `NAPCAT_ALLOWED_GROUPS`（MC 另有 `MC_ALLOWED_GROUPS`，留空时回退到前者），重启 bot。

没有这条 warning → 说明消息根本没被判定成「@机器人」，检查是不是 @ 到了别的号 / 只是打了触发词没 @。

> 这里拦人的是**群白名单**（限制哪些 QQ 群能 @查询），跟「MC 玩家白名单」（服务端 `whitelist.json`）是两回事，别顺着这条去改 `MC_ADMIN_QQ`。
>
> 为什么容易踩：**白名单拦截时群里的表现和「机器人掉线」一模一样**——oopz / MC 两边都不吭声，
> 连 hello 的功能引导也不会出现（因为它被触发词互斥挡掉了）。所以务必以日志为准，别靠群里的表现猜。

### F11 `@机器人 whitelist` 没反应 / 说没权限 / 说未能验证

按回复内容对号入座（**每一步的拒绝都会在日志里留一条 warning**，以日志为准）：

| 群里看到 | 原因 | 修复 |
|---|---|---|
| 「🚫 你没有 MC 管理权限」 | 你的号不在 `MC_ADMIN_QQ` 里；或该项**留空**（留空 = 功能对所有人关闭） | 把自己的 QQ 号加进去，重启 bot。日志里能区分「未配置」和「不在名单内」两种 |
| 「🚫 本群未启用 MC 管理命令」 | 该群不在 `MC_ALLOWED_GROUPS` / `NAPCAT_ALLOWED_GROUPS` 里（与 @查询 共用同一条链） | 加群号或清空该配置 |
| 「⚠️ 未配置 MC_RCON_PASSWORD…」 | 管理命令必须走 RCON，没有降级路径 | 按 4.1 节配好 RCON |
| 「❌ 未生效：名单里仍没有 X」 | 命令发出去了，但服务端没执行 | 看日志里那行 warning 的 **RCON 回执原文**——常见是服务端根本没启用 `whitelist` 命令，或被权限插件接管 |
| 「⚠️ 命令已发送（未能验证）」 | 服务端 `whitelist list` 的输出格式解析不了（没有冒号，多半被插件改写过） | **命令很可能已经生效**，去服务端 `whitelist list` 确认。想根治先跑 `tools\mc_check.py --whitelist` 看原文 |
| 完全没有任何回复 | 消息没被 @ 到 / 不在群里 | 同 F10 |
| 回的是「未知命令」用法提示 | 命令格式不对 | 用法提示里会列出当前触发词 |

其余已知限制（都不是 bug）：

- **`white-list=false`**：`add` 会写进 `whitelist.json`、`list` 也看得到，bot 如实报「已添加」，但服务端不拦人。bot 读不到 `server.properties`，判断不了——自己去服务端确认。
- **已在名单中再 add** / **移除一个不在名单里的名字**：bot 先读名单，发现已经是目标状态就**不下发命令**，回的是「ℹ️ 已在白名单中，无需重复添加」/「ℹ️ 本来就不在白名单里，无需移除」。不会把没发生的事报成「已添加 / 已移出」。
- **名字的大小写**：服务端存的拼写常和你输入的不同（`add vul` → 存成 `Vul`，服务端拿玩家档案里的规范拼写替换）。bot 的判定忽略大小写，**下发命令时也改用服务端记录的那条拼写**，所以不用管自己打的是大写还是小写；回复里若拼写有出入会附一句「（服务端记录为 Vul）」。
- **offline-mode（`online-mode=false`）的两件事**：
  - **切模式会让现有白名单失效**。`whitelist.json` 每条都带 UUID，在线模式存的是 Mojang 正版 UUID，离线模式算的是由名字推出的离线 UUID——**两者不匹配，原有条目全部进不去**。切模式后必须挨个重新加一遍（用 bot 跑 `whitelist list` → 对每个人 `whitelist add` 即可）。
  - **同名不同大小写可能变成两条独立记录**。离线 UUID 由名字字面量算出（区分大小写），`Vul` 和 `vul` 是两个不同的人；在线模式下它们会被解析到同一个账号、只会有一条。bot 两种都认：`remove vul` 会把所有同名不同拼写的条目**一并移除**，并在回复里列出删了哪几条。
- **基岩版玩家（Floodgate/Geyser）**：白名单里带 `.` 前缀（`.Steve`），玩家名正则不允许 `.`，所以**加不进去**，得去服务端控制台手动加。
- **玩家名规则**：只接受 `字母 / 数字 / 下划线`，1~16 位（Java 版规则）。写成别的会被判非法并回用法——这条正则同时也是防注入的关键，不宜放宽。

---

## 9. 日常维护

- **改文案**：
  - oopz 播报格式在 `plugins_napcat/oopz/auto_reporter.py` 的 `_build_broadcast_message()`；进频道欢迎语在同文件顶部的 `_WELCOME_TEMPLATES`。
  - MC 播报格式在 `plugins_napcat/mcs/mc_reporter.py` 的 `_build_report_message()`；进服提醒语在同文件顶部的 `_JOIN_TEMPLATES`；`@mc` 回复格式在 `plugins_napcat/mcs/mc_stats.py` 的 `_build_message()`。
  - 白名单命令的回复文案与用法提示在 `plugins_napcat/mcs/mc_admin.py` 的 `_build_message()` / `_usage()`；命令解析与执行在 `plugins_napcat/_shared/mcadmin.py`。
- **改触发词**：`.env` 的 `OOPZ_TRIGGER` / `MC_TRIGGER` / `MC_ADMIN_TRIGGER`（不用改代码）。归属逻辑在 `plugins_napcat/_shared/triggers.py`。
- **改管理员**：`.env` 的 `MC_ADMIN_QQ`（逗号分隔 QQ 号，**留空 = 关闭该功能**）。鉴权在 `plugins_napcat/_shared/admin.py`。
- **排查 MC 取数**：`.\.venv\Scripts\python.exe tools\mc_check.py`（加 `--self-test` 只跑解析自测，不联网；加 `--whitelist` 只看服务端白名单，只读不改）。
- **续期 oopz JWT（每月一次）**：`OOPZ_JWT_TOKEN` 约 31 天过期。症状：@统计 无回复、播报显示「查询失败」。重跑 `tools\oopz_login.py` → 回填 `.env` → 重启 bot。
- **改间隔/目标群**：改 `.env` 后重启 bot。
- **看日志**：`logs/napcat_bot.log`（注意 PowerShell 用 `-Encoding UTF8` 读）。
- **NapCat 升级/重装**：三个启动坑（F1/F2/F3）会复发，按第 8 节处理。
- **清理旧日志**：`logs/` 下文件可随时删，bot 会自动重建。
