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
        ("没有冒号 → 判不出来", "3 whitelisted players online.", "Alice", None),
        ("空输出 → 判不出来", "", "Alice", None),
        # ↓ 本机服务端的真实抓包（脱敏保留格式）：无空格 + and 连接 + (out of N seen)。
        # 曾经因为它把 Vul / neckProtecter 粘成一个 token，加白明明成功却报「未生效」。
        (
            "真实抓包：and 连接的最后一项能命中",
            _REAL_WL,
            "vul",  # 注意大小写：用户输入小写，白名单存的是 Vul
            True,
        ),
    ]

    # 名单解析：(说明, 输出原文, 期望)。空列表与 None 必须分清：前者是「确实没人」，
    # 后者是「判不出来」，调用方要据此决定报「没有玩家」还是「未能验证」。
    _WL_LIST_CASES: list[tuple[str, str, list[str] | None]] = [
        ("空名单（有冒号、尾部为空）", "There are 0 whitelisted players:", []),
        ("多人", "There are 2 whitelisted players: Alice, Bob", ["Alice", "Bob"]),
        ("中文全角", "有 2 名玩家在白名单中：Alice，Bob", ["Alice", "Bob"]),
        ("英文 and 连接最后两项", "There are 2 whitelisted players: Alice and Bob", ["Alice", "Bob"]),
        ("真实抓包（无空格 + and）", _REAL_WL, ["x_l_t", "Vul", "neckProtecter"]),
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

    # 贴近真实拓扑：代理 + 两个同组子服 + 独立服，白名单指向代理
    _SRV_REAL = """
[whitelist]
target = "proxy"
command = "globalwhitelist"

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
        ("真实拓扑（代理 + 子服 + 别名 + 白名单指向代理）", _SRV_REAL),
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
            "[whitelist]\ntarget = \"\"\n",
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
        (
            "[whitelist].target 指向不存在的 id",
            '[[targets]]\nid="a"\nkind="standalone"\nhost="h"\nport=1\n'
            '[whitelist]\ntarget="nope"\n',
            "不存在的目标",
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

    # 白名单归属：resolve 只能命中目标，group 名不可寻址（它只用于「换服」合并）
    owner = book.whitelist_owner
    ok = owner is not None and owner.id == "proxy"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  白名单归属解析到 proxy（实际 {getattr(owner, 'id', None)}）")
    ok = book.whitelist_command == "globalwhitelist"
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  白名单命令前缀从配置读出（实际 {book.whitelist_command!r}）")

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

    # 汇总/播报的展示顺序：主服排最前，其余保持配置顺序。_SRV_REAL 里 proxy 写在
    # 最前面且 primary 没配，所以「主服 = 列表第一个 = proxy」——正好能和配置顺序
    # 区分开（若实现成「按配置顺序」，结果会和这里期望的一模一样，测不出来）。
    _PF = """
    [defaults]
    primary = "b"
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
    order = [t.id for t in parse_book(_PF).targets_primary_first]
    ok = order == ["b", "a", "c"]
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  主服排最前、其余保持配置顺序：{order}（期望 ['b', 'a', 'c']）")

    # 没配 primary 时退回列表第一个 —— 顺序不变，不能让默认值把展示顺序搅乱
    order = [t.id for t in parse_book(_SRV_REAL).targets_primary_first]
    ok = order == [t.id for t in parse_book(_SRV_REAL).targets]
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
        ("不带参数 → 探测全部目标", [], ("live", "")),
        ("--self-test", ["--self-test"], ("self-test", "")),
        ("--list-targets", ["--list-targets"], ("list-targets", "")),
        ("--whitelist", ["--whitelist"], ("whitelist", "")),
        ("--target <服名>", ["--target", "bingo"], ("live", "bingo")),
        ("--target=<服名>（等号写法）", ["--target=bingo"], ("live", "bingo")),
        ("裸写服名", ["bingo"], ("live", "bingo")),
        ("自测优先于实机探测", ["--self-test", "--target", "x"], ("self-test", "")),
        # ↓ 这四条是本次真正要防的：它们都**不能**变成 ("live", ...)
        ("--help 本脚本没有 → 拦下", ["--help"], None),
        ("--targets（--list-targets 的手滑）→ 拦下", ["--targets"], None),
        ("--target 后面没跟服名 → 拦下", ["--target"], None),
        ("--target= 空值 → 拦下", ["--target="], None),
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
    text = render_summary(rows, total_names=50)
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
    text = render_summary(rows, total_names=50)
    ok = "🗺️ MC 在线总览：3 人／1 台服" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  总览合计不把代理算两遍（3 人／1 台服）")
    if not ok:
        print(f"         实际首行: {text.splitlines()[0]!r}")

    # 代理口径提示：只在子服 ≥2 台且数字对不上时出现（1 台时两种成因结果相同，说了是误导）
    def _proxy_rows(pcount: int, bcounts: list[int]) -> list[Row]:
        return [
            Row(_tgt("proxy", "proxy", "主服群"), _snap(count=pcount)),
            *[
                Row(_tgt(f"b{i}", name=f"子服{i}"), _snap(count=c, names=["N"] * c))
                for i, c in enumerate(bcounts)
            ],
        ]

    text = render_summary(_proxy_rows(1, [3, 4]), total_names=50)  # 1 != 3+4 → 提示
    ok = "ping-passthrough" in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  代理总数与子服之和不符 → 出脚注")
    if not ok:
        print(f"         实际:\n{text}")

    text = render_summary(_proxy_rows(7, [3, 4]), total_names=50)  # 7 == 3+4 → 不提示
    ok = "ping-passthrough" not in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  数字对得上 → 不出脚注（不制造噪音）")

    text = render_summary(_proxy_rows(99, [3]), total_names=50)  # 只有 1 台子服 → 不提示
    ok = "ping-passthrough" not in text
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  只有 1 台子服 → 不出脚注（两种成因分不出来）")

    # ↓ 本次最该钉住的一条：目标多 + 名字长时**尾注必须活下来**。
    #   原来的写法是渲染完直接 truncate()，切掉尾部 —— 尾注和最后几台服一起消失。
    long_names = [f"Player_{i:02d}_" + "x" * 30 for i in range(12)]
    rows = [
        Row(_tgt(f"s{i}", name=f"子服{i}"),
            _snap(count=12, names=long_names, complete=(i != 0), error="测试"))
        for i in range(5)
    ]
    text = render_summary(rows, total_names=50)
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

    # 空目标表不能炸：mcs_servers.toml 里一段 [[targets]] 都没写
    ok = render_summary([], total_names=50).startswith("⚠️")
    failed += not ok
    print(f"   {'PASS' if ok else 'FAIL'}  空目标表 → 给提示而不是空消息")
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


async def _live(query: str = "") -> int:
    """实机探测。query 为空 = 探测全部目标，否则按服名解析出**单个**目标。"""
    from plugins_napcat._shared.mc import fetch_snapshots
    from plugins_napcat._shared.mcservers import ServerConfigError, default_book

    try:
        book = default_book()
    except ServerConfigError as exc:
        print(f"== mcs_servers.toml 读不了 ==\n   [!!] {exc}")
        return 1
    if not book.targets:
        print("== mcs_servers.toml 里没有配置任何目标 ==")
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
                print("   打全一点；或跑 --list-targets 看全部目标的名字。")
            else:
                print(f"== 认不出服名 {query!r} ==")
                print(f"   可选：{'、'.join(t.name for t in book.targets)}")
                print("   （id / 名字 / 别名 / 唯一前缀都认，忽略大小写与全角）")
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


async def _whitelist() -> int:
    """只读地看一眼服务端白名单，确认解析对不对。

    刻意**不发** add / remove：诊断脚本会真实改动服务端，而它没有任何清理逻辑——
    脚本中途挂掉，whitelist.json 就被留在谁也不知道的状态。要测写入，去群里用一次
    性假名字走完整链路，最后用 list 确认收尾干净。
    """
    from plugins_napcat._shared.mc import RconError, rcon_command
    from plugins_napcat._shared.mcadmin import parse_whitelist_names
    from plugins_napcat._shared.mcservers import ServerConfigError, default_book

    try:
        book = default_book()
    except ServerConfigError as exc:
        print(f"== mcs_servers.toml 读不了 ==\n   [!!] {exc}")
        return 1

    owner = book.whitelist_owner
    if owner is None:
        print("mcs_servers.toml 的 [whitelist].target 没有指向任何目标：白名单管理不可用。")
        return 1
    if not owner.rcon_enabled:
        print(f"目标 {owner.id} 没配 rcon.password：白名单管理不可用，也就没有名单可看。")
        return 1

    # 命令前缀来自 [whitelist].command，**不是**硬编码的 "whitelist"。
    # 代理上线后多半是 globalwhitelist，这里跟着配置走才能看出真实命令对不对。
    command = f"{book.whitelist_command} list"
    print(f"== 白名单目标：{owner.name} [{owner.id}] {owner.rcon_addr} ==")
    print(f"   命令前缀（[whitelist].command）：{book.whitelist_command}")
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
    """打印 mcs_servers.toml 解析出的目标、白名单归属与警告。**不联网**。

    排查「新加的子服为什么没生效」最快的入口：先确认它有没有被读进来、
    名字能不能解析、白名单指向谁，再去管连通性。
    """
    from plugins_napcat._shared.mcservers import (
        ServerConfigError,
        book_path,
        load_book,
        round_budget,
    )

    # 走 book_path() 而不是写死 ROOT/mcs_servers.toml：它认 MCS_SERVERS_TOML，
    # 和 bot 运行期用的是同一个解析规则。
    path = book_path()
    try:
        book = load_book(path)
    except ServerConfigError as exc:
        print(f"== mcs_servers.toml 读不了（{path}）==\n   [!!] {exc}")
        return 1

    # 这里**只读配置、一个网络包都不发**（刻意的：它的用途是排查「新加的子服为什么
    # 没生效」，网络探测只会把配置问题和服务端没开混在一起）。所以要显式说清楚这是
    # 配置视图不是实况 —— 否则「三台都关着却列出三个目标」看着像 bug。
    print(f"== 目标（{path}，只读配置、不探测）==")
    for t in book.targets:
        print(f"   {t.name}  [{t.id}]  {t.kind}")
        print(f"      游戏 {t.game_addr}    RCON {t.rcon_addr}    组 {t.group}")
        print(f"      名字 {'、'.join(t.keys)}    "
              f"超时 SLP {t.timeout:g}s / RCON {t.rcon_timeout:g}s")
        if not t.serves_names:
            print("      （代理：SLP 只给全群组总人数，不出分服名单）")
        elif not t.rcon_enabled:
            print("      （没配 RCON 密码：不出名单，只能靠 SLP 样本，>12 人就不完整）")
    print()

    print("== 白名单 ==")
    owner = book.whitelist_owner
    if owner is None:
        print("   未配置 [whitelist].target —— 机器人不提供白名单管理")
    else:
        print(f"   命令发给 {owner.name}（RCON {owner.rcon_addr}）")
        print(f"   命令前缀 {book.whitelist_command!r}  ← 必须与插件注册的命令一致，")
        print("            否则每次操作都回 Unknown command，群里报「未生效」")
        print("   注意：触发词（群里打的）和命令前缀（RCON 里发的）是两个不同的轴")
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

    if book.warnings:
        print("== 警告（能加载，但这些目标在运行期有问题）==")
        for w in book.warnings:
            print(f"   [!] {w}")
        print()
        return 0
    print("== 无警告 ==")
    return 0


_USAGE = (
    "可用参数：\n"
    "   --self-test           只跑解析自测，不联网\n"
    "   --list-targets        打印 mcs_servers.toml 解析出的目标，不联网\n"
    "   --target <服名>       只实机探测这一个目标\n"
    "   --whitelist           只读地看一眼服务端白名单\n"
    "   （不带参数）          实机探测全部目标"
)


def _parse_argv(argv: list[str]) -> tuple[str, str] | None:
    """把命令行解析成 `(动作, 服名)`；返回 None 表示用法错误（提示已打印）。

    动作 ∈ self-test / list-targets / whitelist / live。
    抽成纯函数是为了能自测：「认不出的参数不许退化成实机探测」这条不能只靠读代码保证。
    """
    # 认不出的参数必须拦下。否则 `--help`（本脚本没有这个参数）或 `--targets`
    # 这类手滑会**静默退化成一次全量实机探测** —— 真的去连服务器、发 RCON 命令，
    # 却看起来像正常输出，排查时会被彻底带偏。
    known = {"--self-test", "--list-targets", "--whitelist", "--target"}
    for arg in argv:
        flag = arg.partition("=")[0]
        if flag.startswith("--") and flag not in known:
            print(f"== 不认识的参数 {arg!r} ==\n{_USAGE}")
            return None

    if "--self-test" in argv:
        return "self-test", ""
    if "--list-targets" in argv:
        return "list-targets", ""
    if "--whitelist" in argv:
        return "whitelist", ""

    # --target 的服名走和群里同一套解析，所以脚本能跑通的写法群友也一定能用。
    explicit = False
    query = ""
    for i, arg in enumerate(argv):
        if arg == "--target" or arg.startswith("--target="):
            explicit = True
            if arg == "--target":
                query = argv[i + 1] if i + 1 < len(argv) else ""
            else:
                query = arg.partition("=")[2]
            break
    if not explicit:
        # 裸给一个词也当服名：`mc_check.py bingo` 比 `--target bingo` 顺手
        positional = [a for a in argv if not a.startswith("-")]
        query = positional[0] if positional else ""
    if explicit and not query.strip():
        print(f"== --target 后面要跟一个服名（例如 --target bingo）==\n{_USAGE}")
        return None
    return "live", query


def main() -> int:
    parsed = _parse_argv(sys.argv[1:])
    if parsed is None:
        return 2
    action, query = parsed
    if action == "self-test":
        return _self_test()
    if action == "list-targets":
        return _list_targets()
    if action == "whitelist":
        return asyncio.run(_whitelist())
    return asyncio.run(_live(query))


if __name__ == "__main__":
    raise SystemExit(main())
