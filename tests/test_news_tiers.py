"""采集三层层籍守卫（结构审视 P2，2026-07-27）。

CLAUDE.md §1.4 把采集层定性为三层（T1 新闻流 / T2 数据层归档 / T3 维护回填），
并明写「T2 **绝不并入 T1**——节拍刻意错峰、产出目标不同」。在本档之前，这条
边界只活在散文里：磁盘上 32 档全平铺，谁属于哪层靠读者按文件名猜。

审视过程本身就是反例：`archive_platforms.py` 名字像 T2 归档器，实为 T1 的下游
落盘（把刚采的 news.json 按日切进 data/platforms/），险些据此写出一个把误判固化
进代码的守卫。所以第一件事不是分目录、也不是写方向约束，而是**把层籍变成数据**
——`projects/news/scripts/tier_registry.json`，每条附判据。本档守它。

刻意**不**按层拆物理目录：本层以裸 basename 互相 import（62 条同目录依赖边），
CI 另有 28 处硬编码路径；拆完必须把三个子目录全注入 sys.path 才跑得起来，那是
「形式分层、实质未分」——目录会暗示一条并不存在的边界，比不分更误导。

小学生比喻：厨房里冷食区和熟食区本来只写在墙上的告示里，谁站哪区全靠自觉；
本档不是砌墙（砌了反而挡住端菜的路），而是给每个人发一张写明工位的牌子，
并且规定新来的人上工前必须先领牌。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NEWS = REPO / "projects" / "news" / "scripts"
REGISTRY = NEWS / "tier_registry.json"
WORKFLOWS = REPO / ".github" / "workflows"

_REG = json.loads(REGISTRY.read_text(encoding="utf-8"))
MODULES: dict[str, dict] = _REG["modules"]
VALID_TIERS = set(_REG["_meta"]["tiers"])

#: T2 的归档动作本体。把这些拉进 T1 的入口，就是 CLAUDE.md 禁止的「T2 并入 T1」。
ARCHIVE_ACTIONS = {
    "archive_engine",
    "archive_discord",
    "discord_archiver",
    "discord_cold_compress",
    "community_cold_compress",
}


def _disk_modules() -> set[str]:
    return {p.stem for p in NEWS.glob("*.py") if not p.stem.startswith("__")}


def _local_deps(stem: str) -> set[str]:
    """同目录 basename import（本层模块之间的真实依赖边）。"""
    tree = ast.parse((NEWS / f"{stem}.py").read_text(encoding="utf-8"))
    known = _disk_modules()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names if a.name in known}
        elif isinstance(node, ast.ImportFrom) and node.module in known:
            out.add(node.module)
    return out


def test_registry_covers_every_module_and_nothing_else() -> None:
    """新增模块必须登记；登记了却不存在的幽灵条目同样报红。

    没有这一条，注册表会慢慢退化成一份「写的时候是对的」的旧快照——
    正是本档要取代的那种散文式边界。
    """
    disk, reg = _disk_modules(), set(MODULES)
    assert not (disk - reg), f"以下模块未登记层籍，请补进 tier_registry.json：{sorted(disk - reg)}"
    assert not (reg - disk), f"注册表存在幽灵条目（文件已不在）：{sorted(reg - disk)}"


def test_every_entry_declares_a_valid_tier_and_a_reason() -> None:
    """判据不可空——「为什么是这一层」正是本表存在的理由。"""
    for mod, meta in MODULES.items():
        assert meta.get("tier") in VALID_TIERS, f"{mod}: 层籍非法 {meta.get('tier')!r}"
        why = (meta.get("why") or "").strip()
        assert len(why) >= 8, f"{mod}: 缺少判据（why），层籍就退回成了猜测"


def test_t1_entrypoint_does_not_absorb_the_archive_actions() -> None:
    """「T2 绝不并入 T1」的可执行形式（CLAUDE.md §1.4）。

    注意这条钉的是**动作**不是**数据**：T1 读 archive_layout 求路径是正常的
    （shared 层），把归档器/冷压拉进采集入口那一轮才是违规。

    T1 入口 2026-08-22 由 aggregator 换为 collect_global（「采集 → 直接入湖」裁定）。
    """
    reachable, frontier = set(), {"collect_global"}
    while frontier:
        cur = frontier.pop()
        for dep in _local_deps(cur) - reachable:
            reachable.add(dep)
            frontier.add(dep)
    offenders = sorted(reachable & ARCHIVE_ACTIONS)
    assert not offenders, (
        f"T1 入口 collect_global 经依赖链拉进了 T2 归档动作 {offenders}；"
        "CLAUDE.md §1.4 明定二者节拍错峰，不得并轮"
    )


def test_shared_tier_is_actually_shared() -> None:
    """shared 的名分要配得上：只被一处用的模块不是基础设施，是那一层的私产。

    消费者跨目录（顶层 scripts/）也算——discord_compact 的第二个消费者正是
    顶层的 compact_discord_archive.py。
    """
    shared = [m for m, v in MODULES.items() if v["tier"] == "shared"]
    assert shared, "shared 层不应为空（archive_layout 等跨层 SSOT 无处安放会退回 P4 病根）"
    top_scripts = list((REPO / "scripts").glob("*.py"))
    for mod in shared:
        in_layer = {m for m in MODULES if m != mod and mod in _local_deps(m)}
        cross_dir = {p.name for p in top_scripts if re.search(rf"\b{mod}\b", p.read_text(encoding="utf-8"))}
        assert len(in_layer) + len(cross_dir) >= 2, (
            f"{mod} 标为 shared 却几乎无人共用（层内 {sorted(in_layer)} / 跨目录 {sorted(cross_dir)}）；"
            "请改归它真正隶属的那一层"
        )


def test_t3_is_downstream_not_upstream() -> None:
    """T3 是补跑层：它复用 T1/T2 的部件，而不该被 T1/T2 反向依赖。

    唯一在册的例外是 discord_archiver → discord_reconcile（归档器写完索引后
    自行对账）。例外写在这里、被这条测试点名，就不会悄悄长出第二个。
    """
    KNOWN_EXCEPTIONS = {("discord_archiver", "discord_reconcile")}
    t3 = {m for m, v in MODULES.items() if v["tier"] == "T3"}
    bad = [
        (m, dep)
        for m, v in MODULES.items()
        if v["tier"] in {"T1", "T2"}
        for dep in sorted(_local_deps(m) & t3)
        if (m, dep) not in KNOWN_EXCEPTIONS
    ]
    assert not bad, (
        f"T1/T2 反向依赖了 T3 维护层：{bad}。补跑层应当是下游；"
        "若这是刻意设计，请把它加进本测试的 KNOWN_EXCEPTIONS 并说明理由"
    )


def test_registry_matches_the_workflow_that_drives_each_module() -> None:
    """层籍不能只是一份自说自话的标签：T2 的归档动作必须由 T1 之外的工作流驱动。

    实测 update-news.yml（每 3 小时的 T1 轮）确实不含任何 T2 归档动作——
    archive_platforms 跑在里面是 T1 的下游落盘，不是归档。
    """
    # 采集工作流 2026-09-29 迁往 BIAV-SC-DATA：从数据仓检出读（env 数据根或同级目录），
    # 旧位置仍存在时（迁移前的检出）照读本仓。
    import os
    import pytest

    candidates = [WORKFLOWS / "update-news.yml"]
    if os.environ.get("BIAV_SC_DATA_ROOT"):
        candidates.append(Path(os.environ["BIAV_SC_DATA_ROOT"]) / ".github" / "workflows" / "update-news.yml")
    candidates.append(REPO.parent / "BIAV-SC-DATA" / ".github" / "workflows" / "update-news.yml")
    found = next((p for p in candidates if p.is_file()), None)
    if found is None:
        pytest.skip("update-news.yml 已迁 BIAV-SC-DATA，本机未检出数据仓（设 BIAV_SC_DATA_ROOT 即可覆盖）")
    t1_round = found.read_text(encoding="utf-8")
    invoked = set(re.findall(r"projects/news/scripts/([a-z_0-9]+)\.py", t1_round))
    offenders = sorted(invoked & ARCHIVE_ACTIONS)
    assert not offenders, (
        f"update-news.yml（T1 每 3 小时轮）直接调用了 T2 归档动作 {offenders}——节拍已并轨"
    )
    # 反向的健全性：这一轮确实由 T1 入口领跑，否则本条测试是在守一个空壳。
    # 入口 2026-08-22 由 aggregator 换为 collect_global（「采集 → 直接入湖」裁定）。
    assert "collect_global.py" in t1_round
