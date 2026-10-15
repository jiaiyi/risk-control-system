# -*- coding: utf-8 -*-
"""T-18 Agent 层冒烟验证

前置：先跑 `python scripts/init_db.py` 重建业务数据。

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/smoke_agent.py

**重要**：本脚本**不依赖 LLM_API_KEY** —— 8 个工具都封装服务层，
可以直接 invoke 验证；LLM 未配置时的降级路径也单独验证。
真跑一轮对话需要你在 .env 里填 LLM_API_KEY。

验证：
    1. 路由注册（API-28/29 + 2 个增强端点）
    2. 8 个工具逐个直接调用（风控决策 4 + 数据分析 4）
    3. 工具内部**复用服务层**的证据（工具返回的内容与直接调服务层一致）
    4. 安全约束：query_business_data 拒绝任意 SQL、manage_blacklist 拒绝未知动作
    5. 会话持久化：save/load 往返、TTL、清除
    6. **流式对话端到端（SSE）**：用桩模型顶掉真模型，验证结束帧带回 session_id、
       一次请求只跑一遍 Agent、带上 session_id 后多轮上下文成立
    7. LLM 未配置时的降级（明确报错而不是栈深处的鉴权失败）

第 6 项是**唯一一条经过 HTTP 层跑 SSE 的用例** —— 之前只断言了流式路由「已注册」，
真实流式请求从未被测试覆盖，两个 bug（session_id 丢失、Agent 跑两遍）就是这么漏掉的。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ 供 import _smoke
import _smoke as smoke  # noqa: E402
from _smoke import check, section  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import cache  # noqa: E402
from app.agent import chat as agent_chat  # noqa: E402
from app.agent.tools import ALL_TOOLS, DATA_TOOLS, RISK_TOOLS  # noqa: E402
from app.config import settings  # noqa: E402
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
    RiskUserProfile,
)
from app.service.event import process_event  # noqa: E402






def call(tool, **kwargs) -> str:
    """调用一个 langchain tool，返回它的字符串输出"""
    return tool.invoke(kwargs)


def as_json(text: str):
    """工具输出是 JSON 字符串；解析失败时原样返回"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


db = SessionLocal()
for model in smoke.runtime_models():
    db.query(model).delete()
db.commit()
db.close()
cache.invalidate_list()

# ---- 造几条数据供工具查询 ----
for uid, etype, src, dev, ip in (
    ("1002", "登录", "AGT-1", "D-9002", "203.0.113.7"),
    ("1003", "登录", "AGT-2", "D-SHARED-0001", "172.16.0.10"),
    ("1004", "登录", "AGT-3", "D-SHARED-0001", "172.16.0.10"),
    ("1006", "登录", "AGT-4", "D-7700", "10.99.99.99"),
):
    process_event(SessionLocal(), user_id=uid, event_type=etype, source_id=src,
                  event_data={"device_id": dev, "login_ip": ip})

# ============================================================
section("一、路由注册（API-28/29 + 2 个增强）")

client = TestClient(app)
agent_paths = sorted({r.path for r in app.routes if r.path.startswith("/api/agent")})
print(f"  {agent_paths}")
for must in ("/api/agent/chat", "/api/agent/clear", "/api/agent/chat/stream",
             "/api/agent/sessions"):
    check(must in agent_paths, f"已注册 {must}")

check(len(RISK_TOOLS) == 4 and len(DATA_TOOLS) == 4,
      f"工具分组正确：风控决策 {len(RISK_TOOLS)} + 数据分析 {len(DATA_TOOLS)}")
check(len(ALL_TOOLS) == 8, f"共 8 个工具（SPEC T-18）")

# ============================================================
section("二、风控决策工具（4）")

# risk_check
out = call(ALL_TOOLS[0], user_id="1003", event_type="登录", source_id="AGT-TOOL-1",
           device_id="D-SHARED-0001", login_ip="172.16.0.10")
body = as_json(out)
check(isinstance(body, dict) and "final_score" in body,
      f"risk_check 返回结构化结果（决策={body.get('decision')} 评分={body.get('final_score')}）")
check(body.get("list_hit") is not None, "risk_check 带名单命中情况")

# query_cases
out = call(ALL_TOOLS[1], status="待审核", page=1)
body = as_json(out)
check(isinstance(body, dict) and "total" in body,
      f"query_cases 返回列表（共 {body.get('total')} 条）")

out = call(ALL_TOOLS[1], conflict_only=True)
check("total" in as_json(out), "query_cases 支持冲突筛选")

# query_user_profile
out = call(ALL_TOOLS[2], user_id="1003")
body = as_json(out)
check(isinstance(body, dict) and body.get("user_id") == "1003",
      f"query_user_profile 返回画像（评分 {body.get('risk_score')}，"
      f"trend {len(body.get('trend', []))} 条，名单履历 {body.get('list_history', {}).get('archived_count')} 条）")

out = call(ALL_TOOLS[2], user_id="NO_SUCH")
check("失败" in out or "not_found" in out, f"不存在的用户给出可读错误：{out[:50]}")

# manage_blacklist：查询 / 新增 / 移除
out = call(ALL_TOOLS[3], action="查询", list_type="黑名单")
check("total" in as_json(out), "manage_blacklist 查询")

# ⚠️ 写操作护栏：不带 confirm 一律拒绝（防止模型未经许可擅自动名单）
out = call(ALL_TOOLS[3], action="新增", entity_type="IP", value="10.5.5.5",
           list_type="黑名单", reason="agent 测试")
check("写操作" in out and "confirm" in out, "**不带 confirm 的新增被拒绝**（护栏）")

db = SessionLocal()
_leaked = db.query(RiskList).filter(RiskList.entity_value == "10.5.5.5").count()
db.close()
check(_leaked == 0, f"被拒后数据库零写入（实际 {_leaked} 条）")

body = as_json(call(ALL_TOOLS[3], action="新增", entity_type="IP", value="10.5.5.5",
                    list_type="黑名单", reason="agent 测试", confirm=True))
check(isinstance(body, dict) and body.get("list_id"),
      f"confirm=True 时新增成功（list_id={body.get('list_id')}）")

check("写操作" in call(ALL_TOOLS[3], action="移除", entity_type="IP", value="10.5.5.5"),
      "不带 confirm 的移除同样被拒")
out = call(ALL_TOOLS[3], action="移除", entity_type="IP", value="10.5.5.5", confirm=True)
check(as_json(out).get("action") == "移除", "confirm=True 时移除成功")

# 白名单硬约束在工具里同样生效（要带 confirm 才走得到那层校验）
out = call(ALL_TOOLS[3], action="新增", entity_type="用户", value="1002",
           list_type="白名单", reason="缺有效期", confirm=True)
check("失败" in out or "EXPIRE" in out,
      f"白名单无有效期被拦：{out[:60]}")

# 工具必须能接收有效期，否则「先问用户要有效期」这条路走不通（问到了也没处填）
body = as_json(call(ALL_TOOLS[3], action="新增", entity_type="用户", value="1002",
                    list_type="白名单", reason="agent 加白（带有效期）",
                    expire_time="2026-12-31", confirm=True))
check(isinstance(body, dict) and body.get("list_id")
      and str(body.get("expire_time", "")).startswith("2026-12-31"),
      f"**带有效期的白名单能加上了**（list_id={body.get('list_id') if isinstance(body, dict) else body}）")

# 移除必须只动指定颜色：同一主体可以黑白并存（互斥校验只拦「先黑后白」的加白动作），
# 早先的实现查询里漏了 list_type，会把白名单条目一起删掉
call(ALL_TOOLS[3], action="新增", entity_type="用户", value="1002",
     list_type="黑名单", reason="隔离测试", confirm=True)
body = as_json(call(ALL_TOOLS[3], action="移除", entity_type="用户", value="1002",
                    list_type="黑名单", confirm=True))
check(isinstance(body, dict) and len(body.get("removed", [])) == 1
      and body["removed"][0]["list_type"] == "黑名单",
      f"移除只返回黑名单条目（{len(body.get('removed', [])) if isinstance(body, dict) else body} 条）")

_db = SessionLocal()
_white_left = _db.query(RiskList).filter(
    RiskList.entity_value == "1002", RiskList.entity_type == "用户",
    RiskList.list_type == "白名单").count()
_black_left = _db.query(RiskList).filter(
    RiskList.entity_value == "1002", RiskList.entity_type == "用户",
    RiskList.list_type == "黑名单").count()
_db.close()
check(_white_left == 1 and _black_left == 0,
      f"**移除黑名单不误伤同主体的白名单**（白名单剩 {_white_left} 条、黑名单剩 {_black_left} 条）")

out = call(ALL_TOOLS[3], action="乱写", entity_type="用户", value="1002")
check("未知操作" in out, "未知 action 被拒绝（不静默失败）")

# ============================================================
section("三、数据分析工具（4）")

out = call(ALL_TOOLS[4])
body = as_json(out)
check(isinstance(body, dict) and "overview" in body and "loss_prevention" in body,
      f"query_dashboard_stats 返回大盘 + 资损（今日 {body['overview']['today']['assessments']} 次评估）")

out = call(ALL_TOOLS[5], days=3)
body = as_json(out)
check("trend" in body and "loss_prevention" in body,
      f"analyze_risk_trend 返回趋势（{body.get('days')} 天）")

out = call(ALL_TOOLS[5], days=999)
check(as_json(out).get("days") == 90, "days 被夹到上限 90")

out = call(ALL_TOOLS[6], days=7)
body = as_json(out)
check("rule_stats" in body and body["zero_side_effect"] is True,
      f"analyze_rule_effectiveness 复用仿真服务（{body.get('sample_count')} 样本，"
      f"{len(body.get('rule_stats', []))} 条规则命中，零副作用）")
check(all("hit_rate" in s for s in body.get("rule_stats", [])),
      "规则统计含命中率")

out = call(ALL_TOOLS[6], rule_id="L002")
body = as_json(out)
check(all(s["rule_id"] == "L002" for s in body.get("rule_stats", []))
      or not body.get("rule_stats"),
      f"按 rule_id 过滤生效（{len(body.get('rule_stats', []))} 条）")

# query_business_data：6 个模板全通 + 非法类型拒绝
templates = {
    "user_orders": {"user_id": "1003"},
    "order_detail": {"order_id": None},
    "user_logins": {"user_id": "1003"},
    "user_coupons": {"user_id": "1003"},
    "user_addresses": {"user_id": "1003"},
    "user_postsales": {"user_id": "1003"},
}
from sqlalchemy import text as sql_text  # noqa: E402
db = SessionLocal()
row = db.execute(sql_text("SELECT order_id FROM order_info WHERE user_id='1003' LIMIT 1")).first()
if row:
    templates["order_detail"] = {"order_id": row.order_id}
db.close()

for qtype, params in templates.items():
    out = call(ALL_TOOLS[7], query_type=qtype, limit=5, **params)
    body = as_json(out)
    check(isinstance(body, dict) and "rows" in body,
          f"模板 {qtype:<15} → {body.get('row_count', 0)} 行")

out = call(ALL_TOOLS[7], query_type="DROP TABLE order_info")
check("未知查询类型" in out, "**拒绝任意 SQL**（白名单外一律不执行）")
out = call(ALL_TOOLS[7], query_type="user_orders", user_id="1003' OR '1'='1")
check(isinstance(as_json(out), dict), "参数化查询，注入串被当普通值处理")

# ============================================================
section("四、工具复用服务层的证据（不是另写一套逻辑）")

db = SessionLocal()
from app.service import case_service, dashboard_service, profile_service  # noqa: E402

direct_total, _ = case_service.list_cases(db, case_status="待审核", page=1, page_size=10)
tool_total = as_json(call(ALL_TOOLS[1], status="待审核")).get("total")
check(tool_total == direct_total,
      f"query_cases 与直接调 case_service 结果一致（{tool_total}）")

direct_profile = profile_service.get_profile(db, "1003", trend_limit=5)
tool_profile = as_json(call(ALL_TOOLS[2], user_id="1003"))
check(tool_profile.get("risk_score") == direct_profile["risk_score"],
      f"query_user_profile 与 profile_service 一致（{tool_profile.get('risk_score')}）")

direct_ov = dashboard_service.overview(db, days=7)
tool_ov = as_json(call(ALL_TOOLS[4]))["overview"]
check(tool_ov["today"]["assessments"] == direct_ov["today"]["assessments"],
      f"query_dashboard_stats 与 dashboard_service 一致（{tool_ov['today']['assessments']}）")
db.close()

# ============================================================
section("五、会话持久化（Redis，替代参考实现的进程内 dict）")

sid = agent_chat.new_session_id()
check(sid.startswith("sess_"), f"会话 ID 生成：{sid[:24]}…")

key = cache.agent_session_key(sid)
cache.safe_delete(key)
check(agent_chat.load_history(sid) == [], "空会话返回空历史")

from langchain_core.messages import HumanMessage, messages_to_dict  # noqa: E402
agent_chat.save_history(sid, [HumanMessage(content="第一轮问题")])
loaded = agent_chat.load_history(sid)
check(len(loaded) == 1 and loaded[0].content == "第一轮问题",
      "save/load 往返一致（Redis 序列化正常）")
check(cache.safe_get(key) is not None, "会话已落 Redis")
ttl = cache.get_client().ttl(key)
check(0 < ttl <= settings.AGENT_SESSION_TTL,
      f"TTL 在 (0, {settings.AGENT_SESSION_TTL}] 之间（实际 {ttl}s）")

agent_chat.clear_session(sid)
check(cache.safe_get(key) is None, "clear_session 后历史已删（API-29）")

sessions = agent_chat.list_sessions()
check(isinstance(sessions, list), f"会话列表可查（当前 {len(sessions)} 个）")

# ============================================================
section("六、流式对话端到端（SSE，桩模型，不依赖 LLM key）")
#
# 用桩模型顶掉真模型：不联网、不花钱、结果确定。
# 这一段专盯两个「测试盲区」里的 bug：
#   ① 结束帧必须带回 session_id —— 回 None 的话前端下一轮只能以 None 再发，
#      后端又开新会话，表现为「流式对话完全没有多轮上下文」
#   ② 一次请求只能跑一遍 Agent —— 早期实现在流式结束后又 invoke 了一遍，
#      模型与工具调用全部翻倍（写操作会真的执行两次）
from langchain.agents import create_agent  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402


class _StubChat(BaseChatModel):
    """桩模型：每次生成返回固定文本，并累计调用次数"""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "stub"

    def bind_tools(self, tools, **kwargs):   # create_react_agent 构造时会调
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(
            message=AIMessage(content=f"第 {self.calls} 次模型调用的回复"))])


_stub = _StubChat()
_stub_agent = create_agent(model=_stub, tools=[], system_prompt="冒烟用桩 Agent")
_real_get_agent = agent_chat.get_agent
agent_chat.get_agent = lambda: _stub_agent      # 顶掉全局单例


def _sse(message: str, session_id) -> list[dict]:
    """打一次 SSE 接口，把响应体解析成事件列表"""
    r = client.post("/api/agent/chat/stream",
                    json={"message": message, "session_id": session_id})
    return [json.loads(line[6:]) for line in r.text.splitlines()
            if line.startswith("data: ")]


try:
    ev1 = _sse("第一轮问题", None)
    done1 = [e for e in ev1 if e.get("done")]
    check(bool(done1), "流式返回了结束帧")

    sid_stream = done1[0].get("session_id") if done1 else None
    check(bool(sid_stream) and str(sid_stream).startswith("sess_"),
          f"**结束帧带回 session_id**（{sid_stream}）—— 前端靠它维持多轮")
    check(any(e.get("chunk") for e in ev1), "流式返回了文本片段")

    calls_first = _stub.calls
    check(calls_first == 1,
          f"一次流式请求只跑一遍 Agent（模型调用 {calls_first} 次；翻倍会是 2）")

    n1 = len(agent_chat.load_history(sid_stream))
    check(n1 > 0, f"流式对话已落 Redis（{n1} 条消息）")

    _sse("第二轮问题", sid_stream)
    n2 = len(agent_chat.load_history(sid_stream))
    check(n2 > n1, f"带上 session_id 后历史累积（{n1} → {n2}），多轮上下文成立")
    check(_stub.calls == calls_first + 1,
          f"第二轮同样只跑一遍（累计 {_stub.calls} 次）")

    check(agent_chat.cache.safe_get(
              agent_chat.cache.agent_session_key(sid_stream)) is not None,
          "回传的 session_id 与 Redis 里的会话键一致（不是另开的一个）")

    agent_chat.clear_session(sid_stream)
finally:
    agent_chat.get_agent = _real_get_agent

# ============================================================
section("七、LLM 超时 → SYS_LLM_TIMEOUT（本地桩服务，不联网）")
#
# 起一个「只接受连接、永不回包」的本地 socket，把模型指过去、timeout 设 1 秒，
# 就能稳定复现真实超时：不依赖网络，也不用真等 60 秒。
# 覆盖 SPEC 6.3 声明的 SYS_LLM_TIMEOUT —— 之前这个码只写在文档里，
# 代码里超时被混进了笼统的 BIZ_AGENT_ERROR。
import socket  # noqa: E402
import threading  # noqa: E402

import openai  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402

_srv = socket.socket()
_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
_srv.bind(("127.0.0.1", 0))
_srv.listen(8)
_port = _srv.getsockname()[1]
_held: list = []


def _accept_and_stall() -> None:
    while True:
        try:
            conn, _ = _srv.accept()
        except OSError:
            return
        _held.append(conn)          # 收下连接但永不响应 → 客户端读超时


threading.Thread(target=_accept_and_stall, daemon=True).start()

_slow_agent = create_agent(
    model=ChatOpenAI(model="stub", api_key="sk-stub",
                     base_url=f"http://127.0.0.1:{_port}/v1",
                     timeout=1, max_retries=0),   # 不重试，否则要多等好几个 1 秒
    tools=[], system_prompt="超时用桩 Agent",
)
agent_chat.get_agent = lambda: _slow_agent


def _blank_session() -> str:
    sid = agent_chat.new_session_id()
    agent_chat.cache.safe_delete(agent_chat.cache.agent_session_key(sid))
    return sid


try:
    # ① 服务层：异常类型、错误码、HTTP 状态
    try:
        agent_chat.chat("你好", session_id=_blank_session())
        check(False, "超时应当抛异常，实际正常返回了")
    except agent_chat.LLMTimeoutError as exc:
        check(exc.code == "SYS_LLM_TIMEOUT" and exc.http_status == 504,
              f"抛 LLMTimeoutError：码 {exc.code} / HTTP {exc.http_status}")
    except Exception as exc:  # noqa: BLE001
        check(False, f"抛了预期外的异常：{type(exc).__name__}: {exc}")

    # ② 非流式接口要把 504 + 错误码透出去
    r = client.post("/api/agent/chat", json={"message": "你好"})
    check(r.status_code == 504 and r.json().get("code") == "SYS_LLM_TIMEOUT",
          f"POST /api/agent/chat → {r.status_code} {r.json().get('code')}")

    # ③ 流式接口：错误帧同样带码，且带回 session_id（超时后能接着这个会话重试）
    err = [e for e in _sse("你好", None) if e.get("error")]
    check(bool(err) and err[0].get("code") == "SYS_LLM_TIMEOUT",
          f"SSE 错误帧带 code={err[0].get('code') if err else '（无错误帧）'}")
    check(bool(err) and err[0].get("session_id"),
          "SSE 错误帧同时带回 session_id")

    # ④ 判定逻辑本身：普通异常不能误判，被包一层的超时也要认出来
    check(not agent_chat._is_timeout_error(ValueError("普通错误")),
          "普通异常不会被误判为超时")
    _wrapped = RuntimeError("外层包装")
    _wrapped.__cause__ = openai.APITimeoutError(None)
    check(agent_chat._is_timeout_error(_wrapped),
          "被外层包住的超时仍能识别（走 __cause__ 链）")
finally:
    agent_chat.get_agent = _real_get_agent
    _srv.close()
    for _c in _held:
        try:
            _c.close()
        except OSError:
            pass

# ============================================================
section("八、LLM 未配置时的降级")

if not settings.LLM_API_KEY:
    try:
        agent_chat.chat("你好", session_id=agent_chat.new_session_id())
        check(False, "LLM 未配置时应抛错而不是硬跑")
    except agent_chat.LLMNotConfiguredError as exc:
        check(exc.http_status == 503 and exc.code == "SYS_LLM_NOT_CONFIGURED",
              f"明确报 503 + SYS_LLM_NOT_CONFIGURED（{exc.message[:40]}…）")

    r = client.post("/api/agent/chat", json={"message": "你好"})
    check(r.status_code == 503 and r.json()["code"] == "SYS_LLM_NOT_CONFIGURED",
          f"接口层同样返回 503（{r.status_code}）")
else:
    check(True, f"LLM_API_KEY 已配置（model={settings.LLM_MODEL_NAME}），跳过降级用例")

r = client.post("/api/agent/chat", json={"message": ""})
check(r.status_code == 422, f"空消息被 Pydantic 拦下（{r.status_code}）")

r = client.post("/api/agent/clear", json={"session_id": "sess_nonexist"})
check(r.status_code == 200 and r.json()["cleared"] is True,
      "清除不存在的会话也返回成功（幂等）")

r = client.get("/api/agent/sessions")
check(r.status_code == 200 and "total" in r.json(), "会话列表接口可用")

# ============================================================
sys.exit(smoke.summary())

if not settings.LLM_API_KEY:
    print()
    print("⚠️  LLM_API_KEY 未配置 —— 以上只验证了「工具层 + 编排层 + 降级路径」。")
    print("    在 .env 填入 LLM_API_KEY 后，可再跑一次真实的对话冒烟。")
