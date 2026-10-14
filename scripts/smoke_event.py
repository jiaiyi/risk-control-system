# -*- coding: utf-8 -*-
"""T-11 事件管道冒烟验证（端到端）

前置：先跑 `python scripts/init_db.py` 重建业务数据。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/smoke_event.py

验证：
    1. 入参兜底（用户不存在 / 事件类型非法）
    2. 名单直接裁决（黑名单拒绝 / 白名单放行）—— AC-5：零特征快照
    3. 黑白冲突 → 人工审核 + 冲突案件 + 评分仍按正常流程
    4. 灰名单加严（同一场景前后对比差 15 分）
    5. 六类事件端到端跑通
    6. list_hit 载荷对齐 API-01
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

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
    RiskUserProfile,
)
from app.service import event as ev  # noqa: E402
from app.service import list_service as ls  # noqa: E402

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


def feature_count(db, event_id: str) -> int:
    return db.query(RiskFeature).filter(RiskFeature.event_id == event_id).count()


USER = "1003"
DEVICE = "D-SHARED-0001"
IP = "172.16.0.10"

db = SessionLocal()

for model in (RiskList, RiskAuditLog, RiskFeature, RiskAssessment,
              RiskCase, RiskChallenge, RiskEvent, RiskUserProfile):
    db.query(model).delete()
db.commit()
cache.invalidate_list()
cache.invalidate_features(USER)

real_order = db.execute(
    text("SELECT order_id FROM order_info WHERE user_id = :u ORDER BY create_time DESC LIMIT 1"),
    {"u": USER},
).first()
ORDER_ID = real_order.order_id if real_order else None
print(f"  使用用户 {USER}，真实订单 {ORDER_ID}")

# ============================================================
section("一、入参兜底")

expect_error(lambda: ev.process_event(db, user_id="NO_SUCH_USER", event_type="登录"),
             ev.UserNotFoundError, "用户不存在 → BIZ_USER_NOT_FOUND")
expect_error(lambda: ev.process_event(db, user_id=USER, event_type="不存在的类型"),
             ev.EventTypeInvalidError, "事件类型非法 → BIZ_EVENT_TYPE_INVALID")

# ============================================================
section("二、正常流程（名单未命中）")

r0 = ev.process_event(db, user_id=USER, event_type="登录", source_id="EVT-BASE",
                      device_id=DEVICE, login_ip=IP)
print(f"  决策={r0.decision} 评分={r0.final_score} 命中={[r['rule_id'] for r in r0.triggered_rules]}")
check(r0.list_shortcut is False, "未命中名单 → 走完整风控流程")
check(r0.rule_count > 0, f"有规则命中（{r0.rule_count} 条）")
check(feature_count(db, r0.event_id) == 43,
      f"产生 43 条特征快照（实际 {feature_count(db, r0.event_id)}）")
check(len(r0.features) == 43, f"响应携带 43 个特征（实际 {len(r0.features)}）")
base_score = r0.final_score

# ============================================================
section("三、名单直接裁决 —— 黑名单（AC-5：零特征快照）")

ls.add_list_item(db, list_type="黑名单", entity_type="IP", entity_value="10.9.9.9",
                 reason="撞库来源 IP", created_by="tester")
r_black = ev.process_event(db, user_id=USER, event_type="登录", source_id="EVT-BLACK",
                           device_id="D-ANY", login_ip="10.9.9.9")
print(f"  决策={r_black.decision} 评分={r_black.final_score} 等级={r_black.risk_level}")
check(r_black.decision == "拒绝", f"黑名单 → 决策「拒绝」（实际 {r_black.decision}）")
check(r_black.final_score == 100, f"评分固定 100（实际 {r_black.final_score}）")
check(r_black.risk_level == "极高", f"等级「极高」（实际 {r_black.risk_level}）")
check(r_black.rule_count == 0, "命中规则数为 0（未做规则匹配）")
check(r_black.list_shortcut is True, "标记为名单短路路径")
check(feature_count(db, r_black.event_id) == 0,
      f"**AC-5：不产生特征快照**（实际 {feature_count(db, r_black.event_id)} 条）")
check(r_black.list_hit_payload()["type"] == "黑名单", "list_hit.type = 黑名单")
check(db.query(RiskEvent).filter(RiskEvent.event_id == r_black.event_id).count() == 1,
      "短路路径仍写 risk_event（每次检查可追溯）")
check(db.query(RiskAssessment).filter(
    RiskAssessment.assessment_id == r_black.assessment_id).count() == 1,
    "短路路径仍写 risk_assessment")

# ============================================================
section("四、名单直接裁决 —— 白名单（需落审计，PRD 4.6「滥用防护」）")

ls.add_list_item(db, list_type="白名单", entity_type="IP", entity_value="10.8.8.8",
                 reason="战略客户专线", created_by="admin",
                 expire_time=datetime.now() + timedelta(days=30))
r_white = ev.process_event(db, user_id=USER, event_type="登录", source_id="EVT-WHITE",
                           device_id="D-ANY", login_ip="10.8.8.8")
print(f"  决策={r_white.decision} 评分={r_white.final_score} 等级={r_white.risk_level}")
check(r_white.decision == "通过", f"白名单 → 决策「通过」（实际 {r_white.decision}）")
check(r_white.final_score == 0 and r_white.risk_level == "低", "评分 0 / 等级 低")
check(feature_count(db, r_white.event_id) == 0, "白名单短路同样不产生特征快照")
audit_white = db.query(RiskAuditLog).filter(
    RiskAuditLog.action_desc == "白名单命中放行").all()
check(len(audit_white) == 1 and "10.8.8.8" in (audit_white[0].after_value or ""),
      "白名单命中已落审计且记录了主体值")

# ============================================================
section("五、黑白冲突（PRD FR-38：不做自动裁决，评分仍按正常流程）")

# 先给用户加白，再加黑（黑名单添加不受白名单限制，走合法路径）
ls.add_list_item(db, list_type="白名单", entity_type="设备", entity_value=DEVICE,
                 reason="误判澄清", created_by="admin",
                 expire_time=datetime.now() + timedelta(days=30))
ls.add_list_item(db, list_type="黑名单", entity_type="设备", entity_value=DEVICE,
                 reason="后续确认恶意", created_by="tester")

r_conf = ev.process_event(db, user_id=USER, event_type="登录", source_id="EVT-CONFLICT",
                          device_id=DEVICE, login_ip=IP)
print(f"  决策={r_conf.decision} 评分={r_conf.final_score} 案件={r_conf.case_id} "
      f"conflict_flag={r_conf.conflict_flag}")
check(r_conf.decision == "人工审核", f"冲突 → 决策「人工审核」（实际 {r_conf.decision}）")
check(r_conf.conflict_flag == 1, "conflict_flag=1")
check(r_conf.case_id is not None, f"自动创建冲突案件（{r_conf.case_id}）")
check(feature_count(db, r_conf.event_id) == 43,
      "冲突场景仍做完整特征计算（评分按正常流程计算）")
conf_case = db.query(RiskCase).filter(RiskCase.case_id == r_conf.case_id).first()
check(conf_case is not None and conf_case.conflict_flag == 1, "案件中 conflict_flag 已置 1")
check(conf_case.case_category and conf_case.case_category != "未知",
      f"案件分类正常（{conf_case.case_category}）")
check(r_conf.list_hit_payload()["type"] == "冲突", "list_hit.type = 冲突")
audit_conf = db.query(RiskAuditLog).filter(
    RiskAuditLog.action_desc == "名单冲突移交人工审核").all()
check(len(audit_conf) == 1, "冲突已落审计")

# ============================================================
section("六、灰名单加严（+15 分，插在「多规则加分后、一票否决前」）")

# 清掉设备上的黑/白，只留灰名单
for item in db.query(RiskList).filter(
        RiskList.entity_type == "设备", RiskList.entity_value == DEVICE).all():
    ls.remove_list_item(db, item.list_id, operator="tester")

r_before = ev.process_event(db, user_id=USER, event_type="登录", source_id="EVT-GRAY-0",
                            device_id=DEVICE, login_ip=IP)
ls.add_list_item(db, list_type="灰名单", entity_type="设备", entity_value=DEVICE,
                 reason="观察期", created_by="tester")
r_after = ev.process_event(db, user_id=USER, event_type="登录", source_id="EVT-GRAY-1",
                           device_id=DEVICE, login_ip=IP)
print(f"  灰名单前={r_before.final_score}，灰名单后={r_after.final_score}，"
      f"差值={r_after.final_score - r_before.final_score}")
check(r_before.final_score == r_after.final_score - settings.RISK_GRAYLIST_BONUS,
      f"灰名单加严恰为 +{settings.RISK_GRAYLIST_BONUS} 分")
check(r_after.list_hit_payload()["type"] == "灰名单", "list_hit.type = 灰名单")
check(r_after.list_shortcut is False, "灰名单不短路，仍走完整流程")

# ============================================================
section("七、六类事件端到端")

SCENARIOS = [
    ("登录", "SRC-登录", None),
    ("营销领券", "SRC-领券", None),
    ("下单", "SRC-下单", ORDER_ID),
    ("支付", "SRC-支付", ORDER_ID),
    ("售后申请", "SRC-售后", ORDER_ID),
    ("物流投诉", "SRC-投诉", ORDER_ID),
]
for etype, src, oid in SCENARIOS:
    r = ev.process_event(db, user_id=USER, event_type=etype, source_id=src,
                         order_id=oid, device_id=DEVICE, login_ip=IP)
    payload = r.to_dict()
    ok = (payload["user_id"] == USER and payload["event_type"] == etype
          and isinstance(payload["final_score"], int) and payload["list_hit"]["type"])
    check(ok, f"{etype:<8} → 决策={r.decision:<5} 评分={r.final_score:>3} "
              f"命中={r.rule_count} 条特征={feature_count(db, r.event_id)}")

# ============================================================
section("八、响应结构对齐 API-01（SPEC 6.2）")

payload = r0.to_dict()
required = {"assessment_id", "event_id", "user_id", "final_score", "risk_level",
            "decision", "rule_count", "triggered_rules", "features",
            "list_hit", "cache_hit", "create_time"}
missing = required - set(payload.keys())
check(not missing, f"API-01 必需字段齐全（缺 {missing or '无'}）")
check(set(payload["list_hit"].keys()) >= {"type", "entities", "detail"},
      f"list_hit 结构完整：{sorted(payload['list_hit'].keys())}")
check(isinstance(payload["features"], dict) and len(payload["features"]) == 43,
      f"features 为 43 键字典（实际 {len(payload['features'])}）")
check(payload["triggered_rules"] and "rule_id" in payload["triggered_rules"][0],
      f"triggered_rules 元素含 rule_id（{len(payload['triggered_rules'])} 条）")

# 全库一致性
section("九、落库总量校验")
counts = {
    "risk_event": db.query(RiskEvent).count(),
    "risk_feature": db.query(RiskFeature).count(),
    "risk_assessment": db.query(RiskAssessment).count(),
    "risk_case": db.query(RiskCase).count(),
}
for k, v in counts.items():
    print(f"  {k:<20}{v}")
check(counts["risk_event"] == counts["risk_assessment"],
      "每个事件都有对应评估（短路路径也留痕）")
check(counts["risk_feature"] < counts["risk_event"] * 43,
      f"存在短路事件 → 特征总数 {counts['risk_feature']} < 事件数×43")

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
