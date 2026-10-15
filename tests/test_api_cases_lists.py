# -*- coding: utf-8 -*-
"""接口层集成测试 —— 案件管理（API-09~12）与名单管理（API-13~17）

覆盖：**AC-14**（审核拒绝联动加黑）、**AC-16**（详情三向数据）、**AC-31**（白名单准入三条）、
**AC-32**（五类主体）、**AC-34**（批量导入）、**AC-35**（处置回写）、**SPEC Q9**。
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text


def _check_via_api(client, user_id="1003", source_id="PYTEST-API-CASE",
                   device_id="D-SHARED-0001", login_ip="172.16.0.10") -> dict:
    """走 API-01 造一次检查，返回响应体"""
    resp = client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": source_id, "user_id": user_id,
        "event_data": {"device_id": device_id, "login_ip": login_ip},
    })
    assert resp.status_code == 200
    return resp.json()


# ============================================================
# 一、案件（API-09~12）
# ============================================================
def test_case_list_shape(client):
    _check_via_api(client, source_id="PYTEST-CASE-LIST")

    resp = client.get("/api/cases", params={"page": 1, "page_size": 10})
    assert resp.status_code == 200
    body = resp.json()

    assert "total" in body and "items" in body
    if body["items"]:
        item = body["items"][0]
        for key in ("case_id", "case_status", "case_category", "conflict_flag",
                    "user_id", "source_id", "event_type"):
            assert key in item


def test_case_detail_three_way(client):
    """AC-16：详情同时给出案件信息、命中规则明细、用户画像"""
    body = _check_via_api(client, source_id="PYTEST-CASE-DETAIL")
    case_id = body["case_id"]
    assert case_id

    detail = client.get(f"/api/cases/{case_id}")
    assert detail.status_code == 200
    data = detail.json()

    assert data["case"]["case_id"] == case_id
    assert data["assessment"]["assessment_id"] == body["assessment_id"]
    assert data["triggered_rules"]
    assert data["profile"]["user_id"] == "1003"
    assert data["features"]


def test_case_detail_not_found(client):
    assert client.get("/api/cases/cas-not-exist").status_code == 404


def test_case_review_reject_with_blacklist(client, reviewer_client, db):
    """AC-14：走接口审核拒绝并勾选加黑 → 案件已拒绝 + 黑名单新增"""
    body = _check_via_api(client, source_id="PYTEST-CASE-REVIEW")

    resp = reviewer_client.post(f"/api/cases/{body['case_id']}/review", json={
        "decision": "已拒绝",
        "review_comment": "接口层测试：确认刷单",
        "add_to_blacklist": True,
        "blacklist_entity_types": ["用户"],
    })
    assert resp.status_code == 200
    result = resp.json()

    assert result["case_status"] == "已拒绝"
    assert result["blacklist_added"]

    from app.models import RiskList
    db.expire_all()
    rows = db.query(RiskList).filter(RiskList.entity_value == "1003").all()
    assert any(r.list_type == "黑名单" for r in rows)


def test_case_review_uses_login_operator(client, reviewer_client):
    """审核人不传时，服务端从登录会话取真实用户（不写死 admin）"""
    body = _check_via_api(client, source_id="PYTEST-CASE-OP")

    resp = reviewer_client.post(f"/api/cases/{body['case_id']}/review",
                                json={"decision": "已通过", "review_comment": "正常"})
    assert resp.status_code == 200
    assert resp.json()["reviewer"]
    assert "admin" not in resp.json()["reviewer"], "应记录真实登录用户"


def test_case_review_duplicate_rejected(client, reviewer_client):
    body = _check_via_api(client, source_id="PYTEST-CASE-DUP")
    first = reviewer_client.post(f"/api/cases/{body['case_id']}/review", json={"decision": "已通过"})
    assert first.status_code == 200

    second = reviewer_client.post(f"/api/cases/{body['case_id']}/review", json={"decision": "已拒绝"})
    assert second.status_code == 409


def test_case_review_invalid_decision(client, reviewer_client):
    body = _check_via_api(client, source_id="PYTEST-CASE-BAD")
    resp = reviewer_client.post(f"/api/cases/{body['case_id']}/review", json={"decision": "乱写"})
    assert resp.status_code == 422


def test_case_review_writeback_unpaid_order(client, reviewer_client, db):
    """AC-35：走接口审核拒绝 → 业务订单状态变为已取消 + 审计流水记录回写动作"""
    row = db.execute(text(
        "SELECT order_id, user_id, order_status FROM order_info "
        "WHERE order_status = '待支付' LIMIT 1")).first()
    if row is None:
        pytest.skip("业务数据中没有「待支付」订单")

    order_id, user_id, original = row[0], row[1], row[2]

    # 用真实订单号对「下单」事件触发风控检查并建案
    check = client.post("/api/risk/check", json={
        "event_type": "下单", "source_id": order_id, "user_id": user_id,
        "order_id": order_id,
    })
    assert check.status_code == 200
    case_id = check.json().get("case_id")
    if not case_id:
        pytest.skip("该订单未触发建案（评分未达阈值），跳过回写校验")

    try:
        resp = reviewer_client.post(f"/api/cases/{case_id}/review", json={
            "decision": "已拒绝", "review_comment": "PYTEST 回写校验",
        })
        assert resp.status_code == 200
        result = resp.json()

        assert result["writeback"]["attempted"] is True
        assert result["writeback"]["success"] is True
        assert result["writeback"]["to_status"] == "已取消"

        # ⚠ 必须另开会话读：`db` 那个 session 在上面已经做过 SELECT，
        # 处于 REPEATABLE READ 的事务快照里，读不到 TestClient 请求提交的结果。
        from app.database import SessionLocal

        probe = SessionLocal()
        try:
            now = probe.execute(
                text("SELECT order_status FROM order_info WHERE order_id = :o"),
                {"o": order_id}).scalar()
        finally:
            probe.close()
        assert now == "已取消"
    finally:
        db.rollback()
        db.execute(text("UPDATE order_info SET order_status = :s WHERE order_id = :o"),
                   {"s": original, "o": order_id})
        db.commit()


def test_case_statistics(client):
    resp = client.get("/api/cases/statistics")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


# ============================================================
# 二、名单（API-13~17）
# ============================================================
def test_list_create_and_query(client):
    created = client.post("/api/lists", json={
        "list_type": "黑名单", "entity_type": "用户",
        "entity_value": "T-API-U1", "reason": "接口层测试",
    })
    assert created.status_code == 201
    body = created.json()
    assert body["list_id"]
    assert body["entity_value"] == "T-API-U1"

    listing = client.get("/api/lists", params={"keyword": "T-API-U1"})
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


@pytest.mark.parametrize(
    ("entity_type", "value"),
    [("用户", "T-API-5U"), ("手机号", "13900000002"), ("IP", "203.0.113.88"),
     ("设备", "T-API-5D"), ("地址", "上海市浦东新区测试路 2 号")],
)
def test_list_five_entity_types(client, db, entity_type, value):
    """AC-32：五类主体都能加入黑名单，并能触发对应主体的前置拦截"""
    created = client.post("/api/lists", json={
        "list_type": "黑名单", "entity_type": entity_type,
        "entity_value": value, "reason": "五类主体接口测试",
    })
    assert created.status_code == 201

    # 按主体直接判定，确认名单确实生效（五类主体走同一张表、同一段判定逻辑）
    from app.service import list_service as ls

    db.expire_all()
    verdict = ls.check_lists(db, [ls.Subject(entity_type, value)])
    assert verdict.decision == "拒绝", f"{entity_type} 主体未触发拦截"


def test_whitelist_requires_expire_time(client):
    """AC-31 ①：不填有效期 → 拒绝保存"""
    resp = client.post("/api/lists", json={
        "list_type": "白名单", "entity_type": "用户",
        "entity_value": "T-API-W1", "reason": "缺有效期",
    })
    assert resp.status_code == 400
    assert "BIZ_LIST_EXPIRE_REQUIRED" in resp.text


def test_whitelist_expire_too_long(client):
    """AC-31 ③：有效期 120 天 → 超上限，需二次确认"""
    too_long = (datetime.now() + timedelta(days=120)).isoformat(sep=" ")

    resp = client.post("/api/lists", json={
        "list_type": "白名单", "entity_type": "用户",
        "entity_value": "T-API-W2", "reason": "超长", "expire_time": too_long,
    })
    assert resp.status_code == 409
    assert "BIZ_LIST_EXPIRE_TOO_LONG" in resp.text

    forced = client.post("/api/lists", json={
        "list_type": "白名单", "entity_type": "用户",
        "entity_value": "T-API-W2", "reason": "超长", "expire_time": too_long,
        "force": True,
    })
    assert forced.status_code == 201


def test_whitelist_conflicts_with_blacklist(client):
    """AC-31 ②：对黑名单中的主体加白 → 409 并提示先移除黑名单"""
    client.post("/api/lists", json={
        "list_type": "黑名单", "entity_type": "用户",
        "entity_value": "T-API-W3", "reason": "先黑",
    })

    resp = client.post("/api/lists", json={
        "list_type": "白名单", "entity_type": "用户", "entity_value": "T-API-W3",
        "reason": "后白", "expire_time": (datetime.now() + timedelta(days=10)).isoformat(sep=" "),
    })
    assert resp.status_code == 409
    assert "BIZ_LIST_CONFLICT_BLACKLIST" in resp.text


def test_list_remove(client):
    created = client.post("/api/lists", json={
        "list_type": "黑名单", "entity_type": "用户",
        "entity_value": "T-API-RM", "reason": "待移除",
    })
    list_id = created.json()["list_id"]

    removed = client.delete(f"/api/lists/{list_id}")
    assert removed.status_code == 200
    assert removed.json()["removed"] is True

    assert client.get("/api/lists", params={"keyword": "T-API-RM"}).json()["total"] == 0


def test_import_template_download(client):
    """API-17：模板要能直接落成文件（base64 + 列名 + 上限提示）"""
    resp = client.get("/api/lists/template")
    assert resp.status_code == 200
    body = resp.json()
    assert body["content_base64"]
    assert body["columns"]
    assert body["max_rows"] == 5000


def test_import_csv_all_valid(client):
    """批量导入（全量通过）→ 整批入库"""
    csv = ("list_type,entity_type,entity_value,reason\n"
           "黑名单,用户,T-CSV-1,接口导入测试\n"
           "黑名单,设备,T-CSV-2,接口导入测试\n")
    resp = client.post("/api/lists/import",
                       files={"file": ("t.csv", csv.encode("utf-8"), "text/csv")})
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] == 2
    assert body["imported"] == 2
    assert body["success"] is True


def test_import_csv_with_errors_rejects_batch(client, db):
    """AC-34 / SPEC Q9：任一行有错 → 整批不入库，错误行逐条返回

    ⚠ PRD AC-34 期望「8 条成功、2 条标红」，实现取 SPEC Q9 的「不做部分入库」。
    此处按**实现**断言，冲突记入验收报告。
    """
    csv = ("list_type,entity_type,entity_value,reason\n"
           "黑名单,用户,T-BAD-1,ok\n"
           "黑名单,不存在的主体,T-BAD-2,bad\n"
           "黑名单,用户,,bad\n")
    resp = client.post("/api/lists/import",
                       files={"file": ("t.csv", csv.encode("utf-8"), "text/csv")})
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] == 3
    assert body["failed"] == 2
    assert body["imported"] == 0
    assert body["success"] is False
    assert body["code"] == "BIZ_LIST_IMPORT_INVALID"
    for err in body["errors"]:
        assert "row" in err and "message" in err

    from app.models import RiskList
    db.expire_all()
    assert db.query(RiskList).filter(
        RiskList.entity_value.like("T-BAD-%")).count() == 0, "整批拒绝，一行都不该入库"


def test_list_statistics(client):
    resp = client.get("/api/lists/statistics")
    assert resp.status_code == 200
    body = resp.json()
    assert "by_list_type" in body and "by_entity_type" in body


def test_list_archived_query(client):
    resp = client.get("/api/lists/archived")
    assert resp.status_code == 200


def test_list_subject_history(client):
    resp = client.get("/api/lists/subject-history",
                      params={"entity_type": "用户", "entity_value": "1003"})
    assert resp.status_code == 200
    assert "current" in resp.json()
