# -*- coding: utf-8 -*-
"""T-12 案件服务冒烟验证

前置：先跑 `python scripts/init_db.py` 重建业务数据。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/smoke_case.py

验证：
    1. 案件列表筛选（状态 / 分类 / 冲突）+ 分页
    2. 案件详情组装（规则 + 特征快照 + 画像 + 业务单据 + 冲突说明）
    3. 处置回写映射表逐类型生效（按业务字典表**实际状态值**）
       · 下单 待支付 → 已取消            ← AC-1
       · 售后 审核中 → 审核未通过         ← AC-2（字典里的驳回态）
       · 领券 未使用 → 已作废
       · 登录（无业务表）→ 仅记录处置结论
       · 已发货订单 → 不回写，但案件可正常结案
    4. 回写失败 → 案件状态不推进（事务回滚）
    5. 审核拒绝联动加黑名单
    6. 案件审核 + 回写动作 均落审计
    7. 缓存写后失效
    8. 案件统计
"""
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ 供 import _smoke
import _smoke as smoke  # noqa: E402
from _smoke import check, section  # noqa: E402

from sqlalchemy import text  # noqa: E402

from app import cache  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.engine.decision import generate_id  # noqa: E402
from app.models import (  # noqa: E402
    RiskAssessment,
    RiskAuditLog,
    RiskCase,
    RiskEvent,
    RiskFeature,
    RiskList,
    RiskListArchive,
    RiskUserProfile,
)
from app.service import case_service as cs  # noqa: E402






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

for model in smoke.runtime_models():
    db.query(model).delete()
db.commit()
cache.invalidate_list()

# 找一个待支付订单、一个已发货订单、一个审核中的售后单、一张未使用的券
pay_order = db.execute(text(
    "SELECT order_id, user_id, order_status FROM order_info WHERE order_status='待支付' LIMIT 1")).first()
ship_order = db.execute(text(
    "SELECT order_id, user_id, order_status FROM order_info WHERE order_status='已发货' LIMIT 1")).first()
postsale = db.execute(text(
    "SELECT postsale_id, postsale_status FROM postsale WHERE postsale_status='审核中' LIMIT 1")).first()
coupon = db.execute(text(
    "SELECT receive_id, user_id, use_status FROM coupon_receive_record "
    "WHERE use_status='未使用' LIMIT 1")).first()
print(f"  待支付订单={pay_order.order_id if pay_order else None}  "
      f"已发货订单={ship_order.order_id if ship_order else None}")
print(f"  售后单={postsale.postsale_id if postsale else None}  "
      f"未使用券={coupon.receive_id if coupon else None}")


def make_case(user_id, source_id, event_type, category="订单欺诈", conflict=0, score=85, decision="拒绝"):
    """构造一个待审核案件（模拟决策引擎已创建）"""
    event_id = generate_id("evt")
    assessment_id = generate_id("ast")
    case_id = generate_id("cas")
    db.add(RiskEvent(event_id=event_id, event_type=event_type,
                     event_source_id=source_id or "", user_id=user_id, event_data="{}"))
    db.add(RiskAssessment(
        assessment_id=assessment_id, event_id=event_id, user_id=user_id,
        rule_results=json.dumps([{"rule_id": "R002", "rule_name": "大额订单异常",
                                  "rule_category": category, "risk_level": "极高",
                                  "risk_score": 95, "action": "拒绝", "description": "示例"}]),
        rule_count=1, final_score=score, risk_level="极高", decision=decision))
    db.add(RiskCase(
        case_id=case_id, assessment_id=assessment_id, user_id=user_id,
        case_status="待审核", case_category=category, conflict_flag=conflict,
        risk_detail=json.dumps({"rule_results": [], "final_score": score,
                                "score_breakdown": {"final_score": score},
                                "graylist_hit": bool(conflict)}),
        source_id=source_id, event_type=event_type))
    db.commit()
    return case_id


# ============================================================
section("一、列表 / 详情 / 统计")

c_pay = make_case(pay_order.user_id, pay_order.order_id, "下单")
c_ship = make_case(ship_order.user_id, ship_order.order_id, "下单", category="支付风险")
c_sale = make_case(pay_order.user_id, postsale.postsale_id, "售后申请", category="售后滥用")
c_coupon = make_case(coupon.user_id, coupon.receive_id, "营销领券", category="营销薅羊毛")
c_conflict = make_case(pay_order.user_id, pay_order.order_id, "下单",
                       category="登录安全", conflict=1)
make_case(pay_order.user_id, None, "登录", category="登录安全")

total, items = cs.list_cases(db, page=1, page_size=3)
check(total == 6 and len(items) == 3, f"列表总数 {total}、单页 {len(items)}（分页生效）")

total_pending, _ = cs.list_cases(db, case_status="待审核")
check(total_pending == 6, f"按状态筛选：待审核 {total_pending} 条")

total_conf, _ = cs.list_cases(db, conflict_only=True)
check(total_conf == 1, f"仅看名单冲突案件：{total_conf} 条")

total_cat, _ = cs.list_cases(db, case_category="售后滥用")
check(total_cat == 1, f"按分类筛选：售后滥用 {total_cat} 条")

detail = cs.case_detail(db, c_pay)
check(detail["case"]["case_id"] == c_pay, "详情返回正确的案件")
check(detail["assessment"] is not None and detail["assessment"]["final_score"] == 85,
      f"含评估结果（评分 {detail['assessment']['final_score']}）")
check(detail["case"]["source_id"] == pay_order.order_id
      and detail["case"]["event_type"] == "下单",
      "含 source_id / event_type（支撑「重做检查」跳转）")
check(detail["business_doc"] is not None
      and detail["business_doc"]["order_status"] == "待支付",
      f"含业务单据（{detail['business_doc'] and detail['business_doc']['type']}"
      f" 状态={detail['business_doc'] and detail['business_doc']['order_status']}）")
check(detail["score_breakdown"] is not None, "含评分链路明细（可解释性）")
check(cs.case_detail(db, c_conflict)["case"]["conflict_flag"] == 1, "冲突案件标记可见")

stats = cs.case_statistics(db)
check(stats["total"] == 6 and stats["pending_review"] == 6,
      f"统计：总数 {stats['total']}、待审 {stats['pending_review']}、冲突 {stats['conflict_cases']}")

# ============================================================
section("二、处置回写 —— 下单 待支付 → 已取消（AC-1）")

res = cs.review_case(db, case_id=c_pay, decision="已拒绝", reviewer="reviewer_zhang",
                     review_comment="确认盗号，订单金额与用户历史行为严重不符")
print(f"  writeback={res.writeback}")
check(res.case_status == "已拒绝", f"案件状态 → 已拒绝（实际 {res.case_status}）")
after = db.execute(text("SELECT order_status FROM order_info WHERE order_id=:k"),
                   {"k": pay_order.order_id}).first()
check(after.order_status == "已取消",
      f"**AC-1：订单状态已回写为「已取消」**（实际 {after.order_status}）")
check(res.writeback["success"] and res.writeback["affected_rows"] == 1,
      f"回写报告 success、影响 1 行（{res.writeback.get('action')}）")
check(res.writeback["from_status"] == "待支付" and res.writeback["to_status"] == "已取消",
      "回写记录含原状态与新状态")
check(res.reviewer == "reviewer_zhang" and res.review_time is not None, "记录审核人与审核时间")

row = db.query(RiskCase).filter(RiskCase.case_id == c_pay).first()
check(row.risk_detail and "writeback" in row.risk_detail,
      "回写结果已写入案件 risk_detail（SPEC 6.5 约束 3）")

# ============================================================
section("三、处置回写 —— 售后申请 审核中 → 审核未通过（AC-2）")

check(postsale is not None and postsale.postsale_status == "审核中", "前置：售后单当前为「审核中」")
res = cs.review_case(db, case_id=c_sale, decision="已拒绝", reviewer="reviewer_li",
                     review_comment="售后申请存在滥用特征")
after = db.execute(text("SELECT postsale_status FROM postsale WHERE postsale_id=:k"),
                   {"k": postsale.postsale_id}).first()
check(after.postsale_status == "审核未通过",
      f"**AC-2：售后单状态回写为「审核未通过」**（实际 {after.postsale_status}）")
check(after.postsale_status == "审核未通过",
      "用的是字典表实际值（字典里没有「已驳回」）")

# ============================================================
section("四、处置回写 —— 营销领券 未使用 → 已作废")

res = cs.review_case(db, case_id=c_coupon, decision="已拒绝", reviewer="reviewer_wang",
                     review_comment="批量薅羊毛")
after = db.execute(text("SELECT use_status FROM coupon_receive_record WHERE receive_id=:k"),
                   {"k": coupon.receive_id}).first()
check(after.use_status == "已作废",
      f"券状态回写为「已作废」（实际 {after.use_status}，本期为该值扩展了枚举）")

# ============================================================
section("五、不在允许范围的状态 —— 不回写但可正常结案")

check(ship_order.order_status == "已发货", "前置：订单当前为「已发货」")
res = cs.review_case(db, case_id=c_ship, decision="已拒绝", reviewer="reviewer_zhang",
                     review_comment="货已出，走售后流程")
after = db.execute(text("SELECT order_status FROM order_info WHERE order_id=:k"),
                   {"k": ship_order.order_id}).first()
check(after.order_status == "已发货", "已发货订单状态未被改写（风控不越权改写业务终态）")
check(res.writeback["attempted"] and res.writeback["success"],
      "跳过回写不算失败：案件可正常结案")
check("不回写" in (res.writeback.get("action") or ""),
      f"记录跳过原因：{res.writeback.get('action')}")
check(res.case_status == "已拒绝", f"案件状态仍正常推进为已拒绝（{res.case_status}）")

# ============================================================
section("六、回写失败 → 案件状态不推进（事务回滚）")

c_bad = make_case(pay_order.user_id, "ORD_NOT_EXIST_9999", "下单")
before_status = db.query(RiskCase).filter(RiskCase.case_id == c_bad).first().case_status
res = cs.review_case(db, case_id=c_bad, decision="已拒绝", reviewer="reviewer_x")
check(not res.success, "回写报告 success=False")
check(res.message and "回写失败" in res.message, f"返回可读的失败说明：{res.message}")
after_status = db.query(RiskCase).filter(RiskCase.case_id == c_bad).first().case_status
check(after_status == before_status == "待审核",
      f"**案件状态未推进**（仍为 {after_status}，事务已回滚）")
check(res.reviewer is None and res.review_time is None, "审核人 / 审核时间未落库")

# ============================================================
section("七、审核拒绝联动加黑名单（FR-18）")

target_case = make_case("1003", None, "登录", category="登录安全")
res = cs.review_case(db, case_id=target_case, decision="已拒绝", reviewer="reviewer_zhang",
                     review_comment="确认恶意撞库",
                     add_to_blacklist=True, blacklist_entity_types=["用户", "设备"])
check(len(res.blacklist_added) >= 1,
      f"黑名单新增 {len(res.blacklist_added)} 个主体：{res.blacklist_added}")
check(any(b["entity_type"] == "用户" and b["entity_value"] == "1003"
          for b in res.blacklist_added), "用户主体已加黑")
black_row = (db.query(RiskList)
             .filter(RiskList.entity_value == "1003", RiskList.list_type == "黑名单").first())
check(black_row is not None,
      f"黑名单记录已落库（{black_row.list_type} / {black_row.entity_value}）" if black_row
      else "黑名单记录已落库")
check(res.writeback["attempted"] and res.writeback["success"],
      f"登录事件无业务表可改，仅记录处置结论：{res.writeback.get('action')}")

# ============================================================
section("八、审计留痕 + 重复处置拦截")

audit_rows = db.query(RiskAuditLog).filter(RiskAuditLog.action_type == "案件").all()
review_logs = [a for a in audit_rows if a.action_desc.startswith("案件审核")]
wb_logs = [a for a in audit_rows if a.action_desc.startswith("处置回写")]
check(len(review_logs) >= 4, f"案件审核流水 {len(review_logs)} 条")
check(len(wb_logs) >= 3, f"处置回写流水 {len(wb_logs)} 条")
sample = [a for a in review_logs if a.target_id == c_pay]
check(sample and sample[0].before_value and "待审核" in sample[0].before_value,
      "审计含变更前值（待审核）")
check(sample and sample[0].after_value and "已拒绝" in sample[0].after_value,
      "审计含变更后值（已拒绝）")
check(res.audit_log_id is not None, f"响应返回 audit_log_id（{res.audit_log_id}）")
wb_sample = [a for a in wb_logs if a.target_id == pay_order.order_id]
check(wb_sample and wb_sample[0].after_value and "已取消" in wb_sample[0].after_value,
      "回写动作单独记审计且含目标状态")

expect_error(lambda: cs.review_case(db, case_id=c_pay, decision="已通过", reviewer="x"),
             cs.CaseStateError, "已终结案件不可重复处置")
expect_error(lambda: cs.review_case(db, case_id="cas_not_exist", decision="已拒绝", reviewer="x"),
             cs.CaseNotFoundError, "案件不存在")
expect_error(lambda: cs.review_case(db, case_id=c_conflict, decision="随便", reviewer="x"),
             cs.CaseServiceError, "处置结论非法")

# ============================================================
section("九、缓存写后失效")

uid = "1003"
cache.safe_set(cache.profile_key(uid), {"risk_score": 1}, 300)
cache.safe_set(cache.feature_key("user", uid), {"user_total_orders": 1}, 300)
check(cache.safe_get(cache.profile_key(uid)) is not None, "前置：画像缓存已种入")
c_tmp = make_case(uid, None, "登录", category="登录安全")
cs.review_case(db, case_id=c_tmp, decision="已拒绝", reviewer="reviewer_z",
               add_to_blacklist=True)
check(cache.safe_get(cache.profile_key(uid)) is None, "审核后画像缓存已失效")
check(cache.safe_get(cache.feature_key("user", uid)) is None, "审核后用户特征缓存已失效")

stats = cs.case_statistics(db)
print(f"  最终统计：{ {k: v for k, v in stats.items() if k != 'by_category'} }")
check(stats["total"] == db.query(RiskCase).count(), "统计总数与实际一致")

db.close()

# ============================================================
sys.exit(smoke.summary())
