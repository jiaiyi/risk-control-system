# -*- coding: utf-8 -*-
"""性能采样 —— S-01 / NFR-01 / NFR-03

**为什么单独成一个脚本**：压测结论必须是可复现的「跑出来的数字」，而不是
写在文档里的结论。这个脚本每次运行都会重新采样，输出直接可贴进验收报告。

用法（项目根目录）：
    python scripts/perf_check.py                 # 打主库 ecs（真实交付形态）
    python scripts/perf_check.py --test          # 打独立测试库 ecs_test（不污染演示数据）
    python scripts/perf_check.py --n 100         # 采样次数（默认 60）
    python scripts/perf_check.py --no-cold       # 跳过冷缓存轮（只测热缓存）

验收标准：
    · NFR-01  单次风控检查（API-01）P95 < 500 ms
    · NFR-03  仪表盘概览（API-22）加载 < 2 s

口径说明：
    · 走进程内 `TestClient`，测的是**应用侧耗时**（特征 SQL + 规则匹配 +
      5 张表持久化写），不含真实网络往返。要测含网络的真机吞吐，请起
      uvicorn 后用 locust / wrk 另测。
    · 「冷缓存」轮每次请求前清空 `risk:feature:*`，是最坏情况（每维特征都回源）；
      「热缓存」轮连续请求，是稳态。
"""
import argparse
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _parse_args():
    parser = argparse.ArgumentParser(description="风控系统性能采样")
    parser.add_argument("--test", action="store_true",
                        help="打独立测试库（ecs_test / Redis db2），不污染演示数据")
    parser.add_argument("--n", type=int, default=60, help="采样次数，默认 60")
    parser.add_argument("--no-cold", action="store_true", help="跳过冷缓存轮")
    return parser.parse_known_args()[0]


ARGS = _parse_args()

# ⚠ 必须在 import app.* 之前改环境变量（settings 与 engine 都是模块级单例）
if ARGS.test:
    os.environ["DB_NAME"] = os.environ.get("RC_TEST_DB", "ecs_test")
    os.environ["REDIS_DB"] = os.environ.get("RC_TEST_REDIS_DB", "2")

from fastapi.testclient import TestClient  # noqa: E402

from app import cache  # noqa: E402
from app import auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

# 覆盖六类事件 × 多个用户，避免同一 (user, device) 反复命中同一份缓存
SCENARIOS = [
    ("登录", "1002", {"device_id": "D-9002", "login_ip": "203.0.113.7"}),
    ("登录", "1003", {"device_id": "D-SHARED-0001", "login_ip": "172.16.0.10"}),
    ("营销领券", "1004", {"device_id": "D-8004", "login_ip": "172.16.0.20"}),
    ("营销领券", "1005", {"device_id": "D-8005", "login_ip": "172.16.0.20"}),
    ("下单", "1007", {"device_id": "D-9002", "login_ip": "203.0.113.7"}),
    ("支付", "1008", {"device_id": "D-9002", "login_ip": "203.0.113.7"}),
]

WIDTH = 74


def _client() -> TestClient:
    client = TestClient(app)
    resp = client.post("/api/auth/login",
                       json={"user_id": "zhangcx", "password": auth._DEMO_PASSWORD})
    if resp.status_code != 200:
        print(f"登录失败：{resp.status_code} {resp.text[:200]}")
        sys.exit(1)
    return client


def _percentile(values: list[float], pct: float) -> float:
    """线性插值分位数（样本量不大时比 nearest-rank 更稳）"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * pct
    low, high = int(k), min(int(k) + 1, len(ordered) - 1)
    frac = k - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def _report(label: str, samples: list[float], budget_ms: float) -> dict:
    p50 = _percentile(samples, 0.50) * 1000
    p95 = _percentile(samples, 0.95) * 1000
    p99 = _percentile(samples, 0.99) * 1000
    mean = statistics.fmean(samples) * 1000
    worst = max(samples) * 1000

    print(f"\n{'=' * WIDTH}")
    print(f"{label}   样本 n={len(samples)}")
    print(f"{'=' * WIDTH}")
    print(f"  平均 {mean:8.1f} ms")
    print(f"  P50  {p50:8.1f} ms")
    print(f"  P95  {p95:8.1f} ms   ← 验收线 {budget_ms:.0f} ms"
          f"   {'✅ 通过' if p95 < budget_ms else '❌ 未通过'}")
    print(f"  P99  {p99:8.1f} ms")
    print(f"  最慢 {worst:8.1f} ms")

    return {
        "label": label, "n": len(samples),
        "mean_ms": round(mean, 1), "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1), "p99_ms": round(p99, 1),
        "max_ms": round(worst, 1), "budget_ms": budget_ms,
        "passed": p95 < budget_ms,
    }


def run_check_round(client: TestClient, n: int, cold: bool, label: str) -> dict:
    samples: list[float] = []
    for i in range(n):
        event_type, user_id, extra = SCENARIOS[i % len(SCENARIOS)]
        if cold:
            cache.safe_delete_prefix("risk:feature:")
        payload = {
            "event_type": event_type, "user_id": user_id,
            "source_id": f"PERF-{label}-{i}", "event_data": extra,
        }
        started = time.perf_counter()
        resp = client.post("/api/risk/check", json=payload)
        elapsed = time.perf_counter() - started
        if resp.status_code != 200:
            print(f"  ! 第 {i} 次返回 {resp.status_code}：{resp.text[:120]}")
            continue
        samples.append(elapsed)
    return _report(label, samples, 500.0)


def run_dashboard_round(client: TestClient, n: int = 10) -> dict:
    samples: list[float] = []
    for i in range(n):
        cache.safe_delete_prefix("risk:dashboard:")   # 冷缓存，最坏情况
        started = time.perf_counter()
        resp = client.get("/api/dashboard/overview", params={"days": 7})
        elapsed = time.perf_counter() - started
        if resp.status_code != 200:
            print(f"  ! 仪表盘返回 {resp.status_code}")
            continue
        samples.append(elapsed)
    return _report("仪表盘概览 API-22（冷缓存）", samples, 2000.0)


def main() -> int:
    target = f"{settings.DB_NAME}（Redis db {settings.REDIS_DB}）"
    print("=" * WIDTH)
    print("风控系统性能采样")
    print(f"数据库        | {target}")
    print(f"采样次数      | {ARGS.n}")
    print("=" * WIDTH)

    client = _client()
    results = []

    if not ARGS.no_cold:
        results.append(run_check_round(client, ARGS.n, cold=True,
                                       label="单次风控检查 API-01（冷缓存 / 最坏情况）"))
    results.append(run_check_round(client, ARGS.n, cold=False,
                                   label="单次风控检查 API-01（热缓存 / 稳态）"))
    results.append(run_dashboard_round(client))

    print(f"\n{'=' * WIDTH}")
    failed = [r for r in results if not r["passed"]]
    if failed:
        print(f"结论：{len(failed)} 项未达验收线")
        for r in failed:
            print(f"  ✗ {r['label']}：P95 = {r['p95_ms']} ms（线 {r['budget_ms']:.0f} ms）")
        print("=" * WIDTH)
        return 1
    print("结论：全部达到验收线")
    print("=" * WIDTH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
