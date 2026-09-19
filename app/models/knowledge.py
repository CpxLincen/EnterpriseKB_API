"""数据模型定义模块。

使用 SQLAlchemy 2.0 声明式映射定义三张核心表：
- knowledge_bases：知识库（记录所用的 Embedding 模型与维度，防止索引错配）
- documents：文档（记录文件名与内容哈希，用于导入去重）
- document_chunks：文本块（保存切块内容、页码、块序号与 pgvector 向量）

调用关系：
- 依赖 app/database.py 的 Base（声明式基类）
- 被 ingestion.py（写入数据）、rag.py（检索）、api.py（统计/删除）直接使用
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector  # pgvector 提供的向量字段类型
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base  # SQLAlchemy 声明式基类


class KnowledgeBase(Base):
    """知识库表：一个知识库对应一组文档与一套 Embedding 配置。"""

    __tablename__ = "knowledge_bases"
    id: Mapped[int] = mapped_column(primary_key=True)  # 主键
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)  # 知识库名称（唯一）
    embedding_model: Mapped[str] = mapped_column(String(200))  # 建库时使用的 Embedding 模型
    embedding_dimensions: Mapped[int] = mapped_column(Integer)  # Embedding 向量维度
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())  # 创建时间（数据库默认当前时间）
    # 与文档的一对多关系；删除知识库时级联删除其全部文档
    documents: Mapped[list[Document]] = relationship(back_populates="knowledge_base", cascade="all, delete-orphan")


class Document(Base):
    """文档表：记录一次导入的源文件信息。"""

    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True)  # 主键
    knowledge_base_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)  # 所属知识库外键
    filename: Mapped[str] = mapped_column(String(500))  # 原始文件名
    content_hash: Mapped[str] = mapped_column(String(64), index=True)  # 文件内容 SHA-256 哈希，用于导入去重
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())  # 导入时间
    source_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # 源文件相对路径（增量同步用）
    source_mtime: Mapped[float | None] = mapped_column(Float, nullable=True)  # 源文件修改时间戳（增量同步用）
    source_size: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 源文件大小（增量同步用）
    # 反向关系：所属知识库
    knowledge_base: Mapped[KnowledgeBase] = relationship(back_populates="documents")
    # 与文本块的一对多关系；删除文档时级联删除其全部文本块
    chunks: Mapped[list[DocumentChunk]] = relationship(back_populates="document", cascade="all, delete-orphan")


class DocumentChunk(Base):
    """文本块表：保存切块后的文本、元数据与向量，是检索的最小单元。"""

    __tablename__ = "document_chunks"
    id: Mapped[int] = mapped_column(primary_key=True)  # 主键
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)  # 所属文档外键
    content: Mapped[str] = mapped_column(Text)  # 文本块内容
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 页码（非 PDF 为 None）
    chunk_index: Mapped[int] = mapped_column(Integer)  # 块在文档内的序号（从 0 开始）
    content_type: Mapped[str] = mapped_column(String(20), nullable=False, default="text", server_default="text")  # 块类型：text / table
    embedding: Mapped[list[float]] = mapped_column(Vector())  # 文本块的向量（维度由建库时确定）
    # 反向关系：所属文档
    document: Mapped[Document] = relationship(back_populates="chunks")
