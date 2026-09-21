#!/usr/bin/env bash
# 下载 BGE reranker 模型（约 2.3GB）到指定目录（Linux 服务器用）。
# 用法：
#   ./scripts/download-rerank-model.sh                              # 默认走 ModelScope，下载到 ./models/bge-reranker-v2-m3
#   ./scripts/download-rerank-model.sh /opt/kb/models/bge-reranker-v2-m3              # 指定目录
#   ./scripts/download-rerank-model.sh /opt/kb/models/bge-reranker-v2-m3 huggingface   # 走 HuggingFace
set -euo pipefail

TARGET="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/models/bge-reranker-v2-m3}"
SOURCE="${2:-modelscope}"

mkdir -p "$TARGET"

case "$SOURCE" in
  modelscope)
    if ! command -v modelscope >/dev/null 2>&1; then
      echo "未找到 modelscope CLI，请先安装：pip install modelscope" >&2
      exit 1
    fi
    modelscope download --model BAAI/bge-reranker-v2-m3 --local_dir "$TARGET"
    ;;
  huggingface)
    if ! command -v huggingface-cli >/dev/null 2>&1; then
      echo "未找到 huggingface-cli，请先安装：pip install -U huggingface_hub" >&2
      exit 1
    fi
    : "${HF_ENDPOINT:=https://hf-mirror.com}"
    export HF_ENDPOINT
    huggingface-cli download BAAI/bge-reranker-v2-m3 --local-dir "$TARGET"
    ;;
  *)
    echo "SOURCE 只能为 modelscope 或 huggingface" >&2
    exit 1
    ;;
esac

echo "完成。模型目录：$TARGET"
echo "请在部署 .env 设置 RERANK_MODEL_PATH 为容器内挂载路径（见 docker-compose.deploy.rerank.example.yml）。"
