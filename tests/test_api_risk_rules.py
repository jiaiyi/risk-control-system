# -*- coding: utf-8 -*-
"""接口层集成测试 —— 实时风控（API-01）与规则管理（API-02~08）

覆盖：**AC-01**（高风险检查）、**AC-04/05**（名单短路）、**AC-11/18**（热更新与逻辑删除）、
**AC-12/13**（条件容错）、**AC-28**（事件类型隔离）、**NFR-09/10**（输入校验与分值边界）。

全部走进程内 TestClient（不需要先把服务跑起来），登录态由 `client` 夹具提供。
"""
import pytest


# ============================================================
# 一、实时风险检查（API-01）
# ============================================================
def test_risk_check_response_shape(client):
    """一次检查的响应应完整覆盖 O-01~O-05（事件信息/决策/命中明细/特征/留痕）"""
    resp = client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": "PYTEST-API-SHAPE", "user_id": "1003",
        "event_data": {"device_id": "D-SHARED-0001", "login_ip": "172.16.0.10"},
    })
    assert resp.status_code == 200
    body = resp.json()

    for key in ("assessment_id", "event_id", "user_id", "event_type", "source_id",
                "final_score", "risk_level", "decision", "rule_count",
                "triggered_rules", "features", "list_hit", "cache_hit",
                "conflict_flag", "case_id", "challenge_id", "create_time"):
        assert key in body, f"响应缺少字段 {key}"

    assert body["user_id"] == "1003"
    assert body["event_type"] == "登录"
    assert 0 <= body["final_score"] <= 100
    assert body["risk_level"] in ("低", "中", "高", "极高")
    assert body["decision"] in ("通过", "标记", "人工审核", "二次验证", "拒绝")
    assert len(body["features"]) == 43
    assert body["rule_count"] == len(body["triggered_rules"])


def test_risk_check_feature_snapshot_is_complete(client):
    """AC-08（按实现口径 43 项）：响应里的特征快照完整"""
    resp = client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": "PYTEST-API-FEAT", "user_id": "1002",
        "event_data": {"device_id": "D-9002", "login_ip": "203.0.113.7"},
    })
    features = resp.json()["features"]
    assert len(features) == 43
    assert features.get("user_total_orders") is not None


def test_risk_check_blacklist_shortcut(client, db):
    """AC-04：黑名单命中 → 拒绝/100，且响应 list_hit 标明黑名单"""
    from app.service import list_service as ls

    ls.add_list_item(db, list_type="黑名单", entity_type="用户",
                     entity_value="1003", reason="接口层黑名单测试")

    resp = client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": "PYTEST-API-BLK", "user_id": "1003",
    })
    body = resp.json()

    assert body["decision"] == "拒绝"
    assert body["final_score"] == 100
    assert body["risk_level"] == "极高"
    assert body["rule_count"] == 0
    assert body["list_hit"]["type"] == "黑名单"


def test_risk_check_whitelist_shortcut(client, db):
    """AC-05：白名单命中 → 通过/0，且审计可查"""
    from datetime import datetime, timedelta

    from app.service import list_service as ls

    ls.add_list_item(db, list_type="白名单", entity_type="用户", entity_value="1003",
                     reason="接口层白名单测试",
                     expire_time=datetime.now() + timedelta(days=5))

    resp = client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": "PYTEST-API-WLK", "user_id": "1003",
    })
    body = resp.json()
    assert body["decision"] == "通过"
    assert body["final_score"] == 0
    assert body["list_hit"]["type"] == "白名单"


@pytest.mark.parametrize("bad_type", ["不存在的类型", "login", "登录 ", ""])
def test_risk_check_invalid_event_type(client, bad_type):
    """NFR-09：枚举用 Literal 约束，非法事件类型在入参层就被挡下（422）"""
    resp = client.post("/api/risk/check", json={
        "event_type": bad_type, "source_id": "X", "user_id": "1002",
    })
    assert resp.status_code == 422


def test_risk_check_missing_user_returns_404(client):
    """用户不存在 → 404（而不是带着空数据跑完链路）"""
    resp = client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": "PYTEST-API-404", "user_id": "T-NO-SUCH-USER",
    })
    assert resp.status_code == 404


def test_risk_check_missing_required_field(client):
    """必填字段缺失 → 422"""
    resp = client.post("/api/risk/check", json={"event_type": "登录"})
    assert resp.status_code == 422


def test_risk_check_creates_case_for_high_risk(client):
    """AC-10：高风险 → 自动建案，响应带 case_id"""
    resp = client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": "PYTEST-API-CASE", "user_id": "1003",
        "event_data": {"device_id": "D-SHARED-0001", "login_ip": "172.16.0.10"},
    })
    body = resp.json()
    assert body["decision"] in ("人工审核", "拒绝")
    assert body["case_id"], "高风险必须返回案件号"


def test_risk_check_event_type_isolation(client, db):
    """AC-28：登录事件命中的规则都来自「登录安全」或「通用」"""
    from app.models import RiskRule

    resp = client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": "PYTEST-API-ISO", "user_id": "1003",
        "event_data": {"device_id": "D-SHARED-0001", "login_ip": "172.16.0.10"},
    })
    hits = resp.json()["triggered_rules"]
    assert hits

    for hit in hits:
        rule = db.query(RiskRule).filter(RiskRule.rule_id == hit["rule_id"]).first()
        assert rule.event_type in ("登录", "通用")


# ============================================================
# 二、规则管理（API-02~08）
# ============================================================
def test_list_rules_paginated(client):
    resp = client.get("/api/rules", params={"page": 1, "page_size": 5})
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] >= 41, "预置规则 41 条"
    assert len(body["items"]) <= 5
    assert "statistics" in body


def test_list_rules_filter_by_event_type(client):
    resp = client.get("/api/rules", params={"event_type": "登录", "page_size": 200})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items
    assert all(r["event_type"] in ("登录", "通用") for r in items)


def test_validate_condition_endpoint(client):
    """API-08：条件校验不落库，只回结构合法性"""
    ok = client.post("/api/rules/validate", json={
        "rule_condition": {"field": "user_total_orders", "op": ">", "value": 10}})
    assert ok.status_code == 200
    assert ok.json()["valid"] is True

    bad = client.post("/api/rules/validate", json={
        "rule_condition": {"field": "user_total_orders", "op": "=~", "value": 10}})
    assert bad.status_code == 200
    assert bad.json()["valid"] is False
    assert bad.json()["message"]


def test_rule_full_crud_lifecycle(client):
    """AC-18 相关：新建 → 详情 → 编辑 → 启停 → 逻辑删除"""
    created = client.post("/api/rules", json={
        "rule_name": "PYTEST 生命周期规则",
        "rule_category": "订单欺诈",
        "event_type": "通用",
        "rule_condition": {"field": "user_total_orders", "op": ">", "value": 99999},
        "risk_level": "高",
        "risk_score": 50,
        "action": "人工审核",
        "priority": 5,
        "description": "接口层集成测试用",
    })
    assert created.status_code == 201
    rule = created.json()
    rule_id = rule["rule_id"]
    assert rule["risk_score"] == 50

    detail = client.get(f"/api/rules/{rule_id}")
    assert detail.status_code == 200
    assert detail.json()["rule_name"] == "PYTEST 生命周期规则"

    updated = client.put(f"/api/rules/{rule_id}", json={"risk_score": 66})
    assert updated.status_code == 200
    assert updated.json()["risk_score"] == 66

    toggled = client.put(f"/api/rules/{rule_id}/toggle", json={"is_enabled": 0})
    assert toggled.status_code == 200
    assert toggled.json()["is_enabled"] == 0

    deleted = client.delete(f"/api/rules/{rule_id}")
    assert deleted.status_code == 200
    assert deleted.json()["is_deleted"] == 1

    # 逻辑删除后：默认列表不展示
    listing = client.get("/api/rules", params={"keyword": "PYTEST 生命周期规则"})
    assert listing.json()["total"] == 0

    # 详情接口也按「未删除」过滤 → 404（规则管理页不再可见）
    # 这不违背 AC-18：AC-18 要求的「历史评估仍能显示规则名称」靠的是
    # risk_assessment.rule_results 里的**命中快照**，与规则表是否可见无关。
    assert client.get(f"/api/rules/{rule_id}").status_code == 404


def test_delete_rule_is_soft(client, db):
    """AC-18：删除是逻辑删除，行还在"""
    from app.models import RiskRule

    created = client.post("/api/rules", json={
        "rule_name": "PYTEST 逻辑删除", "rule_category": "订单欺诈",
        "rule_condition": {"field": "user_total_orders", "op": ">", "value": 88888},
        "risk_level": "中", "risk_score": 30, "action": "标记",
    })
    rule_id = created.json()["rule_id"]

    client.delete(f"/api/rules/{rule_id}")

    row = db.query(RiskRule).filter(RiskRule.rule_id == rule_id).first()
    assert row is not None, "不得物理删除"
    assert row.is_deleted == 1


def test_rule_not_found(client):
    assert client.get("/api/rules/NO-SUCH-RULE").status_code == 404
    assert client.put("/api/rules/NO-SUCH-RULE", json={"risk_score": 10}).status_code == 404
    assert client.delete("/api/rules/NO-SUCH-RULE").status_code == 404


@pytest.mark.parametrize("score", [-1, 101, 1000])
def test_rule_score_out_of_range(client, score):
    """NFR-10：规则分值限制在 [0, 100] —— 越界在入参层被拒"""
    resp = client.post("/api/rules", json={
        "rule_name": "PYTEST 越界分值", "rule_category": "订单欺诈",
        "rule_condition": {"field": "user_total_orders", "op": ">", "value": 1},
        "risk_level": "高", "risk_score": score, "action": "人工审核",
    })
    assert resp.status_code == 422


def test_rule_priority_bounds(client):
    """优先级越界同样被拒（Field(ge, le)）"""
    resp = client.post("/api/rules", json={
        "rule_name": "PYTEST 越界优先级", "rule_category": "订单欺诈",
        "rule_condition": {"field": "user_total_orders", "op": ">", "value": 1},
        "risk_level": "高", "risk_score": 50, "action": "人工审核", "priority": 1000,
    })
    assert resp.status_code == 422


def test_rule_change_takes_effect_immediately(client):
    """AC-11 / NFR-17：停用规则后立即可见（无需重启）—— 用规则列表复核"""
    created = client.post("/api/rules", json={
        "rule_name": "PYTEST 热更新", "rule_category": "订单欺诈",
        "event_type": "通用",
        "rule_condition": {"field": "user_total_orders", "op": ">", "value": 1},
        "risk_level": "中", "risk_score": 20, "action": "标记",
    })
    rule_id = created.json()["rule_id"]

    client.put(f"/api/rules/{rule_id}/toggle", json={"is_enabled": 0})

    listing = client.get("/api/rules", params={"is_enabled": 0, "page_size": 200})
    assert rule_id in [r["rule_id"] for r in listing.json()["items"]]
