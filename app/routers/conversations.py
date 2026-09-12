"""会话持久化 HTTP API。

- GET    /conversations                    当前用户会话列表（可按 status 过滤，admin 可查全部）
- POST   /conversations                    新建会话
- GET    /conversations/{id}               会话详情 + 一页消息（游标分页，最新在前）
- DELETE /conversations/{id}               删除会话（级联删除消息）
- POST   /conversations/{id}/archive       归档会话（软归档，可恢复）
- POST   /conversations/{id}/unarchive     取消归档
- POST   /conversations/{id}/chat          会话内问答（非流式，落库）
- POST   /conversations/{id}/chat/stream   会话内问答（流式 SSE，落库）
- POST   /conversations/retention/run      立即执行保留策略（仅 admin）
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select

from app.core.config import get_provider
from app.core.database import SessionLocal
from app.models.auth import User
from app.models.conversation import Conversation, ConversationMessage
from app.routers.chat import _sse
from app.routers.deps import get_current_user, require_admin, resolve_kb_names
from app.schemas import ConversationChatRequest, ConversationCreateRequest
from app.services.audit import log_event
from app.services.conversation import apply_retention
from app.services.rag import ask, iter_answer_events
from app.services.review import enqueue_review

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _serialize_conversation(conv: Conversation, message_count: int | None = None) -> dict:
    return {
        "id": conv.id,
        "title": conv.title,
        "knowledge_base": conv.knowledge_base,
        "created_at": _iso(conv.created_at),
        "updated_at": _iso(conv.updated_at),
        "status": conv.status,
        "archived_at": _iso(conv.archived_at),
        "message_count": message_count,
    }


def _serialize_message(msg: ConversationMessage) -> dict:
    return {
        "id": msg.id,
        "role": msg.role,
        "content": msg.content,
        "citations": msg.citations or [],
        "decision": msg.decision,
        "created_at": _iso(msg.created_at),
    }


def _get_owned_conversation(session, conversation_id: int, user: User) -> Conversation:
    """取得会话并校验归属：管理员可访问全部，普通用户仅能访问自己的会话。"""
    conv = session.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="对话不存在。")
    if user.role != "admin" and conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="对话不存在。")
    return conv


def _message_count(session, conversation_id: int) -> int:
    return (
        session.scalar(
            select(func.count(ConversationMessage.id)).where(
                ConversationMessage.conversation_id == conversation_id
            )
        )
        or 0
    )


@router.get("")
def list_conversations(
    status: str = Query("active", description="active（进行中）/ archived（已归档）/ all"),
    user: User = Depends(get_current_user),
) -> dict:
    """返回当前用户的会话列表（按最近更新倒序）；管理员返回全部。"""
    if status not in {"active", "archived", "all"}:
        raise HTTPException(status_code=400, detail="status 必须是 active / archived / all。")
    with SessionLocal() as session:
        count_expr = func.count(ConversationMessage.id)
        stmt = (
            select(Conversation, count_expr)
            .outerjoin(ConversationMessage, ConversationMessage.conversation_id == Conversation.id)
            .group_by(Conversation.id)
            .order_by(desc(Conversation.updated_at), desc(Conversation.id))
        )
        if user.role != "admin":
            stmt = stmt.where(Conversation.user_id == user.id)
        if status != "all":
            stmt = stmt.where(Conversation.status == status)
        rows = session.execute(stmt).all()
        items = [_serialize_conversation(conv, message_count=count) for conv, count in rows]
    return {"items": items}


@router.post("")
def create_conversation(
    body: ConversationCreateRequest,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    """新建会话（标题与知识库可选，缺省给「新对话」）。"""
    with SessionLocal() as session:
        conv = Conversation(
            user_id=user.id,
            title=(body.title or "").strip() or "新对话",
            knowledge_base=body.knowledge_base,
        )
        session.add(conv)
        session.commit()
        session.refresh(conv)
        result = _serialize_conversation(conv, message_count=0)
    log_event(
        "conversation_create",
        user=user.username,
        ip=getattr(request.state, "client_ip", None),
        detail=f"conversation {result['id']}",
        extra={"conversation_id": result["id"]},
    )
    return result


@router.post("/retention/run")
def run_retention(request: Request, user: User = Depends(require_admin)) -> dict:
    """立即执行会话保留策略：自动归档不活跃会话 + 永久删除过期归档会话。"""
    result = apply_retention()
    log_event(
        "conversation_retention",
        user=user.username,
        ip=getattr(request.state, "client_ip", None),
        detail=f"archived={result['archived']} deleted={result['deleted']}",
        extra=result,
    )
    return result


@router.get("/{conversation_id}")
def get_conversation(
    conversation_id: int,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = Query(None, ge=1, description="返回 id 小于该值的更早消息（游标分页）"),
    user: User = Depends(get_current_user),
) -> dict:
    """返回会话详情与一页消息（最新在前）。

    游标分页：缺省返回最新一页；传 before_id 返回比它更早的一页，供前端「加载更早」使用。
    该方式对「流式追加新消息」免疫（新增消息不影响更早页的游标定位）。
    """
    with SessionLocal() as session:
        conv = _get_owned_conversation(session, conversation_id, user)
        total = _message_count(session, conv.id)
        stmt = select(ConversationMessage).where(ConversationMessage.conversation_id == conv.id)
        if before_id is not None:
            stmt = stmt.where(ConversationMessage.id < before_id)
        rows = (
            session.execute(stmt.order_by(desc(ConversationMessage.id)).limit(limit + 1))
            .scalars()
            .all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        messages = [_serialize_message(m) for m in rows]
        return {
            **_serialize_conversation(conv, message_count=total),
            "total": total,
            "has_more": has_more,
            "messages": messages,
        }


@router.delete("/{conversation_id}")
def delete_conversation(
    conversation_id: int,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    """删除会话（级联删除其全部消息）。"""
    with SessionLocal() as session:
        conv = _get_owned_conversation(session, conversation_id, user)
        session.delete(conv)
        session.commit()
    log_event(
        "conversation_delete",
        user=user.username,
        ip=getattr(request.state, "client_ip", None),
        detail=f"conversation {conversation_id}",
        extra={"conversation_id": conversation_id},
    )
    return {"deleted": True, "id": conversation_id}


@router.post("/{conversation_id}/archive")
def archive_conversation(
    conversation_id: int,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    """归档会话（软归档，消息保留，可随时恢复）。"""
    with SessionLocal() as session:
        conv = _get_owned_conversation(session, conversation_id, user)
        conv.status = "archived"
        conv.archived_at = datetime.now(timezone.utc)
        session.commit()
        result = _serialize_conversation(conv, message_count=_message_count(session, conv.id))
    log_event(
        "conversation_archive",
        user=user.username,
        ip=getattr(request.state, "client_ip", None),
        detail=f"conversation {conversation_id}",
        extra={"conversation_id": conversation_id},
    )
    return result


@router.post("/{conversation_id}/unarchive")
def unarchive_conversation(
    conversation_id: int,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    """取消归档，恢复为进行中。"""
    with SessionLocal() as session:
        conv = _get_owned_conversation(session, conversation_id, user)
        conv.status = "active"
        conv.archived_at = None
        session.commit()
        result = _serialize_conversation(conv, message_count=_message_count(session, conv.id))
    log_event(
        "conversation_unarchive",
        user=user.username,
        ip=getattr(request.state, "client_ip", None),
        detail=f"conversation {conversation_id}",
        extra={"conversation_id": conversation_id},
    )
    return result


@router.post("/{conversation_id}/chat")
def conversation_chat(
    conversation_id: int,
    body: ConversationChatRequest,
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    """会话内问答（非流式）：持久化用户问题与助手回答后返回。"""
    with SessionLocal() as session:
        conv = _get_owned_conversation(session, conversation_id, user)
        kb_name, kb_names = resolve_kb_names(user, body.knowledge_base, fallback=conv.knowledge_base)
        if kb_names is not None:
            conv.knowledge_base = None  # 自动路由：会话不绑定具体库
        elif kb_name and conv.knowledge_base != kb_name:
            conv.knowledge_base = kb_name
        # 归档会话出现新消息时自动恢复为进行中
        if conv.status == "archived":
            conv.status = "active"
            conv.archived_at = None
        msg_count = _message_count(session, conv.id)
        if msg_count == 0 and (not conv.title or conv.title == "新对话"):
            conv.title = body.question[:50]
        conv.updated_at = datetime.now(timezone.utc)
        session.add(
            ConversationMessage(conversation_id=conv.id, role="user", content=body.question)
        )
        session.commit()
        try:
            answer, citations, decision = ask(
                session,
                body.question,
                kb_name,
                get_provider(),
                get_provider(for_embeddings=True),
                return_decision=True,
                kb_names=kb_names,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        session.add(
            ConversationMessage(
                conversation_id=conv.id,
                role="assistant",
                content=answer,
                citations=[c.__dict__ for c in citations],
                decision=decision,
            )
        )
        conv.updated_at = datetime.now(timezone.utc)
        session.commit()
        response = {
            "answer": answer,
            "citations": [c.__dict__ for c in citations],
            "decision": decision,
            "conversation_id": conv.id,
        }
    log_event(
        "chat",
        user=user.username,
        ip=getattr(request.state, "client_ip", None),
        detail=body.question[:200],
        extra={"knowledge_base": kb_name or "auto", "decision": decision, "conversation_id": conv.id, "stream": False},
    )
    if decision == "review":
        enqueue_review(
            question=body.question,
            answer=answer,
            knowledge_base=kb_name or "auto",
            citations=[c.__dict__ for c in citations],
            decision=decision,
            asked_by=user.username,
        )
    return response


@router.post("/{conversation_id}/chat/stream")
def conversation_chat_stream(
    conversation_id: int,
    body: ConversationChatRequest,
    request: Request,
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """会话内流式问答（SSE）：先持久化用户问题，流式结束后持久化完整回答。"""
    with SessionLocal() as session:
        conv = _get_owned_conversation(session, conversation_id, user)
        kb_name, kb_names = resolve_kb_names(user, body.knowledge_base, fallback=conv.knowledge_base)
        if kb_names is not None:
            conv.knowledge_base = None
        elif kb_name and conv.knowledge_base != kb_name:
            conv.knowledge_base = kb_name
        if conv.status == "archived":
            conv.status = "active"
            conv.archived_at = None
        msg_count = _message_count(session, conv.id)
        if msg_count == 0 and (not conv.title or conv.title == "新对话"):
            conv.title = body.question[:50]
        conv.updated_at = datetime.now(timezone.utc)
        session.add(
            ConversationMessage(conversation_id=conv.id, role="user", content=body.question)
        )
        session.commit()
        conv_id = conv.id
    log_event(
        "chat",
        user=user.username,
        ip=getattr(request.state, "client_ip", None),
        detail=body.question[:200],
        extra={"knowledge_base": kb_name or "auto", "conversation_id": conv_id, "stream": True},
    )

    def event_stream():
        try:
            chat_config = get_provider()
            embedding_config = get_provider(for_embeddings=True)
            answer_parts: list[str] = []
            decision: str | None = None
            citations: list[dict] = []
            for evt_type, payload in iter_answer_events(
                body.question, kb_name, chat_config, embedding_config, kb_names=kb_names
            ):
                if evt_type == "delta":
                    answer_parts.append(payload["text"])
                elif evt_type == "citations":
                    citations = payload["citations"]
                    decision = payload["decision"]
                elif evt_type == "done":
                    decision = payload["decision"]
                yield _sse({**payload, "type": evt_type})
            with SessionLocal() as session:
                session.add(
                    ConversationMessage(
                        conversation_id=conv_id,
                        role="assistant",
                        content="".join(answer_parts),
                        citations=citations,
                        decision=decision,
                    )
                )
                conv = session.get(Conversation, conv_id)
                if conv is not None:
                    conv.updated_at = datetime.now(timezone.utc)
                session.commit()
            if decision == "review":
                enqueue_review(
                    question=body.question,
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
