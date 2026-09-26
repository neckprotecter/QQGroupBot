r"""验证 Minecraft 服务器取数链路（SLP + RCON），不启动机器人。

用法：
    .venv\Scripts\python.exe tools\mc_check.py --self-test        # 只跑解析自测，不联网
    .venv\Scripts\python.exe tools\mc_check.py --quiet-test       # 夜间静默的行为自测，不联网
    .venv\Scripts\python.exe tools\mc_check.py --list-targets     # 看 mcs_servers.toml 解析出什么，不联网
    .venv\Scripts\python.exe tools\mc_check.py --whitelist        # 只读地看一眼服务端白名单
    .venv\Scripts\python.exe tools\mc_check.py                    # 实机探测**全部**目标
    .venv\Scripts\python.exe tools\mc_check.py --target bingo     # 只探测一个目标
    .venv\Scripts\python.exe tools\mc_check.py bingo              # 同上（裸写服名也行）

--target 的服名走**和群里同一套解析**（id / name / alias / 唯一前缀，忽略大小写与
全角），所以脚本里能跑通的写法，群友打出来也一定跑得通。

实机探测会打印 SLP 的原始数据、RCON 的 `list` 输出**原文**，以及名单完整性判定
与原因。排查「进服提醒不触发」时先看这里的输出，比翻机器人日志直接。

退出码：0 正常 / 1 有目标不正常 / 2 用法错误（认不出的参数、--target 没跟服名）。

为什么要独立于机器人跑：进服提醒依赖「名单条数 == SLP 报的在线人数」这个交叉
校验，任何一环不成立都会静默暂停提醒。这里把每一环都摊开打出来。
"""
import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认是 cp936(GBK)，打不出 ✓ 这类字符时会抛 UnicodeEncodeError
# 把整个诊断脚本带崩。中文本身在 GBK 下没问题，所以不换编码，只兜住个别字符。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass


# 加载 .env：直接用 python-dotenv（bot 用的也是它），别手写解析。
# 手写版不认行尾注释，`MC_WATCH_INTERVAL_SEC=10   # 轮询间隔（秒）` 会被原样当成值，
# 到 float() 那里才炸，报的还是个看不懂的 ValueError。诊断脚本必须和 bot 用
# 同一套解析语义，否则量出来的配置根本不是 bot 看到的那份。
load_dotenv(ROOT / ".env")


# ---------------------------------------------------------------- 解析自测

# 本机服务端的 `whitelist list` 真实输出（名字已换成占位，格式原样保留）。
# 记在这里当回归样本：它和原版有三个差异——冒号后无空格、最后两项用「 and 」连接、
# 多一段「(out of N seen)」。任何一条被解析器漏掉都会导致加白成功却报「未生效」。
_REAL_WL = (
    "There are 3 (out of 15 seen) whitelisted players:x_l_t, Vul and neckProtecter"
)

# Bingo 26.2 的真实抓包（名字同样换成占位，格式逐字保留）。和上面 GTNH 那句差得很远：
# 复数写成 `player(s)`、没有 `(out of N seen)`、也没有 ` and `。解析器只靠
# 「第一个冒号切前缀 + 逗号拆名字」，所以两台文案完全不同也照样通 —— 这个夹具就是
# 拿来钉这一点的。
_REAL_WL_BINGO = "There are 2 whitelisted player(s): Foo, Bar"

# 白名单为空时服务端回的是**另一句话**（vanilla 的 commands.whitelist.none），
# 而且**整句没有冒号**。2026-09-21 在 Bingo 26.2 上抓到的原文，逐字照抄。
_EMPTY_WL = "There are no whitelisted players"

# (说明, RCON `list` 原始输出, SLP 报的在线人数, 期望解析出的名字)
# 人数传 None 表示不提供期望值，走「第一个冒号」兜底路径
_PARSE_CASES: list[tuple[str, str, int | None, list[str]]] = [
    (
        "en_us 有人",
        "There are 3 of a max of 20 players online: Alice, Bob, Carol",
        3,
        ["Alice", "Bob", "Carol"],
    ),
    (
        "en_us 无人（冒号保留、尾部为空）",
        "There are 0 of a max of 20 players online:",
        0,
        [],
    ),
    (
        "中文（全角冒号）",
        "当前有 3 个玩家在线，最多 20 个玩家：Alice, Bob, Carol",
        3,
        ["Alice", "Bob", "Carol"],
    ),
    (
        "中文全角逗号",
        "当前有 2 个玩家在线，最多 20 个玩家：Alice，Bob",
        2,
        ["Alice", "Bob"],
    ),
    (
        "EssentialsX 改写过、没有冒号 → 必须拒绝解析",
        "There are 3 out of maximum 20 players online.",
        3,
        [],
    ),
    (
        "带 § 颜色码",
        "There are 2 of a max of 20 players online: §aAlice, §bBob",
        2,
        ["Alice", "Bob"],
    ),
    (
        # Minecraft 的 ComponentUtils.formatList 在最后两项之间用「 and 」，不是逗号。
        # 漏掉它会把最后两个人粘成一个 token，人数对不上 → 进服提醒静默暂停。
        "英文 and 连接最后两项",
        "There are 3 of a max of 20 players online: Alice, Bob and Carol",
        3,
        ["Alice", "Bob", "Carol"],
    ),
    (
        "带队伍前缀（§ 去掉后前缀保留）",
        "There are 2 of a max of 20 players online: §a[VIP] Alice, Bob",
        2,
        ["[VIP] Alice", "Bob"],
    ),
    (
        "名字里含冒号 → 靠人数消歧，不能被切走",
        "There are 1 of a max of 20 players online: A:B",
        1,
        ["A:B"],
    ),
    (
        "显示名里含冒号且多人 → 靠人数消歧取第一个冒号",
        "There are 2 of a max of 20 players online: [VIP]: Alice, Bob",
        2,
        ["[VIP]: Alice", "Bob"],
    ),
    (
        "颜色码残留段（只有 §r）应被丢弃",
        "There are 2 of a max of 20 players online: Alice, §r",
        2,
        ["Alice"],
    ),
    ("空输出", "", 0, []),
    ("只有空白", "   \n  ", 0, []),
]


def _self_test() -> int:
    from plugins_napcat._shared.admin import allowed_user as admin_allowed
    from plugins_napcat._shared.mc import parse_list_names, parse_whitelist_names
    from plugins_napcat._shared.mcadmin import (
        ERR_LIST_ARGS,
        ERR_NAME,
        ERR_USAGE,
        AdminCommand,
        parse_command,
        plan_mutation,
        settle,
        whitelist_contains,
        whitelist_lookup,
        whitelist_matches,
    )
    from plugins_napcat._shared.schedule import (
        in_quiet_hours,
        parse_quiet_hours,
        seconds_until_slot,
    )
    from plugins_napcat._shared.mcservers import (
        PICK_AMBIGUOUS,
        PICK_NEED_NAME,
        PICK_NOT_WHITELIST,
        PICK_NO_ROUTE,
        PICK_OK,
        PICK_UNKNOWN,
        ServerConfigError,
        WhitelistRoute,
        parse_book,
        round_budget,
    )
    from plugins_napcat._shared.triggers import detect, primary_keyword

    # MC 白名单命令解析用例：(说明, 输入, 期望动词, 期望玩家名, 期望服名, 期望失败原因)。
    # 失败用例的动词/玩家名/服名都是 None（parse_command 返回 None, 原因）。
    _CMD_CASES: list[tuple[str, str, str | None, str | None, str | None, str]] = [
        ("基本 add", "whitelist add Steve", "add", "Steve", "", ""),
        ("基本 remove", "whitelist remove Steve", "remove", "Steve", "", ""),
        ("list（不带参数）", "whitelist list", "list", "", "", ""),
        ("大小写不敏感", "WHITELIST Add Steve", "add", "Steve", "", ""),
        ("前缀噪音不影响解析", "帮我 whitelist add Steve", "add", "Steve", "", ""),
        # P6：服名写在末尾。两个 token 以上时最后一个当服名、其余当玩家名 —— 玩家名
        # 正则不允许空格，所以这个切法无歧义，不必猜「哪个像服名」。
        ("服名放末尾", "whitelist add Steve bingo", "add", "Steve", "bingo", ""),
        ("list 点名一台", "whitelist list bingo", "list", "", "bingo", ""),
        ("服名放末尾（remove）", "whitelist remove Steve backstab", "remove", "Steve", "backstab", ""),
        # 空白（含 \n）只当分隔符，真正拼进 RCON 的是正则校验过的名字
        ("换行只当分隔符", "whitelist add\nSteve", "add", "Steve", "", ""),
        ("缺子命令", "whitelist", None, None, None, ERR_USAGE),
        ("缺玩家名", "whitelist add", None, None, None, ERR_NAME),
        # 三个 token：玩家名 = "Steve please"（过不了正则），服名 = extra。
        # 两个 token 的 "add Steve please" 现在是合法的「发给 please 服」——服名认不认得
        # 出来由调用方判（ServerBook.pick_whitelist），这一层只做语法。
        ("名字里有空格", "whitelist add Steve please extra", None, None, None, ERR_NAME),
        ("注入第二条命令", "whitelist add Steve; stop", None, None, None, ERR_NAME),
        ("路径式名字", "whitelist add ../Steve", None, None, None, ERR_NAME),
        ("非 ASCII 名字", "whitelist add 玩", None, None, None, ERR_NAME),
        ("超长名字（17 位）", "whitelist add aaaaaaaaaaaaaaaaa", None, None, None, ERR_NAME),
        ("动词不在白名单", "whitelist kick Steve", None, None, None, ERR_USAGE),
        ("list 最多一个服名", "whitelist list a b", None, None, None, ERR_LIST_ARGS),
        ("空文本", "", None, None, None, ERR_USAGE),
        ("无关文本", "你好", None, None, None, ERR_USAGE),
    ]

    # 白名单包含判定：(说明, 输出原文, 查的名字, 期望)
    _WL_CASES: list[tuple[str, str, str, bool | None]] = [
        (
            "en_us 命中（不区分大小写）",
            "There are 3 whitelisted players: Alice, Bob, Carol",
            "bob",
            True,
        ),
        ("en_us 未命中", "There are 3 whitelisted players: Alice, Bob, Carol", "Dave", False),
        ("中文全角冒号与逗号", "有 2 名玩家在白名单中：Alice，Bob", "Bob", True),
        ("带 § 颜色码", "There are 2 whitelisted players: §aAlice, §bBob", "Alice", True),
        # 子串匹配会让 Steve 命中 Steve_2，所以必须是整体比对
        ("整体比对：Steve 不命中 Steve_2", "There are 1 whitelisted players: Steve_2", "Steve", False),
        # 若按整段逗号切，第一段会带着前缀文案，这里正是切冒号才有的结果
        ("前缀里的词不算命中", "There are 1 whitelisted players: Alice", "players", False),
        # Floodgate/Geyser 的基岩版玩家带 . 前缀（见 DEPLOY.md F11）
        ("基岩版 .Name 前缀", "There are 1 whitelisted players: .Steve", "Steve", False),
        # 空名单哨兵：必须报「不在」(False)，不能塌成「判不出来」(None) ——
        # 后者会让 add 的前置读失败，命令发不出去（见 mc.py 里哨兵那段注释）
        ("空名单哨兵 → 不在，不是判不出来", _EMPTY_WL, "Alice", False),
        ("没有冒号 → 判不出来", "3 whitelisted players online.", "Alice", None),
        ("空输出 → 判不出来", "", "Alice", None),
        # 命令前缀写错时也是没冒号，绝不能当成「白名单是空的」（实测 Bingo 26.2 回执）
        (
            "错命令前缀 → 判不出来（不是空名单）",
            "Unknown or incomplete command. See below for errornosuchcmd list<--[HERE]",
            "Alice",
            None,
        ),
        # ↓ 本机服务端的真实抓包（脱敏保留格式）：无空格 + and 连接 + (out of N seen)。
        # 曾经因为它把 Vul / neckProtecter 粘成一个 token，加白明明成功却报「未生效」。
        (
            "真实抓包：and 连接的最后一项能命中",
            _REAL_WL,
            "vul",  # 注意大小写：用户输入小写，白名单存的是 Vul
            True,
        ),
        # 换一台服（Bingo 26.2）文案完全变了，命中判定不受影响
        ("真实抓包 26.2：player(s) 写法能命中", _REAL_WL_BINGO, "bar", True),
    ]

    # 名单解析：(说明, 输出原文, 期望)。空列表与 None 必须分清：前者是「确实没人」，
    # 后者是「判不出来」，调用方要据此决定报「没有玩家」还是「未能验证」。
    _WL_LIST_CASES: list[tuple[str, str, list[str] | None]] = [
        ("空名单（有冒号、尾部为空）", "There are 0 whitelisted players:", []),
        ("多人", "There are 2 whitelisted players: Alice, Bob", ["Alice", "Bob"]),
        ("中文全角", "有 2 名玩家在白名单中：Alice，Bob", ["Alice", "Bob"]),
        ("英文 and 连接最后两项", "There are 2 whitelisted players: Alice and Bob", ["Alice", "Bob"]),
        ("真实抓包（无空格 + and）", _REAL_WL, ["x_l_t", "Vul", "neckProtecter"]),
        ("真实抓包 26.2（player(s) + 普通逗号）", _REAL_WL_BINGO, ["Foo", "Bar"]),
        # vanilla 对「空名单」用的是另一个翻译键（commands.whitelist.none），
        # **整句没有冒号**。不特判就会被当成「格式不认识」，于是白名单为空的服
        # （每台新服的初始状态）里，查白名单回「未能解析」、加白也是只报「未能验证」
        # 而根本没发命令。2026-09-21 在 Bingo 26.2 上实测到的原文就是这一句。
        ("空名单哨兵（vanilla 无冒号回执）", _EMPTY_WL, []),
        ("空名单哨兵：颜色码 / 多余空白 / 大小写都不影响", f"§7{_EMPTY_WL.upper()}\n", []),
        # 反向：错命令前缀的回执**同样没有冒号**，绝不能一起当成空名单 ——
        # 那样「前缀写错」会被误报成「白名单是空的」，比报「未能解析」更难查。
        (
            "错命令前缀 → 判不出来（不是空名单）",
            "Unknown or incomplete command. See below for errornosuchcmd list<--[HERE]",
            None,
        ),
        ("没有冒号 → 判不出来", "3 whitelisted players online.", None),
        ("空输出 → 判不出来", "", None),
    ]

    failed = 0

    print("== list 输出解析自测 ==")
    for desc, raw, count, expected in _PARSE_CASES:
        got = parse_list_names(raw, expected=count)
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            print(f"         输入: {raw!r}（人数 {count}）")
            print(f"         期望: {expected}")
            print(f"         实际: {got}")
    print()

    print("== 整点对齐自测 ==")
    for interval, desc in ((0, "0 不能炸（原实现会 ZeroDivisionError）"), (1, "1 分钟"), (30, "30 分钟"), (60, "60 分钟")):
        try:
            delay = seconds_until_slot(interval)
            ok = 0 <= delay <= max(interval, 1) * 60
            failed += not ok
            print(f"   {'PASS' if ok else 'FAIL'}  {desc}: {delay:.1f}s")
        except Exception as exc:
            failed += 1
            print(f"   FAIL  {desc}: {type(exc).__name__}: {exc}")
    print()

    print("== 夜间静默自测 ==")
    # 解析：合法值
    for raw, expected, desc in (
        ("0-9", (0, 9), "0-9 → (0,9)（默认值）"),
        ("23-7", (23, 7), "23-7 → 跨午夜"),
        (" 0-9 ", (0, 9), "两端空白不算错（.env 里手滑敲的空格）"),
        ("", None, "空串 → 不静默"),
        ("   ", None, "纯空白 → 不静默"),
        (None, None, "None → 不静默"),
    ):
        try:
            got = parse_quiet_hours(raw)
            ok = got == expected
        except Exception as exc:
            got, ok = f"{type(exc).__name__}: {exc}", False
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}: {got!r}")

    # 解析：**必须报错**的写法。不报错就会被当成「没配静默」而静悄悄地放行 ——
    # 「夜里安静」是需求本身，配错了还照发等于需求没做，且没有任何迹象。
    for raw, desc in (
        ("9", "只有一个数（少写 -止）"),
        ("0-9-12", "三个数"),
        ("0-24", "24 点越界（小时是 0..23，写 24 的人多半想表达 0-9 那种意思）"),
        ("-1-9", "负号"),
        ("a-b", "不是数字"),
        ("0-0", "起止相同"),
    ):
        try:
            got = parse_quiet_hours(raw)
            failed += 1
            print(f"   FAIL  {desc}：应报错却返回 {got!r}")
        except ValueError:
            print(f"   PASS  {desc} 报错")

    # 判定：半开区间 [起, 止)
    _cases = (
        ((0, 9), 0, True, "0 点算静默"),
        ((0, 9), 8, True, "8 点算静默"),
        ((0, 9), 9, False, "**9 点整不算**（半开区间，9 点那班播报要发）"),
        ((0, 9), 12, False, "白天不算"),
        ((0, 9), 23, False, "23 点不算"),
        ((23, 7), 23, True, "跨午夜：23 点算"),
        ((23, 7), 3, True, "跨午夜：凌晨 3 点算"),
        ((23, 7), 6, True, "跨午夜：6 点算"),
        ((23, 7), 7, False, "跨午夜：7 点整不算"),
        ((23, 7), 12, False, "跨午夜：中午不算"),
        (None, 3, False, "没配静默时恒 False"),
    )
    for window, hour, expected, desc in _cases:
        got = in_quiet_hours(window, datetime(2026, 9, 26, hour, 30))
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}：{hour} 点 → {got}")
    print()

    print("== 触发词归属自测（同一条消息只能被一个插件认领）==")
    # mcadmin 的关键词可被 MC_ADMIN_TRIGGER 改，用例必须跟着 .env 走，不能写死
    # （mc_check 会加载 .env，写死的话本地改了配置就会「自测失败」）
    admin_kw = primary_keyword("mcadmin", "whitelist")
    mc_kw = primary_keyword("mc", "mc")
    for text, expected in (
        ("oopz", "oopz"),
        ("看看 oopz 呗", "oopz"),
        ("mc", "mc"),
        ("我的世界", "mc"),
        ("服务器", "mc"),
        ("MC 服务器", "mc"),
        ("oopz mc", "oopz"),  # 命中多个时按 _ORDER 优先级，oopz 在 mc 前
        (f"{admin_kw} add Steve", "mcadmin"),
        (f"{admin_kw}", "mcadmin"),
        # mcadmin 在 _ORDER 首位：管理命令不能被查询插件吞掉
        (f"mc {admin_kw} add Steve", "mcadmin"),
        ("你好呀", None),
        # 默认关键词里没有中文，所以「白名单」不归属 mcadmin（除非显式配了它）
        ("白名单怎么加", None),
        ("", None),
    ):
        got = detect(text)
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {text!r} → {got!r}（期望 {expected!r}）")
    print()

    # detect 必须是 locate 的薄包装 —— 否则两套归属判断会各自演化，
    # 出现「detect 认 oopz 但 locate 认 mc」这种自相矛盾，载荷被切错插件。
    print("== detect/locate 一致性与载荷切分自测 ==")
    from plugins_napcat._shared.triggers import locate, strip_keyword

    for text in (
        "oopz", "看看 oopz 呗", "mc", f"{mc_kw} bingo", "我的世界", "服务器",
        "oopz mc", f"{admin_kw} add Steve", f"mc {admin_kw} add Steve",
        "你好呀", "", "   ", f"{mc_kw} {mc_kw} bingo",
    ):
        h = locate(text)
        by_locate = h.plugin if h else None
        by_detect = detect(text)
        ok = by_locate == by_detect
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  detect == locate：{text!r} → {by_locate!r} / {by_detect!r}")

    # 载荷切分：触发词之后的才是服名。`服务器 mc bingo` 这种把两个触发词都写上的
    # 写法必须也能剥干净 —— 服名解析会去掉内部空白，所以剥成空格不影响结果。
    for desc, text, expected in (
        ("基础", f"{mc_kw} bingo", "bingo"),
        ("触发词在前、服名在后", f"{mc_kw}   谁是杀手", "谁是杀手"),
        ("只写触发词 → 空载荷（看总览）", mc_kw, ""),
        ("剥掉该插件的**全部**触发词", f"服务器 {mc_kw} bingo", "bingo"),
        ("触发词写在服名后面", f"bingo {mc_kw}", "bingo"),
        ("大小写不敏感", f"{mc_kw.upper()} Bingo", "Bingo"),
        ("服名里含触发词 → 会被剥坏（已知限制，见 mcs_servers.toml.example）", f"{mc_kw} mc服", "服"),
    ):
        h = locate(text)
        got = strip_keyword(text, h) if h else "<无命中>"
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}：{text!r} → {got!r}（期望 {expected!r}）")
    print()

    print("== MC 白名单命令解析自测 ==")
    # (说明, 输入, 期望动词, 期望玩家名, 期望服名, 期望失败原因)
    for desc, raw, verb, player, server, err in _CMD_CASES:
        cmd, got_err = parse_command(raw)
        got_verb = cmd.verb if cmd else None
        got_player = cmd.player if cmd else None
        got_server = cmd.server if cmd else None
        ok = (got_verb, got_player, got_server, got_err) == (verb, player, server, err)
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            print(f"         输入: {raw!r}")
            print(f"         期望: verb={verb!r} player={player!r} server={server!r} err={err!r}")
            print(
                f"         实际: verb={got_verb!r} player={got_player!r} "
                f"server={got_server!r} err={got_err!r}"
            )

    # P6：动词**之前**的 token 原样交给调用方。这是那个「静默丢弃」bug 的墓碑 ——
    # 改之前 `whitelist gtnh add Steve` 里的 gtnh 被扔掉、命令照发往缺省那台。
    cmd, _ = parse_command("whitelist gtnh add Steve")
    ok = cmd is not None and cmd.server == "" and cmd.pre_verb == ("whitelist", "gtnh")
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  动词前的 token 收进 pre_verb（服名位置写错时调用方才能拦）")
    if not ok:
        print(f"         实际: server={getattr(cmd, 'server', None)!r} pre_verb={getattr(cmd, 'pre_verb', None)!r}")

    # 短构造（只有 verb/player）必须仍然有效：调用方与自测里到处这么用
    cmd = AdminCommand("add", "Steve")
    ok = cmd.server == "" and cmd.pre_verb == ()
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  AdminCommand 新增字段有默认值（短构造不破）")
    print()

    print("== MC 白名单名单解析自测（parse_whitelist_names / whitelist_contains）==")
    for desc, raw, name, expected in _WL_CASES:
        got = whitelist_contains(raw, name)
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            print(f"         输入: {raw!r}，查 {name!r}")
            print(f"         期望: {expected}  实际: {got}")
    for desc, raw, expected in _WL_LIST_CASES:
        got = parse_whitelist_names(raw)
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            print(f"         输入: {raw!r}")
            print(f"         期望: {expected}  实际: {got}")
    print()

    # 服务端写白名单时会拿名字查玩家档案，查到就换成档案里的规范拼写。
    # `add vul` 在白名单里变成 `Vul` 是正常现象，回传服务端拼写免得群里以为加错了人。
    print("== 白名单拼写回传自测（区分大小写只影响显示，不影响判定）==")
    for desc, raw, name, expected in (
        ("大小写不同 → 回传服务端拼写", "There are 1 whitelisted players: Vul", "vul", (True, "Vul")),
        ("完全一致 → 原样回传", "There are 1 whitelisted players: vul", "vul", (True, "vul")),
        ("不在名单 → 回传用户输入", "There are 1 whitelisted players: Alice", "Bob", (False, "Bob")),
        ("判不出来 → None", "3 whitelisted players online.", "Bob", None),
    ):
        got = whitelist_lookup(raw, name)
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            print(f"         期望: {expected}  实际: {got}")
    print()

    # offline-mode 下 `Vul` 和 `vul` 是两个不同 UUID 的独立记录，同名不同拼写的重复条目
    # 是真会出现的。只认第一条会让「移除」留下幽灵记录，紧接着的反查又命中它 → 假「未生效」。
    print("== 白名单同名条目自测（忽略大小写取全部，不取第一条）==")
    for desc, raw, name, expected in (
        ("两条同名不同拼写都取到", "There are 2 whitelisted players: Vul, vul", "vul", ["Vul", "vul"]),
        ("只有一条", _REAL_WL, "vul", ["Vul"]),
        ("没有 → 空列表（不是 None）", "There are 1 whitelisted players: Alice", "bob", []),
        ("判不出来 → None", "3 whitelisted players online.", "bob", None),
        ("整体比对：Steve 不命中 Steve_2", "There are 1 whitelisted players: Steve_2", "Steve", []),
    ):
        got = whitelist_matches(raw, name)
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            print(f"         期望: {expected}  实际: {got}")
    print()

    # 这张决策表决定群里看到的是「已添加 / 已移出」还是「本来就在 / 本来就不在」。
    # 两个方向的代价不对称：把「无事发生」报成「已完成」，会让人以为白名单已经改了。
    print("== 白名单变更决策自测（先读后写；先决定下发什么）==")
    for desc, verb, player, matches, expected in (
        ("add 已在名单里 → 不下发", "add", "vul", ["Vul"], (True, [])),
        ("add 不在名单里 → 按用户输入下发", "add", "vul", [], (False, ["vul"])),
        ("remove 在名单里 → 按服务端记录的拼写下发", "remove", "vul", ["Vul"], (False, ["Vul"])),
        ("remove 同名两条 → 两条都下发", "remove", "vul", ["Vul", "vul"], (False, ["Vul", "vul"])),
        ("remove 本来就不在 → 不下发（不能报「已移出」）", "remove", "vul", [], (True, [])),
    ):
        got = plan_mutation(verb, player, matches)
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}：{got}")
        if not ok:
            print(f"         期望: {expected}")
    for desc, verb, player, targets, after, expected in (
        ("add 成功 → 回显规范拼写", "add", "vul", ["vul"], ["Vul"], (True, "Vul")),
        ("add 失败（名单仍无此人）", "add", "vul", ["vul"], [], (False, "vul")),
        ("remove 成功 → 回显实际删掉的那条", "remove", "vul", ["Vul"], [], (True, "Vul")),
        ("remove 失败（同名还剩一条）", "remove", "vul", ["Vul", "vul"], ["vul"], (False, "vul")),
    ):
        got = settle(verb, player, targets, after)
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}：{got}")
        if not ok:
            print(f"         期望: {expected}")
    print()

    # 这组是本次改动里最该被钉住的语义：管理员名单留空 = 全拒，与群白名单的
    # 「留空 = 不限制」正好相反。拒绝分支会往 stderr 打 warning，属预期输出。
    print("== 管理员名单自测（留空必须全拒；拒绝时打 warning 属预期输出）==")
    saved = {k: os.environ.get(k) for k in ("MC_ADMIN_QQ", "_FAKE_ADMIN_QQ")}
    try:
        for var in saved:
            os.environ.pop(var, None)
        for desc, env, var, user, expected in (
            ("留空 → 一律拒绝（fail-closed）", None, "MC_ADMIN_QQ", 123, False),
            ("配置后：本人放行", "123,456", "MC_ADMIN_QQ", 123, True),
            ("配置后：int 与 str 都认", "123,456", "MC_ADMIN_QQ", "123", True),
            ("配置后：名单外拒绝", "123,456", "MC_ADMIN_QQ", 789, False),
            ("显式空串 = 没配 = 拒绝", "", "MC_ADMIN_QQ", 123, False),
            # 回退链：MC_ADMIN_QQ 留空、第二个变量有值 → 用第二个
            ("回退链：首个非空生效", "42", ("MC_ADMIN_QQ", "_FAKE_ADMIN_QQ"), 42, True),
        ):
            for k in saved:
                os.environ.pop(k, None)
            if env == "":  # 区分「没配」和「显式空串」
                os.environ["MC_ADMIN_QQ"] = ""
            elif env is not None:
                if isinstance(var, tuple):
                    os.environ["_FAKE_ADMIN_QQ"] = env
                else:
                    os.environ[var] = env
            got = admin_allowed(user, "test", *((var,) if isinstance(var, str) else var))
            ok = got == expected
            failed += not ok
            print(f"   {'PASS' if ok else 'FAIL'}  {desc}：得到 {got}（期望 {expected}）")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print()

    # ---------------- 块 10：mcs_servers.toml 解析与校验 ----------------
    # 校验分两类，这个分界是刻意的：
    #   报错 = 配置**不能**带着它跑（id 重复会让查询永远歧义、未知键会静默失效）
    #   警告 = 配置能跑，但运行期有问题（两个目标抢同一端口 = 那两台不能同时起）
    # 把后者硬报错会让整个 bot 起不来，而它本可以在「哪台在跑就监控哪台」下正常工作。
    print("== mcs_servers.toml 解析与校验自测 ==")

    _SRV_MIN = """
[[targets]]
id = "solo"
kind = "standalone"
host = "127.0.0.1"
port = 25565
"""

    # 贴近真实拓扑：代理 + 两个同组子服 + 独立服。
    # 白名单归属与主服**已不在这里**（搬去了 mcs_audiences.toml 的 [[audience]]），
    # 所以本夹具里没有 [whitelist] / [defaults].primary；那两样在块 15/16 里测。
    _SRV_REAL = """
[defaults]
timeout = 3

[[targets]]
id   = "proxy"
kind = "proxy"
group = "主服群"
host = "127.0.0.1"
port = 25565
rcon = { port = 25575, password = "x" }

[[targets]]
id      = "bingo"
name    = "Bingo"
kind    = "backend"
group   = "主服群"
host    = "127.0.0.1"
port    = 25566
rcon    = { port = 25576, password = "y" }
timeout = 7

[[targets]]
id      = "backstabbed"
name    = "谁是杀手"
aliases = ["backstab"]
kind    = "backend"
group   = "主服群"
host    = "127.0.0.1"
port    = 25567

[[targets]]
id   = "gtnh"
kind = "standalone"
host = "10.0.0.9"
port = 25565
"""

    for desc, text in (
        ("最小可用（只给 id/kind/host/port）", _SRV_MIN),
        ("真实拓扑（代理 + 子服 + 别名）", _SRV_REAL),
    ):
        try:
            parse_book(text)
        except ServerConfigError as exc:
            failed += 1
            print(f"   FAIL  {desc}：应通过却报错 —— {exc}")
        else:
            print(f"   PASS  {desc}")

    # 报错用例：(说明, toml, 期望错误信息里出现的关键词)
    _SRV_BAD: list[tuple[str, str, str]] = [
        (
            "targets 为空",
            "[defaults]\ntimeout = 5\n",
            "至少要有一个",
        ),
        (
            "id 重复",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=1\n'
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=2\n',
            "id 重复",
        ),
        (
            "kind 非法（拼错 proxy）",
            '[[targets]]\nid="a"\nkind="prox"\nhost="h"\nport=1\n',
            "kind 非法",
        ),
        (
            "别名跨目标撞车",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=1\naliases=["x"]\n'
            '[[targets]]\nid="b"\nkind="standalone"\nhost="h"\nport=2\naliases=["X"]\n',
            "已被目标",
        ),
        (
            "一个目标的名字撞另一个的 id",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=1\n'
            '[[targets]]\nid="b"\nname="a"\nkind="standalone"\nhost="h"\nport=2\n',
            "已被目标",
        ),
        (
            "未知键（rcon_password 写成平级）",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=1\nrcon_password="x"\n',
            "不认识的键",
        ),
        (
            "未知键（顶层）",
            '[mc]\nhost="h"\n',
            "不认识的键",
        ),
        (
            "timeout 非法（0）",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=1\ntimeout=0\n',
            "必须为正数",
        ),
        (
            "port 越界",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=70000\n',
            "超出范围",
        ),
        (
            "port 写成 bool",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=true\n',
            "必须是整数",
        ),
        # ↓ transit（中转服）的四条：它只对「配了 api 的代理」有意义，四种写错法都是
        #   **配了却没生效**那一类 —— 表现只是「人数偏大」，看不出异样，所以都要拦住。
        (
            "transit 写在非代理上",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=1\ntransit=["limbo"]\n',
            "只有 kind",
        ),
        (
            "transit 但没有 api（代理只走 SLP）",
            '[[targets]]\nid="p"\nkind="proxy"\nhost="h"\nport=1\ntransit=["limbo"]\n',
            "没有 api 段",
        ),
        (
            "transit 点名了自己挂着的子服",
            '[[targets]]\nid="p"\nkind="proxy"\nhost="h"\nport=1\n'
            'api={url="http://h:1",token="t"}\ntransit=["limbo"]\n'
            '[[targets]]\nid="l"\nkind="backend"\nsource="p"\nsource_key="limbo"\n',
            "我们的目标 l",
        ),
        (
            "transit 里名字重复（会扣两次）",
            '[[targets]]\nid="p"\nkind="proxy"\nhost="h"\nport=1\n'
            'api={url="http://h:1",token="t"}\ntransit=["limbo","limbo"]\n',
            "重复",
        ),
        (
            "transit 写成字符串（不是数组）",
            '[[targets]]\nid="p"\nkind="proxy"\nhost="h"\nport=1\n'
            'api={url="http://h:1",token="t"}\ntransit="limbo"\n',
            "必须是字符串数组",
        ),
        # ↓ 四条迁移报错：旧键还在全量表里。**按键存在判、不看值** —— 空表
        #   `[whitelist]` 与 `primary = ""` 同样要报，否则「删干净了」只是错觉。
        #   报错必须点出新家（mcs_audiences.toml），只说不认识的键等于没说。
        (
            "[whitelist] 还留在 mcs_servers.toml",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=1\n'
            '[whitelist]\ntarget="a"\ncommand="whitelist"\n',
            "mcs_audiences.toml",
        ),
        (
            "空的 [whitelist] 段（没有键）也要报",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=1\n[whitelist]\n',
            "mcs_audiences.toml",
        ),
        (
            "[defaults].primary 还留在 mcs_servers.toml",
            '[defaults]\nprimary = "a"\n'
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=1\n',
            "mcs_audiences.toml",
        ),
        (
            "[defaults].primary 写成空串也要报",
            '[defaults]\nprimary = ""\n'
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=1\n',
            "mcs_audiences.toml",
        ),
        (
            "语法错误",
            "[[targets]\nid=\n",
            "语法错误",
        ),
        (
            "host 缺失",
            '[[targets]]\nid="a"\nkind="standalone"\nport=1\n',
            "不能为空",
        ),
    ]
    for desc, text, needle in _SRV_BAD:
        try:
            parse_book(text)
        except ServerConfigError as exc:
            ok = needle in str(exc)
            failed += not ok
            print(f"   {'PASS' if ok else 'FAIL'}  {desc}")
            if not ok:
                print(f"         期望错误里含 {needle!r}，实际是 {exc}")
        else:
            failed += 1
            print(f"   FAIL  {desc}：应报错却通过了")

    # 警告用例：(说明, toml, 期望出现的警告关键词) —— 这些必须**能加载**
    _SRV_WARN: list[tuple[str, str, str]] = [
        (
            "两个目标抢同一游戏端口 → 警告而非报错",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="127.0.0.1"\nport=25565\n'
            '[[targets]]\nid="b"\nkind="standalone"\nhost="127.0.0.1"\nport=25565\n',
            "端口冲突",
        ),
        (
            "两个目标抢同一 RCON 端口 → 警告",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="127.0.0.1"\nport=1\n'
            'rcon={port=25575,password="x"}\n'
            '[[targets]]\nid="b"\nkind="standalone"\nhost="127.0.0.1"\nport=2\n'
            'rcon={port=25575,password="y"}\n',
            "RCON 端口冲突",
        ),
        (
            "非代理目标没配 rcon 密码 → 警告（它不出名单）",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="127.0.0.1"\nport=1\n',
            "不出玩家名单",
        ),
    ]
    for desc, text, needle in _SRV_WARN:
        try:
            book = parse_book(text)
        except ServerConfigError as exc:
            failed += 1
            print(f"   FAIL  {desc}：应能加载却报错 —— {exc}")
            continue
        hit = any(needle in w for w in book.warnings)
        failed += not hit
        print(f"   {'PASS' if hit else 'FAIL'}  {desc}")
        if not hit:
            print(f"         期望警告含 {needle!r}，实际是 {book.warnings}")

    # 代理**没配** rcon 不该报警告：代理本来就不出名单，它的 rcon 只服务白名单
    book = parse_book('[[targets]]\nid="p"\nkind="proxy"\nhost="h"\nport=1\n')
    ok = not book.warnings
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  代理没配 rcon 不报警告（代理本就不出名单）")
    if not ok:
        print(f"         实际警告: {book.warnings}")

    # --- rcon.host：SLP 和 RCON 可以不在一个地址上 ---
    # 为什么需要它：RCON 只绑内网/回环、由隧道转到本机，而游戏 SLP 走公网端口。
    # 2026-09-22 对方那台 GTNH 就是这个形状：SLP=203.0.113.10:30004（公网），
    # RCON=隧道这头的 127.0.0.1:25575。只有一个 host 时这两种地址没法同时表达。
    _SRV_RCONHOST = """
[[targets]]
id = "gtnh"
kind = "standalone"
host = "203.0.113.10"
port = 30004
rcon = { host = "127.0.0.1", port = 25575, password = "x" }

[[targets]]
id = "local"
kind = "standalone"
host = "10.0.0.9"
port = 25565
rcon = { port = 25576, password = "y" }

[[targets]]
id = "blank"
kind = "standalone"
host = "10.0.0.10"
port = 25565
rcon = { host = "", port = 25577, password = "z" }
"""
    book = parse_book(_SRV_RCONHOST)
    _by = {t.id: t for t in book.targets}
    for tid, field, want in (
        ("gtnh", "rcon_addr", "127.0.0.1:25575"),      # 填了 → 用填的
        ("gtnh", "rcon_host", "127.0.0.1"),            # mc._open 读的就是这个
        ("local", "rcon_addr", "10.0.0.9:25576"),      # 没填 → 回退到目标 host
        ("blank", "rcon_addr", "10.0.0.10:25577"),     # 填空串 → 同样回退（空是正常取值）
        ("gtnh", "game_addr", "203.0.113.10:30004"),  # SLP 那一侧不受影响
        ("blank", "rcon_host", "10.0.0.10"),
    ):
        got = getattr(_by[tid], field)
        ok = got == want
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {tid}.{field} = {got}（期望 {want}）")

    # 「同一 RCON 端口」的警告必须按 host:port 判 —— 只看端口号的话，两台各自
    # 绑在自己主机上的 25575 会被误报成冲突（加了 rcon.host 之后这条才成立）
    _srv_sameport = (
        '[[targets]]\nid="a"\nkind="standalone"\nhost="10.0.0.1"\nport=1\n'
        'rcon={host="127.0.0.1",port=25575,password="x"}\n'
        '[[targets]]\nid="b"\nkind="standalone"\nhost="10.0.0.2"\nport=2\n'
        'rcon={host="10.0.0.2",port=25575,password="y"}\n'
    )
    book = parse_book(_srv_sameport)
    ok = not any("RCON 端口冲突" in w for w in book.warnings)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  不同 host 上的同一 RCON 端口号不算冲突")
    if not ok:
        print(f"         实际警告: {book.warnings}")

    # _RCON_KEYS 加了 host 之后，别把别的键也放进来
    try:
        parse_book(
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=1\n'
            'rcon={port=1,password="p",hostname="x"}\n'
        )
    except ServerConfigError as exc:
        ok = "hostname" in str(exc)
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  rcon 表里的未知键仍然报错：{exc}")
    else:
        failed += 1
        print("   FAIL  rcon 表里的未知键 hostname 应报错却没报")
    print()

    # --- api / source：群组服的 HTTP 接口（2026-09-22 对接 SZUcraft 那套 bridge）---
    # 形状：代理那条配 api（它一个口就能给出**每个子服**的人数与名单），子服配
    # source 指回代理那条。子服没有自己的游戏端口可探（内网子服没发布），所以
    # 接口型 / 挂在接口上的目标，host/port 都是**可选**的。
    _SRV_API = """
[[targets]]
id = "szu"
name = "主服群"
kind = "proxy"
group = "主服群"
api = { url = "http://127.0.0.1:8080", token = "t" }

[[targets]]
id = "lobby"
name = "大厅"
kind = "backend"
group = "主服群"
source = "szu"

[[targets]]
id = "bingo"
name = "Bingo"
kind = "backend"
group = "主服群"
source = "szu"
source_key = "bingo-s2"
"""
    book = parse_book(_SRV_API)
    _by = {t.id: t for t in book.targets}
    szu, lobby, bingo_api = _by["szu"], _by["lobby"], _by["bingo"]
    for desc, got, want in (
        # 接口型代理**仍然不出名单**：它的名单是全群组口径，按分服数会把人数两遍
        # （各子服的名单里本来就有那些人）。接口能给出名单，是我们**不用**它。
        ("接口型代理仍按全群组口径（不参与分服计数）", szu.serves_names, False),
        ("接口型代理白名单可用（走接口，不是 RCON）", szu.whitelist_ready, True),
        ("接口型代理没有游戏地址", szu.game_addr, "-"),
        ("接口型目标的取数通道", szu.transport, "接口"),
        # 子服：数据从 hub 来，自己没有游戏端口，也没有 RCON
        ("子服的取数通道", lobby.transport, "接口（szu 的子服）"),
        ("子服（有 source）出名单", lobby.serves_names, True),
        ("子服没有游戏地址", lobby.game_addr, "-"),
        ("子服的 source_key 缺省 = id", lobby.source_name, "lobby"),
        ("子服的 source_key 显式给就用给的", bingo_api.source_name, "bingo-s2"),
        # 子服没配 RCON、也没配 api → 它**不能**做白名单操作（真实约束，不能放宽）
        ("子服（无 rcon）白名单不可用", lobby.whitelist_ready, False),
    ):
        ok = got == want
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}：{got!r}（期望 {want!r}）")

    # 挂接口的目标**不该**报「不出玩家名单」——数据来自 hub，不是只靠 SLP 样本。
    # 代理也不该报（它出不出名单是通道决定的，没配接口是设计如此）。
    ok = not any("不出玩家名单" in w for w in book.warnings)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口型目标不报「不出玩家名单」")
    if not ok:
        print(f"         实际警告: {book.warnings}")

    # 没有游戏地址的目标之间**不该**报端口冲突：game_addr 会一律退化成 "-"，
    # 不加 host 判断就是一堆「这几台都绑 -」的假冲突，把真冲突埋掉。
    ok = not any("端口冲突" in w for w in book.warnings)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  无游戏地址的目标之间不报端口冲突")
    if not ok:
        print(f"         实际警告: {book.warnings}")

    # 耗时上界：接口型目标只花一次 HTTP（子服不额外花时间，数据已在 hub 那份响应里）
    _s, _r, _a, _b = round_budget(book)
    ok = (_s, _r, _a, _b) == (0.0, 0.0, 5.0, 5.0)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口型目标的耗时上界 = {_b:g}s（期望 5 = 一次 HTTP）")

    # 代理不参与 RCON 那项：它的 RCON 只服务白名单、从不发 list
    _r2 = round_budget(
        parse_book(
            '[[targets]]\nid="p"\nkind="proxy"\nhost="h"\nport=1\n'
            'rcon={port=2,password="x"}\nrcon_timeout=9\n'
            '[[targets]]\nid="b"\nkind="backend"\nhost="h"\nport=3\n'
            'rcon={port=4,password="y"}\nrcon_timeout=5\n'
        )
    )
    ok = _r2[1] == 5.0
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  代理的 RCON 不计入耗时上界：{_r2[1]:g}s（期望 5，不是 9）")

    # source 允许**前向引用**（代理段写在后面）。写成顺序敏感的话，调一下段落顺序
    # 就报「不存在」，而错误信息还会把人指去查一个明明存在的 id。
    try:
        parse_book(
            '[[targets]]\nid="lobby"\nkind="backend"\nsource="szu"\n'
            '[[targets]]\nid="szu"\nkind="proxy"\n'
            'api={url="http://h:1",token="t"}\n'
        )
    except ServerConfigError as exc:
        failed += 1
        print(f"   FAIL  source 前向引用应能加载，却报错：{exc}")
    else:
        print("   PASS  source 允许前向引用（代理段写在子服后面也能加载）")

    # 每一种写错都要**报错并说清怎么改**，不能静默降级成「没配 RCON」那种极难查的形态
    _API_ERRORS: list[tuple[str, str, str]] = [
        (
            "api 配在子服上（接口给的是整个群组）",
            '[[targets]]\nid="a"\nkind="backend"\nhost="h"\nport=1\n'
            'api={url="http://h:1",token="t"}\n',
            "proxy",
        ),
        (
            "api 没给 token",
            '[[targets]]\nid="a"\nkind="proxy"\napi={url="http://h:1"}\n',
            "api.token",
        ),
        (
            "api.token 是空串",
            '[[targets]]\nid="a"\nkind="proxy"\napi={url="http://h:1",token=""}\n',
            "api.token",
        ),
        (
            "api.url 没带协议",
            '[[targets]]\nid="a"\nkind="proxy"\napi={url="127.0.0.1:8080",token="t"}\n',
            "http://",
        ),
        (
            "api 和 rcon 同时配（两条通道，说不清走哪条）",
            '[[targets]]\nid="a"\nkind="proxy"\napi={url="http://h:1",token="t"}\n'
            'rcon={port=2,password="p"}\n',
            "互斥",
        ),
        (
            "api 和 source 同时配在同一条上",
            '[[targets]]\nid="a"\nkind="proxy"\napi={url="http://h:1",token="t"}\n'
            'source="b"\n',
            "只能留一个",
        ),
        (
            # 这条比 api+rcon 隐蔽：两条路各自都自洽，只是**各说各话** —— 名单来自
            # hub 的接口、白名单命令走本机 RCON，于是「机器人说已添加，人还是进不去」。
            "source 和 rcon 同时配（名单从接口来、白名单却走本机 RCON）",
            '[[targets]]\nid="a"\nkind="backend"\nsource="szu"\n'
            'rcon={port=2,password="p"}\n'
            '[[targets]]\nid="szu"\nkind="proxy"\napi={url="http://h:1",token="t"}\n',
            "同时配了 source 和 rcon",
        ),
        (
            "source 指向不存在的 id",
            '[[targets]]\nid="a"\nkind="backend"\nsource="nope"\n',
            "不存在",
        ),
        (
            "source 指向一台没配 api 的服",
            '[[targets]]\nid="a"\nkind="backend"\nsource="b"\n'
            '[[targets]]\nid="b"\nkind="proxy"\nhost="h"\nport=1\n',
            "没配 api",
        ),
        (
            "source_key 没有 source 陪着",
            '[[targets]]\nid="a"\nkind="backend"\nsource_key="x"\n',
            "source_key",
        ),
        (
            "接口型目标只给了 port 没给 host（SLP 要两个一起）",
            '[[targets]]\nid="a"\nkind="proxy"\nport=25565\n'
            'api={url="http://h:1",token="t"}\n',
            "单独出现",
        ),
        (
            "api 表里的未知键",
            '[[targets]]\nid="a"\nkind="proxy"\napi={url="http://h:1",token="t",port=1}\n',
            "port",
        ),
        (
            "目标表里的未知键（撞错名字的邻居）",
            '[[targets]]\nid="a"\nkind="backend"\nhost="h"\nport=1\nsources="b"\n',
            "sources",
        ),
    ]
    for desc, text, needle in _API_ERRORS:
        try:
            parse_book(text)
        except ServerConfigError as exc:
            ok = needle in str(exc)
            failed += not ok
            print(f"   {'PASS' if ok else 'FAIL'}  {desc}：{exc}")
            if not ok:
                print(f"         期望报错里含 {needle!r}")
        else:
            failed += 1
            print(f"   FAIL  {desc} 应报错却没报")
    print()

    # ---------------- 块 11：服名 → 目标解析 ----------------
    # 三态都必须分得清：命中 / 歧义 / 未知。后两者在群里要出**不同**的回复，
    # 而两种都不能静默 —— 触发词一旦被认领，每个分支都必须应答。
    print("== 服名 → 目标解析自测 ==")
    book = parse_book(_SRV_REAL)
    for desc, query, expected in (
        ("精确 id", "bingo", "bingo"),
        ("精确 name（大小写不同）", "Bingo", "bingo"),
        ("大小写不敏感", "BINGO", "bingo"),
        ("全角 NFKC", "ｂｉｎｇｏ", "bingo"),
        ("中文名", "谁是杀手", "backstabbed"),
        ("alias", "backstab", "backstabbed"),
        ("唯一前缀", "gt", "gtnh"),
        ("前缀歧义（b 同时是 bingo/backstabbed）", "b", "歧义"),
        ("未知服名", "zzz", "未知"),
        ("空串", "", "未知"),
        ("只有空白", "   ", "未知"),
    ):
        res = book.resolve(query)
        got = res.target.id if res.ok else ("歧义" if res.ambiguous else "未知")
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}：{query!r} → {got}（期望 {expected}）")
        if not ok:
            print(f"         候选: {res.candidates}")

    # 白名单归属与命令前缀**已不在这里测** —— 它们是按群关联区分的信息，随
    # [[audience]] 走，断言搬到了块 16（投影视图）。全量 book 上它们恒为空，
    # 在这里测只会测出一条「本来就是空的」结论。

    # defaults.timeout=3 要能被子服的显式 timeout=7 覆盖，没写的继承 3
    bingo, back, proxy = book.get("bingo"), book.get("backstabbed"), book.get("proxy")
    ok = (bingo.timeout, back.timeout, proxy.timeout) == (7.0, 3.0, 3.0)
    failed += not ok
    print(
        f"   {'PASS' if ok else 'FAIL'}  timeout 覆盖与继承"
        f"（bingo={bingo.timeout:g} 覆盖 / 未写的继承 {back.timeout:g}）"
    )

    # 代理不出名单这条必须钉住：它决定 mc_check 用什么标准判定代理是否正常
    ok = not proxy.serves_names and bingo.serves_names
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  代理 serves_names=False，子服 True")

    # 汇总/播报的展示顺序：主服排最前，其余保持配置顺序。
    # 主服已经不写在全量表里了（它住在本条的 [[audience]]），所以这里必须走 scoped()
    # 投影才能测到 —— 直接 parse_book 出来的 book 上 primary 恒为空，那样测的是个空结论。
    # 配置顺序刻意选成 a/b/c 而 primary 选 b：若实现成「按配置顺序」，期望值会一模一样，
    # 这条就测不出任何东西了。
    _PF = """
    [[targets]]
    id = "a"
    kind = "backend"
    host = "h"
    port = 1
    [[targets]]
    id = "b"
    kind = "backend"
    host = "h"
    port = 2
    [[targets]]
    id = "c"
    kind = "backend"
    host = "h"
    port = 3
    """
    order = [t.id for t in parse_book(_PF).scoped(["a", "b", "c"], primary="b").targets_primary_first]
    ok = order == ["b", "a", "c"]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  主服排最前、其余保持配置顺序：{order}（期望 ['b', 'a', 'c']）")

    # 没配 primary 时退回列表第一个 —— 顺序不变，不能让默认值把展示顺序搅乱
    full = parse_book(_SRV_REAL)
    order = [t.id for t in full.scoped([t.id for t in full.targets]).targets_primary_first]
    ok = order == [t.id for t in full.targets]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  没配 primary → 顺序与配置一致：{order}")

    # 一轮耗时取 max **不取和**：目标之间是并发探测，加子服不该把周期撑长。
    # 刻意不复用上面的 fixture —— 只有一台时「取和」和「取 max」结果相同，
    # 必须多台才分得开。
    #   取 max：SLP max(3,5,5)=5 + RCON 5×2 = 15
    #   取和  ：(3+10) + (5+10) + (5+10) = 43
    *_, budget3 = round_budget(
        parse_book(
            """
            [defaults]
            timeout = 3
            rcon_timeout = 5
            [[targets]]
            id = "a"
            kind = "backend"
            host = "h"
            port = 1
            [[targets]]
            id = "b"
            kind = "backend"
            host = "h"
            port = 2
            timeout = 5
            rcon = { port = 3, password = "x" }
            [[targets]]
            id = "c"
            kind = "backend"
            host = "h"
            port = 4
            timeout = 5
            rcon = { port = 5, password = "x" }
            """
        )
    )
    ok = budget3 == 15.0
    failed += not ok
    print(
        f"   {'PASS' if ok else 'FAIL'}  一轮耗时取 max 不取和："
        f"{budget3:g}s（期望 15 = SLP 5 + RCON 5×2；取和是 43）"
    )

    # SLP 失败的两种性质，决定文案说「连不上」还是「应答无法解析」。
    # 归错方向的代价是实打实的：GTNH 启动期间会答一个缺字段的状态响应，mcstatus
    # 把它包成 OSError("Received invalid status response")。若归到「连不上」，
    # 群里就会说一台**正开着**的服连不上，把人指去查防火墙。
    print("== SLP 失败分类自测（决定文案把人指向网络还是服务端）==")
    import socket as _socket

    from plugins_napcat._shared.mc import (
        SLP_UNPARSEABLE as _UNPARSEABLE,
    )
    from plugins_napcat._shared.mc import (
        SLP_UNREACHABLE as _UNREACHABLE,
    )
    from plugins_napcat._shared.mc import (
        _CONN_ERRORS as _CONN,
    )
    from plugins_napcat._shared.mc import (
        _describe_slp_failure as _describe,
    )

    # 复刻 fetch_snapshot 里那一句判据，把它变成可执行约束
    def _classify(exc: BaseException) -> str:
        return _UNREACHABLE if isinstance(exc, _CONN) else _UNPARSEABLE

    # mcstatus 那个 OSError 自带的 cause 链 —— 缺哪个字段只有它说得清
    _bad = OSError("Received invalid status response")
    _bad.__cause__ = KeyError("players")
    for desc, exc, expected in (
        ("服务器没开（ConnectionRefusedError）", ConnectionRefusedError(10061, "拒绝"), _UNREACHABLE),
        ("防火墙丢包（TimeoutError）", TimeoutError(), _UNREACHABLE),
        ("域名解析不了（gaierror）", _socket.gaierror(-2, "unknown host"), _UNREACHABLE),
        ("连上就被关（EOFError）", EOFError(), _UNREACHABLE),
        ("缺 players 字段的 OSError → 应答无法解析", _bad, _UNPARSEABLE),
    ):
        got = _classify(exc)
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}：{got}")

    # cause 链必须写进文案：外层那句「invalid status response」不说缺哪个字段
    text = _describe(_bad)
    ok = "KeyError" in text and "players" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  cause 链写进文案（否则丢掉「缺哪个字段」）：{text!r}")
    print()

    # ---------------- 块 14：命令行解析 ----------------
    # 这条最该被钉住的不是「能不能解析 --target」，而是**认不出的参数必须报错**。
    # 静默退化成实机探测的代价很高：真的去连服务器发 RCON（可能改到真数据），
    # 还打出一屏看着正常的输出。用法错误的分支会打印提示，属预期输出。
    print("== 命令行解析自测（认不出的参数必须拦下，不许退化成实机探测）==")
    import io as _io

    for desc, argv, expected in (
        ("不带参数 → 探测全部目标", [], ("live", "", "")),
        ("--self-test", ["--self-test"], ("self-test", "", "")),
        ("--list-targets", ["--list-targets"], ("list-targets", "", "")),
        ("--list-audiences", ["--list-audiences"], ("list-audiences", "", "")),
        ("--whitelist", ["--whitelist"], ("whitelist", "", "")),
        ("--target <服名>", ["--target", "bingo"], ("live", "bingo", "")),
        ("--target=<服名>（等号写法）", ["--target=bingo"], ("live", "bingo", "")),
        ("裸写服名", ["bingo"], ("live", "bingo", "")),
        ("--audience <关联名>", ["--audience", "社团群"], ("live", "", "社团群")),
        ("--audience=<关联名>（等号写法）", ["--audience=社团群"], ("live", "", "社团群")),
        (
            "--audience + --target 同时给",
            ["--audience", "社团群", "--target", "bingo"],
            ("live", "bingo", "社团群"),
        ),
        (
            "--audience 对 --whitelist 也生效（白名单是按群关联区分的）",
            ["--audience", "社团群", "--whitelist"],
            ("whitelist", "", "社团群"),
        ),
        # --target 对 --whitelist 是**收窄**（只看那一台）。这个分支原本排在
        # --whitelist 之后，于是 `--whitelist --target bingo` 被静默当成「列出全部」——
        # 命令看着成功了，只是没按你说的收窄，而且没有任何提示。
        (
            "--whitelist + --target 收窄到一台",
            ["--whitelist", "--target", "bingo"],
            ("whitelist", "bingo", ""),
        ),
        (
            "--whitelist 裸写服名也收窄",
            ["--whitelist", "bingo"],
            ("whitelist", "bingo", ""),
        ),
        (
            "--whitelist + --audience + --target 三个一起",
            ["--audience", "社团群", "--whitelist", "--target", "bingo"],
            ("whitelist", "bingo", "社团群"),
        ),
        # --api 只看群组接口那一层（不碰 SLP/RCON），所以它和 --target/--audience 一样
        # 是**独立动作者**而不是 --live 的开关：`--api --target x` 必须还是 api，
        # 万一退化成 live 就会去连所有服务器的 RCON。
        ("--api（只看群组接口）", ["--api"], ("api", "", "")),
        ("--api + --target 收窄", ["--api", "--target", "szu"], ("api", "szu", "")),
        (
            "--api + --audience",
            ["--api", "--audience", "社团群"],
            ("api", "", "社团群"),
        ),
        ("--api 写在后头也照样是 api", ["--target", "szu", "--api"], ("api", "szu", "")),
        ("自测优先于实机探测", ["--self-test", "--target", "x"], ("self-test", "", "")),
        ("--quiet-test（静默行为自测）", ["--quiet-test"], ("quiet-test", "", "")),
        (
            "--quiet-test 优先于实机探测",
            ["--quiet-test", "--target", "x"],
            ("quiet-test", "", ""),
        ),
        ("不带参数时关联名留空 = 由 _live 自己挑默认那条", ["bingo"], ("live", "bingo", "")),
        # ↓ 这些是本次真正要防的：它们都**不能**变成实机探测
        ("--help 本脚本没有 → 拦下", ["--help"], None),
        ("--targets（--list-targets 的手滑）→ 拦下", ["--targets"], None),
        ("--target 后面没跟服名 → 拦下", ["--target"], None),
        ("--target= 空值 → 拦下", ["--target="], None),
        # 新增的参数必须同时进 known 集合，否则会被当不认识而退化成实机探测
        ("--audience 后面没跟关联名 → 拦下", ["--audience"], None),
        ("--audience= 空值 → 拦下", ["--audience="], None),
        ("--list-audience（少个 s 的手滑）→ 拦下", ["--list-audience"], None),
        # 开关写成 = 值：partition 出来的 flag 仍在 known 里，但后面那些 `in argv`
        # 判断一个都匹配不上 → **静默退化成全量实机探测**（真连服务器、真发 RCON）。
        # 这正是上面那几条要防的事，所以 = 形式必须拦下，不能靠「没人会这么写」。
        ("--self-test=1（开关写成 = 值）→ 拦下", ["--self-test=1"], None),
        ("--quiet-test=1 → 拦下", ["--quiet-test=1"], None),
    ):
        # 用法错误分支会往 stdout 打提示，自测里屏蔽掉免得刷屏
        buf, sys.stdout = sys.stdout, _io.StringIO()
        try:
            got = _parse_argv(argv)
        finally:
            sys.stdout = buf
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}：{argv} → {got}（期望 {expected}）")
    print()

    # ---------------- 块 13：文案渲染 ----------------
    # 渲染是纯函数，所以能离线把「群里到底会看到什么」钉死。这里最该钉住的是
    # **多目标时尾注不能被 truncate() 吃掉** —— truncate 切的是尾部，而尾注正是
    # 「上面的数对不上」那句提示，被切掉后消息看着完全正常，只是少了最该看的。
    print("== 文案渲染自测 ==")
    from plugins_napcat._shared.mc import (  # noqa: E402
        SLP_UNPARSEABLE,
        SLP_UNREACHABLE,
        McSnapshot,
    )
    from plugins_napcat._shared.mcrender import (
        Row,
        name_budget,
        render_detail,
        render_summary,
    )
    from plugins_napcat._shared.mcservers import ApiSpec, ServerTarget
    from plugins_napcat._shared.textlen import MAX_LEN

    def _tgt(
        tid: str, kind: str = "backend", name: str = "", group: str = "主服群"
    ) -> ServerTarget:
        return ServerTarget(
            id=tid, name=name or tid, kind=kind, group=group,
            host="h", port=1, timeout=5.0, rcon_timeout=5.0,
        )

    def _snap(
        count: int = 0, names: list[str] | None = None, *,
        complete: bool = True, reachable: bool = True,
        kind: str = "", error: str = "",
    ) -> McSnapshot:
        return McSnapshot(
            target_id="x", reachable=reachable, count=count, names=names or [],
            names_complete=complete, error_kind=kind, error=error,
        )

    for desc, total, n, expected in (
        ("单目标拿满额度", 50, 1, 50),
        ("4 个目标均分", 50, 4, 12),
        ("目标极多时保底 5", 50, 20, 5),
        ("目标数为 0 不炸（不能 ZeroDivisionError）", 50, 0, 50),
    ):
        got = name_budget(total, n)
        ok = got == expected
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}：name_budget({total}, {n}) = {got}（期望 {expected}）")

    # 明细：名单在额度处截断并如实报「还有 N 人」
    snap = _snap(count=30, names=[f"P{i}" for i in range(30)], complete=False, error="测试")
    text = render_detail(snap, _tgt("gtnh", "standalone", "GTNH"), max_names=5)
    ok = text.count("  • ") == 5 and "还有 25 人" in text and "名单可能不完整" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  明细按额度截断并如实说「还有 N 人」")
    if not ok:
        print(f"         实际:\n{text}")

    # 明细：不可达的两种性质用**不同**文案（一个叫你等，一个叫你查网络）
    down = _snap(reachable=False, kind=SLP_UNREACHABLE, error="SLP 连不上")
    text = render_detail(down, _tgt("gtnh", "standalone", "GTNH"), max_names=5)
    ok = "连不上了" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  不可达 → 说「连不上了」（指向网络）")
    bad = _snap(reachable=False, kind=SLP_UNPARSEABLE, error="SLP 应答无法解析")
    text = render_detail(bad, _tgt("gtnh", "standalone", "GTNH"), max_names=5)
    ok = "应答异常" in text and "连不上了" not in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  应答无法解析 → 说「应答异常」（指向服务端加载）")

    # 代理的明细不能打出「在线名单（N 人）：」然后一个人名都不列 —— 看着像名单丢了
    text = render_detail(_snap(count=7), _tgt("proxy", "proxy", "主服群"), max_names=5)
    ok = "全群组" in text and "在线名单" not in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  代理明细不假装有分服名单")
    if not ok:
        print(f"         实际:\n{text}")

    # 总览：每个目标都出现，不可达带 😵
    rows = [
        Row(_tgt("gtnh", "standalone", "GTNH"), _snap(count=2, names=["A", "B"])),
        Row(_tgt("bingo", name="Bingo"), _snap(reachable=False, kind=SLP_UNREACHABLE)),
        Row(_tgt("backstabbed", name="谁是杀手"), _snap(count=1, names=["C"])),
    ]
    text = render_summary(rows, total_names=50, all_targets=[r.target for r in rows])
    ok = all(n in text for n in ("GTNH", "Bingo", "谁是杀手")) and "😵" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  总览里每个目标都出现、不可达带 😵")
    if not ok:
        print(f"         实际:\n{text}")

    # 总览的合计**只算分服**：代理的总人数已经把子服包含了一次，加进去就是算两遍
    rows = [
        Row(_tgt("proxy", "proxy", "主服群"), _snap(count=3)),
        Row(_tgt("bingo", name="Bingo"), _snap(count=3, names=["A", "B", "C"])),
    ]
    text = render_summary(rows, total_names=50, all_targets=[r.target for r in rows])
    ok = "🗺️ MC 在线总览：3 人／1 台服" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  总览合计不把代理算两遍（3 人／1 台服）")
    if not ok:
        print(f"         实际首行: {text.splitlines()[0]!r}")

    # 代理口径提示：只在子服 ≥2 台且数字对不上时出现（1 台时两种成因结果相同，说了是误导）
    def _proxy_rows(
        pcount: int, bcounts: list[int], *, all_targets: list[ServerTarget] | None = None
    ) -> tuple[list[Row], list[ServerTarget]]:
        rows = [
            Row(_tgt("proxy", "proxy", "主服群"), _snap(count=pcount)),
            *[
                Row(_tgt(f"b{i}", name=f"子服{i}"), _snap(count=c, names=["N"] * c))
                for i, c in enumerate(bcounts)
            ],
        ]
        return rows, all_targets if all_targets is not None else [r.target for r in rows]

    rows, at = _proxy_rows(1, [3, 4])
    text = render_summary(rows, total_names=50, all_targets=at)  # 1 != 3+4 → 提示
    ok = "ping-passthrough" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  代理总数与子服之和不符 → 出脚注")
    if not ok:
        print(f"         实际:\n{text}")

    rows, at = _proxy_rows(7, [3, 4])
    text = render_summary(rows, total_names=50, all_targets=at)  # 7 == 3+4 → 不提示
    ok = "ping-passthrough" not in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  数字对得上 → 不出脚注（不制造噪音）")

    rows, at = _proxy_rows(99, [3])
    text = render_summary(rows, total_names=50, all_targets=at)  # 只有 1 台子服 → 不提示
    ok = "ping-passthrough" not in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  只有 1 台子服 → 不出脚注（两种成因分不出来）")

    # ↓ 本次新增：**本群只关联到部分子服**时不许出这条脚注。
    #   代理报的是全群组总人数，本群看不到的那几台的人数照样算在里面 —— 此时
    #   「代理总数 ≠ 本群所见子服之和」是关联方式的必然结果，不是故障。不加这个判断，
    #   每次总览都会挂一条把人指去查 ping-passthrough 的误导提示。
    rows, _ = _proxy_rows(1, [3, 4])
    extra = _tgt("b9", name="别人的子服")  # 全量表里有、但本群关联里没有
    at = [r.target for r in rows] + [extra]
    text = render_summary(rows, total_names=50, all_targets=at)
    ok = "ping-passthrough" not in text and "b9" not in text and "别人的子服" not in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  本群只关联到部分子服 → 不出脚注、也不显示别人的服")
    if not ok:
        print(f"         实际:\n{text}")

    # 反向：同组子服**都在**本群关联里、数字却对不上 → 这条脚注必须照出。
    # 和上一条成对，否则「干脆永不出脚注」也能骗过测试。
    rows, _ = _proxy_rows(1, [3, 4])
    at = [r.target for r in rows]
    text = render_summary(rows, total_names=50, all_targets=at)
    ok = "ping-passthrough" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  同组子服全在本群关联里且数字对不上 → 照出脚注")

    # ↓ 接口型代理**永不**出这条脚注，哪怕数字对不上、子服也一台不缺。两条理由缺一不可：
    #   1. 要抓的是 ping-passthrough（代理把自己某一台后端的人数当全群组报）—— 那是
    #      SLP 才有的毛病；接口给的是代理自己的权威计数，不存在这个成因。
    #   2. 接口的 /status 会列出**全部**子服，而我们只挂关心的那几台（对方 6 台挂 3 台）。
    #      只要有人站在没挂的那台里（大厅、limbo 最容易），合计就永远对不上，
    #      每张总览都会挂一条把人指去查不存在问题的提示。
    api_proxy = ServerTarget(
        id="szu", name="主服群", kind="proxy", group="主服群",
        timeout=5.0, rcon_timeout=5.0,
        api=ApiSpec(url="http://127.0.0.1:8080", token="t"),
    )
    rows = [
        Row(api_proxy, _snap(count=1)),
        Row(_tgt("b0", name="子服0"), _snap(count=3, names=["N"] * 3)),
        Row(_tgt("b1", name="子服1"), _snap(count=4, names=["N"] * 4)),
    ]
    text = render_summary(rows, total_names=50, all_targets=[r.target for r in rows])
    ok = "ping-passthrough" not in text and "全群组 1 人" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口型代理：数字对不上也不出脚注（口径本来就不该比）")
    if not ok:
        print(f"         实际:\n{text}")

    # ↓ 本次最该钉住的一条：目标多 + 名字长时**尾注必须活下来**。
    #   原来的写法是渲染完直接 truncate()，切掉尾部 —— 尾注和最后几台服一起消失。
    long_names = [f"Player_{i:02d}_" + "x" * 30 for i in range(12)]
    rows = [
        Row(_tgt(f"s{i}", name=f"子服{i}"),
            _snap(count=12, names=long_names, complete=(i != 0), error="测试"))
        for i in range(5)
    ]
    text = render_summary(rows, total_names=50, all_targets=[r.target for r in rows])
    kept = text.count("  • ")
    ok = (
        len(text) <= MAX_LEN
        and "⚠️ 名单不完整" in text
        and all(f"子服{i}" in text for i in range(5))
        # 缩减额度**不能一路砍到 0**：那样长度是合格了，但一个人名都不显示。
        # 5 个目标各留至少 1 个名字才算「尽力显示了」。
        and kept >= 5
    )
    failed += not ok
    print(
        f"   {'PASS' if ok else 'FAIL'}  超长时保尾注与所有目标"
        f"（{len(text)} 字 ≤ {MAX_LEN}，尾注{'在' if '⚠️ 名单不完整' in text else '**被吃掉了**'}，"
        f"仍显示 {kept} 个名字）"
    )
    if not ok:
        print(f"         实际尾部: {text[-160:]!r}")

    # 跨组之间的虚线（2026-09-23 用户要求：模组服 GTNH 与群组服分开）。
    # 判据是 group 变化，所以这里造一条「GTNH 独占一组 + 群组服的代理与子服同组」的表，
    # 真实部署就是这个形状。断言不用具体线型，只认「一条只由 - 和空格组成的长行」——
    # 线型是审美，位置才是行为。
    def _seps(text: str) -> list[str]:
        return [
            ln
            for ln in text.splitlines()
            if len(ln) >= 8 and "-" in ln and set(ln) <= {"-", " "}
        ]

    rows = [
        Row(_tgt("gtnh", "standalone", "GTNH", group="gtnh"), _snap(count=1, names=["A"])),
        # 代理报 2 人 = 两台子服之和 → 尾注不触发。这是刻意的：下面数空行条数时，
        # 尾注前那条空行会混进来，而这条断言要量的只是**块间距**。
        Row(_tgt("szu", "proxy", "群组服", group="群组服"), _snap(count=2)),
        Row(_tgt("lobby", name="大厅", group="群组服"), _snap(count=1, names=["A"])),
    ]
    text = render_summary(rows, total_names=50, all_targets=[r.target for r in rows])
    lines = text.splitlines()
    seps = _seps(text)

    def _idx(prefix: str) -> int:
        return next(n for n, ln in enumerate(lines) if ln.startswith(prefix))

    # 位置也要卡：只数条数的话，把线画在表头之后、或整条消息最末尾（同样是「一条线」）
    # 也能过。顺序必须是 GTNH → 线 → 群组服那一段。
    ok = (
        len(seps) == 1
        and _idx("【GTNH】") < lines.index(seps[0]) < _idx("【群组服】") < _idx("【大厅】")
        # 块之间不再空行（2026-09-23 用户：太长）。全线**只剩表头后那一条** ——
        # 不钉的话，谁把块间空行加回来这条也照过，而组的形状正是这次要定的东西。
        and lines[1] == ""
        and lines.count("") == 1
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  跨组之间画一条虚线，且在两组的分界上")
    if not ok:
        print(f"         实际:\n{text}")

    # 反向：**只有一组**时不许画线。不钉这条的话，「每个目标之间都画一条」也能让
    # 上面那条过 —— 而那样只挂群组服的群里会变成一行一个横杠。
    rows = [
        Row(_tgt("szu", "proxy", "群组服", group="群组服"), _snap(count=1)),
        Row(_tgt("lobby", name="大厅", group="群组服"), _snap(count=1, names=["A"])),
    ]
    text = render_summary(rows, total_names=50, all_targets=[r.target for r in rows])
    ok = not _seps(text)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  只有一组时不画线（不然每台服之间都是横杠）")
    if not ok:
        print(f"         实际:\n{text}")

    # 空目标表不能炸：[[audience]].targets 写成了 []（建筑群就是这种）。
    # 文案必须与 mc_stats 短路返回的那条**是同一句** —— 两处各写一份一定会漂，
    # 于是同一个群在「总览」和「明细」两条路径上看到两种说法。
    from plugins_napcat._shared.mcrender import NO_AUDIENCE, NO_TARGETS

    text = render_summary([], total_names=50, all_targets=[])
    ok = text == NO_TARGETS and text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  空目标表 → 复用 NO_TARGETS 那句（不是空消息）")
    if not ok:
        print(f"         实际: {text!r}")
    # 「没开通」与「开通了但没服」是两件事，群里要能分辨：混成一句的话，建筑群
    # 会被告知「本群还没有开通此功能」，而它明明是开通的、只是还没服。
    # 只断言「还没有开通」这四个字：NO_AUDIENCE 后面跟的是「此功能」还是别的，
    # 是文案层面的事，这句测的是「两条路没混成一句」。
    ok = NO_TARGETS != NO_AUDIENCE and "还没接入" in NO_TARGETS and "还没有开通" in NO_AUDIENCE
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  「没关联服」与「没开通」是两句不同的话")
    print()

    # ------------- 多台白名单服的 list 渲染（`whitelist list` 不点名时）-------------
    # 与总览/播报共用 _fit 的意义就在这一块：3 台 × 50 个名字正好踩 MAX_LEN，而
    # truncate 切的是尾部 —— 切掉的正是最后几台的**名单**，消息看着完全正常。
    # 渲染层不能记日志，所以「截断了」只能靠第二个返回值传出去给调用方吼一声。
    print("== 多台白名单 list 渲染自测 ==")
    from plugins_napcat._shared.mcrender import WhitelistListRow, render_whitelist_list

    rows = [
        WhitelistListRow("Bingo", ("阿伟", "小明")),
        WhitelistListRow("GTNH", ("Steve",)),
        WhitelistListRow("谁是杀手", ("乙", "丙", "丁")),
    ]
    text, clipped = render_whitelist_list(rows, total_names=6)
    ok = (
        not clipped
        and "（3 台服）" in text
        and all(f"【{r.name}】" in text for r in rows)
        and all(n in text for r in rows for n in r.names)
        and "【GTNH】（1 人）" in text
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  三台按顺序各一块、每台人数正确、无截断（{len(text)} 字）")
    if not ok:
        print(f"         实际: {text!r}")

    # 一台读不到**不影响其余台**：整条回「执行失败」会把已经查到的两台一起丢掉，
    # 而那是「把没发生的事报成没发生」—— 与把没发生的报成发生同样不准。
    text, _ = render_whitelist_list(
        [
            WhitelistListRow("Bingo", ("阿伟",)),
            WhitelistListRow("GTNH", None, "两条通道都没配，这台的名单读不到"),
            WhitelistListRow("谁是杀手", ("乙",)),
        ],
        total_names=2,
    )
    ok = (
        "阿伟" in text
        and "乙" in text
        and "两条通道都没配" in text
        and "【GTNH】⚠️" in text
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  一台读不到 → 那台一行说明，其余台照常列出")
    if not ok:
        print(f"         实际: {text!r}")

    # 「三台都读不到」与「三台都是空名单」必须是两句不同的话：混成一句的话，全崩
    # 会被读成「服务器上一个人都没有」，而空名单才是真的没人。
    down = render_whitelist_list([WhitelistListRow("Bingo", None, "连不上")], total_names=0)[0]
    empty = render_whitelist_list([WhitelistListRow("Bingo", ())], total_names=0)[0]
    ok = down != empty and "连不上" in down and "（0 人）" in empty
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  「名单没读到」与「名单为空」不是同一句话")
    if not ok:
        print(f"         读不到: {down!r}\n         空名单: {empty!r}")

    # 单台（兜底路径）沿用旧表头：群里单台时那句是 @bot mc 一直以来的样子
    text, _ = render_whitelist_list([WhitelistListRow("Bingo", ("阿伟",))], total_names=1)
    ok = text.startswith("📋 MC 玩家白名单：") and "台服" not in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  单台时沿用旧表头（不带「N 台服」）")

    long_names = tuple(f"Player_{i:02d}_" + "x" * 30 for i in range(50))
    rows = [WhitelistListRow(f"子服{i}", long_names) for i in range(3)]
    text, clipped = render_whitelist_list(rows, total_names=150)
    ok = (
        clipped
        and len(text) <= MAX_LEN
        # 服名一个都不能少：它们是被截断时最该留下的东西（人名可以少几个，
        # 「哪几台查到了」不行）。
        and all(f"【子服{i}】" in text for i in range(3))
    )
    failed += not ok
    print(
        f"   {'PASS' if ok else 'FAIL'}  3 台 × 50 个长名字 → 不超长、服名全在、如实报告截断"
        f"（{len(text)} 字 ≤ {MAX_LEN}，clipped={clipped}）"
    )
    if not ok:
        print(f"         实际尾部: {text[-160:]!r}")
    print()

    # ---------------- 块 15：mcs_audiences.toml 解析与校验 ----------------
    # 这是「一个群看哪几台服」的唯一来源。它坏了的表现是**群里的行为悄悄变样**：
    # 拼错的键（groups 写成 group）会让那个群永远收不到 MC 回复，而日志里一条线索
    # 都没有；一个群写进两条关联则会让「该看哪台」无从判断。所以两者都必须报错。
    print("== mcs_audiences.toml 解析与校验自测 ==")
    from plugins_napcat._shared.mcaudiences import (
        _cross_warnings,
        parse_audiences,
    )

    _full = parse_book(_SRV_REAL)  # 全量表：proxy / bingo / backstabbed / gtnh

    # 贴近真实：社团群看代理 + 两台子服 + 独立服，白名单发给代理与 Bingo 两台
    # （代理用 Global Whitelist 的前缀 globalwhitelist，Bingo 省掉 command 用默认值），
    # 两个推送开关都开；建筑群的组服还没搭，targets = [] 且不开推送，群号用字符串写
    # （两种写法都要认）
    _AUD_REAL = """
[[audience]]
name      = "社团群"
groups    = [11111111]
targets   = ["proxy", "bingo", "backstabbed", "gtnh"]
primary   = "bingo"
whitelist = [
  { target = "proxy", command = "globalwhitelist" },
  { target = "bingo" },
]
watch     = true
report    = true

[[audience]]
name    = "建筑群"
groups  = ["22222222"]
targets = []
"""
    try:
        auds = parse_audiences(_AUD_REAL, _full)
    except ServerConfigError as exc:
        failed += 1
        auds = ()
        print(f"   FAIL  真实拓扑的群关联：应通过却报错 —— {exc}")
    else:
        print("   PASS  真实拓扑的群关联（1 条带白名单 + 1 条空 targets）")

    if auds:
        club, arch = auds
        ok = club.name == "社团群" and club.groups == ("11111111",) and arch.groups == ("22222222",)
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  群号归一化成字符串（整数与字符串写法都认）")

        # 白名单路由（哪几台 + 各自的命令前缀）从**本条关联**读出来 —— 这正是它从
        # 全量表搬走的原因。顺序 = 配置里的书写顺序，前缀逐台独立（第二台省了 command）。
        routes = club.book.whitelist_routes
        ok = (
            [r.target.id for r in routes] == ["proxy", "bingo"]
            and [r.command for r in routes] == ["globalwhitelist", "whitelist"]
        )
        failed += not ok
        print(
            f"   {'PASS' if ok else 'FAIL'}  白名单路由与逐台前缀从本条关联读出"
            f"（{[(r.target.id, r.command) for r in routes]}）"
        )
        # 全量表里没有白名单这回事（`primary` 同理）—— 写进 docstring 就要钉住，
        # 否则「全量表也能查到白名单服」会变成一条没人验证过的承诺。
        ok = _full.whitelist_routes == ()
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  全量表（parse_book）里没有白名单路由")

        ok = club.book.primary_target is not None and club.book.primary_target.id == "bingo"
        failed += not ok
        print(
            f"   {'PASS' if ok else 'FAIL'}  主服从本条关联读出"
            f"（{getattr(club.book.primary_target, 'id', None)}）"
        )

        # 顺序：主服排最前，其余保持本条的 targets 顺序（不是全量表的顺序）
        order = [t.id for t in club.book.targets_primary_first]
        ok = order == ["bingo", "proxy", "backstabbed", "gtnh"]
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  主服排最前：{order}（期望 ['bingo', 'proxy', 'backstabbed', 'gtnh']）")

        # 建筑群 targets 为空是**合法**的（群先建着、服还没搭），只给一条警告
        ok = not arch.book.targets and arch.book.primary_target is None
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  空 targets 合法：无目标、无主服，且不报错")
        ok = any("没关联任何服务器" in w for w in arch.warnings)
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  空 targets 给一条警告（合法但要让人知道）")
        if not ok:
            print(f"         实际警告: {arch.warnings}")

        # 推送开关从本条读出；不写 = 不收推送（默认关，不影响存量配置的行为）
        ok = club.watch is True and club.report is True
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  watch / report 从本条关联读出（{club.watch} / {club.report}）")
        ok = arch.watch is False and arch.report is False
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  不写开关 = 不收推送（缺省关，加群不会顺手开始刷屏）")

        # 一句话说清「这条收不收推送」：两个都不开时群里安安静静，和「机器人挂了」
        # 长得一模一样，只有启动日志这一行分得出来。
        ok = "进服提醒" in club.flags_summary and "定时播报" in club.flags_summary
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  开关摘要列出两项（{club.flags_summary}）")
        ok = "不推送" in arch.flags_summary
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  都没开时摘要直说「不推送」（{arch.flags_summary}）")
    print()

    # 报错用例：(说明, 群关联 toml, 期望错误信息里出现的关键词)
    _AUD_BAD: list[tuple[str, str, str]] = [
        ("一条 [[audience]] 都没有", "", "至少要有一条"),
        (
            "未知键（groups 拼成单数 group）",
            '[[audience]]\nname="a"\ngroup=[1]\n',
            "不认识的键",
        ),
        ("缺 name", '[[audience]]\ngroups=[1]\n', "不能为空"),
        ("name 是空白串", '[[audience]]\nname="   "\ngroups=[1]\n', "不能为空"),
        ("缺 groups", '[[audience]]\nname="a"\ntargets=[]\n', "缺 groups"),
        ("groups 是空数组", '[[audience]]\nname="a"\ngroups=[]\n', "非空数组"),
        ("groups 写成标量（没加方括号）", '[[audience]]\nname="a"\ngroups=111\n', "非空数组"),
        ("groups 里写 bool", '[[audience]]\nname="a"\ngroups=[true]\n', "只能写群号"),
        (
            "groups 里写浮点数",
            '[[audience]]\nname="a"\ngroups=[1.5]\n',
            "只能写群号",
        ),
        ("同一条里群号重复", '[[audience]]\nname="a"\ngroups=[1,1]\n', "重复了"),
        (
            "一个群出现在两条关联里",
            '[[audience]]\nname="a"\ngroups=[7]\n'
            '[[audience]]\nname="b"\ngroups=[7]\n',
            "只能关联一组服务器",
        ),
        (
            "targets 指向不存在的 id",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["nope"]\n',
            "不存在的目标",
        ),
        (
            "targets 里 id 重复",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh","gtnh"]\n',
            "重复了",
        ),
        (
            "primary 不在本条 targets 里（但在全量表里）",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\nprimary="bingo"\n',
            "不在本条的 targets 里",
        ),
        (
            "targets 为空却写了 primary",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=[]\nprimary="gtnh"\n',
            "不在本条的 targets 里",
        ),
        (
            "whitelist.target 不在本条 targets 里",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\n'
            'whitelist=[{target="bingo"}]\n',
            "不在本条的 targets 里",
        ),
        # 单表写法（P6 之前的唯一形态）现在**必须报错**，且报错要带可照抄的改法。
        # 静默当成一台会让「配了两台的群其实只有一台在管」无声发生。
        (
            "whitelist 写成单表（旧写法）",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\n'
            'whitelist={target="gtnh"}\n',
            "一组表",
        ),
        (
            "whitelist 写成字符串",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\nwhitelist="proxy"\n',
            "一组表",
        ),
        (
            "whitelist 是空数组",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\nwhitelist=[]\n',
            "空数组",
        ),
        (
            "whitelist 数组元素不是表",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\nwhitelist=["gtnh"]\n',
            "第 1 项必须是一个表",
        ),
        (
            "whitelist 第 2 项里有未知键",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh","bingo"]\n'
            'whitelist=[{target="gtnh"},{target="bingo",cmd="x"}]\n',
            "不认识的键",
        ),
        # 同一台服配两项 = 它有两个前缀，下命令时用哪个无从判断 —— 报错而不是取第一个。
        (
            "whitelist 里同一台服出现两次",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\n'
            'whitelist=[{target="gtnh"},{target="gtnh",command="x"}]\n',
            "出现了两次",
        ),
        # 第二条是**第 2 项**坏：错误里必须点名第 2 项，否则「两台里坏了一台」时
        # 看日志会以为是第一台（诊断成本翻倍）。
        (
            "whitelist 第 2 项的 target 不在本条 targets 里",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\n'
            'whitelist=[{target="gtnh"},{target="bingo"}]\n',
            "第 2 项",
        ),
        # 布尔键只认 TOML 的真布尔。字符串真值必须**报错**：静默当真会让这个群莫名
        # 收到推送，静默当假就是「配置写了没生效」，两种都正是本工程最防的那类键。
        (
            "watch 写成字符串（TOML 里 true 不加引号）",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\nwatch="true"\n',
            "必须是 true / false",
        ),
        # 反方向：isinstance(True, int) 是真的，但 1 不是 bool。拿整数当开关一并挡掉，
        # 和 groups 那边专挡 isinstance(item, bool) 是同一个坑的两面。
        (
            "watch 写成 1",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\nwatch=1\n',
            "必须是 true / false",
        ),
        # 两个开关都要校验，不能只做 watch
        (
            "report 写成 \"yes\"",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\nreport="yes"\n',
            "必须是 true / false",
        ),
        # 关联名是日志和诊断里指代一条关联的唯一标识，重名之后看日志分不清说的是哪条
        (
            "两条关联重名",
            '[[audience]]\nname="a"\ngroups=[1]\n'
            '[[audience]]\nname="a"\ngroups=[2]\n',
            "重复了",
        ),
        ("语法错误", "[[audience]\nname=\n", "语法错误"),
    ]
    for desc, text, needle in _AUD_BAD:
        try:
            parse_audiences(text, _full)
        except ServerConfigError as exc:
            ok = needle in str(exc)
            failed += not ok
            print(f"   {'PASS' if ok else 'FAIL'}  {desc}")
            if not ok:
                print(f"         期望错误里含 {needle!r}，实际是 {exc}")
        else:
            failed += 1
            print(f"   FAIL  {desc}：应报错却通过了")

    # command 逐台独立（省略 → 默认前缀）；whitelist.target 留空串 = 本群不管白名单（合法）
    auds = parse_audiences(
        '[[audience]]\nname="a"\ngroups=[1]\ntargets=["bingo","proxy"]\n'
        'whitelist=[{target="proxy"},{target="bingo",command="globalwhitelist"}]\n'
        '[[audience]]\nname="b"\ngroups=[2]\n'
        '[[audience]]\nname="c"\ngroups=[3]\ntargets=["gtnh"]\nwhitelist=[{target=""}]\n',
        _full,
    )
    a_routes = auds[0].book.whitelist_routes
    ok = [(r.target.id, r.command) for r in a_routes] == [
        ("proxy", "whitelist"),
        ("bingo", "globalwhitelist"),
    ]
    failed += not ok
    print(
        f"   {'PASS' if ok else 'FAIL'}  command 逐台独立、省略用默认、顺序=书写顺序"
        f"（{[(r.target.id, r.command) for r in a_routes]}）"
    )
    ok = auds[1].book.whitelist_routes == () and auds[2].book.whitelist_routes == ()
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  没写 whitelist / target 留空串 → 本群不管白名单")

    # 这一句同时进**启动日志**和 `--list-audiences`，是「这台服的白名单发给谁」的唯一
    # 可见来源。单台必须**逐字**沿用 P6 之前的文案（绝大多数群是单台，DEPLOY 引用过它）；
    # 多台必须逐台列出前缀 —— 只说「发往 2 台」等于把最容易写错的那个字段藏起来。
    ok = auds[1].whitelist_summary == "不提供白名单管理"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  没配白名单 → 摘要直说「不提供白名单管理」")
    got = parse_audiences(
        '[[audience]]\nname="a"\ngroups=[1]\ntargets=["bingo"]\nwhitelist=[{target="bingo"}]\n',
        _full,
    )[0].whitelist_summary
    ok = got == "白名单发往 Bingo[bingo]（命令前缀 'whitelist'）"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  单台摘要逐字沿用旧文案（{got}）")

    multi_summary = parse_audiences(
        '[[audience]]\nname="a"\ngroups=[1]\ntargets=["bingo","backstabbed"]\n'
        'whitelist=[{target="bingo"},{target="backstabbed"}]\n',
        _full,
    )[0].whitelist_summary
    ok = (
        multi_summary.startswith("白名单发往 2 台：")
        and "Bingo[bingo]（命令前缀 'whitelist'）" in multi_summary
        # 第二台两条通道都没配（backstabbed 在全量表里就没有）—— 摘要里必须说出来，
        # 否则它和一台好服长得一模一样，而群里每次命令都会回「发不出去」。
        and "两条通道都没配" in multi_summary
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  多台摘要逐台列前缀并点名没配密码的那台（{multi_summary}）")
    # 只写 command 不写 target：等于白名单整个没配，形态一眼看不出来，必须给警告
    auds = parse_audiences(
        '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\n'
        'whitelist=[{command="globalwhitelist"}]\n',
        _full,
    )
    ok = auds[0].book.whitelist_routes == () and any("只写了 command" in w for w in auds[0].warnings)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  只写 command 没写 target → 不给白名单 + 一条警告")
    if not ok:
        print(f"         实际警告: {auds[0].warnings}")
    # 部分项只写 command：**只丢那一项**，合法的照常生效，且警告要点名被丢的是第几项、
    # 并且把还活着的列出来 —— 否则「两台里有一项没生效」只能靠人去数配置。
    auds = parse_audiences(
        '[[audience]]\nname="a"\ngroups=[1]\ntargets=["bingo","gtnh"]\n'
        'whitelist=[{target="bingo"},{command="globalwhitelist"}]\n',
        _full,
    )
    routes = auds[0].book.whitelist_routes
    hit = [w for w in auds[0].warnings if "第 2 项" in w and "只写了 command" in w]
    ok = (
        [r.target.id for r in routes] == ["bingo"]
        and len(hit) == 1
        and "本群的白名单服只有：Bingo" in hit[0]
        and "第 1 项" not in hit[0]
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  多台里只坏一项 → 合法的仍生效，警告点名第 2 项")
    if not ok:
        print(f"         实际路由: {[r.target.id for r in routes]}  实际警告: {auds[0].warnings}")
    # 白名单服两条通道都没配：能加载，但命令发不出去（backstabbed 在全量表里就没配）
    auds = parse_audiences(
        '[[audience]]\nname="a"\ngroups=[1]\ntargets=["backstabbed"]\n'
        'whitelist=[{target="backstabbed"}]\n',
        _full,
    )
    ok = any("两条通道都没配" in w for w in auds[0].warnings)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  白名单服两条通道都没配 → 警告（命令发不出去）")
    # **逐台**报：两台里只有一台没配通道时，警告必须只点那一台。写成「只看第一台」的话
    # 这条测试照样过（Bingo 有 RCON → 没警告），所以断言里必须同时出现「有警告」和
    # 「警告里不含另一台的名字」两半。
    auds = parse_audiences(
        '[[audience]]\nname="a"\ngroups=[1]\ntargets=["bingo","backstabbed"]\n'
        'whitelist=[{target="bingo"},{target="backstabbed"}]\n',
        _full,
    )
    hit = [w for w in auds[0].warnings if "两条通道都没配" in w]
    ok = len(hit) == 1 and "谁是杀手" in hit[0] and "Bingo" not in hit[0]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  两台里只坏一台 → 警告只点那一台（不含另一台）")
    if not ok:
        print(f"         实际警告: {auds[0].warnings}")

    # 开了推送却没有服可盯：**警告而不是报错**。它不是「被忽略的配置键」—— 我们读懂了
    # 它，并且明确说出它为什么没有输出；而硬报错会让整台 bot 起不来（ServerConfigError
    # 一路冒到 main），代价与收益不成比例。targets 填好之后开关自动生效。
    auds = parse_audiences(
        '[[audience]]\nname="a"\ngroups=[1]\ntargets=[]\nwatch=true\nreport=true\n',
        _full,
    )
    ok = any("watch" in w and "report" in w and "不会有任何输出" in w for w in auds[0].warnings)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  空 targets 却开了推送 → 警告（两个开关合成一句）")
    if not ok:
        print(f"         实际警告: {auds[0].warnings}")
    ok = auds[0].watch is True and auds[0].report is True
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  空 targets 时开关照样读出来（只警告，不把它当没写）")
    print()

    # ---------------- 块 16：群号 → 关联 / 投影视图 ----------------
    # 本次的核心不变量：**关联外的服名必须查不到**。不成立的话建筑群能查到社团群的服，
    # 「两个群各看各的」就是句空话，而且不会报任何错。同时钉住投影必须重建 _by_id ——
    # 漏了它 get() 全返回 None，症状是「查询正常、白名单却总说不可用」。
    print("== 群号 → 关联查找 / 投影视图自测 ==")
    from plugins_napcat._shared.mcaudiences import McConfig

    # 重新解析一份干净的 _AUD_REAL：上面那些用例已经把 auds 覆写成了别的形状
    config = McConfig(book=_full, audiences=parse_audiences(_AUD_REAL, _full))
    club = config.for_group(11111111)
    arch = config.for_group("22222222")

    ok = club is not None and club.name == "社团群"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  整数群号 → 命中「社团群」")
    ok = arch is not None and arch.name == "建筑群"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  字符串群号 → 命中「建筑群」（事件里是 int，配置里两种都写得出）")
    ok = config.for_group(99999999) is None
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  没被任何关联提到的群 → None（调用方据此回绝并打日志）")
    ok = config.known_groups == ("11111111", "22222222")
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  known_groups 汇总全部群号：{config.known_groups}")

    # flag_targets：开了某个开关的关联**覆盖到的全部目标**，去重、保首次出现顺序。
    # 这个数字直接进启动日志（「实际探测 N 台」）和 round_budget 的上界模型，
    # 所以「去重」和「顺序」两条都得钉死：
    #   - 不去重 → 同一台服按关联数重复计入，日志里的 N 偏大，上界也跟着偏大；
    #   - 顺序不对 → 一轮里的探测顺序每轮乱跳，基线与事件的对应关系看着像随机的。
    # 只测纯函数（不碰网络、不读配置文件），所以拿 _AUD_REAL 和一份合成关联就够。
    wt = config.flag_targets("watch")
    ok = [t.id for t in wt] == ["proxy", "bingo", "backstabbed", "gtnh"]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  flag_targets('watch') 取到社团群那条的目标，保序：{[t.id for t in wt]}")
    if not ok:
        print(f"         期望 ['proxy', 'bingo', 'backstabbed', 'gtnh']")

    # report 也开在**同一条**关联上，所以这里必须与 watch 得到同一个答案 ——
    # 两个开关走的是同一段代码，答案不同就说明有一个把关联筛错了。
    ok = config.flag_targets("report") == wt
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  flag_targets('report') 与 watch 结果一致（两个开关同源）")

    # 建筑群 targets=[]、两个开关都没开 → 它两边都不该出现；空 targets 那条就算开了也不贡献目标
    ok = all(t.id in {x.id for x in club.book.targets} for t in wt)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  watch 的目标全部落在社团群自己的投影里（没串到建筑群）")

    # 缺省不推送：不写 watch 的关联在 flag_targets 里一个目标都不贡献。
    # 这条是「新加一条关联不会悄悄开始往那个群发消息」的守门断言。
    _quiet = McConfig(
        book=_full,
        audiences=parse_audiences('[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\n', _full),
    )
    ok = _quiet.flag_targets("watch") == () and _quiet.flag_targets("report") == ()
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  不写开关 → 该条不贡献任何目标（缺省不推送）")

    _AUD_DEDUP = """
[[audience]]
name    = "甲群"
groups  = [1]
targets = ["bingo", "gtnh"]
watch   = true

[[audience]]
name    = "乙群"
groups  = [2]
targets = ["gtnh", "bingo"]
watch   = true
"""
    dedup = McConfig(book=_full, audiences=parse_audiences(_AUD_DEDUP, _full))
    got = [t.id for t in dedup.flag_targets("watch")]
    # bingo 在两条关联里都有 → 只出现一次；且 bingo 在 gtnh 之前，是甲群的书写顺序。
    # 若实现按字母序排，这里会变成 ['bingo','gtnh'] 而**恰好也通过** —— 所以故意让
    # 甲群写成 bingo 在前、乙群反过来，两个「谁先」的答案在两条关联里不一致，
    # 只有这样才排除掉「碰巧」。
    ok = got == ["bingo", "gtnh"]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  两条关联共享 bingo → 去重且取首次出现顺序：{got}")

    ok = dedup.flag_targets("report") == ()
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  没有任何关联开 report → 空元组（据此不打预算告警）")

    # 返回的是真 ServerTarget 对象（不是 id 字符串）：调用方要拿它去 get_snapshots 和取服名
    ok = all(hasattr(t, "name") for t in wt) and dedup.flag_targets("watch")[0].id == "bingo"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  返回 ServerTarget 本身（能取服名，能直接喂 get_snapshots）")

    # 投影必须重建 _by_id：get() 对本条关联的每个 id 都非 None
    assert club is not None and arch is not None
    missing = [t.id for t in club.book.targets if club.book.get(t.id) is None]
    ok = not missing
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  投影 book 的 get(id) 对本条每个目标都非 None")
    if not ok:
        print(f"         查不到的: {missing}")

    # ↓ 跨关联隔离：建筑群看不到社团群的服。这是刻意的，不是 bug。
    for query in ("gtnh", "bingo", "proxy", "backstab"):
        res = arch.book.resolve(query)
        ok = not res.ok and not res.ambiguous
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  建筑群查社团群的服名 {query!r} → 未知（关联外隔离）")
        if not ok:
            print(f"         实际: ok={res.ok} 候选={res.candidates}")

    # 反向：同一个服名在本条关联内必须照常解析得出（否则上一条可以靠「永远查不到」骗过）
    ok = club.book.resolve("gtnh").ok and club.book.resolve("谁是杀手").ok
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  社团群查自己的服名（id 与中文名）正常命中")

    # scoped() 拿到不在全量表的 id 必须报错，不能静默少投影一台
    try:
        _full.scoped(["gtnh", "nope"])
    except ServerConfigError as exc:
        ok = "nope" in str(exc)
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  scoped() 遇到不存在的 id 报错（不静默少一台）")
    else:
        failed += 1
        print("   FAIL  scoped() 遇到不存在的 id 应报错却通过了")

    # 投影层的两道防御：路由不在本视图 / 同一台服两条路由。两者都到不了（mcaudiences
    # 已经对着全量表校验过），但真到了的话后果是「投影出一个少了白名单服的视图」和
    # 「同一台服用两个前缀、命令只按第一条发」—— 都是静默的，所以宁可炸。
    _bingo = _full.get("bingo")
    for desc, ids, routes in (
        ("路由不在本次投影的 targets 里", ["gtnh"], (WhitelistRoute(target=_bingo),)),
        (
            "同一台服两条白名单路由",
            ["bingo"],
            (WhitelistRoute(target=_bingo), WhitelistRoute(target=_bingo, command="x")),
        ),
    ):
        try:
            _full.scoped(ids, whitelist=routes)
        except ServerConfigError:
            print(f"   PASS  scoped() 拦住「{desc}」")
        else:
            failed += 1
            print(f"   FAIL  scoped() 应拦住「{desc}」却通过了")

    # ---- 「这条命令发给哪台」：pick_whitelist 的六态。B 方案（服名放末尾）的正确性
    # 全靠这一层：认错一台 = 改错服务器的白名单，而且群里不会有任何迹象。
    club_routes = club.book.whitelist_routes
    ok = [r.target.id for r in club_routes] == ["proxy", "bingo"]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  社团群的白名单路由是两台（{ [r.target.id for r in club_routes] }）")

    # 不点名 + 多台 = 必须点名。**没有「默认那台」的回退**：猜错就是改错服。
    pick = club.book.pick_whitelist("")
    ok = pick.reason == PICK_NEED_NAME and pick.candidates == ("proxy", "Bingo")
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  两台不点名 → 要点名（候选 {pick.candidates}）")

    # 点名**第二台**：写成「只认第一条」的实现会让这台永远选不中，而第一台照常能用
    # —— 单台用例全绿、只有真去点第二台的人才发现。这条就是为它存在的。
    pick = club.book.pick_whitelist("Bingo")
    ok = pick.ok and pick.route.target.id == "bingo" and pick.route.command == "whitelist"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  点名第二台命中（{pick.route.target.id if pick.ok else pick.reason}）")

    # 前缀：复用的是 resolve() 那套（NFKC + 别名 + 唯一前缀），不是另写一份比对
    pick = club.book.pick_whitelist("prox")
    ok = pick.ok and pick.route.target.id == "proxy" and pick.route.command == "globalwhitelist"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  唯一前缀命中，且带出这一台自己的前缀"
          f"（{pick.route.command if pick.ok else pick.reason}）")

    # 「b」同时是 bingo 和 backstabbed（含它的别名 backstab）的前缀 —— 候选里出现的是
    # **服名**（谁是杀手）不是 id，因为这两个候选就是要拿给群里的人照着打的。
    pick = club.book.pick_whitelist("b")
    ok = pick.reason == PICK_AMBIGUOUS and set(pick.candidates) == {"Bingo", "谁是杀手"}
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  歧义服名 → 列出候选（{pick.reason} {pick.candidates}）")

    # 本群**没有**这台服：能查的都列出来，否则「打错一个字」和「这台不归本群」分不出来
    pick = club.book.pick_whitelist("nope")
    ok = pick.reason == PICK_UNKNOWN and set(pick.candidates) == {
        "proxy",
        "Bingo",
        "谁是杀手",
        "gtnh",
    }
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  不认识的服名 → 列出本群能查的（{pick.candidates}）")

    # 本群**有**这台服、只是没配白名单。这条最容易写错：直接回「没有叫 gtnh 的服」是
    # **假话**（gtnh 就在本群的 targets 里、@bot mc gtnh 能查到），照着改名字是白费功夫。
    pick = club.book.pick_whitelist("gtnh")
    ok = (
        pick.reason == PICK_NOT_WHITELIST
        and pick.found is not None
        and pick.found.id == "gtnh"
        and pick.candidates == ("proxy", "Bingo")
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  是本群的服但不是白名单服 → 不谎称「没这台服」"
          f"（found={getattr(pick.found, 'id', None)}）")

    # 别名也走同一条路（backstabbed 有别名 backstab）
    pick = club.book.pick_whitelist("backstab")
    ok = pick.reason == PICK_NOT_WHITELIST and pick.found.id == "backstabbed"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  别名同样认出来（{getattr(pick.found, 'id', None)}）")

    # 单台：可以不点名，且**不能用多台的规则要求它点名**（逼人写反而容易写错）
    one = _full.scoped(["bingo"], whitelist=(WhitelistRoute(target=_bingo),))
    pick = one.pick_whitelist("")
    ok = pick.reason == PICK_OK and pick.route.target.id == "bingo"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  单台时不点名直接命中（{pick.reason}）")

    pick = _full.scoped(["gtnh"]).pick_whitelist("")
    ok = pick.reason == PICK_NO_ROUTE and not pick.ok
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  一台白名单服都没有 → NO_ROUTE（{pick.reason}）")

    # 跨关联隔离：建筑群的视图里连 proxy 都不存在，更不可能把它当白名单服
    pick = arch.book.pick_whitelist("proxy")
    ok = pick.reason == PICK_UNKNOWN
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  建筑群点名社团群的服 → 未知（隔离）")

    # 命令前缀**必须来自配置**，不许在命令层写死 "whitelist"。
    # 写死的后果是：代理上线后把 whitelist.command 改成 globalwhitelist **毫无效果**，
    # 而配置、日志、诊断三处都会显示新值 —— 这正是「接受了但忽略掉」的配置键，
    # 比没有这个键更坏（改的人会以为自己已经改好了）。
    # 只能用源码钉住：run_whitelist_command 要活的 RCON，跑不起来。
    import inspect as _inspect

    from plugins_napcat._shared.mcadmin import run_whitelist_command as _run

    hard = [
        line.strip()
        for line in _inspect.getsource(_run).splitlines()
        if '"whitelist ' in line or "'whitelist " in line
    ]
    ok = not hard and "command" in _inspect.signature(_run).parameters
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  白名单命令前缀不硬编码（由配置传入命令层）")
    if not ok:
        print(f"         硬编码的行: {hard}")

    # 同一条约束的**文案那一半**：回复里那句「照着这条命令去服务端查」也不能写死。
    # 这里坏掉的表现特别隐蔽 —— 命令真的发对了（前缀走的是 route.command），只有
    # **提示文案**给出服务端不存在的命令，而那句话正是群友下一步照着做的动作指引。
    #
    # mcs/mc_admin.py **不能 import**（mcs/__init__.py 会拉起持有 matcher 的 mc_reporter，
    # 那需要 nonebot.init()），所以只能当文本读。这也是它和 _shared/mcadmin.py 分家的原因。
    mcsrc = (ROOT / "plugins_napcat" / "mcs" / "mc_admin.py").read_text(encoding="utf-8")

    def _block(src: str, head: str) -> str:
        """抠出一个顶层函数（到下一个顶层 def / **async** def 为止）。

        两个前缀都要找：这个文件里下一个就是 `async def _list_rows`，只找 `\\ndef `
        会一路切到几百行之后的 `handle_admin`，于是把它的日志文案也算进「函数体内」。
        """
        rest = src[src.index(head) + len(head) :]
        ends = [i for i in (rest.find("\ndef "), rest.find("\nasync def ")) if i >= 0]
        return rest[: min(ends)] if ends else rest

    body = _block(mcsrc, "def _build_body(")
    hard = [
        line.strip()
        for line in body.splitlines()
        if '"whitelist ' in line or "'whitelist " in line
    ]
    ok = not hard and "command" in _block(mcsrc, "def _build_message(")
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  回复文案里的命令前缀也不硬编码（由 route.command 传入）")
    if not ok:
        print(f"         硬编码的行: {hard}")

    # 「先认服、再下发」是语义，不是实现细节：顺序反过来就会先执行再报「认不出服名」——
    # 那就是**改了服才说不认识它**。用源码位置比较钉住，因为没有更早的层次能拦住它。
    # 锚点取 handle_admin 里那句完整的下发调用：`_list_rows` 里还有一句同名的（多台
    # list 用），拿函数名去比会被它抢先命中。
    pick_at = mcsrc.index("pick_whitelist(cmd.server)")
    run_at = mcsrc.index("await run_whitelist_command(cmd, route.target, command)")
    ok = pick_at < run_at
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  mc_admin 里「认服名」在「下发命令」之前")

    # 六态里除 OK 外每一态都必须有自己的文案分支：漏掉一态不会报错，它会掉进函数末尾
    # 的兜底里回「本群没有指定白名单服」—— 而那是一句**假话**（本群配了），照着它去
    # 改配置永远改不对。所以把「每一态都被提到」钉住，新增态时这里会先红。
    reply = _block(mcsrc, "def _pick_reply(")
    missing = [k for k in ("PICK_NEED_NAME", "PICK_UNKNOWN", "PICK_AMBIGUOUS", "PICK_NOT_WHITELIST") if k not in reply]
    ok = not missing
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  pick 的每一态都有文案分支（没接到的会谎称「本群没配」）")
    if not ok:
        print(f"         缺: {missing}")

    # 异常原文不许进群，只进日志。已经踩过一次：群里那条「连不上 MC 服务器（RCON）：
    # [WinError 1225] 远程计算机拒绝网络连接。」后面半截是给人看日志的，群里那位既
    # 读不懂也做不了什么。两条 tombstone 钉的是**具体的旧形态**（写不成泛化的「不许用
    # exc」——那既不好写也不好读）。
    leak = [p for p in ("（RCON）：", "认证失败：{exc}") if p in mcsrc]
    ok = not leak
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  群里的 RCON 报错不带异常原文（原文只进日志）")
    if not ok:
        print(f"         还在: {leak}")

    # QQ **不渲染 markdown**：`**加粗**` 会连星号一起原样打出来。这个也踩过一次 ——
    # 群友看到的是「服名要写在**最后**」。所以扫一遍**回复文案**：跳过 docstring 和
    # 整行注释（那些是给读代码的人看的），只扫能发到 QQ 群的模块（oopz 那边走另一个
    # 客户端，不在其列）。扫的是源码文本，以后新加的文案自动被覆盖。
    # 认字符串的办法是「这一行里有引号」，所以 `2**31` 这种运算符不会被误判。
    qq_files = (
        "mcs/mc_admin.py",
        "mcs/mc_stats.py",
        "mcs/mc_reporter.py",
        "mcs/__init__.py",
        "_shared/mcrender.py",
        "_shared/mcaudiences.py",
        "_shared/mcservers.py",
        "hello/__init__.py",
    )
    bold = []
    for rel in qq_files:
        src = (ROOT / "plugins_napcat" / rel).read_text(encoding="utf-8")
        stripped = re.sub(r'"""(?:.|\n)*?"""', "", src)
        for lineno, line in enumerate(stripped.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if "**" in line and ('"' in line or "'" in line):
                bold.append(f"{rel}:{lineno}")
    ok = not bold
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  群回复文案里没有 markdown 加粗（QQ 会连星号一起打出来）")
    if not ok:
        print(f"         {bold}")

    # oopz_sdk **默认随 requirements.txt 装上，但装不装由部署的人定**（只用 MC 功能的
    # 人可以特意不装它）。所以它**不许在模块顶层被 import** —— 那样缺 SDK 时
    # load_plugins("plugins_napcat") 会在导入这棵树时抛
    # ImportError，**连着 MC 功能一起起不来**，而 MC 跟 oopz 毫无关系。
    # 2026-09-22 修的就是这个（改前 `from oopz_sdk import OopzBot` 就明晃晃写在顶层）。
    # 判据是「行首没有缩进的 import」：带 try 兜住 / TYPE_CHECKING 里的那种是缩进的，
    # 不会被这里判到。第二条是防这一条变成空断言 —— 把 oopz 支持整个删掉也能过第一条。
    oopz_src = (ROOT / "plugins_napcat" / "oopz" / "client.py").read_text(encoding="utf-8")
    top_import = [
        ln
        for ln in oopz_src.splitlines()
        if ln.startswith(("import oopz_sdk", "from oopz_sdk"))
    ]
    ok = not top_import
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  oopz_sdk 不在模块顶层被 import（缺它也得能起 MC）")
    if not ok:
        print(f"         {top_import}")

    ok = "\n    import oopz_sdk" in oopz_src
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  运行期探测还在（上一条不是靠删掉 oopz 支持过的）")
    print()

    # ---------------- 块 17：跨文件警告 ----------------
    # 两类「配置没写全但能跑」的情况。它们的共同点是**不吼就查不出来**：
    # 孤儿目标在群里表现为「没有叫 X 的服」，而 --list-targets 里它列得好好的。
    print("== 跨文件警告自测（孤儿目标 / .env 残留）==")
    _orphan = parse_book(
        '[[targets]]\nid="gtnh"\nkind="standalone"\nhost="h"\nport=1\n'
        '[[targets]]\nid="lobby"\nname="大厅"\nkind="backend"\nhost="h"\nport=2\n'
    )
    auds = parse_audiences('[[audience]]\nname="社团群"\ngroups=[1]\ntargets=["gtnh"]\n', _orphan)
    warns = _cross_warnings(_orphan, auds)
    ok = any("没被任何群关联" in w and "大厅" in w for w in warns)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  孤儿目标（加进全量表却没关联给任何群）→ 警告")
    if not ok:
        print(f"         实际警告: {warns}")

    # 全都被关联到就不该有这条警告 —— 否则「永远警告」也能骗过上一条
    auds = parse_audiences(
        '[[audience]]\nname="社团群"\ngroups=[1]\ntargets=["gtnh","lobby"]\n', _orphan
    )
    ok = not any("没被任何群关联" in w for w in _cross_warnings(_orphan, auds))
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  每个目标都被关联到了 → 不出孤儿警告")

    # .env 里已作废的群相关变量：删干净了就不该响；留着要吼一声，因为它的存在会让人
    # 以为它还管着事（MC_ALLOWED_GROUPS 是群范围，另两个是「推给谁」）。
    #
    # 遍历 _LEGACY_GROUP_VARS 而不是写死变量名：将来再搬一个变量走，这里自动就有了
    # 覆盖 —— 写死的话漏一个的表现是「那个变量留在 .env 里，谁也不吭声」，
    # 而它正是这张表存在的全部理由。
    from plugins_napcat._shared.mcaudiences import _LEGACY_GROUP_VARS

    _saved_legacy = {v: os.environ.get(v) for v in _LEGACY_GROUP_VARS}
    try:
        # 一个都不留 → 不该有任何残留警告
        for var in _LEGACY_GROUP_VARS:
            os.environ.pop(var, None)
        ok = not any("已不再生效" in w for w in _cross_warnings(_orphan, auds))
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  .env 里三个已作废变量都不留 → 不出残留警告")

        for var in _LEGACY_GROUP_VARS:
            os.environ[var] = "11111111"
            warns = _cross_warnings(_orphan, auds)
            ok = any(var in w and "已不再生效" in w for w in warns)
            failed += not ok
            print(f"   {'PASS' if ok else 'FAIL'}  .env 里留着 {var} → 警告「已不再生效」")
            if not ok:
                print(f"         实际警告: {warns}")
            os.environ.pop(var, None)

        # 搬走的三个变量里，WATCH / REPORT 的修法**不是**改 groups，而是在关联上加一个
        # 推送开关。警告必须把这句说出来，否则照着「改 groups」去改的人会发现提醒照样
        # 不推，而那时残留警告已经因为他「看过一次」被忽略掉了。
        for var, needle in (("MC_WATCH_GROUP", "watch"), ("MC_REPORT_GROUP", "report")):
            os.environ[var] = "11111111"
            warns = _cross_warnings(_orphan, auds)
            ok = any(var in w and needle in w for w in warns)
            failed += not ok
            print(f"   {'PASS' if ok else 'FAIL'}  {var} 的残留警告指向 [[audience]].{needle}")
            if not ok:
                print(f"         实际警告: {warns}")
            os.environ.pop(var, None)

        # 三个都留着 → 只出**一条**汇总警告，不是三条（日志尾巴要能一眼看完）
        for var in _LEGACY_GROUP_VARS:
            os.environ[var] = "11111111"
        warns = [w for w in _cross_warnings(_orphan, auds) if "已不再生效" in w]
        ok = len(warns) == 1
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  三个都留着 → 合成一条警告（实际 {len(warns)} 条）")
    finally:
        for var, value in _saved_legacy.items():
            os.environ.pop(var, None)
            if value is not None:
                os.environ[var] = value
    print()

    # ---------------- 块 18：变更命令「到底发没发」 ----------------
    # ok is None 有两种来源、含义相反，文案和日志都必须分开说：
    #   前置读就读不懂 → **一条命令都没发**；反查读不懂 → 命令已经发出去了。
    # 混成一句的表现是把「没发出去」报成「命令已发送（未能验证）」—— 又一处
    # 「把没发生的事报成发生了」，而且群友会拿着这句话去服务端找一个不存在的改动。
    #
    # 这一块把 rcon_command 换成假的，钉的是**行为**（发了几条、发的是什么），
    # 不是源码文本。上面块 16 那条源码检查只保证「前缀没写死」，保证不了这个。
    print("== 白名单变更命令的下发判定自测（假 RCON）==")

    from plugins_napcat._shared import mcadmin as _mca

    _tgt = parse_book(_SRV_REAL).get("bingo")
    # 命令前缀写错时服务端的真实回执（同样没有冒号，见块 12 那组案例）
    _JUNK = "Unknown or incomplete command. See below for errornosuchcmd list<--[HERE]"

    async def _run_fake(replies, verb, player="", command="whitelist"):
        """跑一条命令，RCON 换成按顺序吐 replies 的假货。返回 (结果, 实际发出去的命令)。"""
        sent: list[str] = []

        async def fake(target, command):
            sent.append(command)
            return replies[min(len(sent) - 1, len(replies) - 1)]

        origin = _mca.rcon_command
        _mca.rcon_command = fake
        try:
            res = await _mca.run_whitelist_command(
                _mca.AdminCommand(verb, player), _tgt, command
            )
        finally:
            _mca.rcon_command = origin
        return res, sent

    # ① 前置读读不懂 → 一条变更命令都不许发
    res, sent = asyncio.run(_run_fake([_JUNK], "add", "Steve"))
    ok = res.ok is None and not res.mutation_sent and not any("add " in c for c in sent)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  前置读读不懂 → 判不出且不下发任何变更命令")
    if not ok:
        print(f"         ok={res.ok} mutation_sent={res.mutation_sent} 发出去的: {sent}")

    # ② 空名单哨兵 → add 真的发得出去（这是块 12 那个 bug 的端到端回归：
    #    修之前前置读在「白名单为空」时永远读不懂，add 一条都发不出去）
    res, sent = asyncio.run(
        _run_fake(
            [_EMPTY_WL, "Added Steve", "There are 1 whitelisted players: Steve"],
            "add",
            "Steve",
        )
    )
    ok = res.ok is True and res.mutation_sent and "whitelist add Steve" in sent
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  空名单 + add → 命令确实发出去且判为成功")
    if not ok:
        print(f"         ok={res.ok} mutation_sent={res.mutation_sent} 发出去的: {sent}")

    # ③ 反查读不懂 → 命令已经发出去了，这是「已发送（未能验证）」，不是「没发」
    res, sent = asyncio.run(_run_fake([_EMPTY_WL, "Added Steve", _JUNK], "add", "Steve"))
    ok = res.ok is None and res.mutation_sent and "whitelist add Steve" in sent
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  反查读不懂 → 判不出但标记「命令已下发」")
    if not ok:
        print(f"         ok={res.ok} mutation_sent={res.mutation_sent} 发出去的: {sent}")

    # ④ 两种 None 的**群内文案**必须不同。mcs/mc_admin.py 导入即注册 matcher，
    #    不经过 nonebot.init() 导不进来，所以这里只能读源码钉一下分支还在。
    _admin_src = (ROOT / "plugins_napcat" / "mcs" / "mc_admin.py").read_text("utf-8")
    _body = _admin_src.split("def _build_message", 1)[-1].split("\n@mc_admin", 1)[0]
    ok = "mutation_sent" in _body and "命令没有发出去" in _body
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  群内文案区分「没发出去」与「发了但未能验证」")
    if not ok:
        print(f"         实际: {_body[:200]!r}")

    # ⑤ command 形参逐台传到底：**三条**命令（前置读 / 变更 / 反查）都得用它。
    #    只看变更那一条的话，「前置读硬编码 whitelist list」这种改法照样过 ——
    #    而那会让配了 Global Whitelist 的那台每次都读不懂名单，命令一条都发不出去。
    res, sent = asyncio.run(
        _run_fake(
            [_EMPTY_WL, "Added Steve", "There are 1 whitelisted players: Steve"],
            "add",
            "Steve",
            command="globalwhitelist",
        )
    )
    ok = (
        res.ok is True
        and sent == ["globalwhitelist list", "globalwhitelist add Steve", "globalwhitelist list"]
        and not any("whitelist list" in c and "globalwhitelist" not in c for c in sent)
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  command 形参逐台传到底（三条命令都是 globalwhitelist）")
    if not ok:
        print(f"         发出去的: {sent}")
    print()

    print("== 玩家进出对账自测（reconcile 纯函数）==")

    from plugins_napcat._shared.mc import McSnapshot
    from plugins_napcat._shared.mcdelta import (
        KIND_JOIN,
        KIND_LEAVE,
        KIND_SWITCH,
        reconcile,
    )

    def _snap(tid, names=(), *, reachable=True, complete=True, count=None):
        """手工造一份快照。count 缺省 = len(names)，与 mc.py 「名单完整」的含义一致；
        要造「答了但名单残缺」的假数据得显式传 count 和 complete=False。"""
        return McSnapshot(
            reachable=reachable,
            target_id=tid,
            count=len(names) if count is None else count,
            names=list(names),
            names_complete=complete,
        )

    _TG = {t.id: t for t in _full.targets}
    # bingo / backstabbed / proxy 同属「主服群」；gtnh 没写 group（缺省成自己的 id）。
    _ORDER = [_TG["bingo"], _TG["backstabbed"], _TG["gtnh"], _TG["proxy"]]

    def _rec(prev, snaps, targets=None):
        return reconcile(prev, {s.target_id: s for s in snaps}, _ORDER if targets is None else targets)

    def _brief(delta):
        return [(e.kind, e.player, e.target_id, e.origin_id) for e in delta.events]

    # ① 首次建基线：prev 是空的，两台各 2 人 → 零事件。
    #    这条挡的是最刺眼的一类假消息 —— 机器人重启后把全服的人报成刚进服。
    d = _rec({}, [_snap("bingo", ["甲", "乙"]), _snap("backstabbed", ["丙", "丁"])])
    ok = (
        d.events == ()
        and set(d.baseline) == {"bingo", "backstabbed"}
        and d.baseline["bingo"] == frozenset({"甲", "乙"})
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  没有基线 → 零事件、baseline 记下当前名单（存量玩家不是刚进服）")

    # ② 单人进服 → 恰 1 条 join，归属到进的那台
    d = _rec({"bingo": frozenset({"甲"})}, [_snap("bingo", ["甲", "乙"])])
    ok = _brief(d) == [(KIND_JOIN, "乙", "bingo", "")]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  单人进服 → 恰 1 条 join 且落在进的那台：{_brief(d)}")

    # ③ 单人退服 → 恰 1 条 leave（对账层必须算得出来；「不推 leave」是文案层的决定）
    d = _rec({"bingo": frozenset({"甲", "乙"})}, [_snap("bingo", ["甲"])])
    ok = _brief(d) == [(KIND_LEAVE, "乙", "bingo", "")]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  单人退服 → 恰 1 条 leave：{_brief(d)}")

    # ④ 同组换服 → **恰 1 条** switch，没有孤立的 join/leave。
    #    多出一条 join 的症状是群里同时收到「换服过来」和「加入了」，
    #    读起来像他进了两次。
    d = _rec(
        {"bingo": frozenset({"甲", "小明"}), "backstabbed": frozenset({"乙"})},
        [_snap("bingo", ["甲"]), _snap("backstabbed", ["乙", "小明"])],
    )
    ok = _brief(d) == [(KIND_SWITCH, "小明", "backstabbed", "bingo")] and d.events[0].count == 2
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  同组换服 → 恰 1 条 switch，且没有多余的 join/leave：{_brief(d)}")
    if not ok:
        print(f"         count={d.events[0].count if d.events else None}（应取到达端人数 2）")

    # ⑤ 跨组移动 → 拆成普通 join + 普通 leave，**没有** switch。
    #    「从 GTNH 过来」是猜的（他可能先退了 GTNH、去吃了饭、再进 Bingo），
    #    而 leave 不推，所以群里只看到「加入了 Bingo」。
    d = _rec(
        # bingo 也要有基线（哪怕空集），否则它只是「本轮新出现的目标」，连 join 都不产
        {"bingo": frozenset(), "gtnh": frozenset({"小明"})},
        [_snap("bingo", ["小明"]), _snap("gtnh", [])],
    )
    ok = _brief(d) == [
        (KIND_JOIN, "小明", "bingo", ""),
        (KIND_LEAVE, "小明", "gtnh", ""),
    ]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  跨组移动 → join + leave，不报换服：{_brief(d)}")

    # ⑥ 两台都没写 group → 各自缺省成自己的 id，天然不同组，永不被判成换服。
    #    钉住 mcservers.py 里 `group = ... or target_id` 那个缺省 —— 谁把它删掉，
    #    这两台服之间就会开始报「换服过来」，而它们其实毫无关系。
    _nogroup = parse_book(
        '[[targets]]\nid = "a"\nkind = "backend"\nhost = "127.0.0.1"\nport = 1\n'
        '[[targets]]\nid = "b"\nkind = "backend"\nhost = "127.0.0.1"\nport = 2\n'
    )
    d = reconcile(
        {"a": frozenset({"小明"})}, {"b": _snap("b", ["小明"])}, _nogroup.targets
    )
    ok = (
        _nogroup.get("a").group == "a"
        and _nogroup.get("b").group == "b"
        and all(e.kind != KIND_SWITCH for e in d.events)
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  不写 group 的两台服不会互报换服（group 缺省成 id）")

    # ⑦ reachable=False → 零事件且基线**原样冻结**。
    #    这条是整个模块最重要的一条：抖一轮就清空基线，恢复时整服的人被报成刚进服。
    d = _rec(
        {"bingo": frozenset({"甲", "乙"})},
        [_snap("bingo", [], reachable=False, complete=False)],
    )
    ok = d.events == () and d.baseline["bingo"] == frozenset({"甲", "乙"})
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  探测失败 → 零事件、基线原样冻结（抖动 ≠ 集体退服）")

    # ⑧ names_complete=False → 同样零事件、基线不变。
    #    与 mc.py 那道 `len(names) == 人数` 闸门是同一条原则：残缺名单里「不在名单中」
    #    等于「我们没看到」，不等于「他走了」。
    d = _rec(
        {"bingo": frozenset({"甲", "乙"})},
        [_snap("bingo", ["甲"], complete=False, count=5)],
    )
    ok = d.events == () and d.baseline["bingo"] == frozenset({"甲", "乙"})
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  名单不完整 → 零事件、基线不变（我们没看到 ≠ 他走了）")

    # ⑨ 代理：names_complete 恒为假，于是自动落进「不参与对账」——
    #    既不会当换服端点，也不会因为「代理名单为空」误伤同组的子服。
    #    这条不变量（代理不出分服名单、不是故障）在这里第一次真正当护栏用。
    d = _rec(
        {"proxy": frozenset({"甲"}), "bingo": frozenset({"乙"})},
        [_snap("proxy", [], complete=False, count=9), _snap("bingo", ["乙", "丙"])],
    )
    ok = _brief(d) == [(KIND_JOIN, "丙", "bingo", "")] and d.baseline["proxy"] == frozenset({"甲"})
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  代理天然不参与对账，也不影响同组子服：{_brief(d)}")

    # ⑩ 来源不唯一 → 降级成普通进服 + 普通离开，不猜是哪台。
    #    跨服同名账号（或上一轮数据本身有残留）就是这个形状。
    d = _rec(
        {"bingo": frozenset({"小明", "甲"}), "backstabbed": frozenset({"小明", "乙"})},
        [_snap("bingo", ["甲"]), _snap("backstabbed", ["小明", "乙"])],
    )
    ok = _brief(d) == [(KIND_LEAVE, "小明", "bingo", "")]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  上轮同时出现在同组两台 → 不猜来源，无 switch：{_brief(d)}")

    # ⑩' 同一形状但到达端唯一、来源端有两个 → 仍然不许猜。
    #    只用三台同组服才构造得出来，所以这里单独起一份小表（主服群只有两台 backend）。
    _GRP3 = parse_book(
        '[[targets]]\nid = "a"\nkind = "backend"\ngroup = "G"\nhost = "127.0.0.1"\nport = 1\n'
        '[[targets]]\nid = "b"\nkind = "backend"\ngroup = "G"\nhost = "127.0.0.1"\nport = 2\n'
        '[[targets]]\nid = "c"\nkind = "backend"\ngroup = "G"\nhost = "127.0.0.1"\nport = 3\n'
    )
    d = reconcile(
        # c 要给个空基线，否则它只是「新目标」，连 join 都不会有
        {"a": frozenset({"小明"}), "b": frozenset({"小明"}), "c": frozenset()},
        {"a": _snap("a", []), "b": _snap("b", []), "c": _snap("c", ["小明"])},
        _GRP3.targets,
    )
    ok = all(e.kind != KIND_SWITCH for e in d.events) and len(d.events) == 3
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  来源有两个候选 → 仍然不猜：{_brief(d)}")

    # ⑪ 新加进关联的一台服：prev 里没有它的键 → 只建基线，存量 5 人不报进服。
    #    这是配置改动的常见后果，DEPLOY 有整节教人分辨这类假事件。
    d = _rec(
        {"bingo": frozenset({"甲"})},
        [_snap("bingo", ["甲"]), _snap("backstabbed", ["a", "b", "c", "d", "e"])],
    )
    ok = d.events == () and len(d.baseline) == 2 and len(d.baseline["backstabbed"]) == 5
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  本轮新出现的目标只建基线，不把存量玩家报成进服")

    # ⑫ prev 里有已经被移出配置的服 → 新基线里没有它（基线跟着配置走，不留残影）
    d = _rec(
        {"bingo": frozenset({"甲"}), "ghost": frozenset({"幻"})}, [_snap("bingo", ["甲"])]
    )
    ok = "ghost" not in d.baseline
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  移出配置的目标不进新基线：{sorted(d.baseline)}")

    # ⑬ **空集 ≠ 没有这个键**。这两行是同一个快照喂出来的两个截然不同的答案，
    #    钉住的就是被删掉的 _initialized（「有没有这个键」本身就是它）。
    _three = [_snap("gtnh", ["甲", "乙", "丙"])]
    d_empty = _rec({"gtnh": frozenset()}, _three)
    d_none = _rec({}, _three)
    ok = (
        len(d_empty.events) == 3
        and all(e.kind == KIND_JOIN for e in d_empty.events)
        and d_none.events == ()
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  空基线报 3 条进服、无键则零事件（空集 ≠ 没有基线）")
    if not ok:
        print(f"         空集: {_brief(d_empty)} / 无键: {_brief(d_none)}")

    # ⑭ 确定性：同一份输入喂两次，事件逐项相等且顺序相同。
    #    顺序不稳的症状是消息里各台服的块每轮换位置，看着像随机刷新。
    _prev = {"bingo": frozenset({"甲"}), "backstabbed": frozenset({"乙"})}
    _snaps = [
        _snap("bingo", ["甲", "戊", "丙"]),
        _snap("backstabbed", ["乙", "丁"]),
        _snap("gtnh", ["己"]),
    ]
    ok = _rec(_prev, _snaps).events == _rec(_prev, _snaps).events
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  同一份输入两次结果逐项相同（消息不会每轮换位置）")

    print()

    print("== 播报文案自测（合成排版 / 定时播报）==")

    from datetime import datetime as _dt

    from plugins_napcat._shared import mcrender as _mr
    from plugins_napcat._shared.mcdelta import KIND_SWITCH as _SW
    from plugins_napcat._shared.mcdelta import PlayerEvent as _PE
    from plugins_napcat._shared.mcdelta import StatusEvent as _SE
    from plugins_napcat._shared.mcrender import (
        Row as _Row,
        format_events as _fe,
        render_report as _rr,
        render_summary as _rs,
    )
    from plugins_napcat._shared.textlen import MAX_LEN as _MAX

    def _t(tid, name="", group="主服群", kind="backend"):
        return ServerTarget(
            id=tid, name=name or tid, kind=kind, group=group,
            host="h", port=1, timeout=5.0, rcon_timeout=5.0,
        )

    def _row(
        tid, name, count, names=(), *, reachable=True, complete=True, kind="backend",
        group="主服群",
    ):
        return _Row(
            _t(tid, name, group=group, kind=kind),
            McSnapshot(
                target_id=tid, reachable=reachable, count=count,
                names=list(names), names_complete=complete,
            ),
        )

    # 进服模板有 4 句，随机挑一句属于「体验」，不属于要钉的东西 —— 而随机性会让
    # 逐字断言变成碰运气。整个块换成固定模板（结束前还原）。
    _tpl_saved = _mr._JOIN_TEMPLATES
    _mr._JOIN_TEMPLATES = ["🎮 {who} 加入了「{where}」"]

    _T2 = [_t("gtnh", "GTNH"), _t("bingo", "Bingo"), _t("backstabbed", "谁是杀手")]
    _NOW = _dt(2026, 9, 21, 14, 32)

    # 分隔虚线的逐字形状（在 mcrender._SEP 里定）。刻意**不**引用 _mr._SEP：
    # 那会变成同义反复（线型改成什么样这条都过），而这里要钉的正是它长什么样。
    _LINE = "- - - - - - - - - -"

    # ① 没有事件就别发消息（空消息在群里是一行空白，比不发更糟）
    text, clipped = _fe([], _T2)
    ok = text is None and clipped is False
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  零事件 → None")

    # ② 单个进服 → 一行：服名带「」（DEPLOY.md 引用着这个形状）、不报人数。
    #    count=3 传进去却断言输出里没有数字，正是「动态消息不报人数」那条规则。
    text, clipped = _fe([_PE(KIND_JOIN, "阿伟", "bingo", count=3)], _T2)
    ok = text == "🎮 阿伟 加入了「Bingo」" and not clipped
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  单个进服 → 一行、服名带「」、不报人数：{text!r}")

    # ③ 三台服都有事件 → 每台一段（段顺序 = 传入的 targets 顺序），段间画虚线。
    #    **没有消息标题、没有【服名】块头、行内自带服名**（2026-09-25 用户要求）：
    #    逐字相等同时钉住了这四件事 —— 标题和块头一旦回来，这里立刻红。
    events = [
        _PE(KIND_JOIN, "阿伟", "gtnh", count=3),
        _PE(_SW, "小明", "bingo", origin_id="backstabbed", count=5),
        _PE(KIND_JOIN, "小红", "bingo", count=9),
    ]
    text, _ = _fe(events, _T2)
    want = "\n".join([
        "🎮 阿伟 加入了「GTNH」",
        _LINE,
        "🔄 小明 从「谁是杀手」换到「Bingo」",
        "🎮 小红 加入了「Bingo」",
    ])
    ok = text == want
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  多台服合成一条：段按 targets 顺序、换服行在进服行之前、段间虚线、无标题无块头")
    if not ok:
        print(f"         期望:\n{want}\n         实际:\n{text}")

    # ④ 纯退服 → 不进消息。**对账层照样产出它**（块 19 有一条用例钉着），
    #    这里是推送层说「不推」的地方 —— 哪天要开退服提醒，改的就是这一处。
    text, _ = _fe([_PE(KIND_LEAVE, "小明", "bingo", count=2)], _T2)
    ok = text is None
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  纯退服 → 不发消息（对账层有它，推送层丢它）")

    # ⑤ 退服不产生行，也**不改变**其余行的顺序/内容 —— 混进来时不能顺手多带一行。
    #    换服只落在**到达**的那台（mcdelta 的 target_id 定义），来源服那段里没有半行。
    text, _ = _fe(
        [
            _PE(KIND_JOIN, "阿伟", "gtnh", count=3),
            _PE(KIND_LEAVE, "小明", "bingo", count=2),
            _PE(_SW, "小红", "bingo", origin_id="gtnh", count=5),
        ],
        _T2,
    )
    want = "\n".join([
        "🎮 阿伟 加入了「GTNH」",
        _LINE,
        "🔄 小红 从「GTNH」换到「Bingo」",
    ])
    ok = text == want and "小明" not in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  退服不占行、不影响其余行；换服只落在到达的那台")
    if not ok:
        print(f"         期望:\n{want}\n         实际:\n{text}")

    # ⑥ 单个换服 → 一行，两端服名都在行里（2026-09-25 用户要求）。
    #    **没有标题、没有块头、不报人数** —— 原来那句「从「X」换服过来」必须配着
    #    【服名】块头才读得懂「过来」是过到哪台，一条事件占三行，用户嫌刷屏；
    #    而「当前 N 人在线」在换服时几乎恒为 1，写了等于没写。
    text, _ = _fe([_PE(_SW, "小明", "bingo", origin_id="backstabbed", count=5)], _T2)
    ok = text == "🔄 小明 从「谁是杀手」换到「Bingo」"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  单个换服 → 一行、两端服名、不带块头与人数：{text!r}")
    if not ok:
        print(f"         实际:\n{text}")

    # ⑥b 两台服各一次换服 → 两段之间画虚线。**段的分界只剩这条线**（没块头了），
    #     所以它必须画；同时钉住「一段里不重复画线」（只有一行虚线）。
    text, _ = _fe(
        [
            _PE(_SW, "小明", "bingo", origin_id="backstabbed", count=5),
            _PE(_SW, "小红", "gtnh", origin_id="bingo", count=4),
        ],
        _T2,
    )
    want = "\n".join([
        "🔄 小红 从「Bingo」换到「GTNH」",
        _LINE,
        "🔄 小明 从「谁是杀手」换到「Bingo」",
    ])
    ok = text == want
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  两台服的换服各占一段、中间画虚线")
    if not ok:
        print(f"         期望:\n{want}\n         实际:\n{text}")

    # ⑦ 掉线 / 恢复各一行，**不带失败轮数、不带人数**（2026-09-25 用户要求）。
    #    streak=2 / count=5 都是为了让「输出里没有数字」这条断言有分量：值都传进去了。
    text, _ = _fe([_SE("bingo", down=True, streak=2, why="连不上了")], _T2)
    ok = text == "⚠️ Bingo 连不上了"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  单个掉线 → 一行、不报失败轮数：{text!r}")

    text, _ = _fe([_SE("bingo", down=False, count=5)], _T2)
    ok = text == "✅ Bingo 已恢复"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  单个恢复 → 一行、不报人数：{text!r}")

    # ⑧ 状态跃迁混进合成消息时，行内**照样**带服名（块头已经没有了，不在这里写就没处写），
    #    并且延续「应答异常」与「连不上了」分开说的老规矩。
    text, _ = _fe(
        [
            _SE("bingo", down=True, streak=2, why="应答异常"),
            _PE(KIND_JOIN, "阿伟", "gtnh", count=3),
        ],
        _T2,
    )
    want = "\n".join([
        "🎮 阿伟 加入了「GTNH」",
        _LINE,
        "⚠️ Bingo 应答异常",
    ])
    ok = text == want
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  状态行也带服名、不报轮数，且区分「应答异常」")
    if not ok:
        print(f"         期望:\n{want}\n         实际:\n{text}")

    # ⑧b 四种行（进服 / 换服 / 掉线 / 恢复）混在一轮里，**逐字**断言它们都不带数字。
    #     单独再钉一条是因为上面各条只钉自己那一句，而规则是「整条消息一个数字都没有」
    #     —— 有人往任何一句里加回「（当前 N 人）」都被这条抓住。
    #     判据用「不带『当前』『已连续』」而不是 `isdigit()`：**玩家名里是可以有数字的**
    #     （真实案例 VulCaN9），拿 isdigit 当判据会在有人给测试换名字时莫名其妙地红。
    text, _ = _fe(
        [
            _PE(KIND_JOIN, "阿伟", "gtnh", count=3),
            _PE(_SW, "小明", "bingo", origin_id="backstabbed", count=5),
            _PE(KIND_JOIN, "小红", "bingo", count=9),
            _SE("gtnh", down=True, streak=12, why="连不上了"),
            _SE("backstabbed", down=False, count=7),
        ],
        _T2,
    )
    want = "\n".join([
        "🎮 阿伟 加入了「GTNH」",
        "⚠️ GTNH 连不上了",
        _LINE,
        "🔄 小明 从「谁是杀手」换到「Bingo」",
        "🎮 小红 加入了「Bingo」",
        _LINE,
        "✅ 谁是杀手 已恢复",
    ])
    ok = text == want and "当前" not in text and "已连续" not in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  四种行混在一轮：都不带人数与失败轮数，段间虚线照画")
    if not ok:
        print(f"         期望:\n{want}\n         实际:\n{text}")

    # ⑨ 播报：一台有人 + 一台 0 人 + 一台不可达 → **照发**，三台各一块。
    #    这是本期行为变更的核心：单目标时代「0 人 → 整条不发」在多目标下没道理，
    #    3 台服的播报里有 1 台没人，不代表这次播报没意义。
    rows = [
        _row("gtnh", "GTNH", 3, ["阿伟", "小明", "小红"]),
        _row("bingo", "Bingo", 0),
        _row("backstabbed", "谁是杀手", 0, reachable=False),
    ]
    _all = [r.target for r in rows]
    text, why = _rr(rows, total_names=50, all_targets=_all, audience_name="社团群", now=_NOW)
    ok = (
        text is not None
        and why == ""
        and text.splitlines()[0] == "📣 MC 播报 · 14:32"
        and "【GTNH】在线 3" in text
        and "【Bingo】在线 0" in text
        # 0 人不再补「目前无人」（2026-09-22 用户要求去掉冗余）。后半句是钉子：
        # 只要那句话被加回来就红，不然这条断言在两种写法下都过。
        and "目前无人" not in text
        and "【谁是杀手】😵 不可达" in text
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  播报：有人 / 没人 / 不可达三台各一块，照发不跳过")
    if not ok:
        print(f"         实际:\n{text}")

    # ⑨b 跨组的虚线在播报里也要有（2026-09-23 用户要求，与总览一致），但**不给自己加
    #     空行** —— 播报的取向是块挨着块、一眼扫完。只加线不加空，正是两条消息排版
    #     取向不同又被同一个规则覆盖的地方，所以两件事都要钉。
    rows = [
        _row("gtnh", "GTNH", 1, ["阿伟"], group="gtnh"),
        _row("bingo", "Bingo", 1, ["小明"], group="群组服"),
        _row("lobby", "大厅", 1, ["小红"], group="群组服"),
    ]
    text, _ = _rr(rows, total_names=50, all_targets=[r.target for r in rows], now=_NOW)
    lines = text.splitlines()
    seps = [ln for ln in lines if len(ln) >= 8 and "-" in ln and set(ln) <= {"-", " "}]
    if seps:
        _i = lines.index(seps[0])
        ok = (
            len(seps) == 1
            # 上一行是 GTNH 块的**名字行**、下一行就是群组服那块的头：紧邻两行都不是空行
            and lines[_i - 1] == "  • 阿伟"
            and lines[_i + 1].startswith("【Bingo】")
        )
    else:
        ok = False
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  播报里也画虚线，且自己不占空行")
    if not ok:
        print(f"         实际:\n{text}")

    # 反向：播报里只有一组时同样不许画（跟总览那对断言成对）
    rows = [_row("bingo", "Bingo", 1, ["小明"]), _row("lobby", "大厅", 1, ["小红"])]
    text, _ = _rr(rows, total_names=50, all_targets=[r.target for r in rows], now=_NOW)
    ok = not [ln for ln in text.splitlines() if len(ln) >= 8 and "-" in ln and set(ln) <= {"-", " "}]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  播报只有一组时不画线")
    if not ok:
        print(f"         实际:\n{text}")

    # ⑩ 两种跳过的**原因必须不同**：全不可达去开服，全员 0 人什么都不用做。
    #    混成一句「当前无人在线」会把前一种的人指去查错方向。
    t1, r1 = _rr(
        [_row("a", "A", 0, reachable=False), _row("b", "B", 0, reachable=False)],
        total_names=50, all_targets=[_t("a", "A"), _t("b", "B")], audience_name="社团群", now=_NOW,
    )
    t2, r2 = _rr(
        [_row("a", "A", 0), _row("b", "B", 0)],
        total_names=50, all_targets=[_t("a", "A"), _t("b", "B")], audience_name="社团群", now=_NOW,
    )
    ok = (
        t1 is None and t2 is None and r1 != r2
        and "全部探测失败" in r1 and "都没人在线" in r2
        and "社团群" in r1
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  两种跳过原因分开说：\n         {r1}\n         {r2}")

    # ⑪ 只挂一台代理且它有 5 人 → **不**跳过。
    #    判定「有没有人」若用 _counting() 的求和，代理被排除在外，求和是 0，
    #    这一条就会被误判成「无人在线」而静默跳过 —— 可明明有人。
    prows = [_row("proxy", "主服群", 5, kind="proxy")]
    text, why = _rr(
        prows, total_names=50, all_targets=[prows[0].target], audience_name="社团群", now=_NOW
    )
    ok = text is not None and "全群组 5 人" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  只挂一台有人的代理 → 不跳过，且报全群组人数")
    if not ok:
        print(f"         实际: text={text!r} why={why!r}")

    # ⑫ 同一台服在「总览」和「播报」里**逐字相同** —— 这是 mcrender 那份 docstring
    #    的承诺（同一个服在两个地方不能显示成两种样子），今天之前它还是句空话：
    #    播报是 mc_reporter 自己拼的，根本没 import mcrender。
    _same = [_row("gtnh", "GTNH", 3, ["阿伟", "小明", "小红"]), _row("bingo", "Bingo", 0)]
    _same_all = [r.target for r in _same]
    summ = _rs(_same, total_names=50, all_targets=_same_all)
    rep, _ = _rr(_same, total_names=50, all_targets=_same_all, audience_name="社团群", now=_NOW)
    block = "【GTNH】在线 3\n  • 阿伟\n  • 小明\n  • 小红"
    ok = block in summ and rep is not None and block in rep
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  同一台服的块在总览与播报里逐字相同")
    if not ok:
        print(f"         总览:\n{summ}\n         播报:\n{rep}")

    # ⑬ 超长：不许从头截断（切掉的是尾部 = 最后几台服 + 尾注，而尾注正是
    #    「上面这些数对不上」那句）。_fit 靠逐行减名额重渲染，总览与播报共用它。
    #
    #    40 台 × 25 个长名字是**量出来的**：初始渲染 3600 字远超上限，减到名额 1 时
    #    仍有 1950 字（还是超），减到 0 才落到 990 字 —— 也就是说「名额被减到 0」
    #    和「尾注还在」这两件事在这一组数据下同时成立，断言才有意义。名字短一点
    #    会在某个中间额度就停下，那两条都会碰巧通过。
    _N = 40
    _many = [
        _row(
            f"t{i}", f"服{i}", 25,
            [f"LongPlayerName{i:03d}{j:02d}" for j in range(25)],
            complete=(i < _N - 2),
        )
        for i in range(_N)
    ]
    _many_all = [r.target for r in _many]
    tot = sum(len(r.snap.names) for r in _many)
    summ = _rs(_many, total_names=tot, all_targets=_many_all)
    rep, why = _rr(
        _many, total_names=tot, all_targets=_many_all, audience_name="社团群", now=_NOW
    )
    _note = f"⚠️ 名单不完整：服{_N - 2}、服{_N - 1}"
    ok = (
        len(summ) <= _MAX and _note in summ and "  • " not in summ
        and rep is not None and len(rep) <= _MAX and _note in rep and "  • " not in rep
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  超长时逐行减名额、砍到 0 也不丢尾注（总览 {len(summ)} / 播报 {len(rep or '')} 字）")
    if not ok:
        print(f"         总览尾注={(_note in summ)} 总览还有人名={('  • ' in summ)}")

    _mr._JOIN_TEMPLATES = _tpl_saved
    print()

    # ---------------- 块 19：接口型目标（同源合并 / SLP 复查 / 三类失败） ----------------
    # 全部在打桩的接口上跑：真接口只有对方那台有，而这一块要钉的是**我们这边的行为**
    # （发几次请求、数字从哪来、失败怎么分类），和服务端在不在没关系。
    #
    # 三件最容易写错、也最贵的事：
    #   1. 一份 /status 必须只请求一次（子服各自再请求一次 = 对方日志里 4 条重复访问）
    #   2. 只探测**一台子服**时也得问到接口（缓存按目标过期，完全可能只有子服过期）
    #   3. 接口挂了要说清是「来源取不到」，不能让子服看起来像自己挂了
    print("== 接口型目标自测（假接口）==")

    import plugins_napcat._shared.mc as _mc
    from plugins_napcat._shared import mcbridge as _br

    _API_TOML = """
[defaults]
timeout = 3

[[targets]]
id   = "szu"
name = "群组服"
kind = "proxy"
host = "10.0.0.1"
port = 30002
api  = { url = "http://127.0.0.1:8080", token = "t" }

[[targets]]
id   = "bingo"
name = "Bingo"
kind = "backend"
source = "szu"

[[targets]]
id         = "survival"
name       = "生电"
kind       = "backend"
source     = "szu"
source_key = "surv"

[[targets]]
id   = "creative"
name = "创造"
kind = "backend"
source = "szu"
source_key = "creative"
"""
    _api_book = parse_book(_API_TOML)
    _api_targets = list(_api_book.targets)

    def _status(proxy=5, servers=()):
        return _br.BridgeStatus(
            proxy_online=proxy,
            servers=tuple(
                _br.BridgeServer(name=n, online=o, players=tuple(p))
                for n, o, p in servers
            ),
        )

    def _slp_ok(count, max_players=20):
        return _mc._SlpProbe(
            ok=True, count=count, max_players=max_players, latency=3.0, version="1.7.10"
        )

    _slp_dead = _mc._SlpProbe(ok=False, error="SLP 连不上（打桩）", error_kind=_mc.SLP_UNREACHABLE)
    _saved_slp, _saved_status = _mc._slp_probe, _br.fetch_status

    def _run_api(targets, *, status=None, error=None, slp=None):
        """在打桩的接口上跑一轮。返回 (快照列表, 接口被调用的次数)。"""
        calls: list[str] = []

        async def fake_status(api, timeout):
            calls.append(api.url)
            if error is not None:
                raise error
            return status

        async def fake_slp(target):
            return (slp or {}).get(target.id, _slp_dead)

        _mc._slp_probe, _br.fetch_status = fake_slp, fake_status
        try:
            snaps = asyncio.run(_mc.fetch_snapshots(list(targets)))
        finally:
            _mc._slp_probe, _br.fetch_status = _saved_slp, _saved_status
        return snaps, calls

    _FULL = _status(
        proxy=3,
        servers=[
            ("bingo", 2, ("Alice", "Bob")),
            ("surv", 1, ("Carol",)),
            ("creative", 0, ()),
        ],
    )

    # ① 一份响应喂四台：只请求一次，各台切自己那一行（生电/创造靠 source_key 认）
    snaps, calls = _run_api(_api_targets, status=_FULL, slp={"szu": _slp_ok(3)})
    by_id = {s.target_id: s for s in snaps}
    ok = (
        len(calls) == 1
        and [s.target_id for s in snaps] == ["szu", "bingo", "survival", "creative"]
        and by_id["szu"].count == 3
        and by_id["szu"].count_source == "slp"
        and not by_id["szu"].names_complete  # 代理不出分服名单：不适用，不是不完整
        and by_id["szu"].names_source == "none"
        and (by_id["bingo"].count, by_id["bingo"].names) == (2, ["Alice", "Bob"])
        and by_id["bingo"].names_complete
        and by_id["bingo"].names_source == "api"
        and (by_id["survival"].count, by_id["survival"].names) == (1, ["Carol"])
        and by_id["creative"].count == 0
        # 0 人时名单为空但**完整**（0 == 0），和 RCON 那条同一条约定
        and by_id["creative"].names_complete
        and by_id["creative"].error == ""
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  一次 /status 喂四台：请求 {len(calls)} 次，各台切自己那一行")
    if not ok:
        print(f"         调用={calls}")
        for s in snaps:
            print(f"         {s.target_id}: 人数={s.count} 来源={s.count_source} "
                  f"名单={s.names} 完整={s.names_complete} error={s.error!r}")

    # ② 只探测一台子服 → 照样去问 hub 的接口（hub 不在本批目标里）。
    #    缓存按目标过期，完全可能只有子服那一条过期；这时若「hub 不在本批就报错」，
    #    群里会看到一台好着的子服莫名其妙「取不到数据」。
    snaps, calls = _run_api([_api_book.get("bingo")], status=_FULL, slp={"szu": _slp_ok(3)})
    ok = len(calls) == 1 and snaps[0].count == 2 and snaps[0].names == ["Alice", "Bob"]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  只探测一台子服：仍然去问 hub（请求 {len(calls)} 次）")
    if not ok:
        print(f"         调用={calls} 快照={snaps[0]}")

    # ③ 人数对不上 → 按 SLP 报，并把「接口可疑」写出来。
    #    这是接口型目标唯一能发现「对方插件把人数算错了」的地方。
    snaps, _ = _run_api(
        [_api_book.get("szu")], status=_status(proxy=2), slp={"szu": _slp_ok(7)}
    )
    ok = (
        snaps[0].count == 7
        and snaps[0].count_source == "slp"
        and "对不上" in snaps[0].error
        and "接口 2" in snaps[0].error
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口 2 人 / SLP 7 人 → 按 SLP 报并标注接口可疑")
    if not ok:
        print(f"         人数={snaps[0].count} 来源={snaps[0].count_source} error={snaps[0].error!r}")

    # ③b 中转服（transit）上的人不算进「全群组」。
    #     用户看到的原始症状：总览里「【群组服】全群组 2 人」，下面几台子服加起来 1 人
    #     ——差额是站在 limbo 里的那个。数字本身没错，但它回答的是「连着代理的有几个」，
    #     不是群里想知道的「有几个在玩」。
    _T_T = parse_book(_API_TOML.replace(
        'api  = { url = "http://127.0.0.1:8080", token = "t" }',
        'api  = { url = "http://127.0.0.1:8080", token = "t" }\ntransit = ["limbo"]',
    ))
    _t_szu, _t_bingo = _T_T.get("szu"), _T_T.get("bingo")
    _lobby_limbo = _status(proxy=2, servers=[("lobby", 1, ("Rcwalter",)), ("limbo", 1, ("Lychee",))])

    snaps, _ = _run_api([_t_szu], status=_lobby_limbo, slp={"szu": _slp_ok(2)})
    ok = (
        snaps[0].count == 1
        and "扣掉中转服 limbo 1 人" in snaps[0].error
        and _t_szu.transit == ("limbo",)
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  transit：中转服的人从「全群组」里扣掉（2 → 1）")
    if not ok:
        print(f"         人数={snaps[0].count} transit={_t_szu.transit} error={snaps[0].error!r}")

    # ③c 扣减要能**扛住 SLP 那条分支**：SLP 报的也是「连着代理的有几个」，两边都含
    #     中转服的人。只在接口那条分支上扣，SLP 一成功就把人带回来了（而且很隐蔽：
    #     接口拿不到数据时才对，接口一通就不对）。
    snaps, _ = _run_api(
        [_t_szu],
        status=_status(proxy=5, servers=[("lobby", 4, ("A",)), ("limbo", 1, ("L",))]),
        slp={"szu": _slp_ok(5)},
    )
    ok = snaps[0].count == 4 and snaps[0].count_source == "slp" and "扣掉中转服" in snaps[0].error
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  transit：走 SLP 那条分支也照样扣")
    if not ok:
        print(f"         人数={snaps[0].count} 来源={snaps[0].count_source} error={snaps[0].error!r}")

    # ③d 对不上号的名字**不能静默**：对方改了 velocity.toml 的服名、或那台中转服这次
    #     没起，都会走到这里。扣不掉的表现是「人数偏大」，而偏大在群里看不出异样。
    snaps, _ = _run_api(
        [_t_szu],
        status=_status(proxy=4, servers=[("lobby", 3, ("A", "B", "C")), ("hub", 1, ("D",))]),
        slp={"szu": _slp_ok(4)},
    )
    ok = (
        snaps[0].count == 4
        and "limbo 不在接口的子服列表里" in snaps[0].error
        and "hub" in snaps[0].error  # 候选清单要打出来，照着改
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  transit：名字对不上 → 照报原数并说明扣不掉")
    if not ok:
        print(f"         人数={snaps[0].count} error={snaps[0].error!r}")

    # ③e transit 的点名与「接口与 SLP 对不上」那条对照互不干扰：后者问的是「对方插件
    #     算错了吗」，拿我们自己的口径（扣减后）去比，等于把唯一能发现对方算错的地方
    #     抹掉 —— 报出去的人数可以是我们修过的，但「接口说了几」必须是原话。
    snaps, _ = _run_api(
        [_t_szu],
        status=_status(proxy=2, servers=[("lobby", 1, ("A",)), ("limbo", 1, ("L",))]),
        slp={"szu": _slp_ok(5)},
    )
    ok = (
        snaps[0].count == 4  # SLP 5 - 中转 1
        and "接口 2" in snaps[0].error  # 原话，不是扣减后的「接口 1」
        and "SLP 5" in snaps[0].error
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  transit：与「接口/SLP 对不上」的对照互不干扰")
    if not ok:
        print(f"         人数={snaps[0].count} error={snaps[0].error!r}")

    # ③f 中转服上没人时什么都不扣、也不留一句噪音备注
    snaps, _ = _run_api(
        [_t_szu],
        status=_status(proxy=1, servers=[("lobby", 1, ("A",)), ("limbo", 0, ())]),
        slp={"szu": _slp_ok(1)},
    )
    ok = snaps[0].count == 1 and snaps[0].error == ""
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  transit：中转服没人时不扣、也不备注")
    if not ok:
        print(f"         人数={snaps[0].count} error={snaps[0].error!r}")

    # ③g transit 只作用于代理那一行：子服的人数一个都不能扣（扣减只在
    #     _api_hub_snapshot 里做）。扣错了的表现是「某台子服的人数莫名其妙少 1」。
    snaps, _ = _run_api(
        [_t_bingo],
        status=_status(proxy=3, servers=[("bingo", 2, ("A", "B")), ("limbo", 1, ("L",))]),
        slp={"szu": _slp_dead},
    )
    ok = snaps[0].count == 2 and snaps[0].names == ["A", "B"]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  transit：只影响代理那一行（子服照旧）")
    if not ok:
        print(f"         人数={snaps[0].count} 名单={snaps[0].names}")
    if not ok:
        print(f"         人数={snaps[0].count} 来源={snaps[0].count_source} error={snaps[0].error!r}")

    # ④ 接口 401 → hub 退回 SLP（代理还活着，别报成整个群组挂了），
    #    子服则如实说「来源取不到」，且**失败性质是「凭证」**，不是「不可达」。
    snaps, calls = _run_api(
        _api_targets, error=_br.BridgeAuthError("token 被轮换了"), slp={"szu": _slp_ok(4)}
    )
    by_id = {s.target_id: s for s in snaps}
    ok = (
        len(calls) == 1
        and by_id["szu"].reachable
        and by_id["szu"].count == 4
        and by_id["szu"].count_source == "slp"
        and "接口取数失败" in by_id["szu"].error
        and not by_id["bingo"].reachable
        and by_id["bingo"].error_kind == _mc.API_AUTH
        and "数据来源 群组服" in by_id["bingo"].error
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口 401：hub 退回 SLP，子服报「来源取不到」且性质＝凭证")
    if not ok:
        for s in snaps:
            print(f"         {s.target_id}: 可达={s.reachable} 人数={s.count} "
                  f"性质={s.error_kind} error={s.error!r}")

    # ⑤ 接口连不上 → 子服的失败性质是「网络」（下一动作是查网络/隧道/对方服务）
    snaps, _ = _run_api(_api_targets, error=_br.BridgeUnreachable("connrefused"), slp=None)
    by_id = {s.target_id: s for s in snaps}
    ok = (
        not by_id["szu"].reachable
        and by_id["szu"].error_kind == _mc.SLP_UNREACHABLE
        and by_id["bingo"].error_kind == _mc.SLP_UNREACHABLE
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口连不上：hub 和子服的性质都是网络（不是凭证）")
    if not ok:
        for s in snaps:
            print(f"         {s.target_id}: 可达={s.reachable} 性质={s.error_kind} error={s.error!r}")

    # ⑥ 接口里没这台子服（服名写错 / 那台没注册）→ 是**配置错**，要把候选名单给出来
    snaps, _ = _run_api(
        [_api_book.get("bingo"), _api_book.get("survival")],
        status=_status(proxy=1, servers=[("lobby", 1, ("Zed",))]),
        slp=None,
    )
    ok = all(
        not s.reachable and "接口里有：lobby" in s.error and "source_key" in s.error
        for s in snaps
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口里没这台子服 → 报配置错并列出候选服名")
    if not ok:
        for s in snaps:
            print(f"         {s.target_id}: {s.error!r}")

    # ⑦ 子服填了 host 也能做 SLP 复查（数据来源同一个响应，但人数仍以 SLP 为准）
    _sub_host_book = parse_book(
        _API_TOML.replace(
            'source = "szu"\n\n[[targets]]\nid         = "survival"',
            'source = "szu"\nhost   = "10.0.0.2"\nport   = 25566\n\n[[targets]]\nid         = "survival"',
        )
    )
    snaps, _ = _run_api(
        [_sub_host_book.get("bingo")], status=_FULL, slp={"szu": _slp_ok(3), "bingo": _slp_ok(9)}
    )
    ok = snaps[0].count == 9 and "对不上" in snaps[0].error
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  子服填了 host：用 SLP 复查人数（接口 2 / SLP 9 → 按 SLP）")
    if not ok:
        print(f"         人数={snaps[0].count} error={snaps[0].error!r}")

    # ⑧ 解析期：api.url 只能填到端口（带路径的写法会被静默忽略 → 必须报错）
    _url_cases = [
        ("带 /status", "http://127.0.0.1:8080/status", False),
        ("只有主机名", "http://bridge:8080", True),
        ("结尾一个斜杠", "http://127.0.0.1:8080/", True),
        ("https + 端口", "https://mc.example.com:8443", True),
    ]
    _url_bad = []
    for desc, url, should_pass in _url_cases:
        text = _API_TOML.replace("http://127.0.0.1:8080", url)
        try:
            parse_book(text)
            got = True
        except ServerConfigError:
            got = False
        if got != should_pass:
            _url_bad.append(f"{desc}（应{'通过' if should_pass else '报错'}）")
    ok = not _url_bad
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  api.url 只填到端口：带路径的写法报错，其余放行")
    if not ok:
        print(f"         不符合预期：{_url_bad}")

    # ⑨ hub 回填要**同时**进 by_id：投影出来的子视图也得带着 hub，否则「某个群里查
    #    bingo」会说不出来源（P5 那次 scoped 忘重建索引的同类坑）
    _scoped = _api_book.scoped(["bingo"])
    ok = (
        _api_book.get("bingo").hub is not None
        and _api_book.get("bingo").hub.id == "szu"
        and _scoped.get("bingo").hub is not None
        and _scoped.get("bingo").hub.id == "szu"
        and _api_book.get("szu").hub is None
    )
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  hub 回填进子服，且全量表与投影视图一致")
    if not ok:
        print(f"         全量={_api_book.get('bingo').hub} 投影={_scoped.get('bingo').hub}")

    # ---- 接口白名单（对比块 18 的假 RCON：同一套「先读后写 + 反查」的判定）----
    # 这一块钉的是**判定逻辑没被通道换掉**：成败一律看反查，接口回执里的 ok/changed
    # 一个字都不信（它在对方不同版本里含义不同，见 BridgeWriteResult）。
    from plugins_napcat._shared import mcadmin as _mca
    from plugins_napcat._shared.mcadmin import AdminCommand as _AC
    from plugins_napcat._shared.mcadmin import WhitelistAuthError as _WlAuth
    from plugins_napcat._shared.mcadmin import WhitelistUnreachable as _WlDown

    _wl_target = _api_book.get("szu")

    def _run_wl(verb, player="Steve", *, before=(), after=None, read_error=None, write_error=None):
        """在打桩的接口上跑一条白名单命令。返回 (结果, 读次数, 下发出去的 (动词, 名字))。"""
        reads: list[int] = []
        writes: list[tuple[str, str]] = []

        async def fake_read(api, timeout):
            if read_error is not None:
                raise read_error
            reads.append(1)
            names = before if len(reads) == 1 else (after if after is not None else before)
            return _br.BridgeWhitelist(enabled=True, entries=tuple(names))

        async def fake_write(api, timeout, v, name):
            if write_error is not None:
                raise write_error
            writes.append((v, name))
            return _br.BridgeWriteResult(ok=True, changed=True, whitelist=None)

        _save_r, _save_w = _br.fetch_whitelist, _br.write_whitelist
        _br.fetch_whitelist, _br.write_whitelist = fake_read, fake_write
        try:
            cmd = _AC("list") if verb == "list" else _AC(verb, player)
            res = asyncio.run(_mca.run_whitelist_command(cmd, _wl_target))
        finally:
            _br.fetch_whitelist, _br.write_whitelist = _save_r, _save_w
        return res, reads, writes

    # ① 空名单是「没人」而不是「判不出来」—— RCON 那边要靠哨兵文案才分得出来，
    #    接口给的就是个空数组，这条路径必须照旧报 ok。
    res, reads, writes = _run_wl("list", before=[])
    ok = res.ok is True and res.names == [] and reads == [1] and writes == []
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口 list：空名单 → ok（不是「判不出来」）")
    if not ok:
        print(f"         ok={res.ok} names={res.names} 读={len(reads)} 写={writes}")

    # ② add 一个已经在名单里的名字 → 不下发（下发会写出同名不同拼写的重复条目）
    res, reads, writes = _run_wl("add", "vul", before=["Vul"])
    ok = res.noop and res.ok is True and writes == [] and res.server_name == "Vul"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口 add 已存在 → 不下发，回显服务端拼写 Vul")
    if not ok:
        print(f"         noop={res.noop} ok={res.ok} 写={writes} 名字={res.server_name}")

    # ③ add 新名字 → 下发，且反查（第二次读）命中才算成功
    res, reads, writes = _run_wl("add", "Steve", before=[], after=["Steve"])
    ok = writes == [("add", "Steve")] and res.ok is True and res.mutation_sent and len(reads) == 2
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口 add 新名字 → 下发后反查命中才算成功")
    if not ok:
        print(f"         写={writes} ok={res.ok} mutation_sent={res.mutation_sent} 读={len(reads)}")

    # ④ 接口回执说 ok，但反查里没有 → **未生效**。这是本块最关键的一条：
    #    信回执就等于把「没发生的事报成发生了」，而对方的 ok 语义还换过版本。
    res, reads, writes = _run_wl("add", "Steve", before=[], after=[])
    ok = len(writes) == 1 and res.ok is False and res.mutation_sent
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口回执 ok 但反查没命中 → 报未生效（不信回执）")
    if not ok:
        print(f"         写={writes} ok={res.ok} mutation_sent={res.mutation_sent}")

    # ⑤ remove 用**服务端记录的拼写**下发（接口返回的条目就是它记着的那个）
    res, reads, writes = _run_wl("remove", "vul", before=["Vul"], after=[])
    ok = writes == [("remove", "Vul")] and res.ok is True and res.targets == ["Vul"]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口 remove 用服务端拼写下发（vul → Vul）")
    if not ok:
        print(f"         写={writes} ok={res.ok} targets={res.targets}")

    # ⑥ 401 → 通道无关的「凭证错」，调用方据此回「接口认证失败（token 可能被轮换）」
    try:
        _run_wl("list", read_error=_br.BridgeAuthError("401"))
        _kind = "没抛"
    except _WlAuth:
        _kind = "auth"
    except Exception as exc:
        _kind = type(exc).__name__
    ok = _kind == "auth"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口 401 → WhitelistAuthError（不是「连不上」）")
    if not ok:
        print(f"         实际={_kind}")

    # ⑦ 写的时候连不上 → 「不可达」，且**前置读已经做过了**（读成功但写失败）
    try:
        _run_wl("add", "Steve", before=[], write_error=_br.BridgeUnreachable("connrefused"))
        _kind = "没抛"
    except _WlDown:
        _kind = "down"
    except Exception as exc:
        _kind = type(exc).__name__
    ok = _kind == "down"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  接口写的时候连不上 → WhitelistUnreachable")
    if not ok:
        print(f"         实际={_kind}")

    # ⑧⑨ --whitelist 在接口型目标上的诊断（不是命令判定，是「只读一览 + 该不该报警」）。
    #    `intercept_off` 是这一段的全部意义：enabled=false 时**名单读得到、返回码也是 1**，
    #    唯一的信号就是这张表 —— 把它漏掉，一台谁都能进的服会被报成「[OK] 解析正常」。
    from plugins_napcat._shared.mcservers import WhitelistRoute as _WR

    _route = _WR(target=_wl_target)

    def _run_wlview(enabled, entries):
        """在打桩的接口上跑一次只读一览。返回 (返回码, intercept_off, 打出来的字)。"""
        off: list[str] = []

        async def fake_read(api, timeout):
            return _br.BridgeWhitelist(enabled=enabled, entries=tuple(entries))

        _save = _br.fetch_whitelist
        _br.fetch_whitelist = fake_read
        buf, sys.stdout = sys.stdout, _io.StringIO()
        try:
            code = asyncio.run(_whitelist_via_api(_route, off))
        finally:
            _br.fetch_whitelist = _save
            text = sys.stdout.getvalue()
            sys.stdout = buf
        return code, off, text

    code, off, text = _run_wlview(True, ["Steve", "Vul"])
    ok = code == 1 and off == [] and "Steve" in text and "enabled（代理层的白名单总开关）: True" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  --whitelist 接口台：名单读得到 → 算正常，不报警")
    if not ok:
        print(f"         码={code} off={off} 文本={text!r}")

    code, off, text = _run_wlview(False, ["Steve"])
    ok = code == 1 and off == [_route.name] and "enabled（代理层的白名单总开关）: False" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  enabled=false → 记进 intercept_off（名单在但拦不住人）")
    if not ok:
        print(f"         码={code} off={off}")

    print()

    print(f"== 自测结果：{'全部通过' if not failed else f'{failed} 项失败'} ==")
    return 1 if failed else 0


# ---------------------------------------------------------------- 夜间静默行为自测

def _quiet_test() -> int:
    """跑 MC 与 oopz 两边的静默分支，证「不发」和「基线照常推进」两件事。

    为什么不并进 `--self-test`：那边**刻意不 init nonebot**（纯解析函数，谁都能跑），
    而 mc_reporter / auto_reporter 在 import 期就要 driver、在包 __init__ 期就要
    `nonebot.on_message` 那一套。两条路只能分开走。

    要钉死的是**看不见的分支**：静默期间该不发消息，这个看代码就知道；但「基线还要
    照常推进」错了的后果在当晚完全无声，只在第二天 09:00 炸出来 —— 把一整晚进过服
    （进过频道）的人当成「刚进服」（「刚进频道」）一次性补报几十条。所以两边的基线
    都一起断言。MC 与 oopz 是同一套语义的两个实现，任何一边漏了都会在 09:00 刷屏。

    不联网：send_to_groups 被替换成记账函数，「发没发、发了什么」全在内存里。
    """
    import nonebot

    try:
        nonebot.init()
    except Exception as exc:  # 缺 .env / driver 装不上 —— 说清楚，别甩一段 traceback
        print(f"== nonebot 初始化失败，跑不了（需要和机器人同一套 .env）: {exc!r} ==")
        return 2

    from plugins_napcat._shared.mc import McSnapshot
    from plugins_napcat._shared.mcdelta import StatusEvent
    from plugins_napcat._shared.mcservers import ServerTarget
    from plugins_napcat._shared.quiet import QuietWindow
    from plugins_napcat._shared.schedule import parse_quiet_hours
    from plugins_napcat.mcs import mc_reporter as mr
    from plugins_napcat.oopz import auto_reporter as ar
    from types import SimpleNamespace

    failed = 0

    def check(ok: bool, what: str) -> None:
        nonlocal failed
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {what}")

    class _Book:
        def __init__(self, targets):
            self.targets = tuple(targets)
            self.targets_primary_first = tuple(targets)

    class _Audience:
        def __init__(self, name, targets, groups=("1",)):
            self.name = name
            self.groups = groups
            self.book = _Book(targets)

    sent: list[tuple[list[str], str]] = []

    async def _fake_send(groups, text):
        sent.append((list(groups), text))
        return len(groups)

    mr.send_to_groups = _fake_send  # 掐掉真发送：只留「发没发、发了什么」

    def _snap(names):
        return McSnapshot(
            reachable=True,
            target_id="t1",
            count=len(names),
            names=list(names),
            names_complete=True,
            names_source="rcon",
        )

    target = ServerTarget(
        id="t1", name="自测服", kind="vanilla", group="", timeout=5.0, rcon_timeout=5.0
    )
    audience = _Audience("自测关联", [target])
    state = mr._AudienceState()
    mr._audiences.clear()
    mr._audiences[audience.groups[0]] = state
    mr._targets.clear()

    print("== 0. 静默窗口取值 ==")
    for quiet_obj, key, label in (
        (mr._QUIET, "MC_QUIET_HOURS", "MC"),
        (ar._QUIET, "OOPZ_QUIET_HOURS", "oopz"),
    ):
        raw = os.environ.get(key, "0-9")
        check(
            quiet_obj.window == parse_quiet_hours(raw),
            f"{label} 解析出的窗口与 .env 一致（{key}={raw!r} → {quiet_obj.span}）",
        )
        if not raw.strip():
            check(quiet_obj.window is None, f"{label}：空值 = 不静默")
        elif os.environ.get(key) is None:
            check(quiet_obj.window == (0, 9), f"{label}：没写这个键时走代码默认 0-9")

    async def tick(names, status=(), quiet=True):
        await mr._tick_audience(audience, {"t1": _snap(names)}, list(status), quiet=quiet)

    print("\n== 1. 第一轮建基线：只记基线，不发消息 ==")
    asyncio.run(tick(["A"]))
    check(sent == [], "建基线那一轮不发消息")
    check(state.known.get("t1") == frozenset({"A"}), "基线建起来了")

    print("\n== 2. 静默时段内有人进服：一条都不发，但基线要推进 ==")
    asyncio.run(tick(["A", "B"]))
    check(sent == [], "B 进服没有推送（静默生效）")
    check(
        state.known.get("t1") == frozenset({"A", "B"}),
        "B 进了基线 —— 不推进的话 09:00 会把他当「刚进服」补报",
    )
    check(state.pending == [], "静默前攒下的事件被丢掉，不会等出静默补发")

    print("\n== 3. 连续两次静默进服也不积压 ==")
    asyncio.run(tick(["A", "B", "C"]))
    check(sent == [] and state.pending == [], "两次静默进服都没有积压")

    print("\n== 4. 出静默后恢复：新进服的人照常发 ==")
    asyncio.run(tick(["A", "B", "C", "D"], quiet=False))
    check(len(sent) == 1, f"恢复后发了 {len(sent)} 条（期望 1）")
    if sent:
        check("D" in sent[-1][1], "发的那条里有新进服的 D")
        check(
            "B" not in sent[-1][1] and "C" not in sent[-1][1],
            "没有把静默期间的 B/C 补发出来",
        )

    print("\n== 5. 掉线/恢复不受静默管辖 ==")
    sent.clear()
    asyncio.run(tick(["A", "B", "C", "D"], [StatusEvent(target_id="t1", down=True, streak=2)]))
    check(len(sent) == 1, f"静默时段内掉线仍然发了 {len(sent)} 条（期望 1）")
    if sent:
        check("自测服" in sent[-1][1], "发的是那台服的掉线提醒")

    # ---------------- oopz：进频道欢迎 ----------------
    # MC 那边是「有人进服」，oopz 这边是「有人进语音频道」，同一个坑：静默期间不欢迎
    # 没问题，但**基线不能不更新** —— 否则 09:00 会把整晚进过频道的人一次性全欢迎一遍。
    print("\n== 6. oopz：静默期间进频道，不欢迎但进基线 ==")

    class _M:
        def __init__(self, uid):
            self.uid, self.is_bot = uid, False

    class _Chan:
        def __init__(self, cid, name):
            self.channel_id, self.name = cid, name

    class _Grp:
        def __init__(self, chans):
            self.channels = chans

    class _Area:
        area_id, name = "a1", "自测域"

    class _Res:
        def __init__(self, channel_members):
            self.channel_members = channel_members

    class _User:
        def __init__(self, uid):
            self.uid, self.name = uid, f"名字{uid}"

    class _FakeBot:
        """假 oopz 客户端：只实现 `_check_joins` 走的那些方法。

        方法必须挂在 `.areas` / `.channels` / `.person` 三个命名空间下，跟真 SDK 一样
        —— 直接挂在 bot 上会 AttributeError，而那个异常会被 `_check_joins` 自己的
        except 吞成一行 warning `continue`，表现是「一轮下来什么也没发生」：
        「静默期间没欢迎」这种断言会**因为错的原因通过**。
        """

        def __init__(self):
            self.members = ["u1"]  # 当前在频道里的 uid
            self.areas = SimpleNamespace(
                get_joined_areas=self._joined, get_area_channels=self._chans
            )
            self.channels = SimpleNamespace(get_voice_channel_members=self._members_of)
            self.person = SimpleNamespace(get_person_infos_batch=self._infos)

        async def _joined(self):
            return [_Area()]

        async def _chans(self, area_id):
            return [_Grp([_Chan("ch1", "游戏开黑")])]

        async def _members_of(self, area):
            return _Res({"ch1": [_M(u) for u in self.members]})

        async def _infos(self, uids):
            return [_User(u) for u in uids]

    fake = _FakeBot()

    async def _fake_get_client():
        return fake

    ar.send_to_groups = _fake_send  # 同一本账：sent
    ar._get_client = _fake_get_client
    ar._filter_areas = lambda joined: list(joined)  # 不受本机 OOPZ_TARGET_AREAS 影响
    ar._known, ar._initialized = {}, False
    sent.clear()

    async def oopz_round(uids, quiet):
        # 时钟不可控，所以直接改窗口而不是改 datetime：now 一定落在 (h, h+1) 里，
        # 取补集 (h+1, h+2) 则一定不在 —— 两个方向都确定，跟跑测试的时刻无关。
        hour = datetime.now().hour
        span = (hour, (hour + 1) % 24) if quiet else ((hour + 1) % 24, (hour + 2) % 24)
        ar._QUIET.window, ar._QUIET._quiet = span, None
        fake.members = list(uids)
        await ar._check_joins()

    asyncio.run(oopz_round(["u1"], quiet=True))
    check(sent == [], "第一轮只建基线，不欢迎（和机器人刚启动时一样）")
    check(ar._known.get(("a1", "ch1")) == {"u1"}, "oopz 基线建起来了")

    asyncio.run(oopz_round(["u1", "u2"], quiet=False))
    check(len(sent) == 1, f"非静默时 u2 进频道 → 欢迎了 {len(sent)} 条（期望 1）")
    if sent:
        check("名字u2" in sent[-1][1], "欢迎的是 u2")

    sent.clear()
    asyncio.run(oopz_round(["u1", "u2", "u3"], quiet=True))
    check(sent == [], "静默时段内 u3 进频道没有欢迎（静默生效）")
    check(
        ar._known.get(("a1", "ch1")) == {"u1", "u2", "u3"},
        "u3 进了基线 —— 不推进的话 09:00 会把他当「刚进频道」补欢迎",
    )

    asyncio.run(oopz_round(["u1", "u2", "u3"], quiet=False))
    check(sent == [], "出静默后没有把静默期间的 u3 补欢迎出来")

    print("\n== 7. 静默窗口配置项的处理 ==")
    # 没配 / 留空 / 配错三种，行为各不同；这里只钉「配错不许抛」这一条 ——
    # 抛了就是整只机器人起不来，而它本来只是个可选功能。
    bad = QuietWindow("__MC_CHECK_不存在的键__", "自测", "任何东西")
    check(bad.window == (0, 9) and bad.enabled, "没配这个键 → 走代码默认 0-9")
    os.environ["__MC_CHECK_坏值__"] = "0-9-5"
    try:
        broken = QuietWindow("__MC_CHECK_坏值__", "自测", "任何东西")
    except Exception as exc:  # noqa: BLE001 — 抛了就是 FAIL，这里就是要抓住它
        check(False, f"配错时不该抛异常，却抛了 {exc!r}")
    else:
        check(broken.window is None and not broken.enabled, "配错 → 降级成不静默，不抛")
    finally:
        os.environ.pop("__MC_CHECK_坏值__", None)
    os.environ["__MC_CHECK_空值__"] = ""
    try:
        blank = QuietWindow("__MC_CHECK_空值__", "自测", "任何东西")
        check(blank.window is None and not blank.enabled, "留空 → 不静默")
    finally:
        os.environ.pop("__MC_CHECK_空值__", None)

    print()
    print(f"== 静默自测结果：{'全部通过' if not failed else f'{failed} 项失败'} ==")
    return 1 if failed else 0


# ---------------------------------------------------------------- 实机探测

def _verdict(target, snap) -> tuple[bool, str]:
    """给单个目标下一个短结论：`(是否正常, 一句话说明)`。

    判定**必须按 kind 分支**。代理的 `names_complete` 恒为假（Velocity 不上报名单，
    SLP 的 sample 也不是全群组名单），拿名单完整性去卡它，会把一台完全正常的代理
    判成故障、并把人指去查一个它本来就不该有的东西。判代理只看 reachable。
    """
    from plugins_napcat._shared.mc import API_AUTH, SLP_UNPARSEABLE

    if not snap.reachable:
        # 凭证失效不是「不可达」：服务好得很，是我们进不去，下一动作是去要新 token
        if snap.error_kind == API_AUTH:
            return False, "接口不认我们的凭证（401）"
        return False, "应答无法解析" if snap.error_kind == SLP_UNPARSEABLE else "不可达"
    if not target.serves_names:
        return True, f"代理可达，报出全群组 {snap.count} 人"
    if snap.names_complete:
        return True, f"名单完整（{snap.count} 人）"
    return False, f"名单不完整（{snap.count} 人在线）"


def _advice(target, snap) -> None:
    """失败目标的排查方向。只在真失败时打印，所以不必吝惜文字。

    只被 `_verdict` 判为 False 的目标调用，所以「代理」那一类到不了这里
    （代理只要可达就算正常），不必在这里再判一次 kind。
    """
    from plugins_napcat._shared.mc import API_AUTH, SLP_UNPARSEABLE

    if not snap.reachable:
        if snap.error_kind == API_AUTH:
            # 401 的下一步动作是「去要新 token」，查网络纯属白费功夫（同 mcrender）
            print("       群组接口拒绝了我们的 token（401）—— 这不是网络问题。")
            print("       对方轮换过 token 的话去要一份新的，填进 mcs_servers.toml 的")
            print("       api.token；隧道和对方服务本身可能好得很。")
            return
        if target.is_api or target.source:
            # 接口这条通道的失败**分三种**，三种的排查方向完全不同（见 mcbridge）
            print("       接口取数失败：先跑 --api 直接看接口（/health 免鉴权，")
            print("       所以它能通就说明隧道和服务都在，问题在 token 或路径）。")
            print("       三条路径的形状见 DEPLOY.md 4.1 的「走群组接口」。")
            return
        if snap.error_kind == SLP_UNPARSEABLE:
            # 服务端**应答了**，只是回的不算合法状态响应。报「不可达」会把人指去
            # 查防火墙，而真正该看的是「是不是还在启动」。
            print("       服务端应答了，内容却不是合法的状态响应 —— 这不是网络问题。")
            print("       大整合包（GTNH 这类）启动期间就是这样：端口已占、能应答，")
            print("       玩家列表还没就绪。等它加载完再跑一次即可。")
            print("       若一直如此，则是服务端/插件改坏了 SLP，看上面那条 cause 链。")
        else:
            print("       服务器不可达：检查 mcs_servers.toml 里的 host/port、")
            print("       服务器是否在跑、防火墙。")
        return

    print("       名单不完整 → 进服提醒会**静默暂停**（不会误报，但也不推消息）：")
    if target.is_api or target.source:
        print("       这台走的是群组接口，名单是**对方插件**给的（我们原样报）。")
        print("       人数和名单条数对不上，就是对方那边报了在线人数却没给全名单 ——")
        print("       先跑 --api 看接口原文，再看上面那行备注。")
    elif not target.rcon_enabled:
        print("       该目标没配 rcon.password（mcs_servers.toml）。")
        print("       在线人数 ≤12 时 SLP 的玩家样本本身就是完整名单；超过就必须开 RCON。")
    else:
        print("       开了 RCON，却没拿到与 SLP 人数一致的名单 —— 看上面那行 `list` 原文。")
    print("       配置步骤见 mcs_servers.toml.example 与 DEPLOY.md。")


def _pick_audience(name: str):
    """按名字挑一条群关联，返回 `(该关联的投影 book, 关联, 退出码)`。

    不传名字 = 取**第一条**（配置文件里的顺序），并在输出里明写是哪条 ——
    「哪几台服」现在是**按群**定的，不说清楚在按谁的视角探测，输出里的服务器集合
    就是个来历不明的集合。

    挑不到就返回 `(None, None, 退出码)`：**绝不退回全量表**。站错视角的探测结果
    看着完全正常（它确实探到了几台服），只是那几台不一定是你要问的那个群能用的。
    """
    from plugins_napcat._shared.mcaudiences import audiences_path, default_config
    from plugins_napcat._shared.mcservers import ServerConfigError

    try:
        config = default_config()
    except ServerConfigError as exc:
        print(f"== MC 配置读不了 ==\n   [!!] {exc}")
        return None, None, 1

    auds = config.audiences
    if not auds:
        # parse_audiences 保证了 ≥1 条，所以这里理论上到不了；留着是因为「静默按空
        # 关联跑」的后果是打出一屏什么都没有的输出，比报错难查得多。
        print(f"== {audiences_path()} 里没有任何 [[audience]]，没有可用的视角 ==")
        return None, None, 1

    if not name:
        picked = auds[0]
        print(f"== 视角：关联「{picked.name}」（第一条；用 --audience <名字> 换一条）==")
        print(f"   覆盖的群：{'、'.join(picked.groups)}")
        print()
        return picked.book, picked, 0

    for candidate in auds:
        if candidate.name == name:
            print(f"== 视角：关联「{candidate.name}」==")
            print(f"   覆盖的群：{'、'.join(candidate.groups)}")
            print()
            return candidate.book, candidate, 0

    print(f"== 没有叫 {name!r} 的群关联 ==")
    print(f"   {audiences_path()} 里的关联：{'、'.join(a.name for a in auds)}")
    print("   跑 --list-audiences 看每条覆盖哪些群、关联了哪几台服。")
    return None, None, 2


async def _live(query: str = "", audience: str = "") -> int:
    """实机探测。query 为空 = 探测所选关联的全部目标，否则按服名解析出**单个**目标。

    `audience` 是本条命令站在哪个群关联的视角上。**默认第一条**而不是「全量表」——
    脚本的承诺是「这里跑得通的写法，群友打出来也一定跑得通」，而群友永远站在某条
    关联里：全量表的服名在群里可能根本不存在（跨关联隔离），反之也成立。
    """
    from plugins_napcat._shared.mc import fetch_snapshots
    from plugins_napcat._shared.mcservers import ServerConfigError

    book, picked, code = _pick_audience(audience)
    if book is None:
        return code
    if not book.targets:
        print(f"== 关联「{picked.name}」里没有任何服务器，无可探测 ==")
        print("   它的 targets 是空的 —— 群里 @查询 会回「本群关联的服务器还没接入」。")
        return 1

    if query:
        res = book.resolve(query)
        if not res.ok:
            # 两种失败给的方向不同，且**都不能静默退回「探测全部」**——否则
            # 打错一个服名（`--target bing`）会变成一次看起来完全正常的全量探测，
            # 让人以为「这台没问题」。
            if res.ambiguous:
                print(f"== 服名 {query!r} 有歧义 ==")
                print(f"   候选：{'、'.join(res.candidates)}")
                print(f"   打全一点；或跑 --list-audiences 看「{picked.name}」关联了哪些目标。")
            else:
                print(f"== 认不出服名 {query!r} ==")
                print(f"   可选：{'、'.join(t.name for t in book.targets)}")
                print("   （id / 名字 / 别名 / 唯一前缀都认，忽略大小写与全角）")
                # 关联外的服名走到这里。**必须点破** —— 否则「这脚本怎么查不到 gtnh」
                # 会把人指去改配置里的名字，而实际原因只是这个视角下没有它。
                print(f"   ⚠️ 服名解析**限定在关联「{picked.name}」内**（这是群里的隔离规则）。")
                print("      它关联外的服在这里查不到是正常的，不是配置写错了 ——")
                print("      想探别的服就加 --audience <那条关联的名字>，或看 --list-audiences。")
            return 2
        targets = [res.target]
    else:
        targets = list(book.targets)

    # 并发探测：一轮耗时取 max 不取和，所以加子服不会让这条命令越来越慢。
    snaps = await fetch_snapshots(targets)

    failed: list[tuple[object, object]] = []
    print(f"== 实机探测：{len(targets)} 个目标（并发）==")
    print()
    for target, snap in zip(targets, snaps):
        ok, why = _verdict(target, snap)
        print(f"--- {'[OK]' if ok else '[!!]'} {target.name} [{target.id}] {target.kind} —— {why}")
        # 三条通道的地址分三种，逐个说清这台靠哪条取数：接口型根本没有游戏端口，
        # 打一行「SLP -」只会让人以为配漏了 host。
        print(f"    SLP   {target.game_addr}" if target.host else "    SLP   ——（没有游戏端口）")
        if target.is_api:
            print(f"    接口  {target.api.url}（整组共用一份 /status）")
        elif target.source:
            hub = target.hub
            src = f"{hub.name} 的接口" if hub is not None else f"目标 {target.source} 的接口"
            print(f"    数据  从 {src} 里取（servers[].name = {target.source_name!r}）")
        if target.is_api or target.source:
            print("    RCON  ——（走接口，不发 RCON）")
        elif not target.serves_names:
            print("    RCON  ——（代理不出分服名单，跳过）")
        elif not target.rcon_enabled:
            print("    RCON  ——（没配 rcon.password，跳过）")
        else:
            print(f"    RCON  {target.rcon_addr}")
        if snap.raw_list:
            # 各服务端/语言/插件的 `list` 文案都不一样，解析对不上时唯一能看的就是这行
            print(f"    `list` 原文: {snap.raw_list!r}")
        elif target.serves_names and target.rcon_enabled and not snap.reachable:
            print("    `list` 原文: （SLP 不通，没去发 RCON）")

        if not snap.reachable:
            # 不可达时**不打**在线人数/名单：count 恒为 0，打出来长得和「在线但 0 人」
            # 一模一样，而这两者要采取的行动完全相反。
            print(f"    探测失败  {snap.error}")
        else:
            print(f"    在线人数  {snap.count} / {snap.max_players}")
            print(f"    延迟      {snap.latency}")
            print(f"    版本      {snap.version or '(未知)'}")
            # 人数来源只有接口型目标会出现第二答案（接口 or SLP 复查）——它是
            # 「这个数字有多可信」的线索，所以跟着接口这条通道一起打
            if target.is_api or target.source:
                print(f"    人数来源  {snap.count_source}")
            print(f"    名单来源  {snap.names_source}")
            print(f"    名单条数  {len(snap.names)}")
            print(f"    名单完整  {snap.names_complete}")
            if snap.names:
                print(f"    名单      {snap.names[:20]}{' …' if len(snap.names) > 20 else ''}")
            if snap.error:
                print(f"    备注      {snap.error}")
        if not ok:
            failed.append((target, snap))
        print()

    # 结论用 ASCII 标记：Windows 控制台是 GBK，打不出 ✓/✗
    good = len(targets) - len(failed)
    print(f"== 结论：{good}/{len(targets)} 个目标正常 ==")
    if not failed:
        print("   [OK] 全部正常，进服提醒可以正常工作。")
        return 0
    for target, snap in failed:
        # 详细提示只对失败目标打，且**带名字** —— 多目标时一句无归属的「服务器不可达」
        # 根本不知道说的是哪台。
        print(f"   [!!] {target.name} [{target.id}]")
        _advice(target, snap)
        print()
    return 1


async def _api(audience: str = "", query: str = "") -> int:
    """直接看一眼群组接口：/health、/status、/whitelist 三条都打，原文照登。

    和上面的实机探测**不是一回事**，所以另开一条命令：那边问的是「机器人看到的
    每台服是什么样」，这边问的是「接口到底给了什么」。接口的形状是**对方**实现的，
    一旦他们改了字段名或路径，那边只会表现成「某几台取不到数据」，看不出是哪一步错的。

    这一条最有用的是最后那两行清单：接口里有、而我们一台都没挂的子服（[未挂]），
    以及代理 `transit` 里点名过的中转服（[中转]）。前者不挂就不会被探测，也就不会
    报错，只是「在线人数里少了几个人」这种谁都不会注意到的偏差；后者的名字一旦和
    对方 velocity.toml 对不上，只是「人数没扣掉」——同样是看不出来的偏差。

    `query` 收窄到某一个接口型目标（按服名解析，和群里同一套名字）。
    """
    from plugins_napcat._shared import mc as _mc
    from plugins_napcat._shared import mcbridge
    from plugins_napcat._shared.mcaudiences import default_config
    from plugins_napcat._shared.mcservers import ServerConfigError

    book, picked, code = _pick_audience(audience)
    if book is None:
        return code

    hubs = [t for t in book.targets if t.is_api]
    if query:
        res = book.resolve(query)
        if not res.ok:
            print(f"== 认不出服名 {query!r} ==")
            print(f"   本条关联的可选：{'、'.join(t.name for t in book.targets)}")
            return 2
        if not res.target.is_api:
            # 认得出但不是接口型：**直说**，别退回「全都探一遍」——那会让人以为
            # 这台也有接口，而它的数据其实来自 SLP/RCON。
            transport = res.target.transport
            print(f"== {res.target.name} [{res.target.id}] 不是接口型目标（{transport}）==")
            if res.target.source:
                print(f"   它的数据来自 {res.target.source} 的接口，直接跑 --api 看那一台。")
            return 2
        hubs = [res.target]
    if not hubs:
        print(f"== 关联「{picked.name}」里没有接口型目标 ==")
        print("   接口型 = mcs_servers.toml 里配了 api 段的那台（通常是代理）。")
        print("   本条的 targets：" + ("、".join(t.name for t in book.targets) or "（空的）"))
        return 1

    # 漏挂清单要拿**全量表**算：一台子服可能属于别的群关联，在别的群里是挂着的，
    # 那就不算漏。只看本条视图会把「别人挂了」误报成「没人挂」。
    try:
        full = default_config().book
    except ServerConfigError as exc:
        print(f"== MC 配置读不了 ==\n   [!!] {exc}")
        return 1

    bad = 0
    for hub in hubs:
        print(f"== 接口目标：{hub.name} [{hub.id}] ==")
        print(f"   url     {hub.api.url}")
        print(f"   超时    {hub.timeout:g}s")
        print()

        try:
            healthy = await mcbridge.fetch_health(hub.api, hub.timeout)
            print(f"   /health    → {'ok' if healthy else '应答了，但 ok 不是 true'}")
        except mcbridge.BridgeError as exc:
            # /health 免鉴权，所以它失败基本就是网络/服务的问题，不是 token
            print(f"   /health    → [!!] {type(exc).__name__}: {exc}")
            bad += 1
            print()
            continue

        try:
            status = await mcbridge.fetch_status(hub.api, hub.timeout)
        except mcbridge.BridgeAuthError as exc:
            # 单独一条：token 错的话后面两条也都不用试了，而且要去要新 token
            print(f"   /status    → [!!] 认证失败（401）：token 可能被对方轮换了")
            print(f"                {exc}")
            bad += 1
            print()
            continue
        except mcbridge.BridgeError as exc:
            print(f"   /status    → [!!] {type(exc).__name__}: {exc}")
            bad += 1
            print()
            continue

        tracked = {
            t.source_name for t in full.targets if t.source == hub.id
        }
        # 中转服：配在 hub 的 transit 里，人数要从「全群组」里扣掉（见 mc._transit_count）。
        # 名字写错的表现只是「人数没扣掉」，所以这里逐个对号，并跟下面那张「没挂」的
        # 清单分开说 —— 它们不是漏挂，是**故意不挂**的。
        transit = set(hub.transit)
        excluded, note = _mc._transit_count(hub, status)
        head = f"   /status    → 全群组 {status.proxy_online} 人"
        if excluded:
            head += f"（扣掉中转服 {excluded} 人 → 群里报 {max(0, status.proxy_online - excluded)} 人）"
        print(f"{head}，{len(status.servers)} 台子服")
        for server in status.servers:
            if server.name in transit:
                mark = "[中转]"
            else:
                mark = "[已挂]" if server.name in tracked else "[未挂]"
            names = "、".join(server.players)
            tail = f"　{names}" if names else ""
            print(f"     {mark} {server.name:12s} {server.online} 人{tail}")
        if note:
            print(f"   [i] {note}")
        missing = [
            s.name for s in status.servers if s.name not in tracked and s.name not in transit
        ]
        if missing:
            # 不挂的子服**不会报错**，只会让人数悄悄少一块 —— 所以要点名说出来
            print(
                f"   [!] 上面 {len(missing)} 台我们没挂：{'、'.join(missing)}"
                f"（它们的玩家会算进群组总数，但不在任何分服行里）"
            )
        print()

        try:
            whitelist = await mcbridge.fetch_whitelist(hub.api, hub.timeout)
        except mcbridge.BridgeError as exc:
            print(f"   /whitelist → [!!] {type(exc).__name__}: {exc}")
            bad += 1
            print()
            continue
        entries = "、".join(whitelist.entries) or "（空的）"
        print(f"   /whitelist → enabled={whitelist.enabled}，{len(whitelist.entries)} 条")
        print(f"     {entries[:200]}{' …' if len(entries) > 200 else ''}")
        if not whitelist.enabled:
            # 「配了却不生效」：名单能读能改，但代理层的拦截是关的，谁都能进
            print("   [!] enabled=false：代理层的白名单拦截**没开**，谁都能进群组服。")
            print("       名单增减照常生效，但拦不住人 —— 要在对方那侧打开 whitelist-enabled。")
        print()

    if bad:
        print(f"== 结论：{bad} 项失败 ==")
        return 1
    print("== 结论：接口三条路径都正常 ==")
    return 0


async def _whitelist(audience: str = "", query: str = "") -> int:
    """只读地看一眼服务端白名单，确认解析对不对。

    **白名单归属是按群关联区分的**：发给哪几台服、每台用什么命令前缀都写在
    `[[audience]].whitelist` 里，所以这条命令必须站在某条关联上跑（默认第一条）。
    不站在关联上就问不出「这个群的白名单发给谁」——那正是本次改造要解决的问题。

    `query` 收窄到其中一台（`--target bingo` 或裸写 `bingo`）。**走的是同一个
    `pick_whitelist`**，不在这里另写一套名字比对：脚本对使用者的承诺是「脚本里跑得通
    的写法，群里打出来也一定跑得通」，自己再实现一遍就等于把承诺兑现两遍，而两份
    一定会漂。

    逐台**串行**：诊断输出要稳定、可读、顺序与配置一致，不为省几秒把顺序交给 gather。

    两条通道都走：RCON 的照旧，接口型的打 `enabled` + 名单数组（见
    `_whitelist_via_api`）。所以「解析对不对」这句话只对 RCON 那几台成立 ——
    接口给的是结构化数据，没有解析这一层。

    刻意**不发** add / remove：诊断脚本会真实改动服务端，而它没有任何清理逻辑——
    脚本中途挂掉，whitelist.json 就被留在谁也不知道的状态。要测写入，去群里用一次
    性假名字走完整链路，最后用 list 确认收尾干净。
    """
    from plugins_napcat._shared.mc import RconError, rcon_command
    from plugins_napcat._shared.mcaudiences import audiences_path
    from plugins_napcat._shared.mcadmin import parse_whitelist_names
    from plugins_napcat._shared.mcservers import (
        PICK_AMBIGUOUS,
        PICK_NEED_NAME,
        PICK_NOT_WHITELIST,
    )

    book, picked, code = _pick_audience(audience)
    if book is None:
        return code

    routes = book.whitelist_routes
    if not routes:
        print(f"关联「{picked.name}」没有指定白名单服：该群的白名单管理不可用。")
        print(f"   在 {audiences_path()} 里给这条 [[audience]] 加（**一组表**，每台服一项）：")
        print('   whitelist = [{ target = "<目标 id>", command = "<插件命令>" }]')
        print("   （群里会回「本群没有指定白名单服」）")
        return 1

    if query:
        pick = book.pick_whitelist(query)
        if not pick.ok:
            # 每一态的修法都不一样，一条笼统的「认不出这个服名」会让人往错的方向改。
            print(f"== --target {query!r} 没认出来：{pick.reason} ==")
            if pick.reason == PICK_NEED_NAME or pick.reason == PICK_AMBIGUOUS:
                print(f"   本群的白名单服：{'、'.join(pick.candidates)}")
            elif pick.reason == PICK_NOT_WHITELIST and pick.found is not None:
                print(f"   {pick.found.name} 是本群关联的服，但它不是白名单服。")
                print(f"   本群的白名单服只有：{'、'.join(pick.candidates)}")
            else:
                names = "、".join(t.name for t in book.targets) or "（一台都没有）"
                print(f"   本群能查的是：{names}（id / 名字 / 别名 / 唯一前缀都认）")
            return 2
        routes = (pick.route,)
        print(f"== --target {query!r} → 收窄到 {pick.route.name} [{pick.route.id}] ==")
        print()

    ok_count = 0
    # 接口报「名单开着但拦截是关的」的那些台（enabled=false）。攒到最后一起说：
    # 单看某一台的名单会以为白名单生效了，而这恰好是「配了却不生效」那一类。
    intercept_off: list[str] = []
    for index, route in enumerate(routes, start=1):
        if len(routes) > 1:
            print(f"---- 第 {index}/{len(routes)} 台 ----")
        if not route.whitelist_ready:
            print(f"== 白名单目标：{route.name} [{route.id}] ==")
            print("   [!!] 两条通道都没配（既没有 rcon.password 也没有 api）：")
            print("        命令发不出去，也就没有名单可看。")
            print("        （群里会回「白名单服 X 两条通道都没配」）")
            print()
            continue

        if route.target.is_api:
            ok_count += await _whitelist_via_api(route, intercept_off)
            continue

        # 命令前缀来自这一台的 route.command，**不是**硬编码的 "whitelist"。
        # 代理上线后多半是 globalwhitelist，这里跟着配置走才能看出真实命令对不对。
        command = f"{route.command} list"
        print(f"== 白名单目标：{route.name} [{route.id}] {route.rcon_addr} ==")
        print(f"   RCON 命令前缀（本条关联里这一台的 whitelist.command）：{route.command!r}")
        print("   群里打的是触发词（.env 的 MC_ADMIN_TRIGGER），与它无关 —— 代理上线后")
        print("   必然是「群里打 whitelist、RCON 里发 globalwhitelist」这种分叉。")
        print()

        try:
            raw = await rcon_command(route.target, command)
        except RconError as exc:
            print(f"== RCON `{command}` 失败 ==\n   [!] {type(exc).__name__}: {exc}")
            print()
            continue

        print(f"== RCON `{command}` 原始输出 ==")
        print(f"   原文: {raw!r}")
        print()
        print("== 解析结果 ==")
        names = parse_whitelist_names(raw)
        if names is None:
            print("   [!!] 判不出来（输出为空，或没有冒号说明格式被改写过）。")
            print("        不是故障——但本服的加白命令会回「未能验证」而不是「已添加」。")
            print()
            continue
        print(f"   共 {len(names)} 人")
        print(f"   {names[:20]}{' …' if len(names) > 20 else ''}")
        print()
        ok_count += 1

    if len(routes) > 1:
        print(f"== 汇总：{len(routes)} 台中 {ok_count} 台正常 ==")
        print()
    if ok_count != len(routes):
        print("== 结论 ==")
        # 单台时不写「1 台没读到」：只有一台的时候「哪台」没有信息量，反而像是漏报了别的。
        if len(routes) > 1:
            print(f"   [!!] {len(routes) - ok_count} 台没读到（原因见上）。")
        else:
            print("   [!!] 这台没读到（原因见上）。")
        return 1
    if intercept_off:
        print("== 结论 ==")
        print(f"   [!!] {'、'.join(intercept_off)} 的名单读得到，但接口报 enabled=false：")
        print("        代理层的**白名单拦截是关的**，谁都能进服。加白命令会照常成功、")
        print("        名单也会变长，只是拦不住人。去对方 velocity.toml 的 whitelist 段落确认。")
        return 1
    print("== 结论 ==")
    print("   [OK] 解析正常，白名单命令可以正常判定成败。")
    if any(r.target.is_api for r in routes):
        print("   接口那几台连「名单有没有生效」都看得到（上面的 enabled）；RCON 那几台")
        print("   看不到服务端有没有开 white-list —— 若名单里有人却仍能自由进出，")
        print("   去 server.properties 确认。")
    else:
        print("   注意：本脚本只能看到 whitelist.json 的内容，看不出服务端有没有开")
        print("   white-list。若白名单里有人却仍能自由进出，去 server.properties 确认。")
    return 0


async def _whitelist_via_api(route: "WhitelistRoute", intercept_off: list[str]) -> int:
    """接口型白名单服的只读一览。返回 1 = 这台算正常，0 = 没读到。

    **刻意不做「解析」这一段**：RCON 那条路上 parse_whitelist_names 是个真实的失败点
    （插件改文案就把 `list` 的输出读成 None），接口给的是结构化数组，没有对应的坑。
    把 RCON 的「判不出来」原样搬过来只会让人以为这里也有一套解析要验。

    `intercept_off` 由调用方持有并汇总 —— 这里只负责把「名单在、拦截关」这件事
    记进去，因为它是**跨台**的结论（单台那一屏很容易被下一台冲掉）。
    """
    from plugins_napcat._shared.mcbridge import BridgeError, fetch_whitelist

    print(f"== 白名单目标：{route.name} [{route.id}] 接口 {route.target.api.url} ==")
    print("   这一台走群组接口的 GET /whitelist：**没有命令前缀** —— 前缀是 RCON 那边")
    print("   插件命令的事，接口直接给结构化数据（本条的 whitelist.command 对这台无效）。")
    print()

    try:
        whitelist = await fetch_whitelist(route.target.api, route.target.timeout)
    except BridgeError as exc:
        print(f"== 接口 GET /whitelist 失败 ==\n   [!] {type(exc).__name__}: {exc}")
        print()
        return 0

    print("== 接口 GET /whitelist ==")
    print(f"   enabled（代理层的白名单总开关）: {whitelist.enabled}")
    print(f"   共 {len(whitelist.entries)} 人")
    print(f"   {list(whitelist.entries[:20])}{' …' if len(whitelist.entries) > 20 else ''}")
    if not whitelist.enabled:
        print("   [!!] enabled=false：名单本身没问题，但**代理层没在拦人**。")
        intercept_off.append(route.name)
    print()
    return 1


def _list_targets() -> int:
    """打印 mcs_servers.toml 解析出的目标、群关联摘要与警告。**不联网**。

    排查「新加的子服为什么没生效」最快的入口：先确认它有没有被读进来、
    名字能不能解析，再去管连通性。

    **必须连带把群关联也报出来**：一台服「被读进来了」和「群里能查到」是两件事，
    新服忘了关联给任何群时，这一段会正常列出它，群里却回「没有叫 X 的服」——
    这个脚本是 README/DEPLOY 指的第一入口，漏报比不报更误导。
    """
    from plugins_napcat._shared.mcaudiences import default_config
    from plugins_napcat._shared.mcservers import (
        ServerConfigError,
        book_path,
        round_budget,
    )

    # 走 book_path() 而不是写死 ROOT/mcs_servers.toml：它认 MCS_SERVERS_TOML，
    # 和 bot 运行期用的是同一个解析规则。
    path = book_path()
    # 用 default_config() 而不是 load_book()：这样群关联文件读不了时**也会**报出来，
    # 而不是打一句「无警告」然后让人以为配置没问题。
    try:
        config = default_config()
    except ServerConfigError as exc:
        print(f"== MC 配置读不了（先看 mcs_servers.toml，路径 {path}）==\n   [!!] {exc}")
        return 1

    book = config.book
    # 每个目标被哪些群关联引用着 —— 下面的孤儿标记就靠它
    seen_by: dict[str, list[str]] = {}
    for audience in config.audiences:
        for t in audience.book.targets:
            seen_by.setdefault(t.id, []).append(audience.name)

    # 这里**只读配置、一个网络包都不发**（刻意的：它的用途是排查「新加的子服为什么
    # 没生效」，网络探测只会把配置问题和服务端没开混在一起）。所以要显式说清楚这是
    # 配置视图不是实况 —— 否则「三台都关着却列出三个目标」看着像 bug。
    print(f"== 目标（{path}，只读配置、不探测）==")
    for t in book.targets:
        watchers = seen_by.get(t.id, [])
        # 孤儿标记写在同一行上，不另起一段：它是「新服没生效」最典型的成因，
        # 而看这个输出的人正是来查那件事的。
        mark = f"　← 被 {'、'.join(watchers)} 关联" if watchers else "　← ⚠️ 没被任何群关联"
        print(f"   {t.name}  [{t.id}]  {t.kind}{mark}")
        # 地址行按通道分支：接口型目标没有游戏端口，打「游戏 -　RCON -」等于一屏
        # 没配的东西，而它其实配得好好的 —— 只是走的是第三条通道。
        addr = f"游戏 {t.game_addr}    RCON {t.rcon_addr}"
        if t.is_api:
            addr = f"接口 {t.api.url}"
        elif t.source:
            src = t.hub.name if t.hub is not None else t.source
            addr = f"数据 {src} 的接口（servers[].name = {t.source_name!r}）"
        print(f"      {addr}    组 {t.group}")
        print(f"      名字 {'、'.join(t.keys)}    "
              f"超时 SLP {t.timeout:g}s / RCON {t.rcon_timeout:g}s")
        if t.is_api:
            print("      （代理：接口给整组数据，全群组人数不进分服合计）")
            if t.transit:
                # 名字与对方 velocity.toml 对不上时什么都不会发生 —— 扣不掉的表现是
                # 「人数偏大」，而偏大在群里看不出异样。所以这里必须点名打出来。
                print(
                    f"      （中转服 {'、'.join(t.transit)}：它们的人从「全群组」里扣掉，"
                    f"名字要与对方 velocity.toml 逐字相同）"
                )
        elif t.source:
            print(f"      （子服：数据从 {t.source} 的接口取，自己不发请求）")
        elif not t.serves_names:
            print("      （代理：SLP 只给全群组总人数，不出分服名单）")
        elif not t.rcon_enabled:
            print("      （没配 RCON 密码：不出名单，只能靠 SLP 样本，>12 人就不完整）")
    print()

    # 白名单**没有全量归属**了 —— 发给哪台、用什么前缀都是按群关联定的（见 mcs_audiences）。
    # 这里只报概要并指向 --list-audiences，免得同一件事有两个地方各说一半。
    print("== 群关联 ==")
    for audience in config.audiences:
        print(f"   {audience.summary()}")
        print(f"      {audience.whitelist_summary}")
        print(f"      推送   {audience.flags_summary}")
    print("   白名单是**按群关联**定的，逐条细节与命令前缀见：")
    # 用**正在跑的那个解释器**而不是写死 .venv\Scripts\python.exe：Windows 和 Linux
    # 的 venv 布局不同（Scripts/ vs bin/），而这行是要给人复制粘贴的。sys.executable
    # 两边都对，也不会因为将来换了 venv 名字而失效。
    print(f"       \"{sys.executable}\" tools/mc_check.py --list-audiences")
    print()

    # 一轮探测的耗时预算。**这是上界，不是典型值**：正常一轮在毫秒级，
    # 只有「SLP 通但 RCON 卡住」这种病态情况才会走满（RCON 可能重连一次所以算两倍）。
    # 目标之间是并发探测，所以取 max 而不是求和 —— 加子服不会让这个数变大。
    #
    # 基数必须是**「开着 watch 的关联覆盖到的目标并集」**，与 mcs/__init__.py 里
    # 启动日志那次完全一致（round_budget 的 docstring 要求两处一致）：全量表里可能
    # 有一台只被 watch = false 的关联引用的服，按它算的上界会永远顶着一条与轮询无关
    # 的告警 —— 告警被无视之后，真超了也看不出来。
    watched = [t.id for t in config.flag_targets("watch")]
    print("== 一轮耗时预算（上界，非典型值）==")
    if not watched:
        # 与 mcs/__init__.py 一致：没有 watch 关联时预算取 (0,0,0)，也就是不打告警。
        # 这里多说一句「为什么没有」，比打一行 0s 强 —— 看这个输出的人多半正在查
        # 「进服提醒怎么没反应」，而原因往往就是忘了写 watch = true。
        print("   没有任何 [[audience]] 写 watch = true，进服提醒不跑轮询，没有预算可言")
    else:
        slp, rcon, api, budget = round_budget(book.scoped(watched))
        print(f"   基数：开着 watch 的关联覆盖到的 {len(watched)} 台服"
              f"（全量表共 {len(book.targets)} 台，没开 watch 的不算）")
        # 接口那一项照样打出来（哪怕是 0s）：算式和右边的小计**必须加得起来**，
        # 否则配了接口型目标之后这里会莫名多出 5s，看着像算错。
        print(f"   SLP {slp:g}s + RCON {rcon:g}s ×2 + 接口 {api:g}s = 最坏 {budget:g}s")
        raw_interval = os.environ.get("MC_WATCH_INTERVAL_SEC", "").strip()
        if raw_interval:
            try:
                interval = float(raw_interval)
            except ValueError:
                print(f"   [!] MC_WATCH_INTERVAL_SEC 不是数字：{raw_interval!r}")
            else:
                if budget > interval:
                    # mc_reporter._watch_loop 是「跑完再补睡剩余时间」：sleep(max(1, 间隔-耗时))，
                    # 所以最坏情况**不会重叠**，只是把轮询周期从 10s 撑到 16s 左右 ——
                    # 进服发现晚几秒，不会堆积。
                    print(f"   [!] 上界 {budget:g}s 超过 MC_WATCH_INTERVAL_SEC={interval:g}s："
                          f"病态情况下周期被拉长到约 {budget + 1:g}s（不会重叠，只是变慢）")
                else:
                    print(f"   轮询间隔 MC_WATCH_INTERVAL_SEC={interval:g}s，够用")
    print()

    # 两层警告都打，内容不重复：book.warnings 是全量的（端口冲突、没配 rcon 密码），
    # config.warnings 是跨文件的（孤儿目标、.env 残留）。
    if book.warnings:
        print("== 目标警告（能加载，但这些目标在运行期有问题）==")
        for w in book.warnings:
            print(f"   [!] {w}")
        print()
    else:
        print("== 目标：无警告 ==")
        print()

    if config.warnings:
        print("== 群关联警告（跨两份文件）==")
        for w in config.warnings:
            print(f"   [!] {w}")
        print()
    else:
        print("== 群关联：无警告 ==")
        print()

    return 0


def _list_audiences() -> int:
    """逐条打印群关联：覆盖哪些群、关联哪几台服、白名单发给谁、孤儿目标。**不联网**。

    排查「新加的群为什么不响应」的第一入口。它和 mcs_audiences.toml.example 里写的
    那四个坑一一对应，所以每段输出都顺着那四条来组织。
    """
    from plugins_napcat._shared.mcaudiences import audiences_path, default_config
    from plugins_napcat._shared.mcservers import ServerConfigError, book_path

    try:
        config = default_config()
    except ServerConfigError as exc:
        print(f"== MC 配置读不了（mcs_servers.toml 在 {book_path()}）==\n   [!!] {exc}")
        return 1

    print(f"== 群关联（{audiences_path()}，只读配置、不探测）==")
    print(f"   目标表：{book_path()}")
    print()

    for index, audience in enumerate(config.audiences, start=1):
        print(f"--- 第 {index} 条：{audience.name}")
        print(f"    群号   {'、'.join(audience.groups)}")
        if audience.book.targets:
            for t in audience.book.targets:
                # 主服单独标出来：不带服名的 @查询 看的就是它，标错了查询结果会
                # 悄悄换一台服，而输出里那一长串目标看着完全正常。
                head = "（主服）" if t is audience.book.primary_target else ""
                # keys = (id, name, *aliases)，所以别名叫 keys[2:]。
                # 只列别名、不重复 id 和 name：那两个上面已经打了，重复一遍只会让
                # 人以为这台服有三个不同的名字。
                alias = f"　别名 {'、'.join(t.aliases)}" if t.aliases else ""
                print(f"    关联   {t.name} [{t.id}] {t.kind}{head}{alias}")
        else:
            # targets = [] 是合法的（群先建着、服还没搭），但必须让人一眼看出
            # 「这个群查什么都是空的」是**配置如此**，不是坏了。
            print("    关联   （空）—— 该群查询会回「本群关联的服务器还没接入」")
        print(f"    白名单 {audience.whitelist_summary}")
        # 推送开关单独一行：两个都没开时这个群「查询一切正常、却什么都收不到」，
        # 而它的表现（群里安安静静）和「机器人挂了」一模一样。
        print(f"    推送   {audience.flags_summary}")
        for w in audience.warnings:
            print(f"    [!] {w}")
        print()

    # 孤儿目标单列一段：它是「新服加了但群里查不到」的典型成因，而上面每条的
    # targets 里都看不到它（正是因为它谁的 targets 里都不在）。
    linked = {t.id for a in config.audiences for t in a.book.targets}
    orphans = [t for t in config.book.targets if t.id not in linked]
    print("== 没被任何群关联的目标（孤儿）==")
    if not orphans:
        print("   无 —— 每台服都至少有一个群能查到")
    else:
        for t in orphans:
            print(f"   [!] {t.name} [{t.id}]：群里打它回「没有叫 {t.id} 的服」")
        print("       把它们加进某条 [[audience]] 的 targets 才会有群能用。")
        print("       （只填了 mcs_servers.toml、忘了填 mcs_audiences.toml 就是这样）")
    print()

    if config.warnings:
        print("== 跨文件警告 ==")
        for w in config.warnings:
            print(f"   [!] {w}")
        print()
    else:
        print("== 跨文件：无警告 ==")
        print()
    return 0


_USAGE = (
    "可用参数：\n"
    "   --self-test            只跑解析自测，不联网\n"
    "   --quiet-test           跑夜间静默的行为自测（进服不发、基线照推进、掉线照发），\n"
    "                          不联网；要 init nonebot，得在有 .env 的机器上跑\n"
    "   --list-targets         打印 mcs_servers.toml 解析出的目标，不联网\n"
    "   --list-audiences       打印 mcs_audiences.toml 的每条群关联，不联网\n"
    "   --target <服名>        只实机探测这一个目标；配 --whitelist 时收窄到那一台\n"
    "   --whitelist            只读地看一眼服务端白名单（本条关联的全部白名单服，\n"
    "                          逐台列出；一台时就是那一台）\n"
    "   --api                  直接看群组接口：/health、/status、/whitelist 三条都打，\n"
    "                          并列出接口里有、我们却没挂的子服（配 --target 收窄一台）\n"
    "   --audience <关联名>    站在哪条群关联的视角上（默认第一条；影响 --target\n"
    "                          能认出的服名，以及 --whitelist 发给哪几台服）\n"
    "   （不带参数）           实机探测所选关联的全部目标"
)


def _parse_argv(argv: list[str]) -> tuple[str, str, str] | None:
    """把命令行解析成 `(动作, 服名, 关联名)`；返回 None 表示用法错误（提示已打印）。

    动作 ∈ self-test / quiet-test / list-targets / list-audiences / whitelist / api / live。
    抽成纯函数是为了能自测：「认不出的参数不许退化成实机探测」这条不能只靠读代码保证。
    """
    # 认不出的参数必须拦下。否则 `--help`（本脚本没有这个参数）或 `--targets`
    # 这类手滑会**静默退化成一次全量实机探测** —— 真的去连服务器、发 RCON 命令，
    # 却看起来像正常输出，排查时会被彻底带偏。
    #
    # 新增参数时**必须同时加进这个集合**：漏了不会自测失败，只会在真跑命令行时
    # 被当成不认识的参数拦下（退出码 2），而自测调的是纯函数，看不出这件事。
    # 只认值的那两个参数用 `--flag=值` 是允许的（见 _value），其余都是开关。
    switches = {
        "--self-test",
        "--quiet-test",
        "--list-targets",
        "--list-audiences",
        "--whitelist",
        "--api",
    }
    known = switches | {"--target", "--audience"}
    for arg in argv:
        flag, eq, _ = arg.partition("=")
        if flag.startswith("--") and flag not in known:
            print(f"== 不认识的参数 {arg!r} ==\n{_USAGE}")
            return None
        if eq and flag in switches:
            # 开关写成 `--self-test=1` 时 flag 仍在 known 里，但下面那些 `in argv`
            # 判断一个都匹配不上 → 静默退化成全量实机探测。与上一条是同一个坑。
            print(f"== {flag} 是个开关，后面不接 = 值 ==\n{_USAGE}")
            return None

    def _value(flag: str) -> str:
        """取 `--flag value` / `--flag=value` 的值。"""
        for i, arg in enumerate(argv):
            if arg == flag:
                return argv[i + 1] if i + 1 < len(argv) else ""
            if arg.startswith(flag + "="):
                return arg.partition("=")[2]
        return ""

    # --audience 对每条动作都可能有意义（--whitelist / --target / 全量探测），
    # 所以先取出来，再判动作。
    picked = _value("--audience")
    if "--audience" in argv or any(a.startswith("--audience=") for a in argv):
        if not picked.strip():
            print(f"== --audience 后面要跟一个关联名（例如 --audience 社团群）==\n{_USAGE}")
            return None

    if "--self-test" in argv:
        return "self-test", "", ""
    if "--quiet-test" in argv:
        return "quiet-test", "", ""
    if "--list-targets" in argv:
        return "list-targets", "", ""
    if "--list-audiences" in argv:
        return "list-audiences", "", ""

    # --target 的服名走和群里同一套解析，所以脚本能跑通的写法群友也一定能用。
    # **在判动作之前算**：--target 对 `--whitelist` 也有意义（收窄到那一台），
    # 放在 --whitelist 分支之后的话 `--whitelist --target bingo` 会被静默当成
    # 「列出全部」—— 命令看着成功了，只是没按你说的收窄。
    explicit = "--target" in argv or any(a.startswith("--target=") for a in argv)
    query = _value("--target") if explicit else ""
    if not explicit:
        # 裸给一个词也当服名：`mc_check.py bingo` 比 `--target bingo` 顺手。
        # 要排除 --audience 的值 —— 它是另一个位置的参数，不是服名。
        positional = [a for a in argv if not a.startswith("-")]
        if picked:
            positional = [a for a in positional if a != picked]
        query = positional[0] if positional else ""
    if explicit and not query.strip():
        print(f"== --target 后面要跟一个服名（例如 --target bingo）==\n{_USAGE}")
        return None

    if "--whitelist" in argv:
        return "whitelist", query, picked
    if "--api" in argv:
        return "api", query, picked
    return "live", query, picked


def main() -> int:
    parsed = _parse_argv(sys.argv[1:])
    if parsed is None:
        return 2
    action, query, audience = parsed
    if action == "self-test":
        return _self_test()
    if action == "quiet-test":
        return _quiet_test()
    if action == "list-targets":
        return _list_targets()
    if action == "list-audiences":
        return _list_audiences()
    if action == "whitelist":
        return asyncio.run(_whitelist(audience, query))
    if action == "api":
        return asyncio.run(_api(audience, query))
    return asyncio.run(_live(query, audience))


if __name__ == "__main__":
    raise SystemExit(main())
