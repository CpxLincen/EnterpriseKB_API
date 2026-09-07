"""问答路由（非流式 + 流式 SSE）。"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.config import get_provider
from app.core.database import SessionLocal
from app.models.auth import User
from app.routers.deps import ensure_kb_access, get_current_user
from app.schemas import ChatRequest
from app.services.audit import log_event
from app.services.providers import OpenAICompatibleProvider
from app.services.rag import SYSTEM_PROMPT, ask, build_user_prompt, retrieve_context

router = APIRouter(tags=["chat"])


def _sse(payload: dict) -> str:
    """把 dict 编码为一条 SSE 事件（data: <json> + 空行）。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/chat")
def chat(request: ChatRequest, http_request: Request, user: User = Depends(get_current_user)) -> dict:
    """执行 RAG 问答，返回回答文本与引用列表（需读权限）。"""
    ensure_kb_access(user, request.knowledge_base, write=False)
    try:
        with SessionLocal() as session:
            answer, citations = ask(
                session,
                request.question,
                request.knowledge_base,
                get_provider(),
                get_provider(for_embeddings=True),
            )
        log_event(
            "chat",
            user=user.username,
            ip=getattr(http_request.state, "client_ip", None),
            detail=request.question[:200],
            extra={"knowledge_base": request.knowledge_base},
        )
        return {"answer": answer, "citations": [citation.__dict__ for citation in citations]}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/chat/stream")
def chat_stream(
    request: ChatRequest,
    http_request: Request,
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """执行流式 RAG 问答（SSE），先下发引用，再逐块下发回答增量（需读权限）。

    事件协议：
      data: {"type":"citations","citations":[...]}  引用列表（回答前一次）
      data: {"type":"delta","text":"..."}           回答增量文本（多次）
      data: {"type":"done"}                         结束
      data: {"type":"error","message":"..."}        出错
    """
    ensure_kb_access(user, request.knowledge_base, write=False)
    log_event(
        "chat",
        user=user.username,
        ip=getattr(http_request.state, "client_ip", None),
        detail=request.question[:200],
        extra={"knowledge_base": request.knowledge_base, "stream": True},
    )

    def event_stream():
        try:
            chat_config = get_provider()
            embedding_config = get_provider(for_embeddings=True)
            with SessionLocal() as session:
                _, citations, context = retrieve_context(
                    session, request.question, request.knowledge_base, embedding_config
                )
            # 防幻觉门禁拒答：直接流式下发固定文案
            if context is None:
                yield _sse({"type": "citations", "citations": []})
                yield _sse({"type": "delta", "text": "知识库中未找到相关依据。"})
                yield _sse({"type": "done"})
                return
            # 先下发引用，再逐块下发回答
            yield _sse({"type": "citations", "citations": [c.__dict__ for c in citations]})
            for text in OpenAICompatibleProvider(chat_config).chat_stream(
                SYSTEM_PROMPT, build_user_prompt(context, request.question)
            ):
                yield _sse({"type": "delta", "text": text})
            yield _sse({"type": "done"})
        except Exception as exc:  # noqa: BLE001 - 流式阶段异常以 SSE error 事件返回
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
