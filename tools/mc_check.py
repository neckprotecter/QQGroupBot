r"""验证 Minecraft 服务器取数链路（SLP + RCON），不启动机器人。

用法：
    .venv\Scripts\python.exe tools\mc_check.py --self-test        # 只跑解析自测，不联网
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
import sys
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
        parse_command,
        plan_mutation,
        settle,
        whitelist_contains,
        whitelist_lookup,
        whitelist_matches,
    )
    from plugins_napcat._shared.schedule import seconds_until_slot
    from plugins_napcat._shared.mcservers import ServerConfigError, parse_book, round_budget
    from plugins_napcat._shared.triggers import detect, primary_keyword

    # MC 白名单命令解析用例：(说明, 输入, 期望动词, 期望玩家名, 期望失败原因)。
    # 失败用例的动词/玩家名都是 None（parse_command 返回 None, 原因）。
    _CMD_CASES: list[tuple[str, str, str | None, str | None, str]] = [
        ("基本 add", "whitelist add Steve", "add", "Steve", ""),
        ("基本 remove", "whitelist remove Steve", "remove", "Steve", ""),
        ("list（不带参数）", "whitelist list", "list", "", ""),
        ("大小写不敏感", "WHITELIST Add Steve", "add", "Steve", ""),
        ("前缀噪音不影响解析", "帮我 whitelist add Steve", "add", "Steve", ""),
        # 空白（含 \n）只当分隔符，真正拼进 RCON 的是正则校验过的名字
        ("换行只当分隔符", "whitelist add\nSteve", "add", "Steve", ""),
        ("缺子命令", "whitelist", None, None, ERR_USAGE),
        ("缺玩家名", "whitelist add", None, None, ERR_NAME),
        ("名字里有空格", "whitelist add Steve please", None, None, ERR_NAME),
        ("注入第二条命令", "whitelist add Steve; stop", None, None, ERR_NAME),
        ("路径式名字", "whitelist add ../Steve", None, None, ERR_NAME),
        ("非 ASCII 名字", "whitelist add 玩", None, None, ERR_NAME),
        ("超长名字（17 位）", "whitelist add aaaaaaaaaaaaaaaaa", None, None, ERR_NAME),
        ("动词不在白名单", "whitelist kick Steve", None, None, ERR_USAGE),
        ("list 不接受参数", "whitelist list Steve", None, None, ERR_LIST_ARGS),
        ("空文本", "", None, None, ERR_USAGE),
        ("无关文本", "你好", None, None, ERR_USAGE),
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
    # (说明, 输入, 期望动词, 期望玩家名, 期望失败原因)
    for desc, raw, verb, player, err in _CMD_CASES:
        cmd, got_err = parse_command(raw)
        got_verb = cmd.verb if cmd else None
        got_player = cmd.player if cmd else None
        ok = (got_verb, got_player, got_err) == (verb, player, err)
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            print(f"         输入: {raw!r}")
            print(f"         期望: verb={verb!r} player={player!r} err={err!r}")
            print(f"         实际: verb={got_verb!r} player={got_player!r} err={got_err!r}")
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
    _, _, budget3 = round_budget(
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
        ("自测优先于实机探测", ["--self-test", "--target", "x"], ("self-test", "", "")),
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
    from plugins_napcat._shared.mcservers import ServerTarget
    from plugins_napcat._shared.textlen import MAX_LEN

    def _tgt(tid: str, kind: str = "backend", name: str = "") -> ServerTarget:
        return ServerTarget(
            id=tid, name=name or tid, kind=kind, group="主服群",
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
    # 会被告知「本群还没有开通」，而它明明是开通的、只是还没服。
    ok = NO_TARGETS != NO_AUDIENCE and "还没接入" in NO_TARGETS and "还没有开通" in NO_AUDIENCE
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  「没关联服」与「没开通」是两句不同的话")
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

    # 贴近真实：社团群看代理 + 两台子服 + 独立服，白名单发给代理；
    # 建筑群的组服还没搭，targets = []，群号用字符串写（两种写法都要认）
    _AUD_REAL = """
[[audience]]
name      = "社团群"
groups    = [11111111]
targets   = ["proxy", "bingo", "backstabbed", "gtnh"]
primary   = "bingo"
whitelist = { target = "proxy", command = "globalwhitelist" }

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

        # 白名单归属与命令前缀从**本条关联**读出来 —— 这正是它从全量表搬走的原因
        ok = (
            club.book.whitelist_owner is not None
            and club.book.whitelist_owner.id == "proxy"
            and club.book.whitelist_command == "globalwhitelist"
        )
        failed += not ok
        print(
            f"   {'PASS' if ok else 'FAIL'}  白名单归属与命令前缀从本条关联读出"
            f"（{getattr(club.book.whitelist_owner, 'id', None)} / {club.book.whitelist_command!r}）"
        )

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
            'whitelist={target="bingo"}\n',
            "不在本条的 targets 里",
        ),
        (
            "whitelist 不是表",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\nwhitelist="proxy"\n',
            "必须是一个表",
        ),
        (
            "whitelist 里有未知键",
            '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\n'
            'whitelist={target="gtnh",cmd="x"}\n',
            "不认识的键",
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

    # command 省略 → 用默认前缀；whitelist.target 留空串 = 本群不管白名单（合法）
    auds = parse_audiences(
        '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\n'
        'whitelist={target="gtnh"}\n'
        '[[audience]]\nname="b"\ngroups=[2]\n'
        '[[audience]]\nname="c"\ngroups=[3]\ntargets=["gtnh"]\nwhitelist={target=""}\n',
        _full,
    )
    ok = auds[0].book.whitelist_command == "whitelist"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  whitelist.command 省略 → 默认 {auds[0].book.whitelist_command!r}")
    ok = auds[1].book.whitelist_owner is None and auds[2].book.whitelist_owner is None
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  没写 whitelist / target 留空串 → 本群不管白名单")
    # 只写 command 不写 target：等于白名单整个没配，形态一眼看不出来，必须给警告
    auds = parse_audiences(
        '[[audience]]\nname="a"\ngroups=[1]\ntargets=["gtnh"]\n'
        'whitelist={command="globalwhitelist"}\n',
        _full,
    )
    ok = auds[0].book.whitelist_owner is None and any("只写了 command" in w for w in auds[0].warnings)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  只写 command 没写 target → 不给白名单 + 一条警告")
    if not ok:
        print(f"         实际警告: {auds[0].warnings}")
    # 白名单服没配 rcon 密码：能加载，但命令发不出去（backstabbed 在全量表里就没配）
    auds = parse_audiences(
        '[[audience]]\nname="a"\ngroups=[1]\ntargets=["backstabbed"]\n'
        'whitelist={target="backstabbed"}\n',
        _full,
    )
    ok = any("没配 rcon.password" in w for w in auds[0].warnings)
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  白名单服没配 rcon.password → 警告（命令发不出去）")
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

    # .env 里的 MC_ALLOWED_GROUPS：删干净了就不该响；留着要吼一声，
    # 因为它的存在会让人以为群范围还归它管（实际拦人的是「有没有被关联提到」）
    _saved = os.environ.get("MC_ALLOWED_GROUPS")
    try:
        os.environ.pop("MC_ALLOWED_GROUPS", None)
        ok = not any("已不再生效" in w for w in _cross_warnings(_orphan, auds))
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  .env 里没有 MC_ALLOWED_GROUPS → 不出残留警告")

        os.environ["MC_ALLOWED_GROUPS"] = "11111111"
        warns = _cross_warnings(_orphan, auds)
        ok = any("MC_ALLOWED_GROUPS" in w and "已不再生效" in w for w in warns)
        failed += not ok
        print(f"   {'PASS' if ok else 'FAIL'}  .env 里留着 MC_ALLOWED_GROUPS → 警告「已不再生效」")
        if not ok:
            print(f"         实际警告: {warns}")
    finally:
        os.environ.pop("MC_ALLOWED_GROUPS", None)
        if _saved is not None:
            os.environ["MC_ALLOWED_GROUPS"] = _saved
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

    async def _run_fake(replies, verb, player=""):
        """跑一条命令，RCON 换成按顺序吐 replies 的假货。返回 (结果, 实际发出去的命令)。"""
        sent: list[str] = []

        async def fake(target, command):
            sent.append(command)
            return replies[min(len(sent) - 1, len(replies) - 1)]

        origin = _mca.rcon_command
        _mca.rcon_command = fake
        try:
            res = await _mca.run_whitelist_command(_mca.AdminCommand(verb, player), _tgt)
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
    print()

    print(f"== 自测结果：{'全部通过' if not failed else f'{failed} 项失败'} ==")
    return 1 if failed else 0


# ---------------------------------------------------------------- 实机探测

def _verdict(target, snap) -> tuple[bool, str]:
    """给单个目标下一个短结论：`(是否正常, 一句话说明)`。

    判定**必须按 kind 分支**。代理的 `names_complete` 恒为假（Velocity 不上报名单，
    SLP 的 sample 也不是全群组名单），拿名单完整性去卡它，会把一台完全正常的代理
    判成故障、并把人指去查一个它本来就不该有的东西。判代理只看 reachable。
    """
    from plugins_napcat._shared.mc import SLP_UNPARSEABLE

    if not snap.reachable:
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
    from plugins_napcat._shared.mc import SLP_UNPARSEABLE

    if not snap.reachable:
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
    if not target.rcon_enabled:
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
        print(f"    SLP   {target.game_addr}")
        if not target.serves_names:
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


async def _whitelist(audience: str = "") -> int:
    """只读地看一眼服务端白名单，确认解析对不对。

    **白名单归属是按群关联区分的**：发给哪台服、用什么命令前缀都写在
    `[[audience]].whitelist` 里，所以这条命令必须站在某条关联上跑（默认第一条）。
    不站在关联上就问不出「这个群的白名单发给谁」——那正是本次改造要解决的问题。

    刻意**不发** add / remove：诊断脚本会真实改动服务端，而它没有任何清理逻辑——
    脚本中途挂掉，whitelist.json 就被留在谁也不知道的状态。要测写入，去群里用一次
    性假名字走完整链路，最后用 list 确认收尾干净。
    """
    from plugins_napcat._shared.mc import RconError, rcon_command
    from plugins_napcat._shared.mcaudiences import audiences_path
    from plugins_napcat._shared.mcadmin import parse_whitelist_names

    book, picked, code = _pick_audience(audience)
    if book is None:
        return code

    owner = book.whitelist_owner
    if owner is None:
        print(f"关联「{picked.name}」没有指定白名单服：该群的白名单管理不可用。")
        print(f"   在 {audiences_path()} 里给这条 [[audience]] 加：")
        print('   whitelist = { target = "<目标 id>", command = "<插件命令>" }')
        print("   （群里会回「本群没有指定白名单服」）")
        return 1
    if not owner.rcon_enabled:
        print(f"关联「{picked.name}」的白名单服 {owner.id} 没配 rcon.password：")
        print("   命令发不出去，也就没有名单可看。（群里会回「白名单服 X 没配 RCON 密码」）")
        return 1

    # 命令前缀来自本条的 whitelist.command，**不是**硬编码的 "whitelist"。
    # 代理上线后多半是 globalwhitelist，这里跟着配置走才能看出真实命令对不对。
    command = f"{book.whitelist_command} list"
    print(f"== 白名单目标：{owner.name} [{owner.id}] {owner.rcon_addr} ==")
    print(f"   命令前缀（本条关联的 whitelist.command）：{book.whitelist_command!r}")
    print(f"   群里打的是触发词（.env 的 MC_ADMIN_TRIGGER），与它无关 —— 代理上线后")
    print("   必然是「群里打 whitelist、RCON 里发 globalwhitelist」这种分叉。")
    print()

    try:
        raw = await rcon_command(owner, command)
    except RconError as exc:
        print(f"== RCON `{command}` 失败 ==\n   [!] {type(exc).__name__}: {exc}")
        return 1

    print(f"== RCON `{command}` 原始输出 ==")
    print(f"   原文: {raw!r}")
    print()

    names = parse_whitelist_names(raw)
    print("== 解析结果 ==")
    if names is None:
        print("   [!!] 判不出来（输出为空，或没有冒号说明格式被改写过）。")
        print("        不是故障——但本服的加白命令会回「未能验证」而不是「已添加」。")
        return 1
    print(f"   共 {len(names)} 人")
    print(f"   {names[:20]}{' …' if len(names) > 20 else ''}")
    print()
    print("== 结论 ==")
    print("   [OK] 解析正常，白名单命令可以正常判定成败。")
    print("   注意：本脚本只能看到 whitelist.json 的内容，看不出服务端有没有开")
    print("   white-list。若白名单里有人却仍能自由进出，去 server.properties 确认。")
    return 0


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
        print(f"      游戏 {t.game_addr}    RCON {t.rcon_addr}    组 {t.group}")
        print(f"      名字 {'、'.join(t.keys)}    "
              f"超时 SLP {t.timeout:g}s / RCON {t.rcon_timeout:g}s")
        if not t.serves_names:
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
    print("   白名单是**按群关联**定的，逐条细节与命令前缀见：")
    print("       .venv\\Scripts\\python.exe tools\\mc_check.py --list-audiences")
    print()

    # 一轮探测的耗时预算。**这是上界，不是典型值**：正常一轮在毫秒级，
    # 只有「SLP 通但 RCON 卡住」这种病态情况才会走满（RCON 可能重连一次所以算两倍）。
    # 目标之间是并发探测，所以取 max 而不是求和 —— 加子服不会让这个数变大。
    slp, rcon, budget = round_budget(book)
    print("== 一轮耗时预算（上界，非典型值）==")
    print(f"   SLP {slp:g}s + RCON {rcon:g}s ×2 = 最坏 {budget:g}s")
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
    "   --list-targets         打印 mcs_servers.toml 解析出的目标，不联网\n"
    "   --list-audiences       打印 mcs_audiences.toml 的每条群关联，不联网\n"
    "   --target <服名>        只实机探测这一个目标\n"
    "   --whitelist            只读地看一眼服务端白名单\n"
    "   --audience <关联名>    站在哪条群关联的视角上（默认第一条；影响 --target\n"
    "                          能认出的服名，以及 --whitelist 发给哪台服）\n"
    "   （不带参数）           实机探测所选关联的全部目标"
)


def _parse_argv(argv: list[str]) -> tuple[str, str, str] | None:
    """把命令行解析成 `(动作, 服名, 关联名)`；返回 None 表示用法错误（提示已打印）。

    动作 ∈ self-test / list-targets / list-audiences / whitelist / live。
    抽成纯函数是为了能自测：「认不出的参数不许退化成实机探测」这条不能只靠读代码保证。
    """
    # 认不出的参数必须拦下。否则 `--help`（本脚本没有这个参数）或 `--targets`
    # 这类手滑会**静默退化成一次全量实机探测** —— 真的去连服务器、发 RCON 命令，
    # 却看起来像正常输出，排查时会被彻底带偏。
    #
    # 新增参数时**必须同时加进这个集合**：漏了不会自测失败，只会在真跑命令行时
    # 被当成不认识的参数拦下（退出码 2），而自测调的是纯函数，看不出这件事。
    known = {
        "--self-test",
        "--list-targets",
        "--list-audiences",
        "--whitelist",
        "--target",
        "--audience",
    }
    for arg in argv:
        flag = arg.partition("=")[0]
        if flag.startswith("--") and flag not in known:
            print(f"== 不认识的参数 {arg!r} ==\n{_USAGE}")
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
    if "--list-targets" in argv:
        return "list-targets", "", ""
    if "--list-audiences" in argv:
        return "list-audiences", "", ""
    if "--whitelist" in argv:
        return "whitelist", "", picked

    # --target 的服名走和群里同一套解析，所以脚本能跑通的写法群友也一定能用。
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
    return "live", query, picked


def main() -> int:
    parsed = _parse_argv(sys.argv[1:])
    if parsed is None:
        return 2
    action, query, audience = parsed
    if action == "self-test":
        return _self_test()
    if action == "list-targets":
        return _list_targets()
    if action == "list-audiences":
        return _list_audiences()
    if action == "whitelist":
        return asyncio.run(_whitelist(audience))
    return asyncio.run(_live(query, audience))


if __name__ == "__main__":
    raise SystemExit(main())
