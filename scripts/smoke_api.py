# -*- coding: utf-8 -*-
"""T-17 接口层冒烟验证（TestClient 端到端）

前置：先跑 `python scripts/init_db.py` 重建业务数据。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/smoke_api.py

验证：
    1. 路由注册完整（SPEC 6.1 接口清单）
    2. 风险检查 API-01（正常流程 + 黑名单短路 + 入参兜底）
    3. 规则 API-02~08 全流程（含逻辑删除、条件校验）
    4. 名单 API-13~17（白名单硬约束、批量导入整批拒绝、归档检索）
    5. 案件 API-09~12（详情组装 + 审核 + 回写）
    6. 图谱 / 画像 / 仪表盘 / 仿真 / 审计 / 二次验证
    7. 配置与健康度 API-46~52（阈值在线调整 + 审计）
    8. **统一异常处理**：服务层异常 → {code, message, detail} + 正确 HTTP 状态
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ 供 import _smoke
import _smoke as smoke  # noqa: E402
from _smoke import check, section  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    RiskAssessment,
    RiskAuditLog,
    RiskCase,
    RiskChallenge,
    RiskEvent,
    RiskFeature,
    RiskList,
    RiskListArchive,
    RiskRule,
    RiskUserProfile,
)






# ---- 清表（保留 41 条预置规则）----
db = SessionLocal()
for model in smoke.runtime_models():
    db.query(model).delete()
db.commit()
db.close()

# ⚠ 必须用已登录的 client —— app/main.py 的鉴权中间件会拦下所有 /api/*，
#   裸 TestClient(app) 转发会拿到 401，看起来像接口坏了。
client = smoke.auth_client()
# 案件审核（API-12）需要 cases:review，按 PRD 只有风控审核员有 ——
# 用策略师的 client 打审核接口会拿到 403，响应体里没有 case_status，
# 表现为 KeyError: 'case_status'（不是接口坏了，是角色不对）。
reviewer_client = smoke.auth_client("风控审核员")
# 配置（API-46~49）与健康度（API-50~52）按 PRD.md:107-108 只属于系统管理员；
# 读取阈值审计人员也能（settings:view），但运维任务只有管理员有 health:view。
admin_client = smoke.auth_client("系统管理员")

# ============================================================
section("一、路由注册（SPEC 6.1）")

api_paths = sorted({r.path for r in app.routes
                    if getattr(r, "path", "").startswith("/api")})
print(f"  /api 下共 {len(api_paths)} 条路由")
for p in api_paths:
    print(f"    {p}")

check(len(api_paths) >= 30, f"/api 路由数 {len(api_paths)}（SPEC 预期 34 个 API 端点）")
for must in ("/api/risk/check", "/api/rules", "/api/cases", "/api/lists",
             "/api/graph", "/api/profile/{user_id}", "/api/dashboard/overview",
             "/api/simulation/run", "/api/audit-logs", "/api/challenge/{challenge_id}/verify",
             "/api/settings/thresholds", "/api/health/metrics"):
    check(must in api_paths, f"已注册 {must}")

r = client.get("/health")
check(r.status_code == 200 and r.json()["status"] == "ok", f"/health 存活探针可用（{r.status_code}）")

# ============================================================
section("二、API-01 风险检查")

# 未命中名单 → 完整流程
r = client.post("/api/risk/check", json={
    "event_type": "登录", "source_id": "API-T1", "user_id": "1003",
    "event_data": {"device_id": "D-SHARED-0001", "login_ip": "172.16.0.10"},
})
check(r.status_code == 200, f"正常检查返回 200（{r.status_code}）")
body = r.json()
check(body["decision"] in ("通过", "标记", "人工审核", "二次验证", "拒绝"),
      f"决策={body['decision']} 评分={body['final_score']} 命中 {body['rule_count']} 条")
check(len(body["features"]) == 43, f"响应携带 43 个特征（{len(body['features'])}）")
check(body["list_hit"]["type"] == "无", f"名单未命中（{body['list_hit']['type']}）")
check("cache_hit" in body and "assessment_id" in body, "响应含 API-01 规定的 list_hit / cache_hit")

# 黑名单短路
client.post("/api/lists", json={"list_type": "黑名单", "entity_type": "IP",
                                "entity_value": "10.7.7.7", "reason": "接口层测试"})
r = client.post("/api/risk/check", json={
    "event_type": "登录", "source_id": "API-T2", "user_id": "1003",
    "event_data": {"device_id": "D-X", "login_ip": "10.7.7.7"},
})
body = r.json()
check(body["decision"] == "拒绝" and body["final_score"] == 100,
      f"黑名单短路 → 拒绝 / 100 分（{body['decision']} / {body['final_score']}）")
check(len(body["features"]) == 0, f"短路路径不返回特征（{len(body['features'])}）")

# 入参兜底
r = client.post("/api/risk/check", json={"event_type": "登录", "source_id": "X",
                                         "user_id": "NO_SUCH_USER"})
check(r.status_code == 404 and r.json()["code"] == "BIZ_USER_NOT_FOUND",
      f"用户不存在 → 404 + BIZ_USER_NOT_FOUND（{r.status_code}）")
r = client.post("/api/risk/check", json={"event_type": "乱写", "source_id": "X", "user_id": "1003"})
check(r.status_code == 422, f"非法事件类型被 Pydantic 拦下（{r.status_code}）")

# ============================================================
section("三、API-02~08 规则管理")

r = client.get("/api/rules", params={"page_size": 5})
check(r.status_code == 200 and r.json()["total"] >= 41,
      f"规则列表 {r.json()['total']} 条（预置 41 + 可能新增）")

r = client.post("/api/rules/validate", json={
    "rule_condition": {"field": "user_total_orders", "op": ">=", "value": 10}})
check(r.json()["valid"] is True, "合法条件校验通过")
r = client.post("/api/rules/validate", json={
    "rule_condition": {"field": "x", "op": "不支持的运算符", "value": 1}})
check(r.json()["valid"] is False and r.json()["message"], f"非法运算符被识别：{r.json()['message'][:40]}")

r = client.post("/api/rules", json={
    "rule_name": "接口层测试规则", "rule_category": "账户风险", "event_type": "通用",
    "rule_condition": {"field": "user_total_orders", "op": ">=", "value": 99999},
    "risk_level": "中", "risk_score": 35, "action": "标记", "priority": 10,
    "description": "冒烟测试用",
})
check(r.status_code == 201, f"新增规则返回 201（{r.status_code}）")
new_rule_id = r.json()["rule_id"]
check(r.json()["is_deleted"] == 0, f"新规则 ID = {new_rule_id}")

r = client.put(f"/api/rules/{new_rule_id}", json={"risk_score": 45, "rule_name": "改过名的规则"})
check(r.status_code == 200 and r.json()["risk_score"] == 45, "部分更新生效（分值 35 → 45）")
check(r.json()["rule_name"] == "改过名的规则", "规则名同步更新")

r = client.put(f"/api/rules/{new_rule_id}/toggle", json={"is_enabled": 0})
check(r.json()["is_enabled"] == 0, "停用生效")
r = client.put(f"/api/rules/{new_rule_id}/toggle", json={"is_enabled": 1})
check(r.json()["is_enabled"] == 1, "启用生效")

r = client.post("/api/rules", json={
    "rule_name": "非法条件规则", "rule_category": "账户风险",
    "rule_condition": {"field": "x", "op": "not_an_op", "value": 1},
    "risk_level": "中", "risk_score": 30, "action": "标记",
})
check(r.status_code == 400 and r.json()["code"] == "BIZ_RULE_CONDITION_INVALID",
      f"非法条件 → 400 + BIZ_RULE_CONDITION_INVALID（{r.status_code}）")

r = client.delete(f"/api/rules/{new_rule_id}")
check(r.status_code == 200 and r.json()["is_deleted"] == 1, "逻辑删除生效")
r = client.get(f"/api/rules/{new_rule_id}")
check(r.status_code == 404, f"已删除规则查不到（{r.status_code}）")
db = SessionLocal()
still = db.query(RiskRule).filter(RiskRule.rule_id == new_rule_id).first()
check(still is not None and still.is_deleted == 1, "**物理行仍在**（逻辑删除，历史评估仍可追溯）")
db.close()

# ============================================================
section("四、API-13~17 名单管理")

r = client.post("/api/lists", json={"list_type": "灰名单", "entity_type": "设备",
                                    "entity_value": "D-API-001", "reason": "接口测试"})
check(r.status_code == 201, f"新增灰名单 201（{r.status_code}）")
gray_id = r.json()["list_id"]
check(r.json()["expire_time"] is not None, f"灰名单自动带观察期（{r.json()['expire_time']}）")

r = client.post("/api/lists", json={"list_type": "白名单", "entity_type": "用户",
                                    "entity_value": "U-NOEXP"})
check(r.status_code == 400 and r.json()["code"] == "BIZ_LIST_EXPIRE_REQUIRED",
      f"白名单无有效期 → 400 + BIZ_LIST_EXPIRE_REQUIRED（{r.status_code}）")

r = client.post("/api/lists", json={"list_type": "黑名单", "entity_type": "用户",
                                    "entity_value": "1002", "reason": "测试互斥"})
black_id = r.json()["list_id"]
r = client.post("/api/lists", json={
    "list_type": "白名单", "entity_type": "用户", "entity_value": "1002",
    "reason": "应被拒", "expire_time": "2026-12-31T00:00:00"})
check(r.status_code == 409 and r.json()["code"] == "BIZ_LIST_CONFLICT_BLACKLIST",
      f"黑名单主体加白 → 409 + BIZ_LIST_CONFLICT_BLACKLIST（{r.status_code}）")

r = client.get("/api/lists", params={"list_type": "灰名单"})
check(r.status_code == 200 and r.json()["total"] >= 1, f"名单列表筛选（{r.json()['total']} 条灰名单）")

r = client.get("/api/lists/template")
check(r.status_code == 200 and r.json()["content_base64"], "导入模板可下载（base64）")

import base64  # noqa: E402
import io  # noqa: E402

# 构造一个 3 行的 xlsx：1 行合法 + 1 行非法主体类型 + 1 行白名单缺有效期
from openpyxl import Workbook  # noqa: E402
wb = Workbook(); ws = wb.active
ws.append(["list_type", "entity_type", "entity_value", "reason", "expire_time", "created_by"])
ws.append(["黑名单", "设备", "D-IMP-API-1", "导入测试", "", "importer"])
ws.append(["黑名单", "不存在类型", "X", "", "", ""])
ws.append(["白名单", "IP", "1.2.3.4", "缺有效期", "", ""])
buf = io.BytesIO(); wb.save(buf)

r = client.post("/api/lists/import",
                files={"file": ("t.xlsx", buf.getvalue(),
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"operator": "importer"})
body = r.json()
check(r.status_code == 200 and body["success"] is False,
      f"有错误行 → success=false（imported={body['imported']}, failed={body['failed']}）")
check(len(body["errors"]) == 2 and all("row" in e for e in body["errors"]),
      f"一次性返回全部错误行：{[(e['row'], e['code']) for e in body['errors']]}")
check(body["imported"] == 0, "**整批不入库**（连合法行也没写）")

# 全合法文件
wb2 = Workbook(); ws2 = wb2.active
ws2.append(["list_type", "entity_type", "entity_value", "reason", "expire_time", "created_by"])
ws2.append(["黑名单", "设备", "D-IMP-API-1", "导入测试", "", "importer"])
ws2.append(["黑名单", "IP", "10.6.6.6", "导入测试", "", "importer"])
buf2 = io.BytesIO(); wb2.save(buf2)
r = client.post("/api/lists/import",
                files={"file": ("t2.xlsx", buf2.getvalue(),
                                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                data={"operator": "importer"})
check(r.json()["success"] is True and r.json()["imported"] == 2,
      f"全合法文件整批入库（imported={r.json()['imported']}）")

r = client.get("/api/lists/archived", params={"entity_type": "设备"})
check(r.status_code == 200, f"历史名单检索可用（{r.json()['total']} 条归档）")
r = client.get("/api/lists/subject-history",
               params={"entity_type": "设备", "entity_value": "D-API-001"})
check(r.status_code == 200 and "current" in r.json() and "archived" in r.json(),
      "主体名单履历可用")

r = client.delete(f"/api/lists/{gray_id}", params={"operator": "tester"})
check(r.status_code == 200 and r.json()["removed"] is True, "移除名单项成功（返回快照）")
r = client.delete("/api/lists/999999")
check(r.status_code == 404 and r.json()["code"] == "BIZ_LIST_NOT_FOUND",
      f"移除不存在的名单项 → 404（{r.status_code}）")

# ============================================================
section("五、API-09~12 案件管理")

# 先跑几次检查产生案件
for uid, dev, ip in (("1002", "D-9002", "203.0.113.7"),
                     ("1004", "D-SHARED-0001", "172.16.0.10"),
                     ("1006", "D-7700", "10.99.99.99")):
    client.post("/api/risk/check", json={
        "event_type": "登录", "source_id": f"API-CASE-{uid}", "user_id": uid,
        "event_data": {"device_id": dev, "login_ip": ip}})

r = client.get("/api/cases", params={"page_size": 5})
check(r.status_code == 200 and r.json()["total"] > 0, f"案件列表 {r.json()['total']} 条")
case_id = r.json()["items"][0]["case_id"]
check("conflict_flag" in r.json()["items"][0], "列表含冲突标记字段")

r = client.get("/api/cases/statistics")
check(r.status_code == 200 and "by_status" in r.json(), f"案件统计可用：{r.json()['by_status']}")

r = client.get(f"/api/cases/{case_id}")
detail = r.json()
check(r.status_code == 200, f"案件详情（{case_id[:20]}…）")
check(detail["case"]["source_id"] and detail["case"]["event_type"],
      "详情含 source_id / event_type（支撑「重做检查」跳转）")
check(detail["assessment"] is not None, "详情含评估结果")
check("features" in detail and "profile" in detail, "详情含特征快照与用户画像")

r = client.get("/api/cases/cas_not_exist")
check(r.status_code == 404 and r.json()["code"] == "BIZ_CASE_NOT_FOUND",
      f"案件不存在 → 404 + BIZ_CASE_NOT_FOUND（{r.status_code}）")

r = reviewer_client.post(f"/api/cases/{case_id}/review", json={
    "decision": "已拒绝", "reviewer": "reviewer_api",
    "review_comment": "接口层测试", "add_to_blacklist": True,
    "blacklist_entity_types": ["用户"]})
body = r.json()
check(r.status_code == 200 and body["case_status"] == "已拒绝",
      f"审核通过（{body['case_status']}），回写={body['writeback'].get('action')}")
check(body["audit_log_id"] is not None, f"响应返回 audit_log_id（{body['audit_log_id']}）")

r = reviewer_client.post(f"/api/cases/{case_id}/review", json={"decision": "已通过", "reviewer": "x"})
check(r.status_code == 409 and r.json()["code"] == "BIZ_CASE_STATE_INVALID",
      f"重复处置被拦（{r.status_code}）")

# ============================================================
section("六、图谱 / 画像 / 仪表盘 / 仿真 / 审计")

r = client.get("/api/graph", params={"entity_type": "用户", "entity_value": "1003", "hops": 2})
g = r.json()
check(r.status_code == 200 and g["node_count"] > 1,
      f"图谱查询：{g['node_count']} 节点 / {g['edge_count']} 边 / truncated={g['truncated']}")
r = client.get("/api/graph", params={"entity_type": "用户", "entity_value": "1003", "hops": 99})
check(r.status_code == 422, f"跳数超上限被 Pydantic 拦下（{r.status_code}）")
r = client.get("/api/graph/related-accounts",
               params={"entity_type": "设备", "entity_value": "D-SHARED-0001"})
check(r.status_code == 200 and "account_count" in r.json(),
      f"聚集判定：{r.json()['account_count']} 个账号 / 达阈值={r.json()['reached_threshold']}")

r = client.get("/api/profile/1003")
check(r.status_code == 200 and r.json()["risk_score"] is not None,
      f"画像详情（评分 {r.json()['risk_score']}，含 trend {len(r.json()['trend'])} 条）")
r = client.get("/api/profile/NO_SUCH_USER")
check(r.status_code == 404 and r.json()["code"] == "BIZ_PROFILE_NOT_FOUND", "无画像用户 404")
r = client.get("/api/profile", params={"min_score": 0, "page_size": 3})
check(r.status_code == 200 and "items" in r.json(), f"画像列表 {r.json()['total']} 条")

r = client.get("/api/dashboard/overview")
ov = r.json()
check(r.status_code == 200 and "today" in ov,
      f"大盘概览：今日 {ov['today']['assessments']} 次评估，拦截率 {ov['today']['block_rate']}")
check("trend" in ov and "top_rules" in ov and "risk_distribution" in ov,
      f"含趋势/TOP规则/风险分布（TOP5 {len(ov['top_rules'])} 条）")
r = client.get("/api/dashboard/loss-prevention", params={"days": 7})
check(r.status_code == 200 and "blocked_amount" in r.json(),
      f"资损防护：拦截 {r.json()['blocked_cases']} 案 / ¥{r.json()['blocked_amount']}")
r = client.get("/api/dashboard/realtime", params={"limit": 5})
check(r.status_code == 200 and r.json()["total"] > 0,
      f"实时事件流 {r.json()['total']} 条")

r = client.post("/api/simulation/run", json={
    "event_type": "登录", "user_id": "1002", "source_id": "API-SIM",
    "device_id": "D-9002", "login_ip": "203.0.113.7"})
s = r.json()
check(r.status_code == 200 and len(s["steps"]) > 0,
      f"单条仿真：{len(s['steps'])} 条规则求值，决策={s['decision']} 评分={s['final_score']}")
check(s["side_effects"]["db_writes"] == 0, "仿真是只读的（side_effects.db_writes=0）")
r = client.post("/api/simulation/replay", json={"limit": 20})
check(r.status_code == 200 and r.json()["zero_side_effect"] is True,
      f"批量回放零副作用（{r.json()['sample_count']} 样本 / {len(r.json()['rule_stats'])} 条规则命中）")
r = client.get("/api/simulation/samples", params={"limit": 10})
check(r.status_code == 200 and r.json()["total"] > 0, f"样本库 {r.json()['total']} 条")

r = client.get("/api/audit-logs", params={"page_size": 5})
check(r.status_code == 200 and r.json()["total"] > 0, f"审计流水 {r.json()['total']} 条")
check("by_action_type" in r.json()["statistics"], f"审计统计：{r.json()['statistics']['by_action_type']}")
r = client.get("/api/audit-logs", params={"action_type": "规则"})
check(r.json()["total"] >= 3, f"按类别筛选审计（规则类 {r.json()['total']} 条）")
r = client.put("/api/audit-logs")
check(r.status_code == 405, f"审计不提供修改接口（PUT 返回 {r.status_code}）")

# ============================================================
section("七、API-46~52 配置与健康度")

# 全部走系统管理员的 client：阈值调整（settings:manage）、运维任务（health:view）
# 按 PRD.md:107-108 都只属于系统管理员，用策略师会拿到 403。
r = admin_client.get("/api/settings/thresholds")
before = r.json()["thresholds"]
check(r.status_code == 200 and "RISK_PASS_THRESHOLD" in before, f"读取阈值：{before['RISK_PASS_THRESHOLD']}")

r = admin_client.put("/api/settings/thresholds", json={"RISK_PASS_THRESHOLD": 25, "operator": "tester"})
body = r.json()
check(r.status_code == 200 and body["before"]["RISK_PASS_THRESHOLD"] == before["RISK_PASS_THRESHOLD"],
      f"调整阈值并记录变更前值（{body['before']['RISK_PASS_THRESHOLD']} → {body['after']['RISK_PASS_THRESHOLD']}）")
r = client.get("/api/audit-logs", params={"action_type": "配置"})
check(r.json()["total"] >= 1, f"配置类变更已写审计（{r.json()['total']} 条）")

r = admin_client.put("/api/settings/thresholds", json={"RISK_PASS_THRESHOLD": 90})
check(r.status_code == 400 and "阈值顺序非法" in r.json()["message"],
      f"阈值顺序非法被拦：{r.json()['message'][:32]}")
admin_client.put("/api/settings/thresholds", json={"RISK_PASS_THRESHOLD": before["RISK_PASS_THRESHOLD"]})

r = admin_client.get("/api/settings/cache-policy")
check(r.status_code == 200 and "REDIS_ENABLED" in r.json(), f"缓存策略读取（available={r.json()['available']}）")
r = admin_client.get("/api/settings/runtime-overrides")
check(r.status_code == 200 and "overrides" in r.json(), "运行时覆盖项可查")

r = admin_client.get("/api/health/metrics")
m = r.json()
check(r.status_code == 200 and "performance" in m, "健康度指标可用")
check(m["performance"]["requests"] > 0,
      f"性能采样：{m['performance']['requests']} 次请求，P95={m['performance']['p95_ms']}ms，"
      f"错误率={m['performance']['error_rate']}")
check("hit_rate" in m["cache"], f"缓存命中率 {m['cache']['hit_rate']}")
check(m["dependencies"]["mysql"] is True, "MySQL 依赖健康")
check("by_path" in m["performance"], f"耗时分解按路径统计（{len(m['performance']['by_path'])} 组）")

r = admin_client.get("/api/health/tasks")
check(r.status_code == 200 and len(r.json()["tasks"]) >= 3,
      f"运维任务列表：{[t['name'] for t in r.json()['tasks']]}")
r = admin_client.post("/api/health/tasks/健康检查/run")
check(r.status_code == 200 and "elapsed_ms" in r.json(),
      f"手动触发健康检查（{r.json()['elapsed_ms']}ms）")
r = admin_client.post("/api/health/tasks/不存在的任务/run")
check(r.status_code == 400 and r.json()["code"] == "BIZ_SETTINGS_ERROR",
      f"未知任务 → 400（{r.status_code}）")

# ============================================================
section("八、二次验证（API-27）")

r = client.post("/api/challenge/chl_not_exist/verify", json={"verify_token": "x"})
check(r.status_code == 404 and r.json()["code"] == "BIZ_CHALLENGE_NOT_FOUND",
      f"验证记录不存在 → 404（{r.status_code}）")

# 构造一条 challenge（经由决策引擎的二次验证档不便触发，直接造数据）
db = SessionLocal()
from app.engine.decision import generate_id  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
evt = generate_id("evt"); ast = generate_id("ast"); chl_id = generate_id("chl")
db.add(RiskEvent(event_id=evt, event_type="登录", event_source_id="CHL-1",
                 user_id="1003", event_data="{}"))
db.add(RiskAssessment(assessment_id=ast, event_id=evt, user_id="1003",
                      rule_results="[]", rule_count=0, final_score=70,
                      risk_level="高", decision="二次验证"))
db.add(RiskChallenge(challenge_id=chl_id, assessment_id=ast, user_id="1003",
                     verify_token="token-abc", status="待验证",
                     expire_time=datetime.now() + timedelta(minutes=5)))
db.commit()
db.close()

r = client.post(f"/api/challenge/{chl_id}/verify", json={"verify_token": "wrong"})
check(r.status_code == 403 and r.json()["code"] == "BIZ_CHALLENGE_TOKEN_INVALID",
      f"凭据错误 → 403（{r.status_code}）")
r = client.post(f"/api/challenge/{chl_id}/verify", json={"verify_token": "token-abc"})
check(r.status_code == 200 and r.json()["verified"] is True and r.json()["case_id"] is None,
      "验证通过 → 不创建案件（SPEC Q10）")
r = client.post(f"/api/challenge/{chl_id}/verify", json={"verify_token": "token-abc"})
check(r.status_code == 409, f"重复提交被拦（{r.status_code}）")
r = client.post("/api/challenge/expire-scan")
check(r.status_code == 200 and "escalated" in r.json(), f"超时扫描可用（升级 {r.json()['escalated']} 条）")

# ============================================================
section("九、统一异常处理汇总")

cases_ = [
    ("GET", "/api/cases/nope", None, 404, "BIZ_CASE_NOT_FOUND"),
    ("GET", "/api/rules/nope", None, 404, "BIZ_RULE_NOT_FOUND"),
    ("GET", "/api/profile/nope", None, 404, "BIZ_PROFILE_NOT_FOUND"),
    ("DELETE", "/api/lists/999999", None, 404, "BIZ_LIST_NOT_FOUND"),
    ("GET", "/api/graph?entity_type=乱写&entity_value=x", None, 400, "BIZ_GRAPH_ERROR"),
]
for method, path, payload, expect_status, expect_code in cases_:
    r = client.request(method, path, json=payload)
    body = r.json()
    ok = (r.status_code == expect_status and body.get("code") == expect_code
          and "message" in body and "detail" in body)
    check(ok, f"{method} {path} → {r.status_code} {body.get('code')}（含 message/detail）")

db = SessionLocal()
print()
print("  落库状态：")
for name, model in (("risk_event", RiskEvent), ("risk_assessment", RiskAssessment),
                    ("risk_case", RiskCase), ("risk_list", RiskList),
                    ("risk_audit_log", RiskAuditLog), ("risk_challenge", RiskChallenge)):
    print(f"    {name:<20}{db.query(model).count()}")
db.close()

# ============================================================
sys.exit(smoke.summary())
