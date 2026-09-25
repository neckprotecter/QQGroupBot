# tools/ —— 诊断与凭据工具

这里放**三个独立脚本**。它们不属于机器人本体（`bot.py` / `bot_napcat.py` 都不会
import 它们，`plugins*` 也不引用），是**人手动跑**的。

> ⚠️ **它们不是「只有测试的时候才用」**。只有 `mc_check.py` 的 `--self-test` /
> `--quiet-test` 两个是测试；
> 另外两个的日常用途是**排查线上问题**和**拿凭据**。三者分工：

| 脚本 | 类别 | 联网 | 会不会改动东西 |
|---|---|---|---|
| `mc_check.py` | MC 取数链路诊断（+ 离线自测 / 静默行为自测） | 看参数 | **只读**，任何时候都不写 |
| `oopz_check.py` | oopz 凭据与查询链路诊断 | 是 | 只读 |
| `oopz_login.py` | 一次性凭据生成（手机号+密码 → `.env`） | 是 | **会写 `.env`**（只写 OOPZ_* 四项） |

> 后两个都**依赖 oopz_sdk**（它跟着 `requirements.txt` **默认装上**，见 DEPLOY.md 第 3 节）。
> 真没装时（当初特意跳过了）它们会直接报 `ModuleNotFoundError: No module named 'oopz_sdk'`
> —— 那是对的，不是坏了：没有 oopz 就没有可查/可登录的东西。补装：
> `pip install ./vendor/Oopzbot-SDK`（在工程根目录）。**`mc_check.py` 完全不依赖它**，
> MC 那半边照常。机器人本身则会在启动日志里说清「未安装 oopz_sdk，oopz 功能不启动，
> MC 不受影响」。

为什么这样跑得起来：三个脚本都自己 `load_dotenv()` 读 `.env`，**和机器人用同一套解析
语义**；`mc_check.py` 还直接 import `plugins_napcat/_shared/` 里的解析与渲染代码。
所以它验的是机器人**真正在用的那份代码**，不是抄一份到测试里的镜像。反过来说，
`_shared/` 里那几个模块**不得** import nonebot —— 否则这些脚本在 `nonebot.init()`
之前 import 就会抛 `ValueError`（那条约束写在模块 docstring 里，别去破）。

**命令写法**（venv 布局 Windows / Linux 不同，下文一律用 `<python>` 指代）：

| 平台 | 解释器 |
|---|---|
| Windows | `.venv\Scripts\python.exe` |
| Linux / macOS | `.venv/bin/python` |

---

## 1. `mc_check.py` —— MC 取数链路诊断

> 退一步说：**遇到任何 MC 相关的怪事，先跑它，再看机器人日志。** 它会打印 SLP / RCON /
> 群组接口的**原始响应**和每一条判定理由，而机器人日志里只有结论。

```bash
<python> tools/mc_check.py --self-test        # 离线自测：447 条断言，不联网
<python> tools/mc_check.py --quiet-test       # 夜间静默的行为自测，不联网（要 init nonebot）
<python> tools/mc_check.py --list-targets     # 只读：服务器清单解析成什么，不联网
<python> tools/mc_check.py --list-audiences   # 只读：每条群关联覆盖哪些群、哪几台服
<python> tools/mc_check.py --whitelist        # 只读：看一眼服务端白名单
<python> tools/mc_check.py --api              # 只看群组 HTTP 接口那一层
<python> tools/mc_check.py                    # 实机探测**第一条群关联**的全部目标
<python> tools/mc_check.py --target bingo     # 只探一台（服名走和群里同一套解析）
<python> tools/mc_check.py bingo              # 同上，裸写服名也行
<python> tools/mc_check.py --audience 社团群   # 换一条群关联的视角
```

**什么时候用**（对应 DEPLOY.md 第 8 节的几条 F）：

| 症状 | 先跑 | 看什么 |
|---|---|---|
| 群里打 `@bot mc` 没反应 | `--list-audiences` | 那个群有没有被某条关联提到；`targets` 是不是空的 |
| 进服提醒不触发 | 无参数（实机探测） | 每台的「名单条数 / 名单完整」—— **名单不完整时提醒会静默暂停**，这是刻意的 |
| 某台服查不到 / 人数不对 | `--target <服名>` | 「人数来源」那一行（slp / api）、接口与 SLP 有没有互相矛盾 |
| 接口报错、分不清是隧道还是 token | `--api` | `/health` **免鉴权**：它通而 `/status` 401 = token 问题；它本身不通 = 隧道/地址问题 |
| 白名单命令报「未生效」 | `--whitelist` | `enabled` 是不是 true、前缀对不对、名单里到底有没有那个人 |
| 改完配置想确认解析对不对 | `--list-targets` | 启动前就能看到冲突与警告，不用起机器人 |

**几个要点**：

- `--target` / `--audience` **不写 `--audience` 时默认第一条关联**，且服名只在所选关联内
  解析（和群友看到的是同一个集合）。认不出的服名会明确报错，不会退化成「探测全部」。
- `--whitelist` **永远只读**（只发 `list`）。它**没有清理逻辑** —— 真要改白名单请在群里
  发命令，别在脚本里加写操作：中途挂掉会把服务端白名单留在谁也不知道的状态。
- 无参数的实机探测会真的连服务器（MC 端会留下 SLP 探测与 RCON `list` 的记录）。
  目标之间的探测是**并发**的，一轮耗时按最慢的单通道算，不是求和。
- 退出码：`0` 全部正常 / `1` 有目标不正常（或 `--whitelist` 撞上「名单在、拦截关」）/
  `2` 用法错误。

**关于 `--self-test`**：这是本项目**唯一的解析/文案测试**，447 条断言，全部离线。没有 pytest、
没有 `tests/` 目录 —— 不引依赖，且这些断言钉的是**解析与文案**（RCON 返回值怎么拆名字、
进服对账的各种边界、QQ 文案里不许出现 markdown 星号……），它们依赖的是真实响应原文，
离线钉住比联网更适合天天跑。改 `_shared/` 里任何解析或文案，先跑它。

**关于 `--quiet-test`**：`--self-test` 刻意**不 init nonebot**（它跑的是 `_shared/`
里的纯函数，所以在任何机器上都能跑）。而夜间静默的分支长在 `mcs/mc_reporter.py` 里
——那个模块 import 期就要 driver，所以那一路只能单独走：`--quiet-test` 会
`nonebot.init()`，再把 `send_to_groups` 换成记账函数，然后真的调 `_tick_audience`。
它钉的是**当晚完全看不见、第二天早上才炸**的一条：静默期间既不发消息，**基线也要
照常推进** —— 不推进的话 09:00 会把一整晚进过服的人当成「刚进服」一次性补报。

---

## 2. `oopz_check.py` —— oopz 凭据与查询链路诊断

```bash
<python> tools/oopz_check.py
```

列出账号加入的域，以及各域语音频道的在线成员。`@bot oopz` 查不到东西时先跑它：
它把「凭据是否有效」「接口返回了哪些域」「哪个频道解析失败」直接摊开。

`.env` 里已有 `OOPZ_*` 就直接用；没有的话它等价于先跑一遍 `oopz_login.py`。只读。

---

## 3. `oopz_login.py` —— 一次性拿 oopz 凭据

```bash
# Windows（PowerShell）
$env:OOPZ_LOGIN_PHONE = "你的oopz手机号"
$env:OOPZ_LOGIN_PASSWORD = "你的oopz密码"
.venv\Scripts\python.exe tools\oopz_login.py

# Linux
OOPZ_LOGIN_PHONE=... OOPZ_LOGIN_PASSWORD=... .venv/bin/python tools/oopz_login.py
```

登录成功后把 `OOPZ_DEVICE_ID` / `OOPZ_PERSON_UID` / `OOPZ_JWT_TOKEN` /
`OOPZ_PRIVATE_KEY` 写进仓库根的 `.env`。

- **手机号 / 密码只从环境变量读，不要写进 `.env`**（脚本本身不读 `.env`），也**不会被
  保存**——只有登录换来的那四样会被写下来。
- 什么时候要重跑：`OOPZ_JWT_TOKEN` 约 31 天过期，症状是统计 / 播报突然全部失效
  （DEPLOY.md 的 F7）。重跑 → 重启机器人。
- 会**写 `.env`**（只动上面那四个键），这是三个脚本里唯一会改文件的。
