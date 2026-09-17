"""知识库与文档管理路由（需认证 + 知识库级权限）。"""

from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import func, select

from app.core.config import get_provider, max_upload_bytes
from app.core.database import SessionLocal
from app.models.auth import KnowledgeBaseAccess, User
from app.models.knowledge import Document, DocumentChunk, KnowledgeBase
from app.routers.deps import get_current_user, require_admin, require_kb_access
from app.schemas import KnowledgeBaseRebuildRequest
from app.services.audit import log_event
from app.services.ingestion import ALLOWED_SUFFIXES, ingest_file_detailed, rebuild_knowledge_base
from app.services.providers import ProviderError
from app.services.retrieval import invalidate

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])


@router.get("")
def list_knowledge_bases(user: User = Depends(get_current_user)) -> list[dict]:
    """列出当前用户可访问的知识库，附带每个库的文档数量（admin 可见全部）。"""
    with SessionLocal() as session:
        query = (
            select(KnowledgeBase, func.count(Document.id).label("document_count"))
            .outerjoin(Document, Document.knowledge_base_id == KnowledgeBase.id)
        )
        if user.role != "admin":
            query = query.join(
                KnowledgeBaseAccess, KnowledgeBaseAccess.knowledge_base_id == KnowledgeBase.id
            ).where(KnowledgeBaseAccess.user_id == user.id, KnowledgeBaseAccess.can_read.is_(True))
        rows = session.execute(query.group_by(KnowledgeBase.id).order_by(KnowledgeBase.id)).all()
        return [
            {
                "name": kb.name,
                "embedding_model": kb.embedding_model,
                "embedding_dimensions": kb.embedding_dimensions,
                "document_count": count,
                "created_at": kb.created_at.isoformat() if kb.created_at else None,
            }
            for kb, count in rows
        ]


@router.get("/{knowledge_base}/documents")
def list_documents(knowledge_base: str, _: User = Depends(require_kb_access(write=False))) -> list[dict]:
    """列出指定知识库内的文档，附带每个文档的文本块数量（需读权限）。"""
    with SessionLocal() as session:
        kb = session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == knowledge_base))
        if not kb:
            raise HTTPException(status_code=404, detail=f"Knowledge base '{knowledge_base}' does not exist.")
        rows = session.execute(
            select(Document, func.count(DocumentChunk.id).label("chunk_count"))
            .outerjoin(DocumentChunk, DocumentChunk.document_id == Document.id)
            .where(Document.knowledge_base_id == kb.id)
            .group_by(Document.id)
            .order_by(Document.id.desc())
        ).all()
        return [
            {
                "id": doc.id,
                "filename": doc.filename,
                "chunk_count": count,
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
            }
            for doc, count in rows
        ]


@router.post("/{knowledge_base}/documents")
async def upload_document(
    knowledge_base: str,
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_kb_access(write=True)),
) -> dict:
    """上传并导入一个文档到指定知识库（需写权限）。"""
    # 安全校验：文件名去掉路径（防目录穿越），扩展名仅允许白名单
    filename = Path(file.filename or "document.txt").name
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix}'. Allowed: .md, .txt, .docx, .xlsx, .pptx, .html, .epub, .pdf",
        )
    # 流式写入临时文件并限制大小，避免大文件一次性读入内存
    limit = max_upload_bytes()
    path: Path | None = None
    try:
        with NamedTemporaryFile(suffix=suffix, delete=False) as temp:
            path = Path(temp.name)
            written = 0
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > limit:
                    raise HTTPException(status_code=413, detail=f"File too large (limit {limit} bytes).")
                temp.write(chunk)
        if written == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        warnings_list: list[str] = []
        with SessionLocal() as session:
            chunks, stats = ingest_file_detailed(
                session,
                path,
                knowledge_base,
                get_provider(for_embeddings=True),
                display_filename=filename,
                warnings_out=warnings_list,
            )
        log_event(
            "document_upload",
            user=user.username,
            ip=getattr(request.state, "client_ip", None),
            detail=filename,
            extra={
                "knowledge_base": knowledge_base,
                "chunks": chunks,
                "format": stats.format,
                "text_chunks": stats.text_chunks,
                "table_chunks": stats.table_chunks,
                "skipped_pages": stats.skipped_pages,
                "ocr_pages": stats.ocr_pages,
                "parse_ms": round(stats.parse_ms, 1),
            },
        )
        return {
            "filename": filename,
            "knowledge_base": knowledge_base,
            "chunks": chunks,
            "text_chunks": stats.text_chunks,
            "table_chunks": stats.table_chunks,
            "skipped_pages": stats.skipped_pages,
            "ocr_pages": stats.ocr_pages,
            "parse_ms": round(stats.parse_ms, 1),
            "warnings": warnings_list,
        }
    except ProviderError as exc:
        log_event(
            "document_upload_failed",
            user=user.username,
            ip=getattr(request.state, "client_ip", None),
            detail=filename,
            extra={"knowledge_base": knowledge_base, "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        log_event(
            "document_upload_failed",
            user=user.username,
            ip=getattr(request.state, "client_ip", None),
            detail=filename,
            extra={"knowledge_base": knowledge_base, "error": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


@router.delete("/{knowledge_base}/documents/{document_id}")
def delete_document(
    knowledge_base: str,
    document_id: int,
    request: Request,
    user: User = Depends(require_kb_access(write=True)),
) -> dict:
    """删除知识库中的某个文档，级联删除其全部文本块（需写权限）。"""
    with SessionLocal() as session:
        kb = session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == knowledge_base))
        if not kb:
            raise HTTPException(status_code=404, detail=f"Knowledge base '{knowledge_base}' does not exist.")
        document = session.scalar(
            select(Document).where(Document.id == document_id, Document.knowledge_base_id == kb.id)
        )
        if not document:
            raise HTTPException(
                status_code=404,
                detail=f"Document {document_id} not found in knowledge base '{knowledge_base}'.",
            )
        filename = document.filename
        session.delete(document)
        session.commit()
        invalidate(kb.id)  # 文本块已删除，使该知识库的 BM25 缓存失效
        log_event(
            "document_delete",
            user=user.username,
            ip=getattr(request.state, "client_ip", None),
            detail=filename,
            extra={"knowledge_base": knowledge_base, "document_id": document_id},
        )
        return {"deleted": True, "filename": filename, "knowledge_base": knowledge_base}


@router.post("/{knowledge_base}/reingest")
def reingest_knowledge_base(
    knowledge_base: str,
    body: KnowledgeBaseRebuildRequest,
    request: Request,
    user: User = Depends(require_admin),
) -> dict:
    """按当前解析策略重导知识库（保持 Embedding 配置不变，逐文件替换同名文档）。

    仅 admin 可执行；source_dir 为服务器本机源文档目录。单文件失败不阻断其余文件，
    错误计入返回的 errors 列表。
    """
    warnings_list: list[str] = []
    try:
        result = rebuild_knowledge_base(
            knowledge_base,
            Path(body.source_dir),
            get_provider(for_embeddings=True),
            replace_embedding=False,
            warnings_out=warnings_list,
        )
    except ProviderError as exc:
        log_event(
            "kb_reingest_failed",
            user=user.username,
            ip=getattr(request.state, "client_ip", None),
            detail=knowledge_base,
            extra={"knowledge_base": knowledge_base, "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        log_event(
            "kb_reingest_failed",
            user=user.username,
            ip=getattr(request.state, "client_ip", None),
            detail=knowledge_base,
            extra={"knowledge_base": knowledge_base, "error": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        "kb_reingest",
        user=user.username,
        ip=getattr(request.state, "client_ip", None),
        detail=knowledge_base,
        extra={
            "knowledge_base": knowledge_base,
            "source_dir": body.source_dir,
            "processed": result.processed,
            "chunks": result.chunks,
            "errors": result.errors,
        },
    )
    return {
        "knowledge_base": knowledge_base,
        "processed": result.processed,
        "chunks": result.chunks,
        "errors": result.errors,
        "warnings": warnings_list,
    }


@router.post("/{knowledge_base}/rebuild")
def rebuild_knowledge_base_endpoint(
    knowledge_base: str,
    body: KnowledgeBaseRebuildRequest,
    request: Request,
    user: User = Depends(require_admin),
) -> dict:
    """按当前 Embedding 与解析配置重建知识库索引（清空后从源目录全量重导）。

    仅 admin 可执行；目录即知识库的完整真相，目录之外的旧文档会被清空。
    """
    warnings_list: list[str] = []
    try:
        result = rebuild_knowledge_base(
            knowledge_base,
            Path(body.source_dir),
            get_provider(for_embeddings=True),
            replace_embedding=True,
            warnings_out=warnings_list,
        )
    except ProviderError as exc:
        log_event(
            "kb_rebuild_failed",
            user=user.username,
            ip=getattr(request.state, "client_ip", None),
            detail=knowledge_base,
            extra={"knowledge_base": knowledge_base, "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        log_event(
            "kb_rebuild_failed",
            user=user.username,
            ip=getattr(request.state, "client_ip", None),
            detail=knowledge_base,
            extra={"knowledge_base": knowledge_base, "error": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event(
        "kb_rebuild",
        user=user.username,
        ip=getattr(request.state, "client_ip", None),
        detail=knowledge_base,
        extra={
            "knowledge_base": knowledge_base,
            "source_dir": body.source_dir,
            "processed": result.processed,
            "chunks": result.chunks,
            "errors": result.errors,
        },
    )
    return {
        "knowledge_base": knowledge_base,
        "processed": result.processed,
        "chunks": result.chunks,
        "errors": result.errors,
        "warnings": warnings_list,
    }
