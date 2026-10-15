# -*- coding: utf-8 -*-
"""案件服务单测 —— `app/service/case_service.py`

覆盖：**AC-14/15**（审核拒绝联动加黑 → 闭环验证）、**AC-16/17**（详情三向数据 / 重做检查回填）、
**AC-35**（处置回写业务系统）、**AC-37**（二次验证与超时升级）、**AC-39**（关联主体）。

处置回写会**真的改业务表**（`order_info.order_status` 等），因此相关用例
必须在 finally 里把原状态写回 —— 业务只读表是全项目共用的「既定事实」，
被测试改脏会让后续的特征计算静默出错（不报错但数字不对）。
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.engine.decision import generate_id
from app.models import RiskCase, RiskChallenge, RiskList
from app.service import case_service
from app.service import event as event_svc


def _make_case(db, *, source_id=None, event_type="登录", user_id="1002",
               status="待审核", category="订单欺诈") -> RiskCase:
    """造一条待审案件（案件创建归决策引擎，本文件只测「处置」环节）"""
    case = RiskCase(
        case_id=generate_id("cas"),
        assessment_id=generate_id("ast"),
        user_id=user_id,
        case_status=status,
        case_category=category,
        conflict_flag=0,
        risk_detail="{}",
        source_id=source_id,
        event_type=event_type,
    )
    db.add(case)
    db.commit()
    return case


def _make_challenge(db, *, user_id="1002", status="待验证", expired=False) -> RiskChallenge:
    chl = RiskChallenge(
        challenge_id=generate_id("chl"),
        assessment_id=generate_id("ast"),
        user_id=user_id,
        verify_token="vt-pytest",
        status=status,
        expire_time=(datetime.now() - timedelta(minutes=1) if expired
                     else datetime.now() + timedelta(minutes=5)),
    )
    db.add(chl)
    db.commit()
    return chl


# ============================================================
# 一、审核处置 + 加黑（AC-14）
# ============================================================
def test_review_reject_adds_blacklist(db):
    """AC-14：审核选「拒绝」并勾选加入黑名单 → 案件状态=已拒绝；黑名单新增一条"""
    case = _make_case(db, user_id="1002")

    result = case_service.review_case(
        db, case_id=case.case_id, decision="已拒绝", reviewer="张晨曦(zhangcx)",
        review_comment="确认刷单", add_to_blacklist=True,
        blacklist_entity_types=["用户"], blacklist_reason=None,
    )

    assert result.case_status == "已拒绝"
    assert result.reviewer == "张晨曦(zhangcx)"
    assert result.blacklist_added, "应记录新增的黑名单条目"
    assert result.audit_log_id, "审核动作必须留痕"

    rows = db.query(RiskList).filter(RiskList.entity_value == "1002").all()
    assert any(r.list_type == "黑名单" for r in rows)
    black = [r for r in rows if r.list_type == "黑名单"][0]
    assert case.case_id in (black.reason or ""), "黑名单原因里应带上案件号"


def test_review_pass_does_not_add_blacklist(db):
    """选「通过」时不加黑（加黑只在拒绝了才做）"""
    case = _make_case(db, user_id="1002")
    result = case_service.review_case(
        db, case_id=case.case_id, decision="已通过", reviewer="pytest",
        add_to_blacklist=True,
    )
    assert result.case_status == "已通过"
    assert result.blacklist_added == []
    assert db.query(RiskList).filter(RiskList.entity_value == "1002").count() == 0


def test_review_updates_audit_log(db):
    """AC-19：审核动作要落审计，含操作人、动作、目标、前后值"""
    from app.models import RiskAuditLog

    case = _make_case(db, user_id="1002")
    case_service.review_case(db, case_id=case.case_id, decision="已拒绝",
                             reviewer="pytest", review_comment="测试意见")

    logs = db.query(RiskAuditLog).filter(RiskAuditLog.target_id == case.case_id).all()
    assert logs, "案件审核应写审计"
    assert any("案件审核" in (row.action_desc or "") for row in logs)
    assert logs[0].operator == "pytest"


def test_review_twice_rejected(db):
    """已终结的案件不可重复处置"""
    case = _make_case(db, user_id="1002")
    case_service.review_case(db, case_id=case.case_id, decision="已拒绝", reviewer="pytest")

    with pytest.raises(case_service.CaseStateError):
        case_service.review_case(db, case_id=case.case_id, decision="已通过", reviewer="pytest")


def test_review_unknown_case(db):
    with pytest.raises(case_service.CaseNotFoundError):
        case_service.review_case(db, case_id="cas-not-exist", decision="已拒绝", reviewer="pytest")


def test_review_invalid_decision(db):
    case = _make_case(db, user_id="1002")
    with pytest.raises(case_service.CaseServiceError):
        case_service.review_case(db, case_id=case.case_id, decision="随便写", reviewer="pytest")


# ============================================================
# 二、闭环验证（AC-15）
# ============================================================
def test_blacklist_closes_the_loop(db):
    """AC-15：上一步已加黑 → 同一用户再发起检查 → 前置拦截返回拒绝/100"""
    case = _make_case(db, user_id="1002")
    case_service.review_case(
        db, case_id=case.case_id, decision="已拒绝", reviewer="pytest",
        add_to_blacklist=True, blacklist_entity_types=["用户"],
    )

    out = event_svc.process_event(
        db, user_id="1002", event_type="登录", source_id="PYTEST-LOOP",
        device_id="D-9002", login_ip="203.0.113.7",
    )

    assert out.decision == "拒绝"
    assert out.final_score == 100
    assert out.risk_level == "极高"
    assert out.list_shortcut is True, "应走名单前置拦截（不跑规则）"


# ============================================================
# 三、案件详情（AC-16 / AC-17）
# ============================================================
def test_case_detail_provides_three_way_data(db):
    """AC-16：案件详情同时返回案件信息、命中规则明细、用户画像"""
    source_id = "PYTEST-DETAIL"
    out = event_svc.process_event(
        db, user_id="1003", event_type="登录", source_id=source_id,
        device_id="D-SHARED-0001", login_ip="172.16.0.10",
    )
    assert out.case_id, "该场景应自动建案"

    detail = case_service.case_detail(db, out.case_id)

    assert detail["case"]["case_id"] == out.case_id
    assert detail["assessment"]["assessment_id"] == out.assessment_id
    assert detail["triggered_rules"], "必须带命中规则明细"
    assert detail["profile"]["user_id"] == "1003"
    assert detail["features"], "必须带特征快照"
    assert detail["score_breakdown"]


def test_case_detail_carries_replay_fields(db):
    """AC-17：详情要带原始业务 ID 与事件类型，供「重做检查」一键回填"""
    out = event_svc.process_event(
        db, user_id="1003", event_type="登录", source_id="PYTEST-REPLAY",
        device_id="D-SHARED-0001", login_ip="172.16.0.10",
    )
    detail = case_service.case_detail(db, out.case_id)

    assert detail["case"]["source_id"] == "PYTEST-REPLAY"
    assert detail["case"]["event_type"] == "登录"


@pytest.mark.xfail(reason="AC-39/P-10 待修复：case_detail 的 related_entities 恒为空列表", strict=False)
def test_case_detail_includes_related_entities(db):
    """AC-39 / P-10：案件详情应展示该主体下的关联实体（同地址/同设备其他账号）

    ⚠ 2026-10-15 验收发现：`case_detail` 里写的是
    `"related_entities": []  # T-14 图谱接入` —— 硬编码空列表。T-14 图谱服务
    其实已经完成（`app/service/graph_service.py`），只是没接进案件详情，
    因此案件详情页拿不到关联账号。
    """
    out = event_svc.process_event(
        db, user_id="1003", event_type="登录", source_id="PYTEST-RELATED",
        device_id="D-SHARED-0001", login_ip="172.16.0.10",
    )
    detail = case_service.case_detail(db, out.case_id)
    assert detail["related_entities"], "案件详情应带关联实体（AC-39 / P-10）"


# ============================================================
# 四、处置回写（AC-35 / FR-48 / SPEC 6.5）
# ============================================================
def test_writeback_cancels_unpaid_order(db):
    """AC-35：未支付订单对应的待审案件被拒绝 → 业务订单状态变为已取消"""
    row = db.execute(text(
        "SELECT order_id, user_id, order_status FROM order_info "
        "WHERE order_status = '待支付' LIMIT 1")).first()
    assert row, "业务数据里应存在「待支付」订单"

    order_id, user_id, original = row[0], row[1], row[2]
    case = _make_case(db, source_id=order_id, event_type="下单",
                      user_id=user_id, category="订单欺诈")
    try:
        result = case_service.review_case(
            db, case_id=case.case_id, decision="已拒绝", reviewer="pytest",
        )

        assert result.case_status == "已拒绝"
        assert result.writeback["attempted"] is True
        assert result.writeback["success"] is True
        assert result.writeback["from_status"] == "待支付"
        assert result.writeback["to_status"] == "已取消"
        assert result.writeback["affected_rows"] == 1

        now_status = db.execute(
            text("SELECT order_status FROM order_info WHERE order_id = :o"),
            {"o": order_id}).scalar()
        assert now_status == "已取消", "业务订单状态应被回写为已取消"
    finally:
        db.rollback()
        db.execute(text("UPDATE order_info SET order_status = :s WHERE order_id = :o"),
                   {"s": original, "o": order_id})
        db.commit()


def test_writeback_skips_finalized_order(db):
    """SPEC 6.5：已发货等终态单据**不回写**（风控不越权改写业务终态）"""
    row = db.execute(text(
        "SELECT order_id, user_id FROM order_info "
        "WHERE order_status = '已发货' LIMIT 1")).first()
    if row is None:
        pytest.skip("业务数据中没有「已发货」订单，跳过终态回写校验")

    order_id, user_id = row[0], row[1]
    case = _make_case(db, source_id=order_id, event_type="下单", user_id=user_id)

    result = case_service.review_case(db, case_id=case.case_id,
                                      decision="已拒绝", reviewer="pytest")

    assert result.case_status == "已拒绝", "不能因不回写而卡住结案"
    assert result.writeback["success"] is True
    assert result.writeback["affected_rows"] == 0
    assert "已超出允许范围" in (result.writeback["action"] or "")

    now_status = db.execute(
        text("SELECT order_status FROM order_info WHERE order_id = :o"),
        {"o": order_id}).scalar()
    assert now_status == "已发货", "终态单据不得被改写"


def test_writeback_for_login_event_has_no_table(db):
    """登录事件没有可写的业务表 —— 属于「固有属性」，不应被报成错误"""
    case = _make_case(db, source_id="PYTEST-LOGIN", event_type="登录", user_id="1002")

    result = case_service.review_case(db, case_id=case.case_id,
                                      decision="已拒绝", reviewer="pytest")

    assert result.writeback["success"] is True
    assert result.writeback["attempted"] is True
    assert result.writeback["affected_rows"] == 0


def test_writeback_missing_business_doc_reports_error(db):
    """单据不存在 → 回写失败，事务回滚，案件状态不推进（API-12 约定）"""
    case = _make_case(db, source_id="ORD-NOT-EXIST-9999", event_type="下单",
                      user_id="1002")

    result = case_service.review_case(db, case_id=case.case_id,
                                      decision="已拒绝", reviewer="pytest")

    assert result.writeback["success"] is False
    assert "不存在" in (result.writeback["error"] or "")
    assert result.case_status == "待审核", "回写失败时案件状态不得推进"


# ============================================================
# 五、二次验证（AC-37 / SPEC Q10）
# ============================================================
def test_verify_challenge_success_creates_no_case(db):
    """Q10：验证通过 → status=已验证，**不创建案件**"""
    chl = _make_challenge(db)
    result = case_service.verify_challenge(db, chl.challenge_id, "vt-pytest")

    assert result["verified"] is True
    assert result["case_id"] is None

    db.expire_all()
    assert db.query(RiskChallenge).filter(
        RiskChallenge.challenge_id == chl.challenge_id).first().status == "已验证"
    assert db.query(RiskCase).filter(
        RiskCase.assessment_id == chl.assessment_id).count() == 0


def test_verify_challenge_wrong_token(db):
    chl = _make_challenge(db)
    with pytest.raises(case_service.ChallengeTokenInvalidError):
        case_service.verify_challenge(db, chl.challenge_id, "wrong-token")


def test_expired_challenge_escalates_to_case(db):
    """AC-37：超时未验证 → status=已超时，并自动升级为人工审核案件"""
    chl = _make_challenge(db, expired=True)

    n = case_service.expire_challenges(db)
    assert n >= 1

    db.expire_all()
    assert db.query(RiskChallenge).filter(
        RiskChallenge.challenge_id == chl.challenge_id).first().status == "已超时"

    cases = db.query(RiskCase).filter(RiskCase.assessment_id == chl.assessment_id).all()
    assert len(cases) == 1, "超时应复用同一 assessment_id 建案"
    assert cases[0].case_status == "待审核"


def test_verify_after_expire_escalates_instead_of_passing(db):
    """Q10：超时后凭据即使正确也不再放行，直接升级为人工审核"""
    chl = _make_challenge(db, expired=True)

    result = case_service.verify_challenge(db, chl.challenge_id, "vt-pytest")

    assert result.get("verified") is not True
    assert result["status"] == "已超时"
    assert result["case_id"], "超时应建案"


def test_verify_unknown_challenge(db):
    with pytest.raises(case_service.ChallengeNotFoundError):
        case_service.verify_challenge(db, "chl-not-exist", "x")


# ============================================================
# 六、列表与统计
# ============================================================
def test_list_cases_filters(db):
    _make_case(db, user_id="1002", status="待审核", category="订单欺诈")
    _make_case(db, user_id="1003", status="已拒绝", category="登录安全")

    total, items = case_service.list_cases(db, case_status="待审核")
    assert total >= 1
    assert all(c.case_status == "待审核" for c in items)

    total2, items2 = case_service.list_cases(db, case_category="登录安全")
    assert total2 >= 1
    assert all(c.case_category == "登录安全" for c in items2)


def test_list_cases_conflict_filter(db):
    conflict = _make_case(db, user_id="1002")
    conflict.conflict_flag = 1
    db.commit()
    _make_case(db, user_id="1003")

    total, items = case_service.list_cases(db, conflict_only=True)
    assert total >= 1
    assert all(c.conflict_flag == 1 for c in items)


def test_case_statistics_shape(db):
    _make_case(db, user_id="1002", status="待审核")
    stats = case_service.case_statistics(db)
    assert "total" in stats
    assert "pending" in stats or "by_status" in stats
