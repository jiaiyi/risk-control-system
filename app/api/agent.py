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
    """

    def _event_generator():
        try:
            for piece in agent_chat.chat_stream(payload.message, session_id=payload.session_id):
                yield f"data: {json.dumps({'chunk': piece}, ensure_ascii=False)}\n\n"
            yield "data: " + json.dumps(
                {"done": True, "session_id": payload.session_id}, ensure_ascii=False) + "\n\n"
        except Exception as exc:  # noqa: BLE001
            yield "data: " + json.dumps(
                {"error": getattr(exc, "message", str(exc)),
                 "code": getattr(exc, "code", "BIZ_AGENT_ERROR")},
                ensure_ascii=False) + "\n\n"

    return StreamingResponse(_event_generator(), media_type="text/event-stream")


@router.post("/clear", summary="清除会话（API-29）")
def clear_session(payload: AgentClearRequest) -> dict:
    """清除会话历史 —— 重新开始一轮新对话"""
    agent_chat.clear_session(payload.session_id)
    return {"session_id": payload.session_id, "cleared": True}


@router.get("/sessions", summary="当前会话列表")
def sessions() -> dict:
    """Redis 里存活的会话（含消息数与最近更新时间），供助手页展示历史会话"""
    items = agent_chat.list_sessions()
    return {"total": len(items), "items": items}
