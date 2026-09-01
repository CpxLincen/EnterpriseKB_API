"""FastAPI Web 接口模块。

提供 REST 端点：
- GET    /health                                存活探针
- GET    /ready                                 就绪探针
- GET    /knowledge-bases                       列出当前用户可访问的知识库
- GET    /knowledge-bases/{name}/documents      列出知识库内的文档（需读权限）
- POST   /knowledge-bases/{name}/documents      上传并导入文档（需写权限）
- DELETE /knowledge-bases/{name}/documents/{id} 删除文档（需写权限）
- POST   /chat                                  执行 RAG 问答（需读权限）

认证与授权：
- auth_middleware 在进入路由前校验 Bearer Token（公开路径除外）；
- 知识库级权限通过 require_kb_access / ensure_kb_access 控制；
- /auth/* 登录相关路由见 app/auth/routes.py。
"""

from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import func, select, text

from app.audit import log_event
from app.auth.dependencies import ensure_kb_access, get_current_user, require_kb_access
from app.auth.middleware import auth_middleware
from app.auth.models import KnowledgeBaseAccess, User
from app.auth.routes import router as auth_router
from app.database import SessionLocal, engine, init_db
from app.ingestion import ingest_file
from app.models import Document, DocumentChunk, KnowledgeBase
from app.rag import ask
from app.settings import cors_origins, get_provider, max_upload_bytes


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库（pgvector 扩展 + 数据表）。"""
    init_db()
    yield


# 创建 FastAPI 应用实例（用 lifespan 替代已弃用的 @app.on_event("startup")）
app = FastAPI(title="Enterprise KB Agent", version="0.1.0", lifespan=lifespan)

# 前后端分离：允许前端开发服务器 / 静态站点跨域调用本 API。
# 开发阶段放开全部来源；生产环境请按实际前端域名收紧。
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 认证中间件：公开路径（/health、/docs、/auth/* 等）之外均需 Bearer Token
app.middleware("http")(auth_middleware)

# 登录 / SSO 预留 / 当前用户 / 登出路由
app.include_router(auth_router)


class ChatRequest(BaseModel):
    """聊天请求体：问题 + 目标知识库（默认为 default）。"""

    question: str
    knowledge_base: str = "default"


# 允许上传的文档扩展名（白名单）
ALLOWED_SUFFIXES = {".md", ".txt", ".pdf"}


@app.get("/health")
def health() -> dict:
    """存活探针：进程在运行即返回 ok。"""
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    """就绪探针：确认数据库连接可用（SELECT 1）。"""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.get("/knowledge-bases")
def list_knowledge_bases(user: User = Depends(get_current_user)) -> list[dict]:
    """列出当前用户可访问的知识库，附带每个库的文档数量（admin 可见全部）。"""
    with SessionLocal() as session:
        query = (
            select(KnowledgeBase, func.count(Document.id).label("document_count"))
            .outerjoin(Document, Document.knowledge_base_id == KnowledgeBase.id)
        )
        if user.role != "admin":
            query = (
                query.join(KnowledgeBaseAccess, KnowledgeBaseAccess.knowledge_base_id == KnowledgeBase.id)
                .where(KnowledgeBaseAccess.user_id == user.id, KnowledgeBaseAccess.can_read.is_(True))
            )
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


@app.get("/knowledge-bases/{knowledge_base}/documents")
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


@app.post("/knowledge-bases/{knowledge_base}/documents")
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
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{suffix}'. Allowed: .md, .txt, .pdf")
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
        with SessionLocal() as session:
            chunks = ingest_file(
                session,
                path,
                knowledge_base,
                get_provider(for_embeddings=True),
                display_filename=filename,
            )
        log_event(
            "document_upload",
            user=user.username,
            ip=getattr(request.state, "client_ip", None),
            detail=filename,
            extra={"knowledge_base": knowledge_base, "chunks": chunks},
        )
        return {"filename": filename, "knowledge_base": knowledge_base, "chunks": chunks}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


@app.delete("/knowledge-bases/{knowledge_base}/documents/{document_id}")
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
        log_event(
            "document_delete",
            user=user.username,
            ip=getattr(request.state, "client_ip", None),
            detail=filename,
            extra={"knowledge_base": knowledge_base, "document_id": document_id},
        )
        return {"deleted": True, "filename": filename, "knowledge_base": knowledge_base}


@app.post("/chat")
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
