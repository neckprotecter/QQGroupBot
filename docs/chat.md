# 项目文档：QQ群机器人 + oopz 语音频道在线统计

> 技术方案与实现说明。项目为**双入口**：`bot_napcat.py`（NapCat/OneBot v11，个人 QQ 号，**可主动推送**，推荐）+ `bot.py`（QQ 官方版，仅被动响应，作回退）。本文档主体以 QQ 官方版为准，NapCat 版差异见 [§8](#8-napcat-版bot_napcatpy)。
>
> **文档分工**：本文档讲**方案与原理 / 踩坑原因**；从零部署、操作步骤、常见问题见 [DEPLOY.md](../DEPLOY.md)；项目总览、快速上手见 [README.md](../README.md)。

---

## 1. 核心需求与背景决策

在 QQ 群部署机器人，群成员 @ 机器人发「统计」，机器人实时查询 oopz 语音频道在线成员并回复。

**关键背景（务必先读）**：QQ 官方机器人的**主动推送**（定时播报、进频道欢迎通知）已于 **2025-04-21 官方停止支持**——主动向群发送消息的接口不再可用，发起即报错 `40034105 主动消息失败`。

因此针对 **QQ 官方版**（`bot.py`）：

- ✅ **保留**：@机器人「统计」→ 实时 oopz 成员报告 → 被动回复（响应 @ 消息携带 `msg_id`，不受主动消息限制）
- ❌ **移除**：进频道自动欢迎、定时播报（均为主动推送，已不可行）

> **若改用 NapCat 版**（`bot_napcat.py`，走个人 QQ 号 + OneBot v11），**主动推送恢复可用**，上述 ❌ 项可重新加回（见 [§8](#8-napcat-版bot_napcatpy)）。

---

## 2. 总体技术架构

单个 Python 异步进程，两个部分：

- **NoneBot2**（`nonebot2` + `nonebot-adapter-qq`）：接入 QQ 官方机器人，接收 `GROUP_AT_MESSAGE_CREATE`（群内 @ 机器人）事件，被动回复。
- **oopz-sdk**：**只使用 REST 查询**（`get_joined_areas` / `get_voice_channel_members` / `get_person_infos_batch`），**不连 WebSocket**。查询仅在收到「统计」时按需触发，无常驻监听。

```mermaid
graph LR
    A[QQ群成员 @机器人「统计」] --> B[NoneBot2 事件]
    B --> C[oopz-sdk REST 查询]
    C --> D[实时在线成员 + 昵称]
    D --> E[被动回复 msg_id=原消息]
```

---

## 3. 环境与配置

各配置键的含义与填写见 [DEPLOY.md](../DEPLOY.md#4-配置-env)，`OOPZ_*` 凭据由 `tools/oopz_login.py` 生成（DEPLOY 第 5 节）；依赖清单见 `requirements.txt`。

这里只记录一个容易踩的实现细节：`.env` 里 `OOPZ_PRIVATE_KEY` 是**单行 + `\n` 转义**的 PEM（登录脚本 `_escape_pem` 写入），插件读取时用 `.replace("\\n", "\n")` 还原成多行（见 §5.3）。

---

## 4. 目录结构

目录结构与文件职责见 [README.md](../README.md#目录结构)（随工程更新），完整部署文件清单见 [DEPLOY.md](../DEPLOY.md#0-工程文件清单)。

---

## 5. 实现细节与踩坑记录

### 5.1 被动回复链路（已在真群验证）

QQ 被动回复 = 在回复消息里带 `msg_id`（取事件 `id`），5 分钟内有效、每个 @ 最多回 5 条。代码见 [plugins/oopz_stats.py](plugins/oopz_stats.py) 末尾：

```python
await bot.post_group_messages(
    group_openid=group_id,   # 事件里的 group_openid
    msg_type=0,
    content=msg,
    msg_id=event.id,         # 关键：被动回复必须带
)
```

### 5.2 oopz 查询：只启 REST，别用 bot.start()

`OopzBot(config)` 构造后**不要** `await bot.start()`——它会去连 `wss://ws.oopz.cn` WebSocket 并启动适配器服务器，纯查询场景会挂住。只需：

```python
bot = OopzBot(config)
await bot.rest.start()   # 只启动 HTTP REST 客户端
# ... 查询 ...
await bot.rest.close()
```

### 5.3 ⚠️ oopz-sdk 的 `from_env_async` 有 bug——必须绕开

SDK 的 `OopzConfig._require_env()` 校验通过后**没有 `return`**，隐式返回 `None`。于是 `from_env_async()` 走 credentials 分支后 `cls(**values)` 里三个字段全是 `None`，进 `__post_init__` 被 `str(None or "")` 洗成空串，最终 `OopzBot(config)` 报 `ValueError: OopzConfig credentials are incomplete`。

**绕开方法**：直接手动构造（[plugins/oopz_stats.py](plugins/oopz_stats.py#L39-L51)）：

```python
OopzConfig(
    device_id=os.environ["OOPZ_DEVICE_ID"],
    person_uid=os.environ["OOPZ_PERSON_UID"],
    jwt_token=os.environ["OOPZ_JWT_TOKEN"],
    private_key=os.environ["OOPZ_PRIVATE_KEY"].replace("\\n", "\n").strip(),
    app_version=os.environ.get("OOPZ_APP_VERSION", "").strip(),
)
```

### 5.4 oopz 关键 API（以安装版为准，已核对）

| 用途 | 调用 |
|------|------|
| 列出已加入的域 | `bot.areas.get_joined_areas()` → `list[JoinedAreaInfo(area_id, name)]` |
| 域内语音频道在线成员 | `bot.channels.get_voice_channel_members(area=area_id)` → `VoiceChannelMembersResult.channel_members: dict[频道id, list[VoiceChannelMemberInfo]]` |
| 频道 id → 频道名 | `bot.areas.get_area_channels(area_id)` → `list[ChannelGroupInfo]`（`channels[].channel_id/name`） |
| uid 批量查昵称 | `bot.person.get_person_infos_batch(uids)` → `list[UserInfo(uid, name)]`（每批≤30，带缓存） |

`VoiceChannelMemberInfo` 只有 `uid`/`is_bot`/`enter_time`，**没有昵称**，需用 `get_person_infos_batch` 批量解析。消息里跳过 `is_bot` 的机器人。

### 5.5 已知问题

- **部分域频道列表解析失败**：某域（如 "Voxel Passion 像素乐园"）的 `ChannelGroupInfo.channels` 接口返回显式 `null`，SDK 模型 `list[ChannelInfo]` 在 pydantic v2 下拒收 → 该域被跳过。插件对每个域 try/except，不影响其他域。若需修复，可给 SDK 模型加 `validate_default`/自定义校验，或对出错域走裸 REST。
- **JWT 有效期**约 31 天（`OOPZ_JWT_TOKEN` 的 `exp`），过期后 `@统计` 会查询失败，需重跑 `tools/oopz_login.py`。

---

## 6. 运行与联调

启动步骤（凭据生成 / 验证 / 两个入口）见 [DEPLOY.md](../DEPLOY.md#5-生成-oopz-登录凭据) 与 [DEPLOY.md 第 6 节](../DEPLOY.md#6-启动)。群内 @ 机器人发「你好」/「统计」的回复示例见 [README.md](../README.md#功能)。

---

## 7. 限制与扩展

| 限制 | 说明 |
|------|------|
| 只统计不监听（仅 QQ 官方版） | 官方版无 join/leave 事件，无法做欢迎/通知（且主动推送已官方停用）；**NapCat 版不受此限**（见 §8） |
| 查询延迟 | 每次 @统计 约 2~5s（REST + 批量昵称），带 20s 超时兜底 |
| 群消息长度 | 文本上限约 2000 字符，插件截断到 1800 并在结尾加省略号 |
| 单域限定 | 已通过 `.env` 的 `OOPZ_TARGET_AREAS` 限定只统计指定域名（如「奇妙小房间」），改/删该行可调整范围 |

扩展方向：按频道在线时长统计、多群绑定不同域、把在线状态写数据库做趋势看板（仍是被动查询，不推送）。

---

## 8. NapCat 版（bot_napcat.py）

### 8.1 为什么需要它

QQ 官方机器人的主动推送（定时播报、进频道欢迎等）2025-04-21 起被官方停用（`40034105`）。NapCat 版走**个人 QQ 号 + OneBot v11 协议**，不受官方限制，**主动推送恢复可用**；同时 @统计 的被动回复也照常工作。

### 8.2 架构差异（与 QQ 官方版）

| 维度 | QQ 官方版 `bot.py` | NapCat 版 `bot_napcat.py` |
|------|------|------|
| 适配器 | `nonebot-adapter-qq` | `nonebot-adapter-onebot`（v11） |
| 收发链路 | 官方 WS（`bots.qq.com`） | 本地 NapCat 的 WS（默认 `127.0.0.1:3001`） |
| 主动推送 | ❌ 官方停用 | ✅ 可用（`send_group_msg` 即可） |
| 事件回调 | `GROUP_AT_MESSAGE_CREATE` | OneBot `message`（群/私聊统一） |
| 回复方式 | `post_group_messages(..., msg_id=event.id)` | `send_group_msg(group_id=..., message=...)` |
| 群标识 | `group_openid` | `group_id`（真实 QQ 群号） |

**正向 WS 的选择**：NoneBot 的 `~aiohttp` 驱动是 `class Driver(Mixin, NoneDriver)`——**纯客户端、无 Web 服务器**，无法做反向 WS（nonebot 当服务端）。因此 NapCat 版用**正向 WS**：NapCat 起服务（默认 3001），NoneBot 连过去。若要反向 WS，需换成带 Web 服务器的驱动（如 `~fastapi`）。

代码几乎与 QQ 版同构，仅适配器 import 与发送方式不同；oopz 查询逻辑（`_config_from_env` / `bot.rest.start()` / 频道名映射 / 消息拼装）完全复用，见 [plugins_napcat/oopz/oopz_stats.py](../plugins_napcat/oopz/oopz_stats.py)。

### 8.3 配置与运行

1. 安装 [NapCat](https://github.com/NapNeko/NapCatQQ)，用**个人 QQ 号**登录（第三方协议，风险见 8.4）。
2. NapCat 里开启「正向 WebSocket」，端口默认 **3001**（若改了端口，同步改 `.env` 的 `ONEBOT_V11_WS_URLS`）。
3. `.env` 已配好：
   ```bash
   ONEBOT_V11_WS_URLS='["ws://127.0.0.1:3001"]'
   ```
4. 启动：`.venv\Scripts\python.exe bot_napcat.py`

日志出现 `Bot <QQ号> connected` 即成功。NapCat 未运行时日志会持续报连不上 3001，属正常。

### 8.4 风险

- **账号风控**：第三方协议登录个人 QQ 号可能被风控掉线（与账号年龄 / 登录 IP / 环境有关），NapCat 面板「反检测」开关可尝试缓解。
- **协议不稳定**：NapCat 更新频繁，依赖其 WS 事件格式；升级后需回归测试 @统计。
- **仅供个人/小范围使用**，勿用于大规模营销场景。

> QQ 官方版 `bot.py` + `plugins/` 保持原样未动，作为回退方案随时可切回。

---

## 9. MC 群关联：一个 bot、多个群、各看各的服

### 9.1 要解决的问题

机器人同时服务两类 QQ 群：**社团群**看自己的三台服（GTNH / Bingo / 谁是杀手，将来挂代理），
**建筑群**面向社会的建筑群组服，那时**一台服都还没搭**。改造前 MC 功能是完全扁平的：
`mcs_servers.toml` 只说「有哪些服」，`.env` 里三个群变量（`MC_ALLOWED_GROUPS` /
`MC_WATCH_GROUP` / `MC_REPORT_GROUP`）只决定「哪些群能用、往哪推」——
**没有任何一处把「群」和「服务器集合」关联起来**，所以两个群看到的东西必然一模一样。

（这三个变量**现在都已作废**：`MC_ALLOWED_GROUPS` 由 `[[audience]].groups` 取代，
另两个由 `[[audience]].watch` / `.report` 取代。见 9.11。）

这一层必须排在白名单（发给哪台服）和播报（盯哪些服）之前：那两件事都要**按群**做，
先按全局做一遍等于各做两遍。

### 9.2 两份配置、两种敏感度

| 文件 | 管什么 | 含密码？ | 能否给人看 |
|---|---|---|---|
| `mcs_servers.toml` | **有哪些服**：id / 名称 / kind / host:port / RCON | ✅ 各服 RCON 明文密码 | ❌ |
| `mcs_audiences.toml` | **哪个群看哪几台**：群号 / targets / primary / whitelist | ❌ | ✅（前提是别把密码抄进来） |

分成两个文件是刻意的：`mcs_audiences.toml` 不含密码，所以「这份可以单独贴出来讨论、
单独交给别人核对群号」，而改造过程中确实需要反复对着它讨论。代价是两份配置要对上，
所以**加载入口只有一个**（见 9.4）。

一条 `[[audience]]` 就是一条**群关联**：

```toml
[[audience]]
name      = "社团群"                                   # 只在运维看的日志/诊断里出现
groups    = [123456789]                                # 必填非空；群号 int / str 都认
targets   = ["gtnh", "bingo", "backstabbed"]           # 顺序 = 总览顺序；可为空
primary   = "gtnh"                                     # 不带服名的 @查询 默认看它
whitelist = { target = "bingo", command = "whitelist" } # 整段省略 = 本群不管白名单
```

### 9.3 复用了 ServerBook，而不是新造一套视图

`ServerBook`（目标表 + 服名解析 + 白名单归属 + `targets_primary_first`）**一字未改**，
只是多了一个 `scoped()`：

```python
book.scoped(ids, primary=..., whitelist_target=..., whitelist_command=...) -> ServerBook
```

投影出的子 book 里**只有本关联的目标**，所以：

- 关联外的服名 `resolve()` 不出结果 —— 「建筑群打 `gtnh` 回『没有叫 gtnh 的服』」
  是投影本身的性质，**不靠调用方自觉过滤**。隔离因此不可能被某条分支忘掉。
- `whitelist_owner` / `primary_target` / `targets_primary_first` 全部原样复用，零重复代码。

⚠️ **`scoped()` 必须同时重建 `_by_id`**，这是本次最容易踩的坑：漏了它 `get()` 会全返回
`None`，症状是「查询正常、白名单却总说不可用」，而 `primary_target` 因为退回
`targets[0]` **碰巧是对的** —— 最难发现的那种。自测里有一条专门钉住它
（投影 book 的 `get(id)` 对每个 id 都必须非 `None`）。

### 9.4 单一加载入口：`default_config()`

`mcaudiences.default_config()` 是**唯一**的配置加载入口，一次读两个文件、只缓存一次
（`mcservers.default_book()` 因此被**删掉**了）。这样就不存在「清了 book 的缓存、没清
audiences 的缓存」这种中间态 —— 两个缓存各自记着不同时刻的配置，症状是「改完配置重启，
群里一半新一半旧」。`lru_cache` 不缓存异常这条性质保留：配置写错时每次调用都重试读盘，
现场改好、**下一条群消息就正常**。

### 9.5 「被提到才算开通」——拒绝也是要说话的

| 群的状态 | 群里看到的回复 |
|---|---|
| 没被任何 `[[audience]]` 提到 | 🤔 本群还没有开通 MC 查询。其它功能可以发「@机器人 你好」看看。 |
| 提到了，但 `targets = []` | 🏗️ 本群关联的服务器还没接入，暂时没有可查询的内容。 |
| 本群那条没写 `whitelist` | ⚠️ 本群没有指定白名单服，白名单管理不可用。 |
| 写了白名单服但没配 RCON 密码 | ⚠️ 白名单服 Bingo 没配 RCON 密码，命令发不出去。 |

**群内文案一律说「本群」，不带关联名** —— 群友不知道配置文件里那些名字是什么。
关联名只出现在运维看的启动日志与 `--list-audiences` 里。

第三条和第四条是**分开**的两件事（「没配」和「配了但没密码」修法完全不同），
在改造前它们共用一条文案、而那条文案指向的是这次刚删掉的 `[whitelist]` 段 ——
照着改永远改不对。

**为什么必须回一句**：`mc` / `我的世界` / **`服务器`** 这几个触发词是在整条消息上做
子串匹配的，被 MC 认领的消息不会再有别的插件应答。改造前这里是**静默 return**，
于是「没开通的群」和「机器人掉线」在群里长得一模一样。代价是拒绝文案会盖掉 hello 的
功能引导（它被触发词互斥挡掉了），所以文案末尾附了一句指路。与此同时，**日志里必须
留一条同等信息的 warning**（群号、读的是哪个文件、文件里提到了哪些群）——
DEPLOY 的 F10 整节就是教人 grep 它。

### 9.6 孤儿目标：本工程花最多力气防的那类故障

新服加进 `mcs_servers.toml`、却忘了关联给任何群时：**群里打它回「没有叫 X 的服」，
而 `--list-targets` 和启动日志都会正常列出它** —— 看哪个都像没问题。
所以加载期算一次「哪些目标没被任何 `[[audience]]` 引用」，然后在三个地方都标出来：
启动日志的 warning、`--list-targets` 每行的 `← ⚠️ 没被任何群关联`、
`--list-audiences` 末尾单独一段。

参考的补丁点：`mcaudiences._cross_warnings()`（孤儿 + `.env` 里 `MC_ALLOWED_GROUPS`
的残留）。后者留着不生效、但会让人以为它还在拦人，所以非空就报一条
「已不再生效，请删掉这行」。

### 9.7 诊断脚本的新视角

`tools/mc_check.py` 的承诺是「脚本里跑得通的写法，群友打出来也一定跑得通」。
加了群关联之后这条承诺隐含一个变化：**群友永远站在某条关联里，脚本不能站在全量表上**。
所以：

- 新增 `--list-audiences`（只读，不联网），新增 `--audience <关联名>`，
  **默认取第一条关联**并在输出头两行写明是哪条；
- `--target` 的服名解析**限定在所选关联内**，关联外的服名如实报「认不出」，
  并点明这是隔离规则、不是配置写错（否则会把人指去改配置里的名字）；
- `--whitelist` 也走关联（白名单归属本就是按群定的）；
- 不带参数时探测的是**所选关联的目标**，不再是「全部目标」。

> ⚠️ 新增命令行参数**必须同时加进 `_parse_argv` 的 `known` 集合**：漏了不会自测失败
> （自测调的是纯函数），只有在真跑命令行时才被当成不认识的参数拦下（退出码 2）。

### 9.8 命令前缀真的接上了（这条是刻意提前做的）

`whitelist.command` 从关联里一路传进 `mcadmin.run_whitelist_command(cmd, target, command)`，
不再在命令层硬编码 `"whitelist"`。这原本排在 P6，提前做的理由是：**把它挪进关联、
却让命令层继续写死，就等于造了一个「被接受但被忽略」的配置键** —— 配置、日志、诊断
三处都会显示新前缀，而实际发出去的老是 `whitelist`。改的人会以为自己已经改好了。
本工程一直在防的就是这类东西，所以宁可现在接上（今天 `command` 就是 `whitelist`，
所以行为零变化），也不要带着一个会撒谎的键进 P6。

钉住它的自测只能读源码（`run_whitelist_command` 要活的 RCON，跑不起来）：
断言那个函数体内不再出现 `"whitelist ` 字面量、且签名里有 `command` 参数。

### 9.9 本期没做的（两条都已在 P7 做掉，见 9.11）

- **进服提醒 / 定时播报**只看「`MC_WATCH_GROUP` 第一个群所属那条关联的**主服**」，
  模块级状态还没按关联分桶（P7）。本期只把它的监控目标从 `targets[0]` 的**隐式回退**
  改成显式解析：`[defaults].primary` 移走后，那个回退会静默改看另一台服 ——
  今天恰好 `primary = "gtnh"` 且 gtnh 就是第一个 target，所以**现在完全看不出问题**，
  谁哪天调一下 targets 顺序才会踩到，而启动日志会跟着一起错。
  → P7 把两个循环整个按关联重写了（9.11）。
- `MC_WATCH_GROUP` / `MC_REPORT_GROUP` 暂时留在 `.env`，等 P7 再搬进关联文件 ——
  **本期刻意不加这两个键**，不能有「接受了但忽略掉」的配置。
  → P7 换成了 `[[audience]].watch` / `.report` 两个布尔键（9.11）。

### 9.10 群内联调抓到的 bug：空名单的回执没有冒号

不是群关联本身的缺陷，但只有把功能真跑起来才会暴露，所以记在这里。

`parse_whitelist_names` 的解析策略是「**第一个冒号**处切掉前缀、余下按逗号拆」——
这样设计是为了**不解析本地化的前缀文案**，`There are N whitelisted players:` 换成
中文也照样能切。但服务端对**空名单**用的是**另一个翻译键**（vanilla 的
`commands.whitelist.none`），那句话**整句没有冒号**：

```
There are no whitelisted players          # Bingo 26.2 实测原文，2026-09-21
```

于是它被「没有冒号 = 格式不认识」这条规则误判成 `None`。**后果远不止查不到**：
`add` / `remove` 的第一步是**前置读**名单（要先看清单里有没有、以及服务端记的是什么
拼写），读到 `None` 就在 `mcadmin.run_whitelist_command` 里提前 return —— **一条命令
都不会发出去**。而「白名单为空」正是每一台新服的初始状态，也就是说这个功能在最需要
它的那一刻完全是废的。

之前几轮没发现，是因为 GTNH 的名单里有 3 个人（有冒号、解析正常），而 Bingo 一直没启动。

**修法是加「已知空名单哨兵句」，而不是「没冒号就算空名单」。** 后者会把**命令前缀
写错**误报成「白名单是空的」—— 实测 Bingo 对 `nosuchcmd list` 回的是
`Unknown or incomplete command. See below for errornosuchcmd list<--[HERE]`，
**同样没有冒号**。而这恰好是 P9 换 `globalwhitelist` 时最容易踩的时刻。

哨兵是**整句比对**（去颜色码、压空白、忽略大小写），只认服务端语言文件里那句固定
文案；匹配不上时行为完全不变（照旧 `None`、照旧如实报「未能解析」）。所以这个改动
**只可能把被误伤的正经回执救回来，不会让任何现有判定退化**，代价是换语言的服要照着
`--whitelist` 打出的原文往常量表里补一句。这类「宁可少认、绝不猜错」的取舍，和
`--list-targets` 里那些「认不出来就报出来」的地方是同一条原则。

**顺带修掉了同一现场暴露的另一处谎话。** `AdminResult.ok` 是三态，
`None` 表示「未能验证」——但它有两个来源，含义正相反：

| 来源 | 命令发没发 | 该说什么 |
|---|---|---|
| **前置读**就读不懂，提前 return（上面那条路径） | **一条都没发** | 「命令没有发出去」 |
| **反查**读不懂（改动已经下发） | 发了 | 「命令已发送（未能验证）」 |

原来的文案只有后者，于是「没发出去」被报成「命令已发送（未能验证）」—— 群友会拿着
这句话去服务端找一个**根本不存在的改动**。这也是「把没发生的事报成发生了」，
只是它塌在文案上而不是返回值上：`AdminResult` 的 docstring 专门写了「`None` 不能塌缩
成 `False`」，但文案层悄悄把它们合成了一句。

修法是给 `AdminResult` 加一个 `mutation_sent` 字段（**变更命令**是否真的下发过），
文案与日志各按它分叉。块 18 用**假的 `rcon_command`** 把三条路都跑了一遍，钉的是
行为（发了几条、发的是什么）而不是源码文本 —— 块 16 那条源码检查只保证「前缀没写死」，
保证不了「没发就不许说发了」。

### 9.11 P7：推送按群关联分桶

**要解决的真实症状**：测试群关联了 `gtnh` / `bingo` / `backstabbed` 三台，但进服提醒
只盯 `gtnh` —— 另外两台有人进服群里没反应，而**日志里一切正常**。定时播报同理，只报一台。
根因是 `mc_reporter.py` 的 8 个模块级状态变量是扁平的，`_primary()` 只取「`.env` 里第一个
群所属那条关联的主服」，一个群只能盯一台服。

**四个设计决策**（都写进了配置文件注释，改之前先读）：

1. **关联里只放开关，不放节奏。** `watch` / `report` 两个严格 TOML 布尔（缺省 `false`），
   含义是「本条的 groups 收进服提醒 / 收定时播报」，推送目标**恒为本条的 `groups`**。
   间隔类参数（`MC_WATCH_INTERVAL_SEC` / `MC_JOIN_MIN_INTERVAL_SEC` /
   `MC_NOTIFY_SERVER_STATE` / `MC_REPORT_INTERVAL_MIN`）继续留在 `.env` —— 那是整台机器人
   的节奏，抄进 N 条关联只会变成「A 群改了 B 群没改」，而且看不出另外几条是什么值。
2. **合成一条消息。** 一条关联挂 3 台服时，一个周期内该关联的事件合成一条发进它全部的群。
3. **只在同一 `group` 内算「换服」，且要推。** 跨 group 移动拆成普通的加入 + 普通的离开，
   而离开不推 —— 所以群里只看到 `🎮 小明 加入了 Bingo`。理由：`group` 的含义就是「无缝相连
   的一组服」，同组内那一次是真的没下线；跨组时玩家确实断开重连了，说「从 GTNH 过来」是**猜**
   （他可能先退了 GTNH、去吃了饭、再进 Bingo），而本项目的规矩是「解析不唯一时绝不猜」。
4. **`watch = true` 配 `targets = []` → 只给警告，能启动。** 这不是「被忽略的键」：机器人读懂了
   它，并明确说出它为什么没有输出；硬报错会让整台 bot 起不来，代价与收益不成比例。

**默认关是刻意的。** 「被提到 = 开通查询」这条不变量**不能被推流继承**：新加一条关联就悄悄
开始每小时往那个群发消息，是本工程最不想有的静默副作用。（查询是「群友拉」，推送是
「机器人自己说话」，两件事的风险不一样。）代价是两个开关都没开的群「查询一切正常、却什么
都收不到」，而它的表现（群里安安静静）和「机器人挂了」一模一样 —— 所以 `Audience` 加了
`flags_summary`，启动日志和 `--list-audiences` 每个群都打一行 `推送 …`。

**新模块 `_shared/mcdelta.py`：`reconcile(prev, snaps, targets) -> Delta`。** 纯函数，
不碰网络、不读配置、不看时钟。三条最容易被写错的规则各有专门自测用例：

| 规则 | 写错的后果 |
|---|---|
| **不可用的目标本轮不参与对账**（`not reachable` / `not names_complete`），基线**原样冻结** | 探测抖一轮就把整服报成退服、下一轮再整批报进服，真事件被这堆假的埋掉 |
| **`prev` 里没有某个 target_id 的键 = 那台服还没建过基线**，只建基线、零事件 | 把存量玩家报成「刚进服」。注意**空集 ≠ 没有基线**：`{gtnh: frozenset()}` 是「确认它现在没人」 |
| **来源必须唯一**才能报换服 | 同一名字上轮同时出现在同组两台服上时，猜错来源 |

「不可用目标不参与」这条顺带让**代理自动落进这一类**（`names_complete` 对
`kind="proxy"` 恒为假）—— 它既不会被当换服端点，也不会因为「代理名单为空」误伤别人。
「代理不出分服名单、不是故障」那条不变量在这里第一次真正当护栏用，不是新增特例。

`leave` **照常产出**（对账层完整、可自测、将来要开退服提醒不用改这里），是 `format_events`
决定不推它 —— 决策点放在推送层，不在对账层。

**状态分三层，键就是语义**（`mc_reporter.py`）：

| 状态 | 粒度 | 为什么 |
|---|---|---|
| `_TargetState`（`fail_streak` / `down` / `last_state`） | per **目标** | 连通性是**服务器的事实**，不是某个群的事实。放进 audience 桶会让两条关联对同一台服给出**互相矛盾**的结论，而两条都会推给各自的群 |
| `_AudienceState.known`（基线） | per **(关联, 目标)** | 某台服这轮名单不全时只冻结它自己，同一关联里另一台照常对账。新加一台服只有它没键，于是只建基线 —— 不会把那台服上所有人报成刚进服 |
| `_AudienceState.pending` / `last_push` | per 关联 | 消息按关联合成，A 的推送节奏不该被 B 的进服带偏。全局一份时 B 的人进服会占掉 A 的窗口，表现是「A 群的提醒时快时慢，而且看不出规律」 |

**`_audiences` 的键用 `groups[0]`，不用关联名。** 群号唯一是**已有的硬校验**；关联名在本期
之前没有唯一性校验，两条都叫「社团群」的关联会静默共用一份基线，症状是两边都在乱报进服且
完全不报错。（本期顺手补上了重名校验，但状态的键不依赖它 —— 键应当建在已经被强制唯一的
东西上，而不是建在「我们刚刚也想起来要校验它」的东西上。）

**`_initialized` 整个删掉**：「`known` 里有没有那个键」就是它。少一个能和字典不一致的状态。

**一轮只探一次，再扇出给各关联。** `get_snapshot(max_age=0)` 每次调用都强制真探，按关联
逐个探会从「并发取 max」退化成「并发 + 串行叠加」，一轮耗时上界模型当场失效，
同一台服的 `_log_state_transition` 也会按关联数重复打印。所以先 `_dedupe` 出去重全集探一遍，
再把快照扇给各关联。

**顺带消失的失败模式（决策 1 的最大收益）**：旧版 `.env` 里指一个没写进 `mcs_audiences.toml`
的群 → 循环停摆并打一条 error。参数搬进关联之后这种配置**在结构上不可能存在**。
这比「少一个配置变量」重要得多。

**文案：一条规则统管两种形态。**

> 整条消息**只有 1 个事件、且该事件属于 {进服, 掉线, 恢复}** 时，用旧文案原样输出；
> 其余一律用 `【服名】` 块 + 两空格缩进行的合成排版。

这条规则的价值：DEPLOY / README 里引用的三句文案（`🎮 X 加入了 Bingo（当前 3 人在线）`、
`⚠️ Bingo 连不上了（已连续 2 轮探测失败）`、`✅ Bingo 已恢复，当前 5 人在线`）**一字不改地
继续成立**，而它们是 99% 的情况。`switch` 是本期的全新事物，没有旧文案，一律走合成排版，
块头用 `【服名】` 与 `mcrender._summary_block` 一致 —— 同一个群看的 `@mc` 就长这样，
推送不该另起一套排版。

**定时播报的核心行为变更**：某台服无人在线或不可达**不再导致整条播报跳过** —— 3 台服的播报里
有 1 台没人，不代表这次播报没意义。整条跳过只剩两种，且**日志里的跳过原因必须分开说**
（都写成「静默跳过」就没法判断是没人还是全挂了）。判定「有没有人在线」用 `any(count)` 而
**不是**求和：代理的 `count` 是全群组总人数，只挂一台代理的关联会被求和判成「无人在线」——
明明有人。

**渲染层搬进 `_shared/mcrender.py`（`render_report()`）。** 这不只是挪位置：
`mcrender` 的 docstring 早就写着「`@mc` 与进服提醒/定时播报共用同一份格式」，
**而 `mc_reporter.py` 根本没 import 它**，两套排版各写各的（一句写在文档里的谎话）。
接上之后同一个服的块在两个地方逐字相同，自测钉住了这条。

**「超长被截断」必须在调用方 `logger.warning`，不能在渲染层里打日志。** 渲染层在 `_shared/`，
那条「不 import nonebot」的硬约束（`tools/mc_check.py` 要在 `init()` 之前 import 它们）
意味着它**不能 log**。所以 `format_events` / `render_report` 把「被截断了」**返回**给调用方。
截掉的正是最后几台服的事件，而消息看着完全正常 —— 这条 warning 是唯一能发现它的地方。
`render_summary` 与 `render_report` 共用一个 `_fit(build, budget)`，那条「不能从头截断」的
性质因此不可能只在一处生效。

**迁移顺序（本期唯一一个「配错不报错」的坑）**：先给关联加 `watch = true` / `report = true`、
重启、确认推送照旧，**再**删 `.env` 的两行。反过来做会让提醒静默消失，而群里安安静静和
「机器人挂了」长得一模一样。
