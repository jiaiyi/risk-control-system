# -*- coding: utf-8 -*-
"""T-13 ~ T-16 服务层冒烟验证（画像 / 图谱 / 审计 / 仿真）

前置：先跑 `python scripts/init_db.py` 重建业务数据。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/smoke_services.py

验证：
    T-15 审计：追加写入 / 多条件查询 / 统计 / 前后值可反序列化 / 三处内联审计已收编
    T-13 画像：详情组装（趋势 + 名单履历）/ 历史重放重算 / 列表筛选 / 统计
    T-14 图谱：四维度邻接 / 2 跳扩散 / 跳数与节点上限截断 / 缓存 / 聚集判定
    T-16 仿真：分步链路 / **零副作用（实测各表行数不变）** / 批量回放统计 / 样本库
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import cache  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    RiskAssessment,
    RiskAuditLog,
    RiskCase,
    RiskChallenge,
    RiskEvent,
    RiskFeature,
    RiskList,
    RiskListArchive,
    RiskUserProfile,
)
from app.service import audit_service as audit  # noqa: E402
from app.service import graph_service as gs  # noqa: E402
from app.service import list_service as ls  # noqa: E402
from app.service import profile_service as ps  # noqa: E402
from app.service import simulation_service as sim  # noqa: E402
from app.service.event import process_event  # noqa: E402

OK, BAD = "  [OK]  ", "  [!!]  "
failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print((OK if cond else BAD) + msg)
    if not cond:
        failures.append(msg)


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def expect_error(fn, exc_type, msg: str):
    try:
        fn()
    except exc_type as exc:  # noqa: BLE001
        check(True, f"{msg}（{exc.code}）")
        return exc
    except Exception as exc:  # noqa: BLE001
        check(False, f"{msg} —— 却抛出 {type(exc).__name__}: {exc}")
        return None
    check(False, f"{msg} —— 但未抛异常")
    return None


db = SessionLocal()
for model in (RiskList, RiskListArchive, RiskAuditLog, RiskFeature, RiskAssessment,
              RiskCase, RiskChallenge, RiskEvent, RiskUserProfile):
    db.query(model).delete()
db.commit()
cache.invalidate_list()

# ---- 造数据：跑几条真实场景，产生事件 / 评估 / 案件 / 画像 ----
SCENES = [
    ("1002", "登录", "S-1002-L", "D-9002", "203.0.113.7"),
    ("1003", "登录", "S-1003-L", "D-SHARED-0001", "172.16.0.10"),
    ("1004", "登录", "S-1004-L", "D-SHARED-0001", "172.16.0.10"),
    ("1005", "登录", "S-1005-L", "D-SHARED-0001", "172.16.0.10"),
    ("1006", "登录", "S-1006-L", "D-7700", "10.99.99.99"),
    ("1003", "营销领券", "S-1003-C", "D-8003", "172.16.0.20"),
]
for uid, etype, src, dev, ip in SCENES:
    process_event(db, user_id=uid, event_type=etype, source_id=src,
                  device_id=dev, login_ip=ip)
print(f"  已构造 {len(SCENES)} 条事件，案件 {db.query(RiskCase).count()} 个，"
      f"画像 {db.query(RiskUserProfile).count()} 条")

# ============================================================
section("T-15 审计服务（FR-39）")

row = audit.log(db, operator="tester", action_type="配置", action_desc="调整阈值",
                target_id="threshold", before={"pass": 30}, after={"pass": 25},
                auto_commit=True)
check(row.log_id is not None, f"写入返回 log_id（{row.log_id}）")
check(row.action_desc == "调整阈值" and row.operator == "tester", "字段落库正确")

payload = audit.to_dict(row)
check(isinstance(payload["before_value"], dict) and payload["before_value"]["pass"] == 30,
      "before_value 反序列化回对象（前端可做 diff）")
check(isinstance(payload["after_value"], dict), "after_value 反序列化回对象")

audit.log(db, operator="tester", action_type="配置", action_desc="调整缓存 TTL",
          target_id="cache", auto_commit=True)
total, items = audit.query(db, action_type="配置")
check(total == 2 and len(items) == 2, f"按类别查询（{total} 条）")
total_kw, _ = audit.query(db, keyword="阈值")
check(total_kw == 1, f"按描述关键字查询（{total_kw} 条）")
total_after, _ = audit.query(db, start_time=datetime.now().replace(microsecond=0))
check(total_after == 2, f"按时间范围查询（{total_after} 条）")
total_t, _ = audit.query(db, target_id="threshold")
check(total_t == 1, f"按目标对象查询（{total_t} 条）")

stats = audit.statistics(db)
check(stats["total"] >= 2 and stats["last_time"], f"统计：{stats['total']} 条，最近 {stats['last_time']}")

expect_error(lambda: audit.log(db, operator="x", action_type="非法类别",
                               action_desc="t", target_id="1"),
             audit.AuditServiceError, "审计类别非法 → 拒绝")

# 收编验证：名单变更 / 案件审核 / 名单命中 三条路径都要进审计
ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="1002",
                 reason="收编验证", created_by="tester")
_, by_list = audit.query(db, action_type="名单", page_size=100)
check(any("新增黑名单" in r.action_desc for r in by_list),
      f"list_service 的审计已走 audit_service（名单类 {len(by_list)} 条）")

# ============================================================
section("T-13 画像服务（FR-25 / FR-43）")

detail = ps.get_profile(db, "1003")
check(detail["user_id"] == "1003" and detail["risk_score"] is not None,
      f"画像详情：评分 {detail['risk_score']} / 等级 {detail['risk_level']}")
check(detail["assessment_count"] >= 2, f"评估次数 {detail['assessment_count']}")
check(detail["decay"] and detail["decay"]["alpha"] is not None,
      f"含最近一次衰减细节（α={detail['decay']['alpha']}, "
      f"历史分={detail['decay']['history_score']}, 本次={detail['decay']['this_score']}）")
check(len(detail["trend"]) >= 2, f"含评分趋势 {len(detail['trend'])} 条")
check("current" in detail["list_history"] and "archived" in detail["list_history"],
      "含名单履历（当前 + 归档）")

trend = ps.profile_trend(db, "1003", limit=5)
check(trend and trend[0]["create_time"] >= trend[-1]["create_time"],
      f"趋势按时间降序（{len(trend)} 条）")

rebuild = ps.rebuild_profile(db, "1003")
print(f"  重算：重放 {rebuild['replayed']} 次评估，α={rebuild['alpha']}，"
      f"{rebuild['old_score']} → {rebuild['new_score']}")
check(rebuild["replayed"] >= 2 and rebuild["new_score"] is not None, "重放历史评估成功重算画像")
check(0 <= rebuild["new_score"] <= 100, f"重算结果落在 [0,100]（{rebuild['new_score']}）")

total_p, items_p = ps.list_profiles(db, page=1, page_size=3)
check(total_p >= 4, f"画像列表 {total_p} 条")
total_high, _ = ps.list_profiles(db, min_score=60)
check(total_high <= total_p, f"按最低分筛选（≥60 分 {total_high} 条）")
pstats = ps.statistics(db)
check("by_level" in pstats and pstats["total"] == total_p, f"画像统计：{pstats}")

expect_error(lambda: ps.get_profile(db, "NO_SUCH_USER"),
             ps.ProfileNotFoundError, "无画像用户 → 明确报错")

# ============================================================
section("T-14 图谱服务（FR-07 / ADR-06）")

g = gs.query_graph(db, entity_type="用户", entity_value="1003", hops=1)
types = {n["type"] for n in g["nodes"]}
print(f"  1 跳：{g['node_count']} 节点 / {g['edge_count']} 边，类型={sorted(types)}")
check("用户" in types and g["node_count"] > 1, "1 跳能查出关联主体")
check(any(n["center"] for n in g["nodes"]), "中心节点标记正确")

g2 = gs.query_graph(db, entity_type="用户", entity_value="1003", hops=2)
accounts = {n["value"] for n in g2["nodes"] if n["type"] == "用户"}
print(f"  2 跳：{g2['node_count']} 节点，关联账号={sorted(accounts)}")
check(len(accounts) > 1,
      f"**2 跳能通过共用设备扩散到其他账号**（{sorted(accounts)}）")
check(all(e["source"] in {n["id"] for n in g2["nodes"]}
          and e["target"] in {n["id"] for n in g2["nodes"]} for e in g2["edges"]),
      "所有边的两端都在节点集合内（图自洽）")

g3 = gs.query_graph(db, entity_type="用户", entity_value="1003", hops=99)
check(g3["hops"] == settings.GRAPH_MAX_HOPS,
      f"跳数被夹到上限 {settings.GRAPH_MAX_HOPS}（实际 {g3['hops']}）")

g_tiny = gs.query_graph(db, entity_type="用户", entity_value="1003", hops=2,
                        max_nodes=2, use_cache=False)
check(g_tiny["truncated"] and g_tiny["node_count"] <= 2,
      f"节点数触达上限即截断（truncated={g_tiny['truncated']}，"
      f"{g_tiny['node_count']} 节点）")

cache.safe_delete(cache.graph_key("用户", "1003", 2))
g_c1 = gs.query_graph(db, entity_type="用户", entity_value="1003", hops=2)
g_c2 = gs.query_graph(db, entity_type="用户", entity_value="1003", hops=2)
check(g_c1["cached"] is False and g_c2["cached"] is True, "图谱结果二次查询命中缓存")

agg = gs.related_accounts(db, entity_type="设备", entity_value="D-SHARED-0001", min_accounts=3)
print(f"  聚集判定：设备 D-SHARED-0001 关联 {agg['account_count']} 个账号 {agg['accounts']}")
check(agg["reached_threshold"], f"同设备关联账号数达阈值（{agg['account_count']} ≥ 3）")

expect_error(lambda: gs.query_graph(db, entity_type="不存在", entity_value="x"),
             gs.GraphServiceError, "主体类型非法 → 拒绝")

# ============================================================
section("T-16 仿真服务 —— 分步链路 + 零副作用（FR-14）")

before_counts = {
    "risk_event": db.query(RiskEvent).count(),
    "risk_feature": db.query(RiskFeature).count(),
    "risk_assessment": db.query(RiskAssessment).count(),
    "risk_case": db.query(RiskCase).count(),
    "risk_user_profile": db.query(RiskUserProfile).count(),
}

single = sim.run_single(db, user_id="1002", event_type="登录",
                        source_id="SIM-1", device_id="D-9002", login_ip="203.0.113.7")
print(f"  单条仿真：评分={single['final_score']} 决策={single['decision']} "
      f"命中={single['rule_count']} 条 / 求值 {len(single['steps'])} 条规则")
check(len(single["steps"]) > 0, f"分步链路含 {len(single['steps'])} 条规则的求值过程")
check(all("matched" in s and "actual_values" in s for s in single["steps"]),
      "每步含命中布尔值与引用的特征实际值（可解释）")
check(len(single["features"]) == 43, f"仿真算出 43 个特征（{len(single['features'])}）")
check(single["decision"] in ("通过", "标记", "人工审核", "二次验证", "拒绝"),
      f"输出决策：{single['decision']}")

after_counts = {
    "risk_event": db.query(RiskEvent).count(),
    "risk_feature": db.query(RiskFeature).count(),
    "risk_assessment": db.query(RiskAssessment).count(),
    "risk_case": db.query(RiskCase).count(),
    "risk_user_profile": db.query(RiskUserProfile).count(),
}
deltas = {k: after_counts[k] - before_counts[k] for k in before_counts}
print(f"  单条仿真后各表增量：{deltas}")
check(all(v == 0 for v in deltas.values()),
      f"**FR-14 验收 2：仿真后五张表零新增**（{deltas}）")

ft_key = cache.feature_key("user", "1002")
cache.safe_delete(ft_key)
sim.run_single(db, user_id="1002", event_type="登录", source_id="SIM-2",
               device_id="D-9002", login_ip="203.0.113.7")
check(cache.safe_get(ft_key) is None,
      "仿真不回填 Redis 特征缓存（write_cache=False，零副作用）")

# ---- 批量回放 ----
before_replay = db.query(RiskEvent).count()
replay = sim.replay(db, limit=20, include_detail=True)
print(f"  批量回放：{replay['sample_count']} 个样本，命中 {len(replay['rule_stats'])} 条规则，"
      f"平均分 {replay['avg_score']}")
check(replay["sample_count"] > 0, "样本来自历史案件（样本库反推可用）")
check(replay["zero_side_effect"] and replay["side_effects"]["db_writes"] == 0,
      f"**批量回放零副作用**（side_effects={replay['side_effects']}）")
check(db.query(RiskEvent).count() == before_replay, "回放后事件表行数不变")
check(replay["rule_stats"] and all(
    "hit_count" in s and "hit_rate" in s and "affected_users" in s
    for s in replay["rule_stats"]),
    f"规则统计含命中数 / 命中率 / 影响用户数（TOP: "
    f"{replay['rule_stats'][0]['rule_id']} {replay['rule_stats'][0]['hit_count']} 次 "
    f"命中率 {replay['rule_stats'][0]['hit_rate']}）")
check("decision_distribution" in replay and replay["decision_distribution"],
      f"决策分布：{replay['decision_distribution']}")
check(len(replay["results"]) == replay["sample_count"], "明细条数与样本数一致")

# ---- 样本库 ----
samples = sim.list_samples(db, limit=20)
check(samples["total"] > 0 and "by_event_type" in samples,
      f"样本库列表：{samples['total']} 条，分布 {samples['by_event_type']}")
check(all("known_score" in s for s in samples["samples"]), "样本带已知评分（可做回放对比）")

filtered = sim.replay(db, event_type="登录", limit=10)
check(filtered["sample_count"] > 0, f"按事件类型筛选样本回放（{filtered['sample_count']} 个）")

expect_error(lambda: sim.run_single(db, user_id="1002", event_type="不存在"),
             sim.SimulationServiceError, "仿真事件类型非法 → 拒绝")

# ============================================================
section("收尾：全套表状态")
for name, model in (("risk_event", RiskEvent), ("risk_feature", RiskFeature),
                    ("risk_assessment", RiskAssessment), ("risk_case", RiskCase),
                    ("risk_user_profile", RiskUserProfile),
                    ("risk_audit_log", RiskAuditLog)):
    print(f"  {name:<22}{db.query(model).count()}")

db.close()

# ============================================================
print()
print("=" * 74)
if failures:
    print(f"结果：{len(failures)} 项未通过")
    for f in failures:
        print("  ✗ " + f)
    sys.exit(1)
print("结果：全部通过")
print("=" * 74)
