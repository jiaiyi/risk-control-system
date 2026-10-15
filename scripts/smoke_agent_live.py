# -*- coding: utf-8 -*-
"""T-18 Agent 真实对话冒烟（**需要 LLM_API_KEY**）

前置：
  1. 在 `.env` 中填好 `LLM_API_KEY`（DashScope / 通义千问的 key）
  2. 先跑 `python scripts/init_db.py` 重建业务数据

用法（项目根目录）：
    ./.venv/Scripts/python.exe scripts/smoke_agent_live.py

与 smoke_agent.py 的区别：那个验证「工具层 + 编排层 + 降级路径」（不需要 key），
本脚本验证**真实多轮对话**：模型能否选对工具、能否记住上下文、流式是否可用。

说明：LLM 的行为有不确定性，所以断言只卡「必须成立」的部分
（能连通、能回复、会话有记忆、工具被调用过），不断言它一定调某个具体工具 ——
具体调了什么会打印出来供人工判断。
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ 供 import _smoke
import _smoke as smoke  # noqa: E402
from _smoke import check, section  # noqa: E402

from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

from app.agent import chat as agent_chat  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    RiskAssessment, RiskAuditLog, RiskCase, RiskChallenge, RiskEvent,
    RiskFeature, RiskList, RiskListArchive, RiskUserProfile,
)
from app.service.event import process_event  # noqa: E402






def used_tools(messages) -> list[str]:
    """从消息链里提取被调用过的工具名"""
    return [m.name for m in messages if isinstance(m, ToolMessage) and getattr(m, "name", None)]


# ---- 准备数据 ----
db = SessionLocal()
for model in smoke.runtime_models():
    db.query(model).delete()
db.commit()
db.close()

for uid, dev, ip in (("1002", "D-9002", "203.0.113.7"),
                     ("1003", "D-SHARED-0001", "172.16.0.10"),
                     ("1004", "D-SHARED-0001", "172.16.0.10")):
    process_event(SessionLocal(), user_id=uid, event_type="登录",
                  source_id=f"LIVE-{uid}",
                  event_data={"device_id": dev, "login_ip": ip})
print("  已构造 3 条风控事件（含画像、案件）")

if not settings.LLM_API_KEY:
    print()
    print("=" * 74)
    print("  LLM_API_KEY 未配置 —— 无法运行真实对话测试。")
    print("  请在 .env 中设置 LLM_API_KEY 后重试，")
    print("  或先用 scripts/smoke_agent.py 验证工具层（不需要 key）。")
    print("=" * 74)
    sys.exit(2)

section("一、连通性与单轮问答")

CASES = [
    ("画像查询", "帮我看看用户 1003 的风险画像"),
    ("实时检查", "对用户 1002 发起一次登录风控检查"),
    ("数据分析", "最近风控大盘情况怎么样"),
]

session_id = None
for title, question in CASES:
    started = time.time()
    try:
        reply, session_id = agent_chat.chat(question, session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        check(False, f"{title}：调用失败 —— {type(exc).__name__}: {exc}")
        continue
    elapsed = time.time() - started

    history = agent_chat.load_history(session_id)
    tools = used_tools(history)
    check(bool(reply and reply.strip()), f"{title}：拿到回复（{len(reply)} 字，{elapsed:.1f}s）")
    check(bool(tools), f"{title}：调用了工具 {tools}")
    print(f"      → 回复：{reply[:110].replace(chr(10), ' ')}…")

section("二、多轮上下文记忆（FR-29 / NFR-41）")

before_count = len(agent_chat.load_history(session_id))
reply, session_id = agent_chat.chat("那他最近有风控案件吗？", session_id=session_id)
after_count = len(agent_chat.load_history(session_id))

check(before_count > 0, f"第一轮后会话已有 {before_count} 条消息")
check(after_count > before_count,
      f"第二轮消息已累积（{before_count} → {after_count}，说明带了上文）")
check(bool(reply and reply.strip()), f"多轮问答拿到回复：{reply[:110].replace(chr(10), ' ')}…")

# 「他」指代上一轮的 1003 —— 若模型能理解指代，说明上下文真的传进去了
history = agent_chat.load_history(session_id)
check(len(history) >= 4, f"消息链长度 {len(history)}（含 Human / AI / Tool 消息）")

section("三、会话持久化（Redis）")

key = cache_key = agent_chat.cache.agent_session_key(session_id)
check(agent_chat.cache.safe_get(cache_key) is not None, "会话已落 Redis")
ttl = agent_chat.cache.get_client().ttl(cache_key)
check(0 < ttl <= settings.AGENT_SESSION_TTL,
      f"TTL 在 (0, {settings.AGENT_SESSION_TTL}]（实际 {ttl}s）")

section("四、SSE 流式（T-24 前端要用）")

chunks = []
try:
    for piece in agent_chat.chat_stream("用一句话说明风控系统是做什么的",
                                        session_id=None):
        chunks.append(piece)
    check(len(chunks) >= 1, f"流式返回 {len(chunks)} 个片段")
    joined = "".join(chunks)
    check(len(joined) > 10, f"拼起来是完整回复（{len(joined)} 字）")
    print(f"      → 流式回复：{joined[:110].replace(chr(10), ' ')}…")
except Exception as exc:  # noqa: BLE001
    check(False, f"流式调用失败 —— {type(exc).__name__}: {exc}")

section("五、清会话（API-29 / FR-32）")

agent_chat.clear_session(session_id)
check(agent_chat.load_history(session_id) == [], "清除后历史为空")
check(agent_chat.cache.safe_get(cache_key) is None, "Redis 中的会话已删")

sys.exit(smoke.summary())
