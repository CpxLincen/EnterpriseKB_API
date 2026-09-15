"""问答路由（非流式 + 流式 SSE）。"""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.config import get_provider
from app.core.database import SessionLocal
from app.models.auth import User
from app.routers.deps import get_current_user, resolve_kb_names
from app.schemas import ChatRequest
from app.services.audit import log_event
from app.services.rag import ask, iter_answer_events
from app.services.review import enqueue_review

router = APIRouter(tags=["chat"])


def _sse(payload: dict) -> str:
    """把 dict 编码为一条 SSE 事件（data: <json> + 空行）。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/chat")
def chat(request: ChatRequest, http_request: Request, user: User = Depends(get_current_user)) -> dict:
    """执行 RAG 问答，返回回答文本与引用列表（未指定知识库时自动路由）。"""
    kb_name, kb_names = resolve_kb_names(user, request.knowledge_base)
    try:
        with SessionLocal() as session:
            answer, citations, decision = ask(
                session,
                request.question,
                kb_name,
                get_provider(),
                get_provider(for_embeddings=True),
                return_decision=True,
                kb_names=kb_names,
            )
        log_event(
            "chat",
            user=user.username,
            ip=getattr(http_request.state, "client_ip", None),
            detail=request.question[:200],
            extra={"knowledge_base": kb_name or "auto", "decision": decision},
        )
        if decision == "review":
            enqueue_review(
                question=request.question,
                answer=answer,
                knowledge_base=kb_name or "auto",
                citations=[citation.__dict__ for citation in citations],
                decision=decision,
                asked_by=user.username,
            )
        return {"answer": answer, "citations": [citation.__dict__ for citation in citations], "decision": decision}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/chat/stream")
def chat_stream(
    request: ChatRequest,
    http_request: Request,
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """执行流式 RAG 问答（SSE），先下发引用，再逐块下发回答增量（未指定知识库时自动路由）。

    事件协议：
      data: {"type":"citations","citations":[...],"decision":"answer|review|refuse"}  引用 + 门禁三态（回答前一次）
      data: {"type":"delta","text":"..."}           回答增量文本（多次）
      data: {"type":"done"}                         结束
      data: {"type":"error","message":"..."}        出错
    """
    kb_name, kb_names = resolve_kb_names(user, request.knowledge_base)
    log_event(
        "chat",
        user=user.username,
        ip=getattr(http_request.state, "client_ip", None),
        detail=request.question[:200],
        extra={"knowledge_base": kb_name or "auto", "stream": True},
    )

    def event_stream():
        try:
            chat_config = get_provider()
            embedding_config = get_provider(for_embeddings=True)
            answer_parts: list[str] = []
            decision: str | None = None
            citations: list[dict] = []
            for evt_type, payload in iter_answer_events(
                request.question, kb_name, chat_config, embedding_config, kb_names=kb_names
            ):
                if evt_type == "delta":
                    answer_parts.append(payload["text"])
                elif evt_type == "citations":
                    citations = payload["citations"]
                    decision = payload["decision"]
                elif evt_type == "done":
                    decision = payload["decision"]
                elif evt_type == "chunks":
                    continue  # 内部事件：仅供会话落库，非会话流式接口不转发
                yield _sse({**payload, "type": evt_type})
            # review 状态：流式结束后把完整回答写入待复核队列
            if decision == "review":
                enqueue_review(
                    question=request.question,
                    answer="".join(answer_parts),
                    knowledge_base=kb_name or "auto",
                    citations=citations,
                    decision=decision,
                    asked_by=user.username,
                )
        except Exception as exc:  # noqa: BLE001 - 流式阶段异常以 SSE error 事件返回
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
