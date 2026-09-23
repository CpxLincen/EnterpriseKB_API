# 企业知识库助手后端（FastAPI + pgvector RAG）
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUTF8=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

# 先装依赖，利用 Docker 缓存（rerank / OCR 为可选重依赖，单独安装以启用）
ARG INSTALL_RERANK=true
ARG INSTALL_OCR=false
COPY requirements.txt requirements-rerank.txt requirements-ocr.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_RERANK" = "true" ]; then \
         pip install --no-cache-dir -r requirements-rerank.txt; \
       fi \
    && if [ "$INSTALL_OCR" = "true" ]; then \
         apt-get update \
         && apt-get install -y --no-install-recommends \
              libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 libfontconfig1 \
              libxcb1 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
              libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libxkbcommon0 libxkbcommon-x11-0 \
         && pip install --no-cache-dir -r requirements-ocr.txt \
         && rm -rf /var/lib/apt/lists/*; \
       fi

# 复制应用代码与模型配置（.env 不入镜像，密钥通过运行时环境变量注入）
COPY app ./app
COPY config ./config

EXPOSE 8000

# 启动时 @app.on_event("startup") 会自动 init_db（pgvector 扩展 + 建表）
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
