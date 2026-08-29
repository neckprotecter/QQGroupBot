# 项目文档：QQ群机器人 + Oopz 语音频道在线统计

> 技术方案与实现说明。项目为**双入口**：`bot_napcat.py`（NapCat/OneBot v11，个人 QQ 号，**可主动推送**，推荐）+ `bot.py`（QQ 官方版，仅被动响应，作回退）。本文档主体以 QQ 官方版为准，NapCat 版差异见 [§8](#8-napcat-版bot_napcatpy)。
>
> **文档分工**：本文档讲**方案与原理 / 踩坑原因**；从零部署、操作步骤、常见问题见 [DEPLOY.md](../DEPLOY.md)；项目总览、快速上手见 [README.md](../README.md)。

---

## 1. 核心需求与背景决策

在 QQ 群部署机器人，群成员 @ 机器人发「统计」，机器人实时查询 Oopz 语音频道在线成员并回复。

**关键背景（务必先读）**：QQ 官方机器人的**主动推送**（定时播报、进频道欢迎通知）已于 **2025-04-21 官方停止支持**——主动向群发送消息的接口不再可用，发起即报错 `40034105 主动消息失败`。

因此针对 **QQ 官方版**（`bot.py`）：

- ✅ **保留**：@机器人「统计」→ 实时 Oopz 成员报告 → 被动回复（响应 @ 消息携带 `msg_id`，不受主动消息限制）
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

### 5.2 Oopz 查询：只启 REST，别用 bot.start()

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

### 5.4 Oopz 关键 API（以安装版为准，已核对）

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

代码几乎与 QQ 版同构，仅适配器 import 与发送方式不同；Oopz 查询逻辑（`_config_from_env` / `bot.rest.start()` / 频道名映射 / 消息拼装）完全复用，见 [plugins_napcat/oopz_stats.py](plugins_napcat/oopz_stats.py)。

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
