"""Pydantic 请求/响应模型，统一收口供路由层使用。"""

from pydantic import BaseModel


class ChatRequest(BaseModel):
    """聊天请求体：问题 + 目标知识库（默认为 default）。"""

    question: str
    knowledge_base: str = "default"


class LoginRequest(BaseModel):
    """开发模式登录请求体。"""

    username: str


class RunEvalRequest(BaseModel):
    """启动评测的请求体。"""

    eval_set: str  # 评测集文件名（如 hr-eval.yaml）
    judge: bool = False  # 是否启用 LLM 裁判
    modes: list[str] = ["dense", "hybrid", "rerank"]  # 要对比的检索方式
