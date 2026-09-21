#!/usr/bin/env bash
# 方案一 Linux 部署辅助脚本：前置检查 → 下载模型 → 生成 override → 构建启动。
# 用法（在 EnterpriseKB 目录下）：
#   ./scripts/deploy-linux-rerank.sh                            # 默认模型放 /opt/kb/models/bge-reranker-v2-m3，走 ModelScope
#   ./scripts/deploy-linux-rerank.sh /data/models/rerank        # 指定宿主机模型目录
#   MODEL_SOURCE=huggingface ./scripts/deploy-linux-rerank.sh   # 走 HuggingFace 下载
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_HOST_DIR="${1:-/opt/kb/models/bge-reranker-v2-m3}"
MODEL_CONTAINER_DIR="/models/bge-reranker-v2-m3"
COMPOSE="docker compose -f docker-compose.deploy.yml -f docker-compose.deploy.rerank.yml"

echo "==> 前置检查"
for cmd in docker pip3; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "缺少命令：$cmd"; exit 1; }
done
docker compose version >/dev/null 2>&1 || { echo "需要 docker compose v2（插件）"; exit 1; }

echo "==> 检查 .env"
if [ ! -f "$REPO_DIR/.env" ]; then
  cp "$REPO_DIR/deploy.env.example" "$REPO_DIR/.env"
  echo "已生成 $REPO_DIR/.env；请先编辑填写 QWEN_API_KEY / AUTH_JWT_SECRET 后重新运行。"
  exit 1
fi
if ! grep -qE '^QWEN_API_KEY=.+' "$REPO_DIR/.env" || grep -qE '^QWEN_API_KEY=(sk-你的千问密钥|)$' "$REPO_DIR/.env"; then
  echo "请在 $REPO_DIR/.env 填写有效的 QWEN_API_KEY"; exit 1
fi
if grep -qE '^AUTH_JWT_SECRET=$' "$REPO_DIR/.env"; then
  echo "提示：生产建议填写 AUTH_JWT_SECRET（openssl rand -hex 32）"; fi

echo "==> 下载 BGE Rerank 模型（约 2.3GB）"
"$REPO_DIR/scripts/download-rerank-model.sh" "$MODEL_HOST_DIR" "${MODEL_SOURCE:-modelscope}"

echo "==> 校验模型完整性"
if [ ! -f "$MODEL_HOST_DIR/config.json" ]; then
  echo "模型目录缺 config.json：$MODEL_HOST_DIR"; exit 1
fi

echo "==> 生成 override：$REPO_DIR/docker-compose.deploy.rerank.yml"
cat > "$REPO_DIR/docker-compose.deploy.rerank.yml" <<EOF
# 由 scripts/deploy-linux-rerank.sh 生成（可手动调整）
services:
  backend:
    environment:
      RERANK_MODEL_PATH: $MODEL_CONTAINER_DIR
    volumes:
      - $MODEL_HOST_DIR:$MODEL_CONTAINER_DIR:ro
EOF

echo "==> 构建并启动"
cd "$REPO_DIR"
$COMPOSE up -d --build
$COMPOSE ps

echo ""
echo "==> 后续手动步骤："
echo "1) 初始化管理员："
echo "   $COMPOSE exec backend python -m app.cli create-user admin --role admin"
echo "2) 导入示例文档："
echo "   $COMPOSE exec backend python -m app.cli ingest examples/employee-handbook.md --knowledge-base hr"
echo "3) 提问并验证 rerank（日志应无 'Rerank 不可用' 降级 warning）："
echo "   $COMPOSE exec backend python -m app.cli ask '年假如何计算？' --knowledge-base hr"
echo "   $COMPOSE logs backend | grep -i rerank"
