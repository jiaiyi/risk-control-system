# -*- coding: utf-8 -*-
"""API-28~29 AI 助手对话

额外提供 `GET /api/agent/sessions` 与 SSE 流式接口（前端 T-24 的逐段展示需要）。
"""
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agent import chat as agent_chat
from app.config import settings
from app.database import get_db
from app.schemas import AgentChatRequest, AgentClearRequest

router = APIRouter(prefix="/agent", tags=["AI 助手"])


@router.post("/chat", summary="AI 助手对话（API-28）")
def chat(payload: AgentChatRequest, db: Session = Depends(get_db)) -> dict:
    """单轮问答（非流式）。

    返回结构含 `reply` / `session_id` / `message_count`，
    前端拿到后直接渲染即可；需要逐段展示请改用 `/chat/stream`。
    """
    reply, session_id = agent_chat.chat(payload.message, session_id=payload.session_id)
    history = agent_chat.load_history(session_id)
    return {
        "reply": reply,
        "session_id": session_id,
        "message_count": len(history),
        "model": settings.LLM_MODEL_NAME,
    }


@router.post("/chat/stream", summary="AI 助手对话（SSE 流式）",
             description="逐段返回 `data: {\"chunk\": \"...\"}`；"
                         "结束帧 `data: {\"done\": true, \"session_id\": ...}`")
def chat_stream(payload: AgentChatRequest) -> StreamingResponse:
    """SSE 流式对话（T-24 前端流式展示用）

    `chat_stream` 本身是同步生成器，`StreamingResponse` 会自动丢到线程池执行，
    不会阻塞事件循环。

    **session_id 必须在流开始之前定下来**：生成器的返回值走 `StopIteration.value`，
    `for piece in gen` 这种消费方式拿不到它。这里先取（或新生成）一个 ID，
    显式传给 agent，再在结束帧里原样回给前端 —— 前端据此把 ID 存下来，
    下一轮带着它继续对话。早期版本回的是 `payload.session_id`（首轮为 None），
    导致流式对话永远开新会话、上下文全丢。
    """
    session_id = payload.session_id or agent_chat.new_session_id()

    def _event_generator():
        try:
            for piece in agent_chat.chat_stream(payload.message, session_id=session_id):
                yield f"data: {json.dumps({'chunk': piece}, ensure_ascii=False)}\n\n"
            yield "data: " + json.dumps(
                {"done": True, "session_id": session_id}, ensure_ascii=False) + "\n\n"
        except Exception as exc:  # noqa: BLE001
            yield "data: " + json.dumps(
                {"error": getattr(exc, "message", str(exc)),
                 "code": getattr(exc, "code", "BIZ_AGENT_ERROR"),
                 "session_id": session_id},
                ensure_ascii=False) + "\n\n"

    return StreamingResponse(_event_generator(), media_type="text/event-stream")


@router.post("/clear", summary="清除会话（API-29）")
def clear_session(payload: AgentClearRequest) -> dict:
    """清除会话历史 —— 重新开始一轮新对话

    对不存在的会话按幂等处理（清一个不存在的会话 = 成功），
    `deleted` 带上真实删除条数，便于分辨「确实清掉了」和「本来就没有 / 缓存不可用」。
    """
    deleted = agent_chat.clear_session(payload.session_id)
    return {"session_id": payload.session_id, "cleared": True, "deleted": deleted}


@router.get("/sessions", summary="当前会话列表")
def sessions() -> dict:
    """Redis 里存活的会话（含消息数与最近更新时间），供助手页展示历史会话"""
    items = agent_chat.list_sessions()
    return {"total": len(items), "items": items}


@router.get("/sessions/{session_id}", summary="读取会话历史消息")
def session_detail(session_id: str) -> dict:
    """切回历史会话时用来还原对话内容。

    只返回「用户说的话」与「AI 的最终回复」，不含 System / Tool 消息 ——
    工具调用是 Agent 的内部过程，历史回看不需要，也不该塞给前端。
    会话不存在（或已过期）返回空列表而不是 404：前端切过去看到空对话，
    比弹一个错误更符合预期（Redis 里会话有 TTL，过一会儿就没了是正常的）。
    """
    items = agent_chat.session_messages(session_id)
    return {"session_id": session_id, "total": len(items), "items": items}
