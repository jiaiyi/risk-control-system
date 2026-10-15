# -*- coding: utf-8 -*-
"""接口层集成测试 —— 鉴权与权限（PRD 2.2 权限矩阵）＋ 观测与运维接口

覆盖：**AC-19**（审计留痕）、**AC-20**（审计不可篡改）、**AC-21/22**（缓存生效与降级）、
**AC-29**（关联图谱）、**AC-30**（仿真零副作用）、**AC-36**（资损统计）、
**P-01~P-15**（页面可渲染）、以及 401/403 分离与开放重定向防护。

> ⚠️ 角色权限容易记混，矩阵原文见 `app/auth.py::PERMISSIONS`：
> · 案件审核只有**风控审核员**；规则/名单管理只有**风控策略师**；
> · 系统配置与健康度只有**系统管理员**（审计人员对配置只读、对健康度无权）。
"""
import pytest


# ============================================================
# 一、鉴权：401 / 403 必须分开
# ============================================================
def test_health_endpoint_is_public(anon_client):
    """存活探针不要求登录（否则编排系统探活会被 401 拦下）"""
    assert anon_client.get("/health").status_code == 200


def test_login_page_is_public(anon_client):
    assert anon_client.get("/login").status_code == 200


def test_api_without_login_returns_401(anon_client):
    """接口未登录 → **401**（前端据此跳登录页）"""
    assert anon_client.get("/api/rules").status_code == 401
    assert anon_client.get("/api/cases").status_code == 401


def test_page_without_login_redirects_to_login(anon_client):
    """页面未登录 → 302 跳登录页，并带上 next 便于回跳"""
    resp = anon_client.get("/cases")
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("/login")
    assert "next=" in location
    assert "%2Fcases" in location or "/cases" in location


def test_open_redirect_blocked(anon_client):
    """开放重定向防护：next 传站外地址时，登录后必须回落到站内

    （`next` 是用户可控参数，不做校验会变成钓鱼跳板。）
    """
    from app import auth

    resp = anon_client.post("/login", data={
        "user_id": "zhangcx", "password": auth._DEMO_PASSWORD,
        "next": "//evil.com/x",
    })
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "evil.com" not in location, f"开放重定向未拦截：{location}"
    assert location.startswith("/"), f"应跳站内相对路径，实际跳到 {location}"


def test_login_next_is_respected_when_safe(anon_client):
    """站内 next 应被尊重（登录后回到原页面）"""
    from app import auth

    resp = anon_client.post("/login", data={
        "user_id": "zhangcx", "password": auth._DEMO_PASSWORD, "next": "/cases",
    })
    assert resp.status_code == 302
    assert resp.headers["location"] == "/cases"


def test_login_with_wrong_password_rejected(anon_client):
    resp = anon_client.post("/login", data={
        "user_id": "zhangcx", "password": "definitely-wrong",
    })
    assert resp.status_code == 200, "口令错误应重渲染登录页而不是 302"
    assert "不正确" in resp.text


def test_api_permission_denied_returns_403(client):
    """接口权限不足 → **403**（不是 401 —— 前端据此提示「无权限」而非跳登录）

    风控策略师没有 `settings:view` 与 `health:view`。
    """
    assert client.get("/api/settings/thresholds").status_code == 403
    assert client.get("/api/health/metrics").status_code == 403


def test_reviewer_cannot_touch_rules(client_factory):
    """审核员无权访问规则管理（`rules:view` 只给策略师与审计人员）"""
    reviewer = client_factory("风控审核员")
    assert reviewer.get("/api/rules").status_code == 403
    assert reviewer.post("/api/rules", json={}).status_code == 403


def test_strategist_cannot_review_cases(client):
    """策略师看得到案件但不能审核（`cases:review` 只给审核员）"""
    assert client.get("/api/cases").status_code == 200
    assert client.post("/api/cases/cas-whatever/review",
                       json={"decision": "已通过"}).status_code == 403


def test_analyst_cannot_manage_lists(client_factory):
    """分析师对名单只读……实际矩阵里分析师连读都没有（`lists:view` 只给策略师与审计人员）"""
    analyst = client_factory("数据分析师")
    assert analyst.post("/api/lists", json={
        "list_type": "黑名单", "entity_type": "用户", "entity_value": "T-X"}).status_code == 403


def test_auditor_read_only_on_rules(client_factory):
    """审计人员对规则**只读**：GET 通、写拒"""
    auditor = client_factory("审计人员")
    assert auditor.get("/api/rules").status_code == 200
    assert auditor.post("/api/rules", json={}).status_code == 403


def test_admin_can_access_settings_and_health(admin_client):
    """系统管理员是唯一能访问系统配置与健康度的角色"""
    assert admin_client.get("/api/settings/thresholds").status_code == 200
    assert admin_client.get("/api/health/metrics").status_code == 200
    assert admin_client.get("/api/health/tasks").status_code == 200
    assert admin_client.get("/settings").status_code == 200


def test_me_endpoint_returns_current_user(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["user"]["user_id"] == "zhangcx"
    assert body["user"]["role"] == "风控策略师"


def test_roles_matrix_endpoint(client):
    """角色权限矩阵可查（供前端与文档核对）"""
    resp = client.get("/api/auth/roles")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["roles"]) == {"风控策略师", "风控审核员", "数据分析师",
                                  "系统管理员", "审计人员"}
    assert "rules:manage" in body["permissions"]["风控策略师"]
    # 审核员不得拥有规则管理权 —— 权限矩阵的「只读格子」必须体现在数据里
    assert "rules:manage" not in body["permissions"]["风控审核员"]


# ============================================================
# 二、页面渲染（P-01~P-15）
# ============================================================
@pytest.mark.parametrize("path", [
    "/", "/risk-check", "/rules", "/rules/new", "/simulation",
    "/lists", "/cases", "/graph", "/chat", "/audit", "/dashboard/loss",
])
def test_pages_render_for_strategist(client, path):
    """策略师权限内的页面都应正常渲染（T-19~T-24 的验收底线）"""
    resp = client.get(path)
    assert resp.status_code == 200, f"{path} → {resp.status_code}"
    assert "text/html" in resp.headers["content-type"]


def test_settings_page_only_for_admin(client, admin_client):
    assert client.get("/settings").status_code == 403
    assert admin_client.get("/settings").status_code == 200


# ============================================================
# 三、审计（AC-19 / AC-20）
# ============================================================
def test_audit_logs_recorded_and_queryable(client):
    """AC-19：执行「新增规则 / 加黑名单」后，审计表各新增一条，含操作人与动作"""
    client.post("/api/rules", json={
        "rule_name": "PYTEST 审计规则", "rule_category": "订单欺诈",
        "rule_condition": {"field": "user_total_orders", "op": ">", "value": 77777},
        "risk_level": "中", "risk_score": 25, "action": "标记",
    })
    client.post("/api/lists", json={
        "list_type": "黑名单", "entity_type": "用户",
        "entity_value": "T-AUDIT-U", "reason": "审计测试",
    })

    resp = client.get("/api/audit-logs", params={"page_size": 50})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 2
    assert "statistics" in body

    items = body["items"]
    for row in items[:5]:
        assert row.get("operator")
        assert row.get("action_desc") or row.get("action_type")
        assert row.get("create_time")


def test_audit_filters(client):
    resp = client.get("/api/audit-logs", params={"action_type": "规则", "page_size": 20})
    assert resp.status_code == 200
    body = resp.json()
    for row in body["items"]:
        assert row["action_type"] == "规则"


def test_audit_has_no_write_endpoints(client):
    """AC-20：审计表只提供 INSERT 与 SELECT —— 不得存在写端点，且实际调用被拒"""
    from app.main import app

    for route in app.routes:
        path = getattr(route, "path", "")
        if path.startswith("/api/audit-logs"):
            methods = set(getattr(route, "methods", set()))
            assert methods <= {"GET", "HEAD", "OPTIONS"}, \
                f"审计出现了写端点：{methods} {path}"

    assert client.put("/api/audit-logs", json={}).status_code in (404, 405)
    assert client.delete("/api/audit-logs").status_code in (404, 405)


# ============================================================
# 四、仪表盘与资损（AC-36 / NFR-03）
# ============================================================
def test_dashboard_overview(client):
    resp = client.get("/api/dashboard/overview", params={"days": 7})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert body


def test_loss_prevention_endpoint(client):
    """AC-36：资损防护统计要给出「拦下了多少钱」"""
    resp = client.get("/api/dashboard/loss-prevention", params={"days": 7})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)

    text = str(body)
    assert any(k in text for k in ("amount", "金额", "loss")), \
        f"资损统计响应里看不出金额字段：{list(body)[:10]}"


def test_realtime_event_stream(client):
    """AC / F-01：实时事件流（仪表盘滚屏）"""
    client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": "PYTEST-RT", "user_id": "1003",
        "event_data": {"device_id": "D-SHARED-0001", "login_ip": "172.16.0.10"},
    })
    resp = client.get("/api/dashboard/realtime", params={"limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert body["total"] <= 5


def test_cache_metrics_exposed(admin_client):
    """S-11：健康度要能给出性能与缓存指标（压测证据来源）"""
    resp = admin_client.get("/api/health/metrics")
    body = resp.json()
    assert isinstance(body, dict)
    text = str(body)
    assert any(k in text for k in ("p95", "P95", "cache", "缓存"))


# ============================================================
# 五、仿真（AC-30）
# ============================================================
def test_simulation_run_via_api(client, db):
    """AC-30：仿真接口返回分步链路，且实测零写入"""
    from app.service.simulation_service import _snapshot_counts

    before = _snapshot_counts(db)
    resp = client.post("/api/simulation/run", json={
        "event_type": "登录", "user_id": "1003",
        "device_id": "D-SHARED-0001", "login_ip": "172.16.0.10",
    })
    assert resp.status_code == 200
    body = resp.json()

    assert body["steps"]
    assert body["features"]
    assert body["side_effects"]["db_writes"] == 0

    db.rollback()
    assert _snapshot_counts(db) == before


def test_simulation_replay_via_api(client):
    client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": "PYTEST-REPLAY-SAMPLE", "user_id": "1003",
        "event_data": {"device_id": "D-SHARED-0001", "login_ip": "172.16.0.10"},
    })
    resp = client.post("/api/simulation/replay", json={"limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert "rule_stats" in body
    assert body["zero_side_effect"] is True


def test_simulation_samples_library(client):
    """API-25 / G-27：作弊样本库来自历史案件"""
    client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": "PYTEST-SAMPLE-LIB", "user_id": "1003",
        "event_data": {"device_id": "D-SHARED-0001", "login_ip": "172.16.0.10"},
    })
    resp = client.get("/api/simulation/samples", params={"limit": 10})
    assert resp.status_code == 200


# ============================================================
# 六、图谱与画像（AC-29 / API-19）
# ============================================================
def test_graph_query(client):
    """AC-29：关联图谱返回节点与边，且受跳数上限约束"""
    resp = client.get("/api/graph", params={
        "entity_type": "用户", "entity_value": "1003", "hops": 2})
    assert resp.status_code == 200
    body = resp.json()

    assert "nodes" in body and "edges" in body and "center" in body
    assert "truncated" in body


def test_graph_hops_upper_bound_enforced(client):
    """NFR-06 / SPEC Q3：跳数上限 3，超过在入参层被拒"""
    resp = client.get("/api/graph", params={
        "entity_type": "用户", "entity_value": "1003", "hops": 4})
    assert resp.status_code == 422


def test_related_accounts_helper(client):
    resp = client.get("/api/graph/related-accounts", params={
        "entity_type": "设备", "entity_value": "D-SHARED-0001", "min_accounts": 3})
    assert resp.status_code == 200


def test_profile_detail(client):
    client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": "PYTEST-PROFILE-API", "user_id": "1003",
        "event_data": {"device_id": "D-SHARED-0001", "login_ip": "172.16.0.10"},
    })

    resp = client.get("/api/profile/1003")
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "1003"


# ============================================================
# 七、缓存降级（AC-21 / AC-22 / NFR-14）
# ============================================================
def test_check_works_without_cache(client, monkeypatch):
    """AC-22：缓存服务不可用 → 检查仍正常完成，且结果与有缓存时**完全一致**"""
    payload = {
        "event_type": "登录", "source_id": "PYTEST-CACHE-DEGRADE",
        "user_id": "1003",
        "event_data": {"device_id": "D-SHARED-0001", "login_ip": "172.16.0.10"},
    }

    with_cache = client.post("/api/risk/check", json=payload)
    assert with_cache.status_code == 200
    baseline = with_cache.json()

    from app import cache

    monkeypatch.setattr(cache, "safe_get", lambda key: None)
    monkeypatch.setattr(cache, "safe_set", lambda key, value, ttl: True)
    monkeypatch.setattr(cache, "is_available", lambda: False)

    without_cache = client.post("/api/risk/check", json=payload)
    assert without_cache.status_code == 200, "缓存不可用时风控检查必须仍然成功"
    degraded = without_cache.json()

    assert degraded["final_score"] == baseline["final_score"], \
        "缓存只应加速读，不得改变判定结果"
    assert degraded["decision"] == baseline["decision"]
    assert len(degraded["features"]) == len(baseline["features"])


# ============================================================
# 八、系统配置（FR-56）
# ============================================================
def test_thresholds_readable_by_admin(admin_client):
    resp = admin_client.get("/api/settings/thresholds")
    assert resp.status_code == 200
    body = resp.json()
    assert "RISK_PASS_THRESHOLD" in body or "thresholds" in body


def test_threshold_update_takes_effect_and_restores(admin_client):
    """FR-56：在线调整阈值立即生效，并写审计记录前后值

    改完必须还原 —— 阈值是全局参数，改脏会让后续用例的评分断言全部漂移。
    """
    before = admin_client.get("/api/settings/thresholds").json()
    # 兼容 {..} 与 {"thresholds": {..}} 两种包装
    source = before.get("thresholds", before)
    original = source["RISK_MARK_THRESHOLD"]
    changed = original + 1

    try:
        resp = admin_client.put("/api/settings/thresholds",
                                json={"RISK_MARK_THRESHOLD": changed})
        assert resp.status_code == 200

        after = admin_client.get("/api/settings/thresholds").json()
        after_source = after.get("thresholds", after)
        assert after_source["RISK_MARK_THRESHOLD"] == changed

        # 审计留痕（记录前后值）
        logs = admin_client.get("/api/audit-logs",
                                params={"action_type": "配置", "page_size": 10}).json()
        assert logs["total"] >= 1
    finally:
        admin_client.put("/api/settings/thresholds",
                         json={"RISK_MARK_THRESHOLD": original})


def test_health_metrics_shape(admin_client):
    body = admin_client.get("/api/health/metrics").json()
    assert isinstance(body, dict)


def test_health_tasks_list(admin_client):
    resp = admin_client.get("/api/health/tasks")
    assert resp.status_code == 200


def test_cache_policy_readable(admin_client):
    assert admin_client.get("/api/settings/cache-policy").status_code == 200
