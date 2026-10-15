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
from datetime import datetime

import httpx
import openai
import ulid
from langchain.agents import create_agent
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    messages_from_dict,
    messages_to_dict,
)
from langchain_openai import ChatOpenAI

from app import cache
from app.agent.tools import ALL_TOOLS
from app.config import settings
from app.errors import ServiceError, SystemServiceError

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
- 白名单有硬约束（必须设有效期、不超 90 天、不能与黑名单共存）：
  用户要求加白时，先问清楚**有效期**（这个必填，没有它加不上），
  再用 `expire_time`（YYYY-MM-DD）连同 `confirm=true` 一起传下去。
  若用户要的有效期超过 90 天，先把超期这件事讲清楚、等用户同意后再传 `force=true` 重试。
- 涉及名单增删这类写操作，执行后明确告知「已做了什么、影响了什么」

**写操作红线（最高优先级，违反即事故）**：
- `manage_blacklist` 的「新增」「移除」是**写操作**，只有 `confirm=true` 才会真正执行。
  调用前必须先向用户复述「我要把什么加进/移出哪个名单、原因是什么」，
  **得到用户明确同意后**才带 `confirm=true` 重新调用。
- 用户只说「看看 / 查一下 / 分析一下」时，**只允许** `action="查询"`，
  绝不擅自增删名单条目 —— 哪怕你觉得这个主体明显该进黑名单。
- 确实发现高风险主体时，正确做法是**在回答里建议**「建议将 X 加入黑名单」，
  然后等用户说「加吧」再动手。
"""


class LLMNotConfiguredError(ServiceError):
    """LLM 未配置（通常是 LLM_API_KEY 为空）"""

    code = "SYS_LLM_NOT_CONFIGURED"
    http_status = 503


class AgentServiceError(ServiceError):
    code = "BIZ_AGENT_ERROR"


class LLMTimeoutError(SystemServiceError):
    """LLM 调用超时（SPEC 6.3 的 `SYS_LLM_TIMEOUT`）

    与 `LLMNotConfiguredError` 刻意分开，因为处置方式不同：
        · 未配置（503）= 没配好，重试多少次都一样，得去改 `.env`
        · 超时（504）= 配好了但这次没等到，稍后重试或缩小问题范围有意义
    合并成一个码的话，前端就没法给出正确的下一步提示。
    """

    code = "SYS_LLM_TIMEOUT"
    http_status = 504


# 超时的几种真实形态（实测：读超时抛 openai.APITimeoutError，其 __cause__ 是
# httpx.ReadTimeout）。三个都收着，避免 SDK 换一层包装就漏判。
_TIMEOUT_EXC_TYPES = (TimeoutError, httpx.TimeoutException, openai.APITimeoutError)


def _is_timeout_error(exc: BaseException) -> bool:
    """判断异常链里是否含超时。

    不只看最外层：LangChain / LangGraph 在部分路径上会把底层异常再包一层，
    真正的超时藏在 `__cause__` / `__context__` 里，只看最外层会漏判。
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:   # seen 防异常链成环导致死循环
        seen.add(id(cur))
        if isinstance(cur, _TIMEOUT_EXC_TYPES):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _wrap_agent_exception(exc: Exception) -> ServiceError:
    """把 Agent 执行期的任意异常收敛成对外的业务异常。

    超时单独成码（`SYS_LLM_TIMEOUT`），其余归 `BIZ_AGENT_ERROR` ——
    把「等不到模型」和「Agent 内部出错」混成一个笼统的失败，
    前端就没法区分「可以重试」和「重试也没用」。

    调用点都在 `except` 块内，所以这里的 `logger.exception` 能取到当前异常。
    """
    if _is_timeout_error(exc):
        logger.warning("LLM 调用超时（%ss，模型 %s）: %s",
                       settings.LLM_TIMEOUT, settings.LLM_MODEL_NAME, exc)
        return LLMTimeoutError(
            f"AI 助手响应超时（{settings.LLM_TIMEOUT} 秒），请稍后重试或把问题拆小一些",
            {"timeout_seconds": settings.LLM_TIMEOUT, "model": settings.LLM_MODEL_NAME},
        )
    logger.exception("Agent 执行异常")
    return AgentServiceError(f"Agent 执行失败：{exc}")


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
    # langchain.agents.create_agent 是 LangGraph 1.0 起的正式入口，
    # langgraph.prebuilt.create_react_agent 已弃用（仍能用但会打 DeprecationWarning）。
    # 两者的流式行为一致：stream_mode=["messages","values"] 同样出 (mode, payload)，
    # 系统提示词参数从 prompt 改名为 system_prompt。
    return create_agent(
        model=llm,
        tools=ALL_TOOLS,
        system_prompt=SYSTEM_PROMPT,
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
def new_session_id() -> str:
    """生成会话 ID。

    公开而非私有：流式接口要在**开始生成之前**就确定 session_id 并回给前端，
    否则前端拿不到 ID，下一轮只能以 session_id=None 再发一次，
    后端于是又开一个新会话 —— 表现为「流式对话完全没有上下文」。
    """
    return f"sess_{ulid.new().str.lower()}"


def _trim_history(messages: list[BaseMessage], max_turns: int) -> list[BaseMessage]:
    """只保留最近 max_turns 轮，截断点必须落在 HumanMessage 上。

    不能在任意位置切：AIMessage 的 `tool_calls` 与紧随其后的 ToolMessage 是一对，
    从中间切断会让模型收到「有 tool_call 却没有对应结果」的消息链，部分模型直接报错。
    从倒数第 N 条 HumanMessage 开始切，一定能保证工具调用成对。

    工具输出是缩进 JSON，动辄上千 token，不截断的话聊十几轮就会撑爆上下文窗口。
    截断后的历史会随本轮结果一起落库，所以 Redis 里的量也是有界的。
    """
    if max_turns <= 0:
        return messages
    human_idx = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(human_idx) <= max_turns:
        return messages
    return messages[human_idx[-max_turns]:]


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
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "messages": messages_to_dict(messages),
        }
        cache.safe_set(
            cache.agent_session_key(session_id), payload, settings.AGENT_SESSION_TTL,
        )
    except Exception as exc:  # noqa: BLE001
        # 会话持久化失败不阻断对话（只是这轮之后会失忆）
        logger.warning("会话历史保存失败: %s", exc)


def clear_session(session_id: str) -> int:
    """清除会话历史（API-29 / FR-32），返回实际删除的键数量。

    返回删除数而不是无脑 True：会话不存在时是 0，Redis 不可用时也是 0，
    调用方至少能把这个数带出去，不用猜。接口层对「会话不存在」仍按幂等处理
    （清一个不存在的会话 = 成功），只是会把真实删除数一并返回。
    """
    return cache.safe_delete(cache.agent_session_key(session_id))


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
        session_id = new_session_id()

    history = _trim_history(load_history(session_id), settings.AGENT_MAX_HISTORY_TURNS)
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
        raise _wrap_agent_exception(exc) from exc


def chat_stream(message: str, session_id: str | None = None):
    """流式对话（供前端逐段展示，T-24）

    逐个 yield 文本片段；生成器结束时用 `return session_id` 交出会话 ID。

    ⚠️ 调用方注意：生成器的返回值走 `StopIteration.value`，
    `for piece in chat_stream(...)` 这种写法**拿不到**。
    流式接口必须在调用前自行决定 session_id（用 `new_session_id()`），
    或显式用 `next()` 迭代再捕 StopIteration —— 否则前端拿不到 ID，
    下一轮会以 session_id=None 重新开一个会话，表现为「聊了等于没聊」。

    只跑一遍 Agent：`stream_mode=["messages", "values"]` 让**同一次执行**
    既产出 token 级片段（messages）又产出每步之后的完整 state（values），
    最后拿 values 里的消息链落库。

    早期实现在流式结束后又 `agent.invoke()` 了一遍来取最终消息链 ——
    那是**第二次完整执行**：LLM 与工具调用全部翻倍，`risk_check` 会多落一条评估、
    `manage_blacklist(confirm=True)` 会真的执行两次。
    """
    if not message or not str(message).strip():
        raise AgentServiceError("消息内容不能为空")

    if not session_id:
        session_id = new_session_id()

    history = _trim_history(load_history(session_id), settings.AGENT_MAX_HISTORY_TURNS)
    history = [*history, HumanMessage(content=message)]

    agent = get_agent()
    final_messages: list[BaseMessage] = []
    try:
        for mode, payload in agent.stream(
            {"messages": history}, stream_mode=["messages", "values"],
        ):
            if mode == "messages":
                chunk, _meta = payload
                if isinstance(chunk, AIMessage) and chunk.content:
                    piece = _flatten_content(chunk.content)
                    if piece:
                        yield piece
            elif mode == "values":
                msgs = payload.get("messages") if isinstance(payload, dict) else None
                if msgs:
                    final_messages = msgs
        if final_messages:
            save_history(session_id, final_messages)
    except LLMNotConfiguredError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _wrap_agent_exception(exc) from exc
    return session_id
