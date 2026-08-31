# 从零部署指南（Windows 11）

本项目是 **QQ 群机器人 + oopz 语音频道在线统计**。QQ 侧有两条入口，推荐使用 **NapCat 版**（可主动推送定时播报 / 进频道欢迎）；QQ 官方版（`bot.py`）仅被动回复，且官方已停用主动推送。

本文档按"干净环境从 0 部署"编写。当前工程目录已经过清理，只保留运行所需 + 部署所需文件。

---

## 0. 工程文件清单

```
oopz-bot/
├─ bot.py                    # QQ 官方版入口（被动，可选）
├─ bot_napcat.py             # NapCat / OneBot v11 入口（推荐，含主动推送功能）
├─ plugins/                  # QQ 官方版插件（@统计 等）
├─ plugins_napcat/           # NapCat 版插件：oopz_stats(@统计) / auto_reporter(播报+欢迎)
├─ tools/
│  ├─ oopz_login.py          # 生成 oopz 平台登录凭据（device_id / jwt / 私钥）
│  └─ oopz_check.py          # 诊断脚本
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

- 群里 `@机器人 统计` → 回复 oopz 语音频道在线明细
- 定时播报：到整点槽位（间隔 30 分钟则为 :00/:30），若 oopz 有人在线则推送「📣 oopz 语音频道播报…」（无人在线时静默跳过，属正常）
- 进频道欢迎：有人进入目标域语音频道后，约 1 个轮询周期内推送趣味欢迎语
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

---

## 9. 日常维护

- **改文案**：播报格式在 `plugins_napcat/auto_reporter.py` 的 `_build_broadcast_message()`；欢迎语在文件顶部的 `_WELCOME_TEMPLATES` 列表。
- **续期 oopz JWT（每月一次）**：`OOPZ_JWT_TOKEN` 约 31 天过期。症状：@统计 无回复、播报显示「查询失败」。重跑 `tools\oopz_login.py` → 回填 `.env` → 重启 bot。
- **改间隔/目标群**：改 `.env` 后重启 bot。
- **看日志**：`logs/napcat_bot.log`（注意 PowerShell 用 `-Encoding UTF8` 读）。
- **NapCat 升级/重装**：三个启动坑（F1/F2/F3）会复发，按第 8 节处理。
- **清理旧日志**：`logs/` 下文件可随时删，bot 会自动重建。
