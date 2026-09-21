# 企业知识库助手后端（FastAPI + pgvector RAG）
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用 Docker 缓存（rerank 为可选重依赖，单独安装以启用）
ARG INSTALL_RERANK=true
COPY requirements.txt requirements-rerank.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_RERANK" = "true" ]; then \
         pip install --no-cache-dir -r requirements-rerank.txt; \
       fi

# 复制应用代码与模型配置（.env 不入镜像，密钥通过运行时环境变量注入）
COPY app ./app
COPY config ./config

EXPOSE 8000

# 启动时 @app.on_event("startup") 会自动 init_db（pgvector 扩展 + 建表）
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
