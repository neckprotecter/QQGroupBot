# Oopz 群统计机器人

QQ 群机器人：群成员 @ 机器人发「**统计**」，实时查询 Oopz 语音频道在线成员并回复。

提供**两套接入**：

- **NapCat 版（推荐）**：`bot_napcat.py`，走个人 QQ 号 + [NapCat](https://github.com/NapNeko/NapCatQQ)（OneBot v11），**可主动推送**，不受官方限制，含定时播报 / 进频道欢迎。
- **QQ 官方版（备选/回退）**：`bot.py`，官方机器人仅被动响应（2025-04-21 起官方停用主动推送，发起即报 `40034105`）。

> 🚀 **从零部署**（全新 Windows 环境搭建）：见 [DEPLOY.md](DEPLOY.md)。

## 功能

| 群内 @ | 回复 |
|--------|------|
| `@机器人 你好` | 「收到！被动回复链路已打通 🎉」——链路自检 |
| `@机器人 统计` | 📊 Oopz 语音频道在线成员报告（分域/频道 + 昵称） |

**自动功能（NapCat 版，配置见 `.env`）**

- ⏰ **定时播报**：整点对齐推送（间隔 30 分钟则在 :00/:30，间隔 60 则每小时整点），向 `NAPCAT_REPORT_GROUP` 推送独立格式的「📣 Oopz 语音频道播报」——**仅当 Oopz 有人在线时**，无人则静默跳过
- 👋 **进频道欢迎**：每 `NAPCAT_WELCOME_INTERVAL_SEC` 秒轮询，检测到有人进入目标域语音频道时推送趣味欢迎语（随机文案）

示例回复：

```
📊 Oopz 语音频道在线：4 人

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
├── .env                      # 全部凭证（勿提交公开仓库）
├── .env.example              # 配置模板 → 复制为 .env
├── bot.py                    # 启动入口①：QQ 官方版（被动响应）
├── bot_napcat.py             # 启动入口②：NapCat/OneBot 版（可主动推送）
├── requirements.txt          # Python 依赖清单
├── vendor/Oopzbot-SDK/       # oopz_sdk 源码（不在 PyPI，随工程分发）
├── tools/
│   ├── oopz_login.py         # 手机号+密码 → 写入 OOPZ_* 凭据
│   └── oopz_check.py         # 独立验证 Oopz 查询链路
├── plugins/                  # QQ 官方版插件（bot.py 加载）
│   ├── hello.py              # @你好 → 链路自检
│   └── oopz_stats.py         # @统计 → 实时成员报告
├── plugins_napcat/           # NapCat 版插件（bot_napcat.py 加载，逻辑与上面镜像）
│   ├── hello.py
│   ├── oopz_stats.py         # @统计
│   └── auto_reporter.py      # 定时播报 + 进频道欢迎
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

**3. 首次登录 Oopz**（交互式，终端里输入，密码不进聊天记录）

```powershell
$env:OOPZ_LOGIN_PHONE = "你的Oopz手机号"
$env:OOPZ_LOGIN_PASSWORD = "你的Oopz密码"
.venv\Scripts\python.exe tools\oopz_login.py
```

> 脚本会把 OOPZ_* 四要素自动写进 `.env`。Linux 用 `OOPZ_LOGIN_PHONE=xxx OOPZ_LOGIN_PASSWORD=xxx python tools/oopz_login.py`。

**4. 验证 Oopz 查询**（不启动机器人，直接查一次）

```powershell
.venv\Scripts\python.exe tools\oopz_check.py
```

**5. 启动机器人（QQ 官方版）**

```powershell
.venv\Scripts\python.exe bot.py
```

看到 `Bot <机器人AppID> connected` 即成功。

**6. 改用 NapCat 版（可选，可主动推送）**

安装 [NapCat](https://github.com/NapNeko/NapCatQQ) / 登录 / 开正向 WebSocket / 配 token 的完整步骤见 [DEPLOY.md 第 2 节](DEPLOY.md#2-安装-napcat协议端)。`ONEBOT_V11_WS_URLS` / `ONEBOT_V11_ACCESS_TOKEN` 配好后启动：

```powershell
.venv\Scripts\python.exe bot_napcat.py
```

看到日志出现 `Bot <QQ号> connected` 即成功（NapCat 未运行时日志会持续报连不上 3001，属正常）。

## 群内使用

在 QQ 群 @ 机器人发送 `统计` 或 `你好` 即可。仅响应群内 @，私聊不回复。

> NapCat 版默认**所有群**都能触发查询；想限定群，在 `.env` 设 `NAPCAT_ALLOWED_GROUPS`（逗号分隔群号，留空 = 不限制）。官方版对应 `QQ_ALLOWED_GROUPS`（group_openid 列表）。

## 自定义回复格式

回复文本由 `_build_stats_message()` 拼装。注意有两个镜像副本：QQ 版在 [plugins/oopz_stats.py](plugins/oopz_stats.py)、NapCat 版在 [plugins_napcat/oopz_stats.py](plugins_napcat/oopz_stats.py)，**改格式需同步两份**（下方代码以 QQ 版行号为准）。

**1. 回复文案 / 排版**（[plugins/oopz_stats.py:160-167](plugins/oopz_stats.py#L160-L167)）

```python
lines = [f"📊 Oopz 语音频道在线：{total_online} 人"]   # 第一行：总人数
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

**2. 触发词**（[plugins/oopz_stats.py:181](plugins/oopz_stats.py#L181)）：`if text != "统计":` 改成你想要的词，如 `if text != "在线":`。

**3. 消息长度上限**（[plugins/oopz_stats.py:24](plugins/oopz_stats.py#L24)）：`_MAX_LEN = 1800`，QQ 群文本约 2000 上限，超长自动截断加 `…`。

**4. 统计范围**（[.env](.env)）：`OOPZ_TARGET_AREAS=<你的域名>`——逗号分隔可加多个域（area_id 或域名），删除该行则统计全部已加入的域。

改完**重启 `bot.py`** 生效。

## 维护与常见问题

完整排查清单见 [DEPLOY.md 常见问题](DEPLOY.md#8-常见问题已知坑)（F1~F7）与 [日常维护](DEPLOY.md#9-日常维护)。高频三条：

- **统计 / 播报突然失效**：多半是 `OOPZ_JWT_TOKEN` 过期（约 31 天），重跑 `tools/oopz_login.py` → 回填 `.env` → 重启，见 DEPLOY.md 第 8 节 F7。
- **部分域不出现在统计里**：如「Voxel Passion 像素乐园」频道接口返回 `channels: null`，SDK 解析失败被跳过，不影响其他域（原因见 docs/chat.md §5.5）。
- **主动推送（仅 NapCat 版）**：官方版无主动推送能力；定时播报 / 进频道欢迎只在 NapCat 版生效，文案在 `plugins_napcat/auto_reporter.py`（顶部 `_WELCOME_TEMPLATES` 与 `_build_broadcast_message()`）。

## 详细文档

技术方案、Oopz API 对照、SDK 三个已知坑 → 见 [docs/chat.md](docs/chat.md)。
