# -*- coding: utf-8 -*-
"""T-09 决策引擎冒烟验证

前置：先跑 `python scripts/init_db.py` 重建数据
（场景依赖的时间窗口基于 datetime.now()，隔天即失效）。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/smoke_decision.py

验证三件事：
    1. 评分链路各步骤数值符合 SPEC 11.1 Q5 的固定顺序
    2. 9 个已知场景命中预期规则（与 init_db.py 打印的可触发场景一致）
    3. 六张表按决策档位正确落库（案件 / 二次验证 / 画像）
"""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.engine.decision import (  # noqa: E402
    blend_profile_score,
    compute_score,
    profile_alpha,
    run_risk_check,
    score_to_decision,
)
from app.engine.rule import RuleHitResult  # noqa: E402
from app.models import (  # noqa: E402
    RiskAssessment,
    RiskCase,
    RiskChallenge,
    RiskEvent,
    RiskFeature,
    RiskRule,
    RiskUserProfile,
)

OK, BAD = "  [OK]  ", "  [!!]  "
failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print((OK if cond else BAD) + msg)
    if not cond:
        failures.append(msg)


def fake_hit(score: int, level: str = "高", action: str = "人工审核", rule_id: str = "T999") -> RuleHitResult:
    """构造命中结果，用于评分链路的纯函数验证"""
    rule = SimpleNamespace(
        rule_id=rule_id, rule_name=rule_id, rule_category="测试场景",
        risk_level=level, risk_score=score, action=action,
        description="", priority=1,
    )
    return RuleHitResult(rule)


def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


# ============================================================
# 一、评分链路
# ============================================================
section("一、评分链路（SPEC 11.1 Q5，顺序：max → 多规则加分 → 灰名单加严 → 否决 → 截断）")

b = compute_score([])
check(b.final_score == 0, "零规则命中 → 0")

b = compute_score([fake_hit(60)])
check(b.final_score == 60 and b.multi_rule_bonus == 0, "单条命中 60 → 60（不叠加加分）")

b = compute_score([fake_hit(60), fake_hit(50), fake_hit(40)])
check(b.final_score == 66 and b.multi_rule_bonus == 6, "3 条 60/50/40 → 60 + 3×2 = 66")

b = compute_score([fake_hit(60)], graylist_hit=True)
check(b.final_score == 75 and b.graylist_bonus == settings.RISK_GRAYLIST_BONUS,
      f"灰名单加严 → 60 + {settings.RISK_GRAYLIST_BONUS} = 75")

b = compute_score([fake_hit(85, level="极高", action="拒绝")])
check(b.veto and b.final_score == 90, "极高 85 → 一票否决抬到 90")

b = compute_score([fake_hit(60), fake_hit(85, level="极高", action="拒绝")])
check(b.veto and b.final_score == 90, "普通 60 + 极高 85 → max(85+3, 90) = 90")

b = compute_score([fake_hit(70, level="极高", action="拒绝")], graylist_hit=True)
check(b.veto and b.final_score == 90, "极高 70 + 灰名单加严 → 85 → 否决抬到 90（加严不改变否决语义，Q7）")

b = compute_score([fake_hit(98) for _ in range(4)])
check(b.final_score == 100, "98×4 → 98 + 9 = 107 → 截断 100")

b = compute_score([fake_hit(90, level="极高", action="拒绝")] * 4)
check(b.final_score == 99, "极高 90×4 → 90 + 3×3 = 99（不触发加分截断）")

check(score_to_decision(70, [fake_hit(70, action="二次验证")]) == "二次验证",
      "60~79 档 + 命中 action=二次验证 → 决策「二次验证」")
check(score_to_decision(70, [fake_hit(70, action="人工审核")]) == "人工审核",
      "60~79 档 + 其他 action → 决策「人工审核」")
check(score_to_decision(29, []) == "通过", "29 → 通过")
check(score_to_decision(30, []) == "标记", "30 → 标记")
check(score_to_decision(59, []) == "标记", "59 → 标记")
check(score_to_decision(60, []) == "人工审核", "60 → 人工审核")
check(score_to_decision(80, []) == "拒绝", "80 → 拒绝")

# ============================================================
# 二、画像动态衰减
# ============================================================
section("二、用户画像动态衰减加权（PRD FR-43）")

check(profile_alpha(0) == settings.PROFILE_ALPHA_LOW, f"近30天订单 0 → α={settings.PROFILE_ALPHA_LOW}（低活跃）")
check(profile_alpha(3) == settings.PROFILE_ALPHA_MID, f"近30天订单 3 → α={settings.PROFILE_ALPHA_MID}（中活跃）")
check(profile_alpha(5) == settings.PROFILE_ALPHA_HIGH, f"近30天订单 5 → α={settings.PROFILE_ALPHA_HIGH}（高活跃）")
check(blend_profile_score(80, None, 0.7) == 80, "首次评估（无历史）→ 直接取本次评分")
check(blend_profile_score(80, 20, 0.7) == 62, "α=0.7：round(0.7×80 + 0.3×20) = 62")
check(blend_profile_score(80, 20, 0.4) == 44, "α=0.4：round(0.4×80 + 0.6×20) = 44")
check(abs(blend_profile_score(80, 20, 0.7) - 20) > abs(blend_profile_score(80, 20, 0.4) - 20),
      "同分序列下高活跃画像变化幅度更大")

# ============================================================
# 三、9 个场景端到端
# ============================================================
section("三、9 个已知场景端到端（含落库）")

# (标题, user_id, device_id, login_ip, event_type, 期望命中规则)
SCENARIOS = [
    ("① 撞库试登（真实设备）",   "1002", "D-9002",        "203.0.113.7", "登录",     ["L001"]),
    ("② 新设备异地登录",         "1002", "D-NEW-8888",    "203.0.113.7", "登录",     ["L001", "L004"]),
    ("③ 同设备养号（3 账号）",   "1003", "D-SHARED-0001", "172.16.0.10", "登录",     ["L002"]),
    ("④ 同 IP 批量（5 账号）",   "1006", "D-7700",        "10.99.99.99", "登录",     ["L003", "A004"]),
    ("⑤ 短时高频登录",           "1010", "D-7710",        "10.99.99.99", "登录",     ["L005"]),
    ("⑥ 短时批量领券",           "1003", "D-8003",        "172.16.0.20", "营销领券", ["C001"]),
    ("⑦ 高频 + 全券种扫荡",      "1004", "D-8004",        "172.16.0.20", "营销领券", ["C003", "C004"]),
    ("⑧ 大额累计领券",           "1005", "D-8005",        "172.16.0.20", "营销领券", ["C005"]),
    ("⑨ 同设备多账号领券",       "1006", "D-SHARED-0002", "172.16.0.20", "营销领券", ["C002"]),
]

db = SessionLocal()

# 清空风控运行数据，保证本脚本可重复执行（19 张业务只读表的数据不动）
for _model in (RiskFeature, RiskAssessment, RiskCase, RiskChallenge, RiskEvent, RiskUserProfile):
    db.query(_model).delete()
db.query(RiskRule).filter(RiskRule.rule_id == "SMOKE-CHL-001").delete()
db.commit()

baseline = {
    "risk_event": db.query(RiskEvent).count(),
    "risk_feature": db.query(RiskFeature).count(),
    "risk_assessment": db.query(RiskAssessment).count(),
    "risk_case": db.query(RiskCase).count(),
    "risk_challenge": db.query(RiskChallenge).count(),
    "risk_user_profile": db.query(RiskUserProfile).count(),
}

print(f"{'场景':<24}{'命中规则':<26}{'评分':>4}  {'决策':<8}{'案件/验证':<12}")
print("-" * 74)
outcomes = []
for title, uid, dev, ip, etype, expect in SCENARIOS:
    out = run_risk_check(
        db, user_id=uid, event_type=etype, source_id=f"SMOKE-{uid}-{etype}",
        event_data={"device_id": dev, "login_ip": ip, "source": "smoke_decision"},
        device_id=dev, login_ip=ip,
    )
    outcomes.append((title, expect, out))
    hit_ids = [h.rule_id for h in out.hits]
    extra = [r for r in hit_ids if r not in expect]
    tag = f"{out.case_id or out.challenge_id or '-'}"
    line = f"{title:<24}{','.join(hit_ids) or '(无)':<26}{out.final_score:>4}  {out.decision:<8}{tag:<12}"
    if extra:
        line += f"  ← 额外命中 {extra}"
    print(line)

print("-" * 74)
for title, expect, out in outcomes:
    hit_ids = [h.rule_id for h in out.hits]
    missing = [r for r in expect if r not in hit_ids]
    check(not missing, f"{title}：期望规则全部命中 {expect}（实际 {hit_ids}）")

# 场景②与⑨命中「极高」规则，应触发一票否决
veto_cases = [(t, o) for t, _, o in outcomes if o.breakdown.veto]
check(len(veto_cases) >= 2, f"含「极高」规则的场景触发一票否决（实际 {[t for t, _ in veto_cases]}）")
for t, o in veto_cases:
    check(o.final_score >= settings.RISK_VETO_MIN_SCORE,
          f"{t}：否决后评分 {o.final_score} ≥ {settings.RISK_VETO_MIN_SCORE}")

for title, _, out in outcomes:
    check(0 <= out.final_score <= 100, f"{title}：评分落在 [0,100]（{out.final_score}）")
    check(out.rule_count == len(out.hits), f"{title}：rule_count 与命中数一致（{out.rule_count}）")

# ============================================================
# 四、落库校验
# ============================================================
section("四、落库校验（单事务写多表）")

delta = {
    "risk_event": db.query(RiskEvent).count() - baseline["risk_event"],
    "risk_feature": db.query(RiskFeature).count() - baseline["risk_feature"],
    "risk_assessment": db.query(RiskAssessment).count() - baseline["risk_assessment"],
    "risk_case": db.query(RiskCase).count() - baseline["risk_case"],
    "risk_challenge": db.query(RiskChallenge).count() - baseline["risk_challenge"],
    "risk_user_profile": db.query(RiskUserProfile).count() - baseline["risk_user_profile"],
}

n = len(SCENARIOS)
expect_case = sum(1 for _, _, o in outcomes if o.decision in ("人工审核", "拒绝"))
expect_challenge = sum(1 for _, _, o in outcomes if o.decision == "二次验证")
expect_users = len({uid for _, uid, *_ in SCENARIOS})

for k, v in delta.items():
    print(f"  {k:<20} +{v}")
print()

check(delta["risk_event"] == n, f"risk_event 新增 {n} 行（实际 {delta['risk_event']}）")
check(delta["risk_assessment"] == n, f"risk_assessment 新增 {n} 行（实际 {delta['risk_assessment']}）")
check(delta["risk_feature"] == n * 43, f"risk_feature 新增 {n}×43 = {n * 43} 行（实际 {delta['risk_feature']}）")
check(delta["risk_case"] == expect_case,
      f"risk_case 新增 {expect_case} 行（决策=人工审核/拒绝，实际 {delta['risk_case']}）")
check(delta["risk_challenge"] == expect_challenge,
      f"risk_challenge 新增 {expect_challenge} 行（决策=二次验证，实际 {delta['risk_challenge']}）")
check(delta["risk_user_profile"] == expect_users,
      f"risk_user_profile 新增 {expect_users} 个用户（实际 {delta['risk_user_profile']}）")

# 特征快照的 entity_type 分布（SPEC Q6 前缀映射）
section("五、特征快照 entity_type 分布（SPEC Q6 前缀映射）")
row = db.execute(
    text(
        "SELECT entity_type, COUNT(*) AS c FROM risk_feature "
        "WHERE event_id = :eid GROUP BY entity_type ORDER BY c DESC"
    ),
    {"eid": outcomes[0][2].event_id},
).fetchall()
for r in row:
    print(f"  {r.entity_type:<8}{r.c}")
total = sum(r.c for r in row)
check(total == 43, f"单次事件特征快照共 43 条（实际 {total}）")
check({r.entity_type for r in row} >= {"用户", "订单", "地址", "登录", "领券", "设备", "IP", "手机号"},
      "覆盖 8 个实体类型（订单与商品同属「订单」）")

# 案件与评估的关联完整性
section("六、案件 / 评估关联校验")
case = db.query(RiskCase).filter(RiskCase.source_id.like("SMOKE-%")).first()
if case:
    ast = db.query(RiskAssessment).filter(RiskAssessment.assessment_id == case.assessment_id).first()
    check(ast is not None, "案件可通过 assessment_id 关联到评估记录")
    check(case.case_category and case.case_category != "未知",
          f"案件分类取自最高优先级命中规则（{case.case_category}）")
    check(case.risk_detail and "score_breakdown" in case.risk_detail,
          "案件 risk_detail 含评分链路明细（可解释性）")

profile = db.query(RiskUserProfile).filter(RiskUserProfile.user_id == "1002").first()
if profile:
    check(profile.assessment_count == 2, f"1002 评估次数累计为 2（实际 {profile.assessment_count}）")
    check(profile.profile_data is not None and "alpha" in profile.profile_data,
          "画像记录含衰减系数快照")

# ============================================================
# 七、二次验证链路（PRD AC-37 / SPEC Q10）
# ============================================================
# 预置 41 条规则中没有 action='二次验证' 的规则，这里临时插一条恒命中规则，
# 验证「决策=二次验证 → 只建 challenge、不建 case」这条链路的落库行为。
section("七、二次验证链路（PRD AC-37 / SPEC Q10）")

CHL_RULE = "SMOKE-CHL-001"
db.query(RiskRule).filter(RiskRule.rule_id == CHL_RULE).delete()   # 幂等：清掉上次残留
db.commit()
db.add(RiskRule(
    rule_id=CHL_RULE,
    rule_name="冒烟-二次验证规则",
    rule_category="登录安全",
    event_type="登录",
    rule_condition='{"field": "user_total_orders", "op": ">=", "value": 0}',
    risk_level="高",
    risk_score=65,
    action="二次验证",
    is_enabled=1,
    priority=999,          # 最高优先级 → 保证成为 hits[0]，决策细分取决于它
    is_deleted=0,
    description="冒烟验证用，恒命中；验证结束后自动删除",
))
db.commit()

base_chl = db.query(RiskChallenge).count()
base_cas = db.query(RiskCase).count()
chl_out = run_risk_check(
    db, user_id="1002", event_type="登录", source_id="SMOKE-CHL",
    event_data={"source": "smoke_decision", "purpose": "challenge"},
    device_id="D-9002", login_ip="203.0.113.7",
)
print(f"  决策={chl_out.decision}  评分={chl_out.final_score}  "
      f"命中={[h.rule_id for h in chl_out.hits]}")

check(chl_out.decision == "二次验证",
      "命中 action=二次验证 的规则 → 决策「二次验证」")
check(chl_out.challenge_id is not None, f"生成二次验证记录（{chl_out.challenge_id}）")
check(db.query(RiskChallenge).count() - base_chl == 1, "risk_challenge 新增 1 行")
check(db.query(RiskCase).count() - base_cas == 0, "二次验证档不创建案件（SPEC Q10）")

chl = db.query(RiskChallenge).filter(
    RiskChallenge.challenge_id == chl_out.challenge_id).first()
if chl:
    check(chl.status == "待验证", f"验证状态为「待验证」（实际 {chl.status}）")
    check(chl.expire_time > datetime.now(),
          f"过期时间在未来 {settings.CHALLENGE_TIMEOUT_MINUTES} 分钟（{chl.expire_time}）")
    check(bool(chl.verify_token), "生成验证凭据占位（PRD 10.4 模拟实现）")

db.query(RiskRule).filter(RiskRule.rule_id == CHL_RULE).delete()   # 清理
db.commit()

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
