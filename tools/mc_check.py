r"""验证 Minecraft 服务器取数链路（SLP + RCON），不启动机器人。

用法：
    .venv\Scripts\python.exe tools\mc_check.py --self-test    # 只跑解析自测，不联网
    .venv\Scripts\python.exe tools\mc_check.py --whitelist    # 只读地看一眼服务端白名单
    .venv\Scripts\python.exe tools\mc_check.py                # 实机探测 .env 里配置的服务器

实机探测会打印 SLP 的原始数据、RCON 的 `list` 输出**原文**，以及名单完整性判定
与原因。排查「进服提醒不触发」时先看这里的输出，比翻机器人日志直接。

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
# 手写版不认行尾注释，`MC_TIMEOUT=5   # SLP 探测超时（秒）` 会被原样当成值，
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

    print(f"== 自测结果：{'全部通过' if not failed else f'{failed} 项失败'} ==")
    return 1 if failed else 0


# ---------------------------------------------------------------- 实机探测

async def _live() -> int:
    from plugins_napcat._shared.mc import (
        RconError,
        _cfg,
        fetch_snapshot,
        parse_list_names,
        rcon_command,
    )

    cfg = _cfg()
    print("== 配置 ==")
    print(f"   SLP   {cfg.host}:{cfg.port}    显示名：{cfg.name}")
    print(
        f"   RCON  {cfg.host}:{cfg.rcon_port}    "
        f"{'已配置密码' if cfg.rcon_enabled else '未配置 MC_RCON_PASSWORD（跳过 RCON）'}"
    )
    print()

    if cfg.rcon_enabled:
        print("== RCON `list` 原始输出（每个服务端/语言都不一样，以这行为准）==")
        try:
            raw = await rcon_command("list")
            print(f"   原文: {raw!r}")
            # 这里没传人数，只看「不靠人数消歧」时的兜底行为；
            # 真正生效的解析在下面「综合快照」里（带人数消歧）
            print(f"   解析（不带期望人数）: {parse_list_names(raw)}")
        except RconError as exc:
            print(f"   [!] {type(exc).__name__}: {exc}")
        except Exception as exc:
            print(f"   [!] {type(exc).__name__}: {exc}")
        print()

    print("== 综合快照 ==")
    snap = await fetch_snapshot()
    print(f"   reachable       {snap.reachable}")
    print(f"   在线人数        {snap.count} / {snap.max_players}")
    print(f"   延迟            {snap.latency}")
    print(f"   版本            {snap.version or '(未知)'}")
    print(f"   名单来源        {snap.names_source}")
    print(f"   名单条数        {len(snap.names)}")
    print(f"   名单完整        {snap.names_complete}")
    print(f"   名单            {snap.names[:20]}{' …' if len(snap.names) > 20 else ''}")
    if snap.error:
        print(f"   备注            {snap.error}")
    print()

    # 结论用 ASCII 标记：Windows 控制台是 GBK，打不出 ✓/✗
    print("== 结论 ==")
    if not snap.reachable:
        print("   [!!] 服务器不可达：检查 MC_HOST / MC_PORT、服务器是否在跑、防火墙。")
        return 1
    if snap.names_complete:
        print("   [OK] 名单完整，进服提醒可以正常工作。")
        return 0
    print("   [!!] 名单不完整 → 进服提醒会静默暂停（不会误报，但也不推消息）。原因如下：")
    if not cfg.rcon_enabled:
        print("     · 没配 MC_RCON_PASSWORD。")
        print("       在线人数 ≤12 时 SLP 的玩家样本本身就是完整名单；超过就必须开 RCON。")
    else:
        print("     · 开了 RCON 但没能拿到与 SLP 人数一致的名单，看上面「原始输出」那行。")
    print("     配置步骤见 .env.example 的 Minecraft 段与 DEPLOY.md。")
    return 1


async def _whitelist() -> int:
    """只读地看一眼服务端白名单，确认解析对不对。

    刻意**不发** add / remove：诊断脚本会真实改动服务端，而它没有任何清理逻辑——
    脚本中途挂掉，whitelist.json 就被留在谁也不知道的状态。要测写入，去群里用一次
    性假名字走完整链路，最后用 list 确认收尾干净。
    """
    from plugins_napcat._shared.mc import RconError, _cfg, rcon_command
    from plugins_napcat._shared.mcadmin import parse_whitelist_names

    if not _cfg().rcon_enabled:
        print("未配置 MC_RCON_PASSWORD：白名单管理不可用，也就没有名单可看。")
        return 1

    try:
        raw = await rcon_command("whitelist list")
    except RconError as exc:
        print(f"== RCON `whitelist list` 失败 ==\n   [!] {type(exc).__name__}: {exc}")
        return 1

    print("== RCON `whitelist list` 原始输出 ==")
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


def main() -> int:
    if "--self-test" in sys.argv:
        return _self_test()
    if "--whitelist" in sys.argv:
        return asyncio.run(_whitelist())
    return asyncio.run(_live())


if __name__ == "__main__":
    raise SystemExit(main())
