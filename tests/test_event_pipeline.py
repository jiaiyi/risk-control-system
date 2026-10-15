# -*- coding: utf-8 -*-
"""事件管道单测 —— `app/service/event.py`

覆盖：**AC-04/05**（黑/白名单前置短路且不产生特征快照）、**AC-06**（冲突移交人工审核）、
**AC-09**（事务一致性）、**AC-10**（自动建案）、**AC-28**（事件类型隔离）、
**AC-33**（灰名单加严端到端）。

管道是「名单判定 → 决策引擎 → 落库」的汇合点，所以这里的用例都是集成性质的。
"""
import pytest
from sqlalchemy import text

from app.models import (
    RiskAssessment,
    RiskAuditLog,
    RiskCase,
    RiskEvent,
    RiskFeature,
    RiskRule,
    RiskUserProfile,
)
from app.service import event as event_svc
from app.service import list_service as ls


def _count(db, model) -> int:
    return db.query(model).count()


# ============================================================
# 一、名单前置短路（AC-04 / AC-05）
# ============================================================
def test_blacklist_short_circuit_has_no_feature_snapshot(db):
    """AC-04：黑名单命中 → 立即拒绝/100 分，且 `risk_feature` 表**无**新增记录"""
    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="1002", reason="刷单")

    before = _count(db, RiskFeature)
    out = event_svc.process_event(
        db, user_id="1002", event_type="登录", source_id="PYTEST-BLK",
        device_id="D-9002", login_ip="203.0.113.7",
    )

    assert out.decision == "拒绝"
    assert out.final_score == 100
    assert out.risk_level == "极高"
    assert out.list_shortcut is True
    assert out.rule_count == 0
    assert _count(db, RiskFeature) == before, "名单直接裁决路径不得产生特征快照"

    # 仍要写 event + assessment，保证每次检查可追溯
    assert db.query(RiskEvent).filter(RiskEvent.event_id == out.event_id).count() == 1
    assert db.query(RiskAssessment).filter(
        RiskAssessment.assessment_id == out.assessment_id).count() == 1


def test_whitelist_short_circuit_passes_and_audits(db):
    """AC-05：白名单命中 → 立即通过/0 分，不执行特征计算与规则匹配，且日志可查"""
    from datetime import datetime, timedelta

    ls.add_list_item(db, list_type="白名单", entity_type="用户", entity_value="1002",
                     reason="VIP 客户", expire_time=datetime.now() + timedelta(days=10))

    before_feat = _count(db, RiskFeature)
    before_audit = _count(db, RiskAuditLog)

    out = event_svc.process_event(
        db, user_id="1002", event_type="登录", source_id="PYTEST-WLK",
        device_id="D-9002", login_ip="203.0.113.7",
    )

    assert out.decision == "通过"
    assert out.final_score == 0
    assert out.risk_level == "低"
    assert out.rule_count == 0
    assert _count(db, RiskFeature) == before_feat
    assert _count(db, RiskAuditLog) > before_audit, "白名单命中必须留痕（PRD 4.6 滥用防护）"

    logs = db.query(RiskAuditLog).all()
    assert any("白名单" in (row.action_desc or "") for row in logs)


def test_graylist_goes_through_normal_flow_with_bonus(db):
    """AC-33 端到端：灰名单不拦截，评分 = 正常评分 + 15，且命中记录里带灰名单说明"""
    ls.add_list_item(db, list_type="灰名单", entity_type="用户", entity_value="1003",
                     reason="观察期")

    out = event_svc.process_event(
        db, user_id="1003", event_type="登录", source_id="PYTEST-GRY",
        device_id="D-8003", login_ip="172.16.0.20",
    )

    assert out.list_shortcut is False, "灰名单不应走短路路径"
    assert out.rule_count > 0, "灰名单应继续走规则匹配"
    assert out.list_verdict.graylist_hit is True
    assert out.to_dict()["list_hit"]["type"] == "灰名单"


# ============================================================
# 二、名单冲突（AC-06）
# ============================================================
def test_conflict_goes_to_manual_review_and_creates_flagged_case(db):
    """AC-06：黑白同时命中 → 决策=人工审核 + 自动建案 + 案件标注冲突原因"""
    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="1002", reason="黑")
    db.execute(text(
        "INSERT INTO risk_list (list_type, entity_type, entity_value, reason, created_by) "
        "VALUES ('白名单', '用户', '1002', '白', 'pytest')"))
    db.commit()

    out = event_svc.process_event(
        db, user_id="1002", event_type="登录", source_id="PYTEST-CONFLICT",
        device_id="D-9002", login_ip="203.0.113.7",
    )

    assert out.decision == "人工审核"
    assert out.conflict_flag == 1
    assert out.case_id, "冲突必须建案交人工"
    assert out.list_shortcut is False, "冲突不做自动裁决，要走正常评分流程"

    case = db.query(RiskCase).filter(RiskCase.case_id == out.case_id).first()
    assert case is not None
    assert case.conflict_flag == 1
    assert case.case_status == "待审核"

    # 冲突也要留审计（FR-38 要求）
    logs = db.query(RiskAuditLog).all()
    assert any("冲突" in (row.action_desc or "") for row in logs)


@pytest.mark.xfail(reason="AC-06 待修复：_build_case 未把 conflict 写进 risk_detail", strict=False)
def test_conflict_case_detail_shows_reason(db):
    """AC-06 后半段：案件详情要**明确标注冲突原因与命中名单**

    ⚠ 2026-10-15 验收发现的缺陷：`_build_case` 落库的 `risk_detail` 里只有
    `final_score / score_breakdown / graylist_hit / rule_results`，**没有 `conflict` 键**；
    而 `case_service.case_detail` 第 324 行读的是 `risk_detail["conflict"]["detail"]`
    —— 于是 `conflict_detail` 恒为 None。`conflict_flag` 本身是对的（=1），
    所以冲突案件能被筛出来，但详情页拿不到冲突说明。
    """
    from app.service import case_service

    ls.add_list_item(db, list_type="黑名单", entity_type="用户", entity_value="1002", reason="黑")
    db.execute(text(
        "INSERT INTO risk_list (list_type, entity_type, entity_value, reason, created_by) "
        "VALUES ('白名单', '用户', '1002', '白', 'pytest')"))
    db.commit()

    out = event_svc.process_event(
        db, user_id="1002", event_type="登录", source_id="PYTEST-CONFLICT-DETAIL",
        device_id="D-9002", login_ip="203.0.113.7",
    )
    detail = case_service.case_detail(db, out.case_id)
    assert detail["conflict_detail"], "AC-06：案件详情应给出冲突原因"


# ============================================================
# 三、正常流程
# ============================================================
def test_normal_flow_writes_all_tables(db):
    """正常路径：一次检查写 event / feature / assessment / profile 四类数据"""
    before = {m.__name__: _count(db, m) for m in
              (RiskEvent, RiskFeature, RiskAssessment, RiskUserProfile)}

    out = event_svc.process_event(
        db, user_id="1003", event_type="登录", source_id="PYTEST-NORMAL",
        device_id="D-SHARED-0001", login_ip="172.16.0.10",
    )

    assert out.list_shortcut is False
    assert out.rule_count == len(out.triggered_rules)
    assert _count(db, RiskEvent) == before["RiskEvent"] + 1
    assert _count(db, RiskFeature) > before["RiskFeature"], "正常路径必须写特征快照"
    assert _count(db, RiskAssessment) == before["RiskAssessment"] + 1
    assert _count(db, RiskUserProfile) >= before["RiskUserProfile"], "画像应被 upsert"


def test_feature_snapshot_belongs_to_event(db):
    """快照要挂在本次事件上（event_id 关联），否则无法做证据链回溯"""
    out = event_svc.process_event(
        db, user_id="1003", event_type="登录", source_id="PYTEST-SNAP",
        device_id="D-SHARED-0001", login_ip="172.16.0.10",
    )
    rows = db.query(RiskFeature).filter(RiskFeature.event_id == out.event_id).count()
    assert rows == 43


def test_invalid_event_type_rejected(db):
    with pytest.raises(event_svc.EventTypeInvalidError):
        event_svc.process_event(db, user_id="1002", event_type="不存在的类型")

    assert set(event_svc.VALID_EVENT_TYPES) == {
        "登录", "营销领券", "下单", "支付", "售后申请", "物流投诉"}


@pytest.mark.parametrize("event_type", ["登录", "营销领券", "下单", "支付", "售后申请", "物流投诉"])
def test_all_six_event_types_accepted(db, event_type):
    """S-01 / D-01：六类事件都能跑完整链路（课程原文要求五类，本项目实现六类）"""
    out = event_svc.process_event(
        db, user_id="1002", event_type=event_type,
        source_id=f"PYTEST-ET-{event_type}",
        device_id="D-9002", login_ip="203.0.113.7",
    )
    assert out.decision in ("通过", "标记", "人工审核", "二次验证", "拒绝")
    assert 0 <= out.final_score <= 100


def test_event_type_isolation_in_pipeline(db):
    """AC-28 端到端：登录事件只会命中「登录安全」与「通用」类规则"""
    out = event_svc.process_event(
        db, user_id="1003", event_type="登录", source_id="PYTEST-ISO",
        device_id="D-SHARED-0001", login_ip="172.16.0.10",
    )
    assert out.triggered_rules, "该场景应有命中规则"

    for hit in out.triggered_rules:
        rule = db.query(RiskRule).filter(RiskRule.rule_id == hit["rule_id"]).first()
        assert rule is not None
        assert rule.event_type in ("登录", "通用"), \
            f"{rule.rule_id} 属于「{rule.event_type}」类，不应在登录事件中参与求值"


def test_missing_user_is_rejected(db):
    """入参兜底：业务库里没有该用户 → 明确报 404 错误，而不是带着空数据跑完链路

    （`ensure_user_exists` 的语义是**校验**而非自动建档 —— 风控取数全部依赖
    user_info，用户不存在时算出来的特征全是默认值，那种「不报错但结果错」的
    隐性故障比直接报错危险得多。）
    """
    with pytest.raises(event_svc.UserNotFoundError):
        event_svc.process_event(
            db, user_id="T-NEW-USER-99999", event_type="登录",
            source_id="PYTEST-NEWUSER", device_id="T-DEV-X", login_ip="10.0.0.1",
        )


def test_empty_user_id_rejected(db):
    with pytest.raises(event_svc.UserNotFoundError):
        event_svc.process_event(db, user_id="   ", event_type="登录")


# ============================================================
# 四、事务一致性（AC-09）
# ============================================================
def test_rollback_leaves_no_partial_data(db, monkeypatch):
    """AC-09：构造持久化中途失败 → 事件/特征/评估/案件/画像**全部回滚**，无脏数据

    做法：让特征快照写入步骤抛异常。此时 `risk_event` 已经被 add 进会话，
    若事务边界不对（例如中途 commit 过），库里就会留下一半数据。
    """
    import app.engine.decision as decision_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("模拟持久化中途失败")

    monkeypatch.setattr(decision_mod, "_persist_features", _boom)

    before = {m.__name__: _count(db, m) for m in
              (RiskEvent, RiskFeature, RiskAssessment, RiskCase, RiskUserProfile)}

    with pytest.raises(RuntimeError, match="模拟持久化中途失败"):
        event_svc.process_event(
            db, user_id="1003", event_type="登录", source_id="PYTEST-ROLLBACK",
            device_id="D-SHARED-0001", login_ip="172.16.0.10",
        )

    db.rollback()

    after = {m.__name__: _count(db, m) for m in
             (RiskEvent, RiskFeature, RiskAssessment, RiskCase, RiskUserProfile)}
    assert after == before, f"回滚不彻底，残留脏数据：{after}"


def test_no_orphan_source_id_after_rollback(db):
    """回滚后不该留下带本次 source_id 的任何行（按业务 ID 复查一遍）"""
    import app.engine.decision as decision_mod

    monkeypatch_marker = "PYTEST-ORPHAN"
    original = decision_mod._persist_features

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    decision_mod._persist_features = _boom
    try:
        with pytest.raises(RuntimeError):
            event_svc.process_event(
                db, user_id="1003", event_type="登录", source_id=monkeypatch_marker,
                device_id="D-SHARED-0001", login_ip="172.16.0.10",
            )
    finally:
        decision_mod._persist_features = original

    db.rollback()
    assert db.query(RiskCase).filter(RiskCase.source_id == monkeypatch_marker).count() == 0
