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

- **⚠️ 语音成员表会多报人（幽灵记录），而且 REST 侧无解 —— 2026-09-26 查到、**没修**，
  别再"顺手修"一次。** `membersByChannels` 会把**已经离开**的人继续算在频道里，直到
  服务端某个超时才清理。实测（域「SaniWang的奇妙小房间」的「游戏开黑」）：机器人报 3 人，
  **客户端里只有 2 人**（老罗不划水 + SaniWang），多的那个是 VulCaN9 —— 而且用户复现了
  三次：在频道**外面**的列表里看得见他，**进**频道就没有，第三次他的名字才从外面那个列表
  里也消失。

  **重要：别拿 `person.online` 当判据去过滤 —— 实测它正好是反的。** 同一时刻：

  | | `person.online` | 实际 |
  |---|---|---|
  | 老罗不划水 `959bef…` | **false** | **真在里面** |
  | VulCaN9 `2e56eb…` | **true** | **幽灵** |
  | SaniWang `b264c4…` | true | 真在里面 |

  照 `online` 摘人会把**真在的人摘掉、把幽灵留下**（人数对了、名单全错），比多报一个更糟。
  这条是 2026-09-26 差点犯的错：当时只看接口自己的说法（`online=false` + 域成员表里不在册），
  推断「老罗不划水是幽灵」，理由看着很硬 —— **是用户打开客户端数了一遍才翻过来的**。
  教训：**涉及"谁在场"的结论，接口的字段只是线索，得有人对着客户端核一遍**。

  - 域成员表（`get_area_members`）也**不能**当第二判据：那张表按分页取，49 条就到头了，
    "某 uid 不在前 49 条里"什么都不能说明。
  - 客户端「进频道」那一步能拿到准确的名单，但它走的是 RTC（`enter_channel` 返回 Agora
    sign），**调用它等于让机器人真的进语音频道**，不能拿来查询。
  - 真要做准，只剩**连 WebSocket** 收实时成员事件这一条路（本工程刻意只走 REST，
    那条路要另起一套常驻连接，没验证过是否也吃到同一份陈旧服务端状态）。
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
whitelist = [{ target = "bingo" }]                     # 一组表，每台白名单服一项（见 9.12）
```

### 9.3 复用了 ServerBook，而不是新造一套视图

`ServerBook`（目标表 + 服名解析 + 白名单归属 + `targets_primary_first`）**一字未改**，
只是多了一个 `scoped()`：

```python
# P6 起：whitelist 是**一组路由**（每台白名单服一条，各带自己的命令前缀）
book.scoped(ids, primary=..., whitelist=[WhitelistRoute(...), ...]) -> ServerBook
```

投影出的子 book 里**只有本关联的目标**，所以：

- 关联外的服名 `resolve()` 不出结果 —— 「建筑群打 `gtnh` 回『没有叫 gtnh 的服』」
  是投影本身的性质，**不靠调用方自觉过滤**。隔离因此不可能被某条分支忘掉。
- `primary_target` / `targets_primary_first` / `pick_whitelist` 全部原样复用，零重复代码。
  （`whitelist_owner` 这个单数属性在 P6 被**删掉**了，理由见 9.12。）

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
| 没被任何 `[[audience]]` 提到 | 🤔 本群还没有开通此功能。其它功能可以发「@机器人 你好」看看。 |
| 提到了，但 `targets = []` | 🏗️ 本群关联的服务器还没接入，暂时没有可查询的内容。 |
| 本群那条没写 `whitelist` | ⚠️ 本群没有指定白名单服，白名单管理不可用。 |
| 写了白名单服但没配 RCON 密码 | ⚠️ 白名单服 Bingo 没配 RCON 密码，命令发不出去。 |

**群内文案一律说「本群」，不带关联名** —— 群友不知道配置文件里那些名字是什么。
关联名只出现在运维看的启动日志与 `--list-audiences` 里。

> **补记（2026-09-25 用户要求）**：第一条原来是「本群还没有开通 **MC 查询**」，
> 而 `NO_AUDIENCE` 这句是 `mc_stats`（查询）和 `mc_admin`（白名单管理）**共用**的 ——
> 在没开通的群里打 `whitelist add`，回一句「还没有开通 MC 查询」是把功能说串了。
> 改成中性的「本群还没有开通**此功能**」，两条路径下都不会有歧义。

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
本工程一直在防的就是这类东西，所以宁可现在接上（当时 `command` 就是 `whitelist`，
所以行为零变化），也不要带着一个会撒谎的键进 P6。**P6 证明这个提前做是对的**：
白名单从「一个目标」变成「一组路由」时，`command` 只是个形参，一行没改就变成了逐台前缀。

钉住它的自测只能读源码（`run_whitelist_command` 要活的 RCON，跑不起来）：
断言那个函数体内不再出现 `"whitelist ` 字面量、且签名里有 `command` 参数。
P6 补了另一半：**三条**命令（前置读 / 变更 / 反查）都得用这个前缀，所以新增用例断言
`command="globalwhitelist"` 时发出去的三条全是 `globalwhitelist ...` —— 只看变更那一条的话，
「前置读硬编码 whitelist list」这种改法照样能过，而那会让配了 Global Whitelist 的那台
每次都读不懂名单、命令一条都发不出去。回复文案那一半同理由源码钉住
（`_build_body` 体内没有 `"whitelist ` 字面量），否则命令发对了、提示里给的却是
服务端不存在的命令。

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
   `MC_NOTIFY_SERVER_STATE` / `MC_REPORT_INTERVAL_MIN` / `MC_QUIET_HOURS`）继续留在 `.env`
   —— 那是整台机器人的节奏，抄进 N 条关联只会变成「A 群改了 B 群没改」，而且看不出
   另外几条是什么值。
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

> **补记（2026-09-25 用户要求，上面那条规则已被取代）**：`【服名】` 块头、消息标题
> `🎮 MC 动态 · HH:MM`、以及「单事件走单条模板」的分支全部去掉，改成**一段一台服、
> 行内自带服名、段间一条虚线**。起因是群里连收三条「换服过来（当前 1 人在线）」刷屏；
> 行内带服名之后两套排版自然合成一套（块头没了，进服行/状态行再没别处能说清哪台服），
> 所以 `_solo_text` 整个删掉了。
>
> 同一天的第二轮把**数字也清干净了**：动态消息一个数字都不留 —— 进服行 `🎮 X 加入了
> 「Bingo」`（服名加「」、去掉「（当前 N 人在线）」）、掉线 `⚠️ Bingo 连不上了`（去掉
> 「已连续 N 轮探测失败」）、恢复 `✅ Bingo 已恢复`。人数只在**定时播报**和 `@mc` 查询里
> 出现：那两处回答「现在几个人可以一起玩」，进服提醒回答的是「谁来了」。所以上面引用的
> 那三句旧文案这次**逐字都变了** —— 但播报/总览那两处的 `【服名】在线 N 人` 一字未动，
> 它们才是「同一个服在两个地方不能显示成两种样子」那条承诺覆盖的地方。



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

### 9.12 P6：一条群关联管多台服的白名单

**要解决的真实症状**：白名单归属原本是 `whitelist.target` + `whitelist.command` 两个标量
——一个群只能管**一台**服，且那台用哪个 RCON 前缀写死在同一个表里。代理上线后必然要
「子服一份 vanilla 白名单 + 代理一份 Global Whitelist（前缀 `globalwhitelist`）」，
这个形态**写不出来**。

**四个设计决策**（改之前先读）：

1. **服名写在末尾，一次只打一台。** `whitelist add Steve bingo`：动词之后的 token 里
   **最后一个**当服名、其余 join 起来当玩家名。这个切法**无歧义**，因为玩家名正则
   `^[A-Za-z0-9_]{1,16}$` 不允许空格 —— 两个以上 token 时第一个一定是玩家名、最后一个
   一定是服名，不需要「猜哪个像服名」。反过来（服名放第一个）在单 token 时无法自解释。
   只有一台白名单服时**可以省服名**，这是绝大多数情况（逼人写反而容易写错）。
   改动命令（`add` / `remove`）一律只打点名的那一台：一条命令同时改多台 = 一次误操作
   同时改坏几台服的白名单，收益与风险不成比例。

2. **配置只认数组，旧的单表写法直接报错。** `whitelist = [{ target = "bingo" }, ...]`，
   `command` 可省（缺省 `whitelist`）。报错里带可照抄的改法 —— 沿用当初 `[whitelist]`
   段搬家那套：一件事只留一种写法，另一种明确报错，**不做兼容**。

3. **`whitelist_owner` 删掉，不留兼容的单数属性。** 写成「返回第一条」会让「配了 3 台、
   实际只动 1 台」静默发生；写成「只有 1 条时才返回」会对着配了 2 台的群**撒谎**说
   「本群没有指定白名单服」。删掉之后漏改的调用点直接 `AttributeError`，比静默少查一台
   强得多。

4. **渲染层不许 import `mcadmin`。** 多台 `list` 的渲染要吃 `WhitelistListRow` 这个
   **纯数据**行类型，而不是 `AdminResult` —— 因为 `_shared/mcadmin.py` 顶层
   `from nonebot.log import logger`，`mcrender` import 它就把 nonebot 拖进那条必须在
   `nonebot.init()` 之前跑通的路（`tools/mc_check.py --self-test`）。

**顺带修掉两个真 bug**（都是「静默做错事」，本工程明令禁止的那一类）：

| bug | 旧行为 | 现在 |
|---|---|---|
| `whitelist bingo add Steve` 里的 `bingo` **被静默丢掉**，命令照发往缺省那台 | 点名了 A 服，改了 B 服 | `parse_command` 把动词前的 token 原样交给调用方（`pre_verb`），`mc_admin` 里逐个拿去 `resolve()`，凡是**本群认得出的服名**就拦住并给出改好的整条命令 |
| `_build_message` 把建议命令**写死**成 `whitelist {verb} {name}` | 配了 `globalwhitelist` 的关联，回复里给的是服务端**不存在**的命令 | 前缀由调用方传 `route.command`；自测用源码钉住「`_build_body` 体内没有 `"whitelist ` 字面量」 |

第一条的判据刻意是「这个名字在本群**解析得出来**」而不是「它是不是服名」：所以
`帮我 whitelist add Steve` 里的「帮我」不是任何一台服的名字，**不会误伤**；而这条检查
只会**多报错**、永远不会**做错事**。已知边角（接受，写进了注释）：服名恰好叫触发词本身
时每条命令都会报错 —— `mcs_servers.toml.example` 末尾本来就禁止用触发词当服名。
（因此**没有**用 `triggers.strip_keyword`：触发词留在动词之前，末尾式语法不需要它。）

**`pick_whitelist(query)` 的两条规则，顺序不能反：**

1. `query` 为空：0 条 → `NO_ROUTE`；恰好 1 条 → 命中（单台可省服名）；**≥2 条 → `NEED_NAME`**。
   **没有「默认那台」的回退** —— 多台时猜错 = 改错服务器的白名单。
2. `query` 非空：先在**本条关联的全部目标**里跑现成的 `resolve()`（NFKC + 唯一前缀 +
   歧义三态），**再**查它是不是白名单路由。必须先在全部目标里解析：`gtnh` 是本群查得到
   的服、只是没配白名单，直接回「没有叫 gtnh 的服」是**假话**，照着改名字是白费功夫
   → 回 `PICK_NOT_WHITELIST` 并带上 `found`，文案说「GTNH 是本群关联的服，但它不是白名单服」。

**多台 `list` 的并发**：`asyncio.gather(..., return_exceptions=True)` 是**硬要求**。
不写的话一台 `RconAuthError` 会把整批结果连坐丢掉，群友收到「命令执行失败」而那几台其实
查到了 —— 是「把没发生的事报成发生了」的镜像，同样不许。例外项也**照样转成一行**，
保证一台都不静默省略。RCON 锁是 per target 的，所以是真并发：整批 ≈ `max(rcon_timeout)`，
不是求和。单台的路径**完全不经过**这里（走的是与 P6 之前逐字相同的那条），只有 ≥2 台才分块。

**渲染**复用 `name_budget()` / `_fit()`：3 台 × 50 个名字正好会踩 `MAX_LEN`，而
`truncate()` 切的是尾部 —— 切掉的正是最后几台的**名单**，消息看着完全正常。所以
`render_whitelist_list` 把「被截断了」**返回**给调用方去打 warning（渲染层不能记日志，
与 `format_events` / `render_report` 同一约定）。注意 `clipped` **不能**用 `_fit` 的返回值
长度去判：`_fit` 认输时返回的是 `truncate(build(0))`，长度**反而正常**，照长度判断会
一律报「没截断」。

**另一个权限盲区**：`MC_ADMIN_QQ` 鉴的是「**人**」、不分服，机制没变，但它的**作用域被
配置放大了** —— 以前一个群只能动一台，现在 `whitelist` 数组里每多一项，那个号能改的
白名单就多一份。这是**静默的权限扩大**（命令、日志、`--list-audiences` 都只会说「发往 N 台」），
所以 `mcs_audiences.toml.example` 的坑 5、DEPLOY 4.2 第 5 条和 README 都专门写了一段。

**迁移（必须做，否则 bot 起不来）**：真配置里那行单表写法要改成数组。
`parse_audiences` 对旧形态**直接报错**，`ServerConfigError` 会一路冒到 `main`。

### 9.13 P9：接入对方群组服的 HTTP 接口（第三条取数通道）

**要解决的真实症状**：SZUcraft 那套群组服（Velocity + 六个子服）**既给不了 SLP 也给不了
RCON** —— 子服只在 docker 网络 `mcnet` 里，游戏端口没发布；代理那台只有一个内网 HTTP 口。
对方装了自己的 Velocity 插件 `szucraft-bridge`（四个端点与认证写法记在 `DEPLOY.md` 4.1；
对方那几份原始文档 `对接/` 2026-09-22 已按用户要求删除，那里面只有一处我们没别处记的
东西 —— 接口还认 `X-Auth-Token` 和 `?token=`，已补进 DEPLOY.md）。
所以有了**第三条通道**：一次 `GET /status` 就带每个子服的人数与名单，`/whitelist` 增删查
白名单（代理层 `PreLoginEvent` 拦截，不经 RCON、不要游戏内权限）。

**八个设计决策**（改之前先读）：

1. **同源合并：一份 `/status` 喂多台目标。** 子服写 `source = "<代理的 id>"`，
   `fetch_snapshots` 按 hub 分组、**每台 hub 一个 `asyncio.ensure_future` 任务**，
   子协程 `await` 同一份结果 —— N 台子服只发 1 次 HTTP。子服自己**一个包都不发**
   （除非它另填了 `host`，见第 4 条）。
2. **`ServerTarget.hub` 在解析期回填「那台目标本身」**（不是 id），并且**要同步换进
   `by_id`** —— `ServerBook.get/scoped` 查的就是它。这样取数层能在**任意子集**里工作：
   `fetch_snapshots([bingo])`（缓存里只有子服过期了、或者某个群只关联了子服）也知道
   去问谁的接口。原先打算「关联里写了 source 却没写 hub 就报配置错」，**放弃** ——
   只关联子服的群是合法的，那不是配置错误，是取数层的活儿。
3. **两条通道互斥，都在解析期报错。** `api`×`rcon`（都能取名单、都能改白名单，同时
   存在就没法说清走哪条）、`api`×`source`（都是「数据从哪来」）、以及本轮**新加的**
   `source`×`rcon`。最后这条最隐蔽：两条路**各自都自洽**，只是各说各话 —— 在线名单
   来自 hub 的接口，而白名单命令走本机 RCON，于是「机器人说已添加，人还是进不去」
   （代理层的白名单才是进服校验那一道）。取数层自己发现不了，只能解析期拦。
4. **接口型目标的 `host`/`port` 是可选的，填了有额外价值。** 同源合并**丢掉了独立
   交叉校验**（人数和名单来自同一份 JSON，以前是 SLP 人数 × RCON 名单互相对账）。
   补法是：填了 `host` 就并发做一次 SLP，对不上时**按 SLP 报**并明说「接口那份数据可疑」，
   所以 `McSnapshot` 多了 `count_source`（`slp` / `api`）和 `names_source`
   （`rcon` / `slp-sample` / `api` / `none`）。SLP 失败不算故障，只说一句「复查没做成」。
5. **失败性质复用 `SLP_UNREACHABLE` / `SLP_UNPARSEABLE`，只加一个 `API_AUTH`。**
   那两个常量其实是一套**性质**分类（连不上 vs 连上了但内容不对）而不只是 SLP 的，
   接口的失败正好落在上面；唯独 401 两边都套不上 —— 它的下一步动作是**去要新 token**，
   报成「连不上了」会把人赶去查防火墙和隧道，方向全错。

   **hub 取不到时退回 `fetch_snapshot(hub)`（纯 SLP）**，子服如实报「数据来源 X 的接口
   这次没取到」—— 一个 token 过期不该让整个群组看起来掉线（那会触发一轮假的掉线提醒）。

6. **接口失败要落到目标上，不能只落在 hub 上。** `_api_snapshot` 里对非 proxy 目标会
   把「名单不完整」那半句丢掉（代理没有分服名单不是故障），但**人数对不上那半句必须留**
   —— 自测 ③ 一开始就是被这里吞掉的（`接口 2 人 / SLP 7 人` 报成了「来源 slp，无备注」）。
   总览那一行（全群组人数）恰恰是唯一能看到对不上的地方。

7. **白名单层做成「通道无关」。** `run_whitelist_command` 的「先读后写 + 反查」逻辑两条
   通道**完全一样**，所以错误族搬到了 `_shared/mcadmin.py`：`WhitelistError` 三个子类
   （`Unreachable` / `AuthError` / `DataError`）+ `_translate()`，`RconAuthError` 和
   `BridgeAuthError` 都翻成「凭证不对」。调用方只看这个族，文案里说「RCON」还是「接口」
   由 `route.channel` 决定（`WhitelistRoute.channel`，与给诊断用的 `transport` 分开：
   前者进群文案要最短，后者要写全来源）。

   **接口那条通道多一条铁律：不信回执，只信反查。** 对方的 `ok` 在不同版本里含义不同
   （老版把「已经在了」的幂等 add 报成 `ok=false`），所以判定成败一律看**重新读一遍名单**
   —— 与 RCON 那条完全一致。自测里「回执 ok=true 但反查没命中 → 报未生效」就是钉这个。

8. **`enabled=false` 必须说出来。** 对方可以把代理层的白名单拦截整体关掉（接口照常工作、
   名单照常增删，但**谁都能进服**）。这是本工程最忌讳的「配了却不生效」那一类，所以
   `--whitelist` 把这种目标单列出来并**退出码 1**，`--api` 也照打。

**顺带两处**：`round_budget` 从 3 元组改成 4 元组（`slp, rcon, api, budget`）——
打印出来的算式必须**加得起来**（`SLP 5s + RCON 5s ×2 + 接口 0s = 最坏 15s`），早先少了
接口那一项，一配接口型目标小计就和算式对不上，看着像算错。`--api` 是新增的**独立动作者**
（不是 `--live` 的开关）：只打 `/health`、`/status`（逐子服一行 + 「接口里有、我们没挂」
的那几台）、`/whitelist` 的原文，一个 SLP 包都不发。

**自测**（`--self-test`，离线、打桩 `_slp_probe` / `fetch_status` / `fetch_whitelist`）：
块 19 有 9 条接口取数 + 7 条接口白名单 + 2 条 `--whitelist` 接口台，另有 4 条 `--api`
的 argv 用例和 1 条 `source`×`rcon` 拒绝用例。全部走假接口，不联网。

**运维上的两条已知限制**：

- **隧道是临时的。** 对方把 8080 只发到它自己主机的 `127.0.0.1`，所以机器人这边必须
  `ssh -L 8080:127.0.0.1:8080` 转过来（`-p 222`，账号 `mcbot` 只能转发、shell 是 nologin）。
  它不是常驻服务：机器重启或网络抖动之后要**重新拉**，症状是「接口连不上」（**不是 401**
  —— 401 是 token 的事）。机器人将来做成容器加进 `mcnet` 后直接用 `http://velocity:8080`，
  这条隧道和两个回环口都能撤。
- **token 是密码**，和 RCON 密码一起躺在 `mcs_servers.toml`（已 gitignore）。
  对方那几份原始文档（`对接/`）2026-09-22 已删 —— 它们**没进过 `.gitignore`**，当初
  是靠「提交时手动排除」挡着的，这也是删掉它们的一个理由：那种保护全靠人记得。

**真配置（2026-09-22 已写完并验过）**：`szu`（`kind = "proxy"` + `api`，另填 `host`/`port`
做 SLP 复查）、`bingo` / `backstabbed` / `survival`（生电）/ `creative`（创造）/
`lobby`（大厅）五台 `source = "szu"`，`source_key` 用对方 `velocity.toml` 里的服名
（`/status` 实测有 `backstabbed`、`bingo`、`creative`、`limbo`、`lobby`、`survival`）。
白名单那一项**指向 `szu`**，不再指向子服。

`lobby` 是**用户后来单独点名要挂的**，挂之前先跟他说清了代价：玩家从代理进来先落在
lobby，于是进服提醒会先来一条「加入 大厅」、再在 `/server bingo` 时来一条「换服 Bingo」
（以前只有后一条）。他接受。`limbo` **仍然不挂** —— 那是掉线/切换的中转，不是给人玩的。

> **补记（2026-09-25，`transit` 字段）**：「不挂 limbo」留下了一个尾巴 —— 它的人照样
> 算在接口报的「全群组人数」里。用户重启后看到的那一幕：`【群组服】全群组 2 人`，
> 下面 `【大厅】在线 1`、其余全 0。差额是当时卡在 limbo 里的那个玩家（反复采样 7 分钟
> 没动过，不是「正好探到一瞬」）。
>
> 这个数字**没错**，但口径不对：`proxy.online` 回答的是「连着代理的有几个」，群里想知道
> 的是「有几个在玩」。于是给代理那条加了 `transit = ["limbo"]`：**先选来源（接口 or SLP
> 复查）、再扣减**（两条分支报的都是含中转服的数，只在接口那条上扣，SLP 一成功就把人
> 带回来了），而「接口 vs SLP 对不上」那句对照用**扣减前的原话** —— 它问的是「对方插件
> 算错了吗」，拿我们自己的口径去修等于把唯一能发现对方算错的地方抹掉。
>
> 三种写错法都在**加载期**报错（写在非代理上 / 代理没配 api / 点名了自己挂着的子服）；
> 唯一拦不住的是**名字与对方 `velocity.toml` 对不上**——那只能运行期发现，表现是
> 「扣不掉」，也就是人数偏大一点点，在群里完全看不出来。所以三处都点名打出来：
> 启动日志、`--list-targets`、`--api`（后者还会逐个对号，并标出 `[中转]`）。
> 顺带记一笔：`ServerBook.describe()` **没有调用方**（`--list-targets` 和启动摘要都
> 自己排版），它的 docstring 说「给 --list-targets 用」是句假话，写这条时一并改成实情。

那条「代理总数 vs 子服之和」的尾注对**接口型代理**是静默的（`mcrender._footnote` 里的
`r.target.api is None`），这正是挂不挂 lobby 都不影响它出不出提示的原因：接口会把**全部**
子服列出来而我们只挂关心的几台，「有人在 lobby」永远让合计对不上。
