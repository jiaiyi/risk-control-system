# -*- coding: utf-8 -*-
"""鉴权端到端验证：未登录跳转 / 登录 / 角色拦截 / 登出。

**为什么用 TestClient 而不是真起 HTTP 打 127.0.0.1:8000**：
早先这脚本走 urllib 打真实端口，结果是它进不了 `smoke_all.py` —— 其它冒烟
脚本都用进程内 TestClient，唯独它需要先手动把服务跑起来，于是只能单独执行，
长期没人跑、也就没人发现 `/api/auth/login` 压根没进公开白名单（见下）。
TestClient 同样会过中间件、同样会走 CookieJar 跟随 302 语义，
所以断言强度不变，却变成自包含的。
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PW = "risk@2026"

failures = []


def check(cond, msg):
    print(("  [OK]  " if cond else "  [!!]  ") + msg)
    if not cond:
        failures.append(msg)


def opener(cookies=True):
    """返回一个客户端。默认带 CookieJar；不跟随重定向以便断言跳转本身。"""
    return TestClient(app, follow_redirects=False)


def get(op, path, headers=None):
    return send(op, "GET", path, None, headers)


def post(op, path, data, headers=None):
    return send(op, "POST", path, data, headers)


def _unpack(resp):
    return resp.status_code, resp.text, {k.lower(): v for k, v in resp.headers.items()}


def send(op, method, path, data=None, headers=None):
    """统一发请求。data 为 dict 时按表单编码；为 None 时发空 body。

    JSON 请求体请用 send_json()，它会自己设 Content-Type。
    """
    if data is not None:
        return _unpack(op.request(method, path, data=data, headers=headers))
    return _unpack(op.request(method, path, headers=headers))


def send_json(op, method, path, payload):
    """JSON 请求。前端 JS 走的接口登录/登出走这条路径。"""
    return _unpack(op.request(method, path, json=payload))


print("=" * 62)
print("1) 未登录：页面跳登录、接口给 401 JSON")
print("=" * 62)
op = opener()
st, body, hdrs = get(op, "/")
check(st == 302, f"GET / 未登录 → 302（实际 {st}）")
check("/login" in hdrs.get("location", ""), f"跳转目标含 /login（实际 {hdrs.get('location')}）")
check("next=%2F" in hdrs.get("location", "") or "next=/" in hdrs.get("location", ""),
      "带 next 参数以便登录后跳回")

st, body, _ = get(op, "/api/rules?page=1&page_size=5")
check(st == 401, f"GET /api/rules 未登录 → 401（实际 {st}）")
try:
    payload = json.loads(body)
except Exception:
    payload = {}
check(payload.get("code") == "AUTH_REQUIRED", f"错误码 AUTH_REQUIRED（实际 {payload.get('code')}）")

st, body, _ = get(op, "/login")
check(st == 200, f"GET /login → 200（实际 {st}）")
check("risk@2026" in body, "登录页展示演示口令提示")
check("zhangcx" in body and "shenjr" in body, "登录页列出演示账号")
check("login.css" in body, "登录页引入 login.css")

st, body, _ = get(op, "/health")
check(st == 200, f"GET /health 免登录 → 200（实际 {st}）")

st, body, _ = get(op, "/static/js/app.js")
check(st in (200, 304), f"静态资源免登录 → 200/304（实际 {st}）")

print()
print("=" * 62)
print("2) 登录：口令错误 / 账号错误 / 正确")
print("=" * 62)
op = opener()
st, body, _ = post(op, "/login", {"user_id": "zhangcx", "password": "wrong", "next": "/"})
check(st == 200, f"错误口令 → 200 重渲染（实际 {st}）")
check("账号或密码不正确" in body, "错误口令给出提示")

st, body, _ = post(op, "/login", {"user_id": "nobody", "password": PW, "next": "/"})
check("账号或密码不正确" in body, "不存在的账号给同样提示（不泄露账号是否存在）")

st, body, hdrs = post(op, "/login", {"user_id": "zhangcx", "password": PW, "next": "/"})
check(st == 302, f"正确口令 → 302（实际 {st}）")
sc = hdrs.get("set-cookie", "")
check("rc_session=" in sc, "下发 rc_session Cookie")
check("HttpOnly" in sc, "Cookie 带 HttpOnly")
check("lax" in sc.lower(), "Cookie 带 SameSite=Lax")

st, body, _ = get(op, "/api/auth/me")
me = json.loads(body).get("user", {})
check(me.get("user_id") == "zhangcx", f"当前用户 zhangcx（实际 {me.get('user_id')}）")
check(me.get("role") == "风控策略师", f"角色 风控策略师（实际 {me.get('role')}）")

print()
print("-" * 62)
print("2b) JSON 接口登录 / 登出（前端 JS 走的路径）")
print("-" * 62)
# 这条路径曾经整体漏掉：/api/auth/login 没进 main.py 的 _PUBLIC_PATHS，
# 中间件先拦下判 401「未登录或登录已过期」，而表单登录 POST /login 正常，
# 所以只测表单永远发现不了。断言要点是「错误文案不能是 AUTH_REQUIRED」。
op_js = opener()
st, body, hdrs = send_json(
    op_js, "POST", "/api/auth/login", {"user_id": "zhangcx", "password": "wrong"})
check(st == 401, f"JSON 登录错误口令 → 401（实际 {st}）")
check("AUTH_REQUIRED" in body and "未登录或登录已过期" not in body,
      "错误口令走的是登录校验而不是中间件拦截（否则说明 /api/auth/login 不在公开白名单）")

st, body, hdrs = send_json(
    op_js, "POST", "/api/auth/login", {"user_id": "zhangcx", "password": PW})
check(st == 200, f"JSON 登录成功 → 200（实际 {st}）")
check("rc_session=" in hdrs.get("set-cookie", ""), "JSON 登录下发 rc_session Cookie")
try:
    js_user = json.loads(body).get("user", {})
except Exception:
    js_user = {}
check(js_user.get("user_id") == "zhangcx", f"JSON 登录返回用户（实际 {js_user.get('user_id')}）")
check(bool(js_user.get("permissions")), "JSON 登录返回权限码，供前端按权限渲染")

st, body, _ = get(op_js, "/api/auth/me")
check(st == 200 and json.loads(body).get("user", {}).get("user_id") == "zhangcx",
      "JSON 登录后会话可用")

st, body, hdrs = send_json(op_js, "POST", "/api/auth/logout", {})
check(st == 200, f"JSON 登出 → 200（实际 {st}）")
st, body, _ = get(op_js, "/api/auth/me")
check(st == 401, f"JSON 登出后会话失效 → 401（实际 {st}）")

print()
print("=" * 62)
print("3) 角色拦截矩阵（5 账号 × 受保护资源）")
print("=" * 62)

# 权限矩阵严格照抄 specs/PRD.md:93-108。每行：
#   (账号, 角色, 应可访问, 应被 403)
# 「只读」在 PRD 里是明确的格子，所以审计人员能 GET 但 PUT 必须 403。
MATRIX = [
    ("zhangcx", "风控策略师",
     ["/api/rules?page=1&page_size=1", "/api/cases?page=1&page_size=1",
      "/api/lists?page=1&page_size=1", "/api/graph?entity_type=用户&entity_value=1001",
      "/api/audit-logs?page=1&page_size=1", "/api/dashboard/overview",
      "/api/profile/1001"],
     ["/api/health/metrics", "/api/settings/thresholds"]),
    ("shenhey", "风控审核员",
     # ⚠ 审核员/分析师**没有** rules:view —— PRD.md:96 规则管理一栏他们就是「—」。
     #   案件详情里的命中规则来自 risk_assessment.rule_results 这份 JSON 快照
     #   （见 app/service/case_service.py），不经过 /api/rules，所以不需要读规则库。
     ["/api/cases?page=1&page_size=1",
      "/api/dashboard/overview", "/api/profile/1001",
      "/api/graph?entity_type=用户&entity_value=1001"],
     ["/api/rules?page=1&page_size=1",
      "/api/settings/thresholds", "/api/audit-logs?page=1&page_size=1",
      "/api/health/metrics"]),
    ("fengxs", "数据分析师",
     ["/api/cases?page=1&page_size=1", "/api/dashboard/overview",
      "/api/profile/1001",
      "/api/graph?entity_type=用户&entity_value=1001"],
     ["/api/rules?page=1&page_size=1",
      "/api/settings/thresholds", "/api/health/metrics",
      "/api/audit-logs?page=1&page_size=1"]),
    ("yunwq", "系统管理员",
     ["/api/settings/thresholds", "/api/health/metrics", "/api/dashboard/overview",
      "/api/audit-logs?page=1&page_size=1"],
     ["/api/rules?page=1&page_size=1", "/api/cases?page=1&page_size=1"]),
    ("shenjr", "审计人员",
     ["/api/rules?page=1&page_size=1", "/api/audit-logs?page=1&page_size=1",
      "/api/cases?page=1&page_size=1", "/api/dashboard/overview",
      "/api/settings/thresholds",                # PRD.md:107 系统配置「只读」
      "/api/lists?page=1&page_size=1",           # PRD.md:100 名单「只读」
      "/api/profile/1001"],
     ["/api/health/metrics", "/api/simulation/runs"]),
]

for uid, role, allowed, denied in MATRIX:
    op = opener()
    post(op, "/login", {"user_id": uid, "password": PW, "next": "/"})
    bad = []
    for path in allowed:
        st, body, _ = get(op, path)
        # ⚠ 判据是「鉴权放行了」，不是「2xx」：本脚本在 smoke_all 里排在其它
        #   脚本之后，那些脚本会清空 risk_* 表，于是 /api/profile/1001 这类
        #   依赖既有数据的接口会合法地返回 404（用户尚无风险画像）。
        #   要求 2xx 会让断言随执行顺序飘 —— 只断言 401/403 才算被拦。
        if st in (401, 403):
            bad.append(f"{path}→{st}")
    check(not bad, f"{role}({uid}) 应有权限的 {len(allowed)} 项全部放行（失败：{bad or '无'}）")
    bad = []
    for path in denied:
        st, body, _ = get(op, path)
        if st != 403:
            bad.append(f"{path}→{st}")
        else:
            try:
                if json.loads(body).get("code") != "AUTH_FORBIDDEN":
                    bad.append(f"{path}→码不对")
            except Exception:
                bad.append(f"{path}→非 JSON")
    check(not bad, f"{role}({uid}) 应被拒的 {len(denied)} 项全部 403（失败：{bad or '无'}）")

print()
print("=" * 62)
print("3b) 「只读」格子：能读但不能写")
print("=" * 62)
# PRD.md:96/100/107 里审计人员对规则/名单/系统配置是「只读」，
# 所以这些角色 GET 必须 200、写操作必须 403。只测 GET 会漏掉「只读变可写」。
READONLY = [
    ("shenjr", "审计人员", [
        ("PUT", "/api/settings/thresholds", {"mark_threshold": 61}),
        ("POST", "/api/rules", {"rule_id": "X1", "rule_name": "x",
                                "rule_category": "登录安全", "event_type": "登录",
                                "risk_level": "低", "risk_score": 10,
                                "action": "通过",
                                "rule_condition": {"field": "user_orders_30d",
                                                   "op": ">", "value": 1}}),
        ("DELETE", "/api/rules/R001", None),
        ("POST", "/api/lists", {"list_type": "黑名单", "entity_type": "用户",
                                "entity_value": "99999", "reason": "t"}),
        ("POST", "/api/lists/import", None),
        ("POST", "/api/health/tasks/清理缓存/run", None),
    ]),
    ("fengxs", "数据分析师", [
        ("DELETE", "/api/lists/1", None),
        ("POST", "/api/lists", {"list_type": "黑名单", "entity_type": "用户",
                                "entity_value": "99999", "reason": "t"}),
    ]),
    ("shenhey", "风控审核员", [
        ("POST", "/api/lists/import", None),       # PRD.md:101 只有策略师能导入
        ("POST", "/api/health/tasks/清理缓存/run", None),
    ]),
]

for uid, role, cases in READONLY:
    op = opener()
    post(op, "/login", {"user_id": uid, "password": PW, "next": "/"})
    bad = []
    for method, path, payload in cases:
        st, body, _ = send(op, method, path, payload)
        if st != 403:
            bad.append(f"{method} {path}→{st}")
    check(not bad, f"{role}({uid}) 的 {len(cases)} 项写操作全部 403（失败：{bad or '无'}）")

print()
print("=" * 62)
print("4) 页面级：侧边栏按权限渲染 + 403 页")
print("=" * 62)
# 审计人员：能看到规则/审计/案件，看不到系统配置与 AI 助手
op = opener()
post(op, "/login", {"user_id": "shenjr", "password": PW, "next": "/"})
st, body, _ = get(op, "/")
check(st == 200, f"审计人员访问 / → 200（实际 {st}）")
check("慎计仁" in body and "审计人员" in body, "侧边栏显示真实姓名与角色")
check(">审计日志<" in body or "审计日志" in body, "菜单含审计日志")
check("系统配置" not in body, "菜单不含系统配置（无 settings:manage）")
check("AI 助手" not in body, "菜单不含 AI 助手（无 agent:use）")
check("规则管理" in body, "菜单含规则管理（审计人员只读可看）")

st, body, _ = get(op, "/settings")
check(st == 403, f"审计人员访问 /settings → 403（实际 {st}）")
check("无权访问" in body, "403 页给出无权访问提示")
check("审计人员" in body, "403 页显示当前角色")

# 策略师：不应有系统配置
op = opener()
post(op, "/login", {"user_id": "zhangcx", "password": PW, "next": "/"})
st, body, _ = get(op, "/")
check("系统配置" not in body, "策略师菜单不含系统配置")
check("AI 助手" in body, "策略师菜单含 AI 助手")
check("张晨曦" in body and "风控策略师" in body, "策略师用户栏正确")

# 系统管理员：只有仪表盘+审计+配置
op = opener()
post(op, "/login", {"user_id": "yunwq", "password": PW, "next": "/"})
st, body, _ = get(op, "/")
check("系统配置" in body, "管理员菜单含系统配置")
check("规则管理" not in body, "管理员菜单不含规则管理（按矩阵无 rules:view）")

print()
print("=" * 62)
print("5) 登出")
print("=" * 62)
st, body, hdrs = post(op, "/logout", {})
check(st == 302, f"POST /logout → 302（实际 {st}）")
check("/login" in hdrs.get("location", ""), "登出后跳登录页")
st, body, _ = get(op, "/api/auth/me")
check(st == 401, f"登出后 /api/auth/me → 401（实际 {st}）")

print()
print("=" * 62)
print("6) 开放重定向防护")
print("=" * 62)
op = opener()
st, body, hdrs = post(op, "/login",
                      {"user_id": "zhangcx", "password": PW,
                       "next": "https://evil.example/x"})
check(st == 302, "站外 next 也返回 302")
check("evil.example" not in hdrs.get("location", ""),
      f"站外 next 被丢弃（实际 {hdrs.get('location')}）")

op = opener()
st, body, hdrs = post(op, "/login",
                      {"user_id": "zhangcx", "password": PW, "next": "//evil.example"})
check("evil.example" not in hdrs.get("location", ""), "// 开头也被丢弃")

print()
print("=" * 62)
print(f"结果：{'全部通过' if not failures else str(len(failures)) + ' 项失败'}")
for f in failures:
    print("  - " + f)
print("=" * 62)
sys.exit(1 if failures else 0)
