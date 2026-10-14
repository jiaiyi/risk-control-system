# -*- coding: utf-8 -*-
"""AI 助手 —— ReAct 编排 + Redis 会话持久化

技术选型（与 SPEC 2 章一致）：
    用 `langgraph.prebuilt.create_react_agent` 搭 ReAct，
    **不引入 deepagents**（参考实现用了它，但不在本项目依赖清单里，多一个依赖就多一处维护面）。

会话持久化（FR-41 / NFR-41）：
    参考实现用进程内 `dict` 存会话 —— 重启即丢，且多 worker 下互不可见。
    本项目改用 **Redis**（键 `risk:agent:sess:{id}`，TTL `AGENT_SESSION_TTL`=24h）。

LLM 不可用时的降级（`LLM_API_KEY` 未配置）：
    明确抛 `LLMNotConfiguredError`，而不是让调用方在调用栈深处收到一个莫名的鉴权失败。
"""
import json
import logging
import uuid

import ulid
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    messages_from_dict,
    messages_to_dict,
)
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from app import cache
from app.agent.tools import ALL_TOOLS
from app.config import settings
from app.errors import ServiceError

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是电商风控系统的 AI 助手，服务对象是风控审核员与策略分析师。

你可以做的事：
1. **风险检查**：对指定用户 + 事件类型执行实时风控评估，解读评分与命中规则
2. **案件管理**：查询待审核 / 已处理的案件，尤其是「黑白名单同时命中」的冲突案件
3. **用户画像**：查看用户综合风险分、订单/退款/投诉统计、评分趋势与名单履历
4. **名单管理**：查询 / 新增 / 移除三色名单（黑/白/灰）× 五类主体（用户/手机号/IP/设备/地址）
5. **数据分析**：风控大盘、近 N 天趋势、拦截金额、规则命中效果
6. **业务查询**：订单、售后、收货地址、登录记录、领券记录

工作原则：
- 先用工具取到真实数据，再下结论；**不要凭印象编数字**
- 用中文回答，结论先行，论据跟上；数据要给出关键值（评分、命中规则、条数）
- 一次说不清就组合多个工具，但别做无意义的重复查询
- 白名单有硬约束（必须设有效期、不超 90 天、不能与黑名单共存），
  用户要求加白但信息不足时，先问清楚有效期和凭据
- 涉及名单增删这类写操作，执行后明确告知「已做了什么、影响了什么」
"""


class LLMNotConfiguredError(ServiceError):
    """LLM 未配置（通常是 LLM_API_KEY 为空）"""

    code = "SYS_LLM_NOT_CONFIGURED"
    http_status = 503


class AgentServiceError(ServiceError):
    code = "BIZ_AGENT_ERROR"


# 全局 Agent 实例（惰性初始化）
_agent = None


def _create_agent():
    """创建 ReAct Agent（绑定 8 个工具）"""
    if not settings.LLM_API_KEY:
        raise LLMNotConfiguredError(
            "LLM 未配置：请在 .env 中设置 LLM_API_KEY 后重试",
            {"model": settings.LLM_MODEL_NAME, "base_url": settings.LLM_BASE_URL},
        )
    llm = ChatOpenAI(
        model=settings.LLM_MODEL_NAME,
        api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL,
        temperature=0.1,
        timeout=settings.LLM_TIMEOUT,
    )
    return create_react_agent(
        model=llm,
        tools=ALL_TOOLS,
        prompt=SYSTEM_PROMPT,
    )


def get_agent():
    """获取（或惰性创建）全局 Agent 实例"""
    global _agent
    if _agent is None:
        _agent = _create_agent()
    return _agent


def reset_agent() -> None:
    """丢弃当前 Agent 实例（配置变更后调用）"""
    global _agent
    _agent = None


# ============================================================
# 会话持久化（Redis）
# ============================================================
def _new_session_id() -> str:
    return f"sess_{ulid.new().str.lower()}"


def load_history(session_id: str) -> list[BaseMessage]:
    """从 Redis 读会话历史；不可用或不存在则返回空列表（降级为无记忆单轮）"""
    raw = cache.safe_get(cache.agent_session_key(session_id))
    if not isinstance(raw, dict):
        return []
    try:
        return messages_from_dict(raw.get("messages", []))
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话历史反序列化失败，按新会话处理: %s", exc)
        return []


def save_history(session_id: str, messages: list[BaseMessage]) -> None:
    """把会话历史写回 Redis（带 TTL）"""
    try:
        payload = {
            "session_id": session_id,
            "message_count": len(messages),
            "updated_at": json.dumps(None),   # 占位，下面覆盖为真实时间
        }
        from datetime import datetime
        payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
        payload["messages"] = messages_to_dict(messages)
        cache.safe_set(
            cache.agent_session_key(session_id), payload, settings.AGENT_SESSION_TTL,
        )
    except Exception as exc:  # noqa: BLE001
        # 会话持久化失败不阻断对话（只是这轮之后会失忆）
        logger.warning("会话历史保存失败: %s", exc)


def clear_session(session_id: str) -> bool:
    """清除会话历史（API-29 / FR-32）"""
    cache.safe_delete(cache.agent_session_key(session_id))
    return True


def list_sessions() -> list[dict]:
    """当前存活的会话列表（供 AI 助手页展示）"""
    client = cache.get_client()
    if client is None:
        return []
    out = []
    try:
        cursor = 0
        pattern = f"{cache.K_AGENT_SESSION}:*"
        while True:
            cursor, found = client.scan(cursor=cursor, match=pattern, count=100)
            for key in found:
                raw = cache.safe_get(key if isinstance(key, str) else key.decode())
                if isinstance(raw, dict):
                    out.append({
                        "session_id": raw.get("session_id"),
                        "message_count": raw.get("message_count"),
                        "updated_at": raw.get("updated_at"),
                    })
            if not cursor:
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话列表查询失败: %s", exc)
        return out
    return out


# ============================================================
# 对话
# ============================================================
def _flatten_content(content) -> str:
    """把消息内容统一成字符串（多模态分块时 content 可能是 list）"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for chunk in content:
            if isinstance(chunk, dict):
                parts.append(str(chunk.get("text", "")))
            else:
                parts.append(str(chunk))
        return "".join(parts)
    return str(content)


def chat(message: str, session_id: str | None = None) -> tuple[str, str]:
    """与 Agent 对话，返回 (回复文本, session_id)

    会话历史来自 Redis；本轮结束后把**完整消息链**写回，
    这样下一轮能看到之前调用了哪些工具、得到了什么结果。
    """
    if not message or not str(message).strip():
        raise AgentServiceError("消息内容不能为空")

    if not session_id:
        session_id = _new_session_id()

    history = load_history(session_id)
    history = [*history, HumanMessage(content=message)]

    agent = get_agent()
    try:
        result = agent.invoke({"messages": history})
        messages = result.get("messages", [])
        if not messages:
            raise AgentServiceError("Agent 未返回任何消息")
        reply = _flatten_content(messages[-1].content)
        save_history(session_id, messages)
        return reply, session_id
    except LLMNotConfiguredError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent 执行异常")
        raise AgentServiceError(f"Agent 执行失败：{exc}") from exc


def chat_stream(message: str, session_id: str | None = None):
    """流式对话（供前端逐段展示，T-24）

    逐个 yield 文本片段；结束后 session_id 已落库。
    """
    if not session_id:
        session_id = _new_session_id()

    history = load_history(session_id)
    history = [*history, HumanMessage(content=message)]

    agent = get_agent()
    chunks: list[str] = []
    try:
        for event in agent.stream({"messages": history}, stream_mode="messages"):
            chunk, _meta = event
            if isinstance(chunk, AIMessage) and chunk.content:
                piece = _flatten_content(chunk.content)
                if piece:
                    chunks.append(piece)
                    yield piece
        # 流式结束后把完整历史存回去（重新取一次最终消息链）
        result = agent.invoke({"messages": history})
        messages = result.get("messages", [])
        if messages:
            save_history(session_id, messages)
    except LLMNotConfiguredError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent 流式执行异常")
        raise AgentServiceError(f"Agent 执行失败：{exc}") from exc
    return session_id
