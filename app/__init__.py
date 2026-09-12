"""企业知识库助手（Enterprise KB Agent）应用包。

基于 RAG 的企业内部知识库问答系统：导入 Markdown / TXT / 文字型 PDF
-> 切块并向量化存入 PostgreSQL + pgvector -> 混合检索（稠密 + BM25 + RRF + BGE Rerank）
-> 由可切换的 OpenAI 兼容模型（Qwen / DeepSeek）生成带引用的回答。
并带 JWT 认证、SSO(OIDC) 预留、知识库级权限、安全加固、审计日志与离线评测。

代码分层：
    app/api.py         FastAPI 应用装配（中间件 + 路由聚合），启动入口 uvicorn app.api:app
    app/core/          基础设施：config(配置) / database(数据库) / security(JWT)
    app/models/        ORM 模型：knowledge / auth / audit
    app/schemas/       Pydantic 请求/响应模型
    app/services/      业务逻辑：providers/ingestion/retrieval/rerank/rag/audit/eval/sso
    app/routers/       路由层（APIRouter + Depends）：health/auth/knowledge/chat/eval/audit
    app/cli.py         命令行入口；app/eval.py 为 `python -m app.eval` 兼容入口
"""
