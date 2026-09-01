"""企业知识库助手（Enterprise KB Agent）应用包。

基于 RAG（检索增强生成）的企业内部知识库问答 MVP：
导入 Markdown / TXT / 文字型 PDF 文档 -> 切块并向量化存入 PostgreSQL + pgvector ->
根据用户问题检索相关片段 -> 由可切换的 OpenAI-compatible 模型（Qwen / DeepSeek）
生成带引用的回答。

模块调用关系总览（逐文件详细调用链见项目根目录开发记录「程序调用过程」一节）：

    命令行入口   cli.py ─────┐
    Web 入口     api.py ─────┤
                             ├─> ingestion.py ──> providers.py ──> OpenAI 兼容 API
                             │       │   ▲              ▲            （Qwen/DeepSeek）
                             │       │   └── settings.py（读 models.yaml + .env）
                             │       ├─> models.py ──> database.py ──> PostgreSQL + pgvector
                             └─> rag.py ──> providers.py / models.py
"""
