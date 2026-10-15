# -*- coding: utf-8 -*-
"""一键跑完所有冒烟脚本

**为什么需要它**

8 个 smoke 脚本各自会清空一部分风控表，顺序连跑本来会互相干扰：
`smoke_agent` 清掉的表，恰好是 `smoke_decision` 依赖的业务数据所依赖的，
按顺序跑完最后一个是红的，而报错长得像代码回归。

现在每个脚本都会自己把需要的状态准备好（`smoke_decision` 发现场景数据
滑出时间窗会自动重建），所以**顺序不再重要、单跑连跑都可重复**；
但手敲 8 条命令仍然麻烦，也没人记得该按什么顺序跑 —— 这个脚本就是那个入口。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/smoke_all.py
    ./.venv/Scripts/python.exe scripts/smoke_all.py --seed          # 顺带预填演示数据
    ./.venv/Scripts/python.exe scripts/smoke_all.py --no-restore
    ./.venv/Scripts/python.exe scripts/smoke_all.py smoke_list smoke_event

跑完默认执行 `init_db.py` 把库恢复成干净状态（各脚本留下的都是零碎测试数据，
不清掉的话仪表盘会很难看）。加 `--no-restore` 可跳过。

注意 `init_db.py` 会把风控事件一并清空，所以恢复完仪表盘仍是 0 ——
想直接看到有数据的界面，加 `--seed`，它会在 init_db 之后接着跑 `seed_demo.py`。

每个脚本起**独立子进程**运行：模块级的 `failures` 列表、DB session、monkeypatch
都不会互相污染，执行方式也和开发者手敲命令完全一致。
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# (模块名, 说明, 是否需要 LLM_API_KEY)
SUITES = [
    ("smoke_agent",     "Agent 层（8 工具 / 会话 / 流式 / 超时降级）", False),
    ("smoke_event",     "事件管道（特征 → 规则 → 决策 → 落库）", False),
    ("smoke_decision",  "决策引擎 + 9 个端到端场景", False),
    ("smoke_list",      "名单服务（三色五类 / 白名单约束 / 归档）", False),
    ("smoke_case",      "案件服务（审核 / 回写 / 冲突）", False),
    ("smoke_services",  "服务层（画像 / 图谱 / 审计 / 仿真）", False),
    ("smoke_api",       "接口层（路由 / 异常 / 分页）", False),
    ("smoke_agent_live", "Agent 真实对话（多轮 / 流式）", True),
]


def _llm_configured() -> bool:
    from app.config import settings  # noqa: PLC0415

    return bool(settings.LLM_API_KEY)


def _run_one(name: str) -> tuple[bool, float, str]:
    """起子进程跑一个脚本，返回 (是否通过, 耗时秒, 失败时的输出尾巴)"""
    started = time.time()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / f"{name}.py")],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    elapsed = time.time() - started
    ok = proc.returncode == 0
    tail = ""
    if not ok:
        tail = "\n".join((proc.stdout or "").splitlines()[-12:]
                         + (proc.stderr or "").splitlines()[-6:])
    return ok, elapsed, tail


def main() -> int:
    argv = sys.argv[1:]
    no_restore = "--no-restore" in argv
    with_seed = "--seed" in argv
    picked = [a for a in argv if not a.startswith("--")]

    suites = [s for s in SUITES if not picked or s[0] in picked]
    if picked:
        unknown = set(picked) - {s[0] for s in SUITES}
        if unknown:
            print(f"未知脚本：{', '.join(sorted(unknown))}")
            print(f"可选：{', '.join(s[0] for s in SUITES)}")
            return 2

    has_key = _llm_configured()
    print("=" * 74)
    print(f"冒烟总入口  |  {len(suites)} 个脚本  |  LLM_API_KEY {'已配置' if has_key else '未配置'}")
    print("=" * 74)

    results: list[tuple[str, str, float]] = []
    for name, desc, needs_key in suites:
        if needs_key and not has_key:
            print(f"  [跳过] {desc}  —— 需要 LLM_API_KEY")
            results.append((name, "跳过", 0.0))
            continue
        ok, elapsed, tail = _run_one(name)
        # 不用 \r 覆盖 —— 输出被重定向到文件时 \r 会变成噪音
        print(f"  [{'通过' if ok else '失败'}] {desc}  ({elapsed:.1f}s)")
        if not ok:
            print("      " + tail.replace("\n", "\n      "))
        results.append((name, "通过" if ok else "失败", elapsed))

    passed = sum(1 for _, r, _ in results if r == "通过")
    failed = [n for n, r, _ in results if r == "失败"]
    skipped = sum(1 for _, r, _ in results if r == "跳过")

    print()
    print("=" * 74)
    print(f"结果：{passed} 通过 / {len(failed)} 失败 / {skipped} 跳过"
          f"  （共 {sum(t for _, _, t in results):.1f}s）")
    if failed:
        for n in failed:
            print(f"  ✗ {n}")
    print("=" * 74)

    if not no_restore:
        steps = [("init_db", "恢复基础数据（41 条规则 + 业务数据）")]
        if with_seed:
            steps.append(("seed_demo", "预填最近 7 天的风控演示数据"))
        for script, label in steps:
            print()
            print(f"{label}（{script}）…")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / f"{script}.py")],
                cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            if proc.returncode == 0:
                print(f"  ✓ {script} 完成")
            else:
                print(f"  ! {script} 失败：")
                print("    " + "\n    ".join(
                    (proc.stderr or proc.stdout or "").splitlines()[-6:]))
        if not with_seed:
            # 说清楚现状：init_db 会把 risk_* 一并清空，仪表盘是 0
            print("    风控事件/案件已清空，仪表盘显示 0；"
                  "要看到有数据的界面，加 --seed 再跑一次")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
