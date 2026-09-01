"""文档导入模块（Ingestion）。

负责完整的入库流水线：
文件读取 -> 文本切块 -> 计算内容哈希去重 -> 批量生成 Embedding -> 写入数据库。

调用关系：
- 被 cli.py（ingest 命令）与 api.py（上传文档接口）调用 ingest_file()
- 依赖 models.py（Document/DocumentChunk/KnowledgeBase）、providers.py（embed）、
  settings.py（ProviderConfig）
- 内部辅助函数：read_document() 读文件、chunk_text() 切块
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pypdf import PdfReader  # 用于提取文字型 PDF 的文本
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, KnowledgeBase
from app.providers import OpenAICompatibleProvider
from app.settings import ProviderConfig


def read_document(path: Path) -> list[tuple[str, int | None]]:
    """读取文档，返回 (文本, 页码) 列表。

    - PDF：逐页提取文本，页码从 1 开始；页面无文字时提取为空字符串。
    - Markdown / TXT：整篇读取，页码为 None。

    参数:
        path: 文档路径。
    返回:
        [(页面/整篇文本, 页码或 None), ...]
    """
    # PDF：逐页提取文本，并记录页码
    if path.suffix.lower() == ".pdf":
        return [(page.extract_text() or "", index + 1) for index, page in enumerate(PdfReader(path).pages)]
    # 其他类型：MVP 阶段仅支持 .md / .txt
    if path.suffix.lower() not in {".md", ".txt"}:
        raise ValueError("Only .md, .txt, and text-based .pdf files are supported in this MVP.")
    # Markdown / TXT：优先按 UTF-8 读取；失败时降级到 GB18030（兼容中文
    # Windows 常见的 GBK/GB2312 编码）；两种编码都无法解析时给出明确错误。
    try:
        return [(path.read_text(encoding="utf-8"), None)]
    except UnicodeDecodeError:
        try:
            return [(path.read_text(encoding="gb18030"), None)]
        except UnicodeDecodeError as exc:
            raise ValueError(
                f"Cannot decode '{path.name}' as UTF-8 or GB18030; convert it to UTF-8 and retry."
            ) from exc


def chunk_text(text: str, size: int = 650, overlap: int = 80) -> list[str]:
    """把长文本按断点切分为块，块间保留少量重叠。

    策略：优先在换行、句末标点（。！？）、停顿（，；、）或空格处断开；
    找不到合适断点且超过一半块长度时才硬切。下一块从上一块结尾往前
    overlap 处开始，避免切断语义。

    参数:
        text:    原始文本。
        size:    目标块大小（字符数）。
        overlap: 相邻块之间重叠的字符数。
    返回:
        切分后的文本块列表（过滤掉过短的碎片）。
    """
    # 将 3 个及以上连续换行压缩为 2 个，并去除首尾空白
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks: list[str] = []
    while text:
        # 剩余文本不超过块大小：整段作为最后一块
        if len(text) <= size:
            chunks.append(text)
            break
        # 在块范围内寻找最后一个自然断点（换行 / 句末标点 / 停顿 / 空格）。
        # 断点候选越丰富，越不容易落到最后的硬切分支，切块更贴近语义。
        separators = ("\n", "。", "！", "？", "；", "，", "、", " ", ".", "!", "?", ";", ",")
        split_at = max(text.rfind(sep, 0, size) for sep in separators)
        # 若断点太靠前（不足半块），说明没有合适断点，只能硬切
        split_at = split_at if split_at > size // 2 else size
        chunks.append(text[:split_at].strip())
        # 下一块从 split_at - overlap 处开始，保证语义衔接
        text = text[max(0, split_at - overlap) :].strip()
    # 过滤掉长度 <= 20 的碎片（通常是切分残留）
    return [chunk for chunk in chunks if len(chunk) > 20]


def ingest_file(
    session: Session,
    path: Path,
    kb_name: str,
    provider_config: ProviderConfig,
    display_filename: str | None = None,
) -> int:
    """导入单个文件到指定知识库。

    流程：读文件 -> 计算内容哈希 -> 找到/创建知识库 -> 内容去重 -> 切块 ->
    批量 Embedding -> 写入 documents 与 document_chunks。

    参数:
        session:         数据库会话。
        path:             待导入文件路径。
        kb_name:          目标知识库名称。
        provider_config:  用于 Embedding 的供应商配置。
        display_filename: 入库时记录的文件名；为 None 时使用 path.name
                          （API 上传场景应传原始文件名，避免显示临时文件名）。
    返回:
        本次导入的文本块数量；文件内容已存在时返回 0（去重跳过）。
    """
    # 计算文件内容 SHA-256 哈希，用于跨导入去重
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    # 查找目标知识库；不存在则创建
    kb = session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == kb_name))
    if not kb:
        # 新建知识库必须提供 Embedding 模型与维度
        if not provider_config.embedding_model or not provider_config.embedding_dimensions:
            raise RuntimeError("Embedding model and dimensions must be configured.")
        kb = KnowledgeBase(name=kb_name, embedding_model=provider_config.embedding_model, embedding_dimensions=provider_config.embedding_dimensions)
        session.add(kb)
        session.flush()  # 先落库拿到 kb.id
    # 校验 Embedding 模型/维度与知识库一致，防止向量索引错配
    if kb.embedding_model != provider_config.embedding_model or kb.embedding_dimensions != provider_config.embedding_dimensions:
        raise RuntimeError("This knowledge base uses a different embedding model/dimension. Create a new KB or rebuild it.")
    # 内容哈希相同则视为已导入，直接跳过（去重）
    existing = session.scalar(select(Document).where(Document.knowledge_base_id == kb.id, Document.content_hash == digest))
    if existing:
        return 0
    # 读取文档并逐段切块，得到 [(文本, 页码)] 列表
    pieces = [(content, page) for text, page in read_document(path) for content in chunk_text(text)]
    if not pieces:
        raise ValueError("No readable text found in document.")
    # 批量生成全部文本块的 Embedding 向量
    provider = OpenAICompatibleProvider(provider_config)
    vectors = provider.embed([content for content, _ in pieces])
    # 创建文档记录
    document = Document(knowledge_base_id=kb.id, filename=display_filename or path.name, content_hash=digest)
    session.add(document)
    session.flush()  # 先落库拿到 document.id
    # 用 zip 把 (文本, 页码) 与向量一一配对，生成文本块记录并批量写入
    session.add_all(DocumentChunk(document_id=document.id, content=content, page_number=page, chunk_index=index, embedding=vector) for index, ((content, page), vector) in enumerate(zip(pieces, vectors)))
    session.commit()  # 一次性提交全部变更
    return len(pieces)  # 返回本次导入的块数
