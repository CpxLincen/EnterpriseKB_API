"""Pydantic 请求/响应模型，统一收口供路由层使用。"""

from pydantic import BaseModel


class ChatRequest(BaseModel):
    """聊天请求体：问题 + 可选目标知识库。

    knowledge_base 为空、"auto" 或未传时走自动路由（检索当前用户全部可读知识库）；
    传具体库名时仅检索该库。
    """

    question: str
    knowledge_base: str | None = None


class ConversationCreateRequest(BaseModel):
    """新建会话请求体：标题与知识库均可选，缺省由服务端补齐。"""

    title: str | None = None
    knowledge_base: str | None = None


class ConversationChatRequest(BaseModel):
    """会话内问答请求体：知识库可选，缺省沿用会话已绑定的知识库。"""

    question: str
    knowledge_base: str | None = None


class LoginRequest(BaseModel):
    """开发模式登录请求体。"""

    username: str


class RunEvalRequest(BaseModel):
    """启动评测的请求体。"""

    eval_set: str  # 评测集文件名（如 hr-eval.yaml）
    judge: bool = False  # 是否启用 LLM 裁判
    modes: list[str] = ["dense", "hybrid", "rerank"]  # 要对比的检索方式


class KnowledgeBaseRebuildRequest(BaseModel):
    """知识库重导 / 重建请求体：服务器本机源文档目录路径。"""

    source_dir: str  # 源文档目录（绝对路径，需在服务器本机存在）
