"""Pydantic 请求/响应模型，统一收口供路由层使用。"""

from pydantic import BaseModel, Field


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


class OrganizationWrite(BaseModel):
    """组织信息写模型（新建 / 全量更新共用，admin）。"""

    org_name: str = Field(min_length=1, max_length=200)
    org_type: str = Field(default="dept", max_length=50)
    # 向后兼容保留该请求字段；服务端始终按 pid 对应的父链计算真实层级。
    layer: int = Field(default=1, ge=1)
    pid: int | None = None
    status: str = Field(default="active", max_length=20)
    external_id: str | None = Field(default=None, max_length=200)


class RoleWrite(BaseModel):
    """角色写模型（新建 / 全量更新共用，admin）。"""

    name: str = Field(min_length=1, max_length=100)
    description: str | None = None


class PersonnelWrite(BaseModel):
    """人员写模型（新建 / 全量更新共用，admin）。

    company_name / dept_name 由服务端按 company_id / dept_id 从组织表推导，
    无需前端传；user_id（本地登录账号关联）由 SSO 流程回填，不在管理界面编辑。
    """

    userid: str = Field(min_length=1, max_length=200)
    username: str = Field(min_length=1, max_length=200)
    company_id: int | None = None
    dept_id: int | None = None
    status: str = "active"
    wework_id: str | None = Field(default=None, max_length=200)


class PersonnelRolesWrite(BaseModel):
    """人员-角色关联写模型：整体替换该人员的角色集合（admin）。"""

    role_ids: list[int] = Field(default_factory=list, max_length=1000)
