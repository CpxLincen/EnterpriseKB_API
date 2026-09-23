#!/usr/bin/env bash
# ============================================================
# 企业知识库助手 —— Linux 服务器一次性验证脚本
#
# 目标：在线上 Linux 服务器把「后端 + 前端 + pgvector +（可选）BGE Rerank」
#       完整跑起来，并逐项验证：健康检查 / 入库 / 问答 / Rerank 是否生效，
#       最后输出 PASS / FAIL / WARN 汇总与退出码（0=全部通过，1=有失败）。
#
# 用法（在 EnterpriseKB 目录下，且 ../EnterpriseKBWeb 必须存在）：
#   bash scripts/verify-linux-deploy.sh                       # 完整验证（开 Rerank，自动下载模型）
#   RERANK=0 bash scripts/verify-linux-deploy.sh              # 低配快速验证（关 Rerank，混合检索）
#   SKIP_MODEL_DOWNLOAD=1 bash scripts/verify-linux-deploy.sh # 模型已就位，跳过下载
#   MODEL_SOURCE=huggingface bash scripts/verify-linux-deploy.sh
#   ADD_SWAP=0 bash scripts/verify-linux-deploy.sh            # 不自动加 swap
#
# 可配置环境变量：
#   RERANK               1=开 BGE Rerank（默认） 0=关（退回 dense+BM25+RRF）
#   MODEL_HOST_DIR       宿主机模型目录（默认 /opt/kb/models/bge-reranker-v2-m3）
#   MODEL_SOURCE         modelscope（默认）| huggingface
#   ADD_SWAP             1=root 且无 swap 时自动加 4G swap（默认 1）
#   SWAP_SIZE_MB         swap 大小（默认 4096）
#   SWAP_FILE            swap 文件路径（默认 /swapfile）
#   SKIP_MODEL_DOWNLOAD  1=跳过模型下载（模型已就位）
#   MAX_WAIT_HEALTH      等待 db healthy 秒数（默认 180）
#   KB_NAME              验证知识库名（默认 hr）
#   QUESTION             验证提问（默认「年假如何计算？」）
#
# 前置：QWEN_API_KEY 必填（Embedding + 默认聊天模型）；需能访问外网
#       （Docker Hub / PyPI / npm registry / dashscope / 模型下载源）。
# ============================================================
set -uo pipefail

# ---------- 可配置项 ----------
RERANK="${RERANK:-1}"
MODEL_HOST_DIR="${MODEL_HOST_DIR:-/opt/kb/models/bge-reranker-v2-m3}"
MODEL_CONTAINER_DIR="/models/bge-reranker-v2-m3"
MODEL_SOURCE="${MODEL_SOURCE:-modelscope}"
ADD_SWAP="${ADD_SWAP:-1}"
SWAP_SIZE_MB="${SWAP_SIZE_MB:-4096}"
SWAP_FILE="${SWAP_FILE:-/swapfile}"
SKIP_MODEL_DOWNLOAD="${SKIP_MODEL_DOWNLOAD:-0}"
MAX_WAIT_HEALTH="${MAX_WAIT_HEALTH:-180}"
KB_NAME="${KB_NAME:-hr}"
QUESTION="${QUESTION:-年假如何计算？}"
ASK_USER="${ASK_USER:-admin}"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="$REPO_DIR/../EnterpriseKBWeb"
ENV_FILE="$REPO_DIR/.env"

# ---------- 输出与统计 ----------
PASSED=0
FAILED=0
WARNINGS=0
info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
ok()   { PASSED=$((PASSED+1)); printf '\033[1;32m[PASS]\033[0m %s\n' "$*"; }
fail() { FAILED=$((FAILED+1)); printf '\033[1;31m[FAIL]\033[0m %s\n' "$*"; }
warn() { WARNINGS=$((WARNINGS+1)); printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FATAL]\033[0m %s\n' "$*" >&2; exit 2; }

# ---------- compose 命令（Rerank 模式叠加 override）----------
COMPOSE_FILES="-f docker-compose.deploy.yml"
if [ "$RERANK" = "1" ]; then
  COMPOSE_FILES="-f docker-compose.deploy.yml -f docker-compose.deploy.rerank.yml"
fi
COMPOSE="docker compose $COMPOSE_FILES"

# ---------- .env 读写辅助 ----------
set_env() {  # set_env <key> <value>：存在则替换，否则追加
  local key="$1" val="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}

get_env() {  # get_env <key>：读取某环境变量值（无则空）
  grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-
}

echo ""
echo "================================================================"
echo "  企业知识库助手 · Linux 服务器验证"
echo "  Rerank=${RERANK}  模型目录=${MODEL_HOST_DIR}  模型源=${MODEL_SOURCE}"
echo "================================================================"
echo ""

# ---------- 1. 前置检查 ----------
info "1/10 前置检查"
command -v docker >/dev/null 2>&1 || die "未找到 docker"
docker compose version >/dev/null 2>&1 || die "需要 docker compose v2（插件）。请运行 'docker compose version' 确认"
[ -f "$REPO_DIR/docker-compose.deploy.yml" ] || die "缺少 docker-compose.deploy.yml"
[ -d "$WEB_DIR" ] || die "未找到前端目录 $WEB_DIR（两个仓库必须为同名兄弟目录，见 docs/方案一-Linux端到端部署验证.md）"
ok "docker / compose v2 / 目录结构"

# ---------- 2. 资源自检 ----------
info "2/10 资源自检"
echo "  docker:    $(docker --version 2>/dev/null)"
echo "  compose:   $(docker compose version 2>/dev/null | head -1)"
echo "  内存:      $(awk '/MemTotal/{printf "%.1fG", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 未知)"
echo "  CPU:       $(nproc 2>/dev/null || echo 未知) 核"
echo "  磁盘:      $(df -h / 2>/dev/null | awk 'NR==2{print $4" 可用 / 共 "$2}')"
if command -v swapon >/dev/null 2>&1 && swapon --show 2>/dev/null | grep -q .; then
  echo "  swap:      已启用"
else
  echo "  swap:      无"
fi

MEM_TOTAL_MB=$(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || echo 0)
if [ "$MEM_TOTAL_MB" != "0" ] && [ "$MEM_TOTAL_MB" -lt 8192 ] && [ "$RERANK" = "1" ]; then
  warn "内存 ${MEM_TOTAL_MB}MB < 8G 且开 Rerank：偏紧。已尝试自动加 swap；如仍 OOM，请改 RERANK=0 验证混合检索"
fi

# ---------- 3. 网络自检（仅提示，不阻断）----------
info "3/10 网络自检（仅提示，不阻断）"
check_url() {
  local name="$1" url="$2"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$url" 2>/dev/null || true)
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    printf '    ok    %-24s (HTTP %s)\n' "$name" "$code"
  else
    printf '    WARN  %-24s 不可达\n' "$name"
  fi
}
if command -v curl >/dev/null 2>&1; then
  check_url "Docker Hub" "https://registry-1.docker.io/v2/"
  check_url "PyPI" "https://pypi.org/simple/"
  check_url "npm registry" "https://registry.npmjs.org/"
  check_url "Qwen/dashscope" "https://dashscope.aliyuncs.com/"
  check_url "DeepSeek" "https://api.deepseek.com/"
  check_url "ModelScope" "https://modelscope.cn/"
  check_url "HF 镜像" "https://hf-mirror.com/"
else
  warn "未安装 curl，跳过网络自检"
fi

# ---------- 4. swap（root 且无 swap 时自动加）----------
info "4/10 swap 处理"
if [ "$ADD_SWAP" = "1" ]; then
  if [ "$(id -u)" = "0" ]; then
    if swapon --show 2>/dev/null | grep -q .; then
      ok "已存在 swap，跳过"
    else
      info "创建 ${SWAP_SIZE_MB}MB swapfile：$SWAP_FILE"
      if fallocate -l "${SWAP_SIZE_MB}M" "$SWAP_FILE" 2>/dev/null \
         || dd if=/dev/zero of="$SWAP_FILE" bs=1M count="$SWAP_SIZE_MB" status=none 2>/dev/null; then
        chmod 600 "$SWAP_FILE"
        mkswap "$SWAP_FILE" >/dev/null
        swapon "$SWAP_FILE"
        grep -q "$SWAP_FILE" /etc/fstab || printf '%s none swap sw 0 0\n' "$SWAP_FILE" >> /etc/fstab
        ok "swap 已启用（$SWAP_FILE）"
      else
        warn "swap 创建失败；请按 docs/方案一 10.2 节手动加 swap"
      fi
    fi
  else
    warn "当前非 root，跳过自动加 swap（无 swap 开 Rerank 有 OOM 风险）"
  fi
else
  info "ADD_SWAP=0，跳过 swap 处理"
fi

# ---------- 5. 环境变量 ----------
info "5/10 准备 .env"
if [ ! -f "$ENV_FILE" ]; then
  cp "$REPO_DIR/deploy.env.example" "$ENV_FILE"
  info "已生成 .env（源自 deploy.env.example）"
fi

QWEN_KEY="$(get_env QWEN_API_KEY || true)"
case "$QWEN_KEY" in
  ""|"sk-你的千问密钥")
    die "请在 $ENV_FILE 填写有效的 QWEN_API_KEY（Embedding + 默认聊天模型必填）后重跑"
    ;;
esac

SECRET=""
if [ -z "$(get_env AUTH_JWT_SECRET || true)" ]; then
  SECRET="$(openssl rand -hex 32 2>/dev/null || (date +%s; head -c 64 /dev/urandom | od -An -tx1 | tr -d ' \n'))"
  set_env AUTH_JWT_SECRET "$SECRET"
  info "已自动生成 AUTH_JWT_SECRET"
fi

if [ "$RERANK" = "1" ]; then
  set_env INSTALL_RERANK true
  set_env RERANK_ENABLED true
else
  set_env INSTALL_RERANK false
  set_env RERANK_ENABLED false
fi
ok ".env 就绪（QWEN_API_KEY 已配置${SECRET:+，AUTH_JWT_SECRET 已生成}）"

# ---------- 6. 下载模型 ----------
info "6/10 BGE Rerank 模型"
if [ "$RERANK" = "1" ]; then
  if [ "$SKIP_MODEL_DOWNLOAD" = "0" ]; then
    info "下载模型（约 2.3GB）到 $MODEL_HOST_DIR"
    if ! "$REPO_DIR/scripts/download-rerank-model.sh" "$MODEL_HOST_DIR" "$MODEL_SOURCE"; then
      die "模型下载失败（可设 SKIP_MODEL_DOWNLOAD=1 跳过，但需保证模型已就位）"
    fi
  else
    info "SKIP_MODEL_DOWNLOAD=1，跳过下载"
  fi
  if [ -f "$MODEL_HOST_DIR/config.json" ]; then
    ok "模型就绪：$MODEL_HOST_DIR"
  else
    die "模型目录缺 config.json：$MODEL_HOST_DIR（请确认已下载完整）"
  fi
else
  info "RERANK=0：不需要模型"
fi

# ---------- 7. 生成 override ----------
info "7/10 生成 rerank override"
if [ "$RERANK" = "1" ]; then
  cat > "$REPO_DIR/docker-compose.deploy.rerank.yml" <<EOF
# 由 scripts/verify-linux-deploy.sh 自动生成（可手动调整）
services:
  backend:
    environment:
      RERANK_MODEL_PATH: $MODEL_CONTAINER_DIR
    volumes:
      - $MODEL_HOST_DIR:$MODEL_CONTAINER_DIR:ro
EOF
  ok "已生成 docker-compose.deploy.rerank.yml"
else
  info "RERANK=0：无需 override"
fi

# ---------- 8. 构建并启动 ----------
info "8/10 构建并启动服务（首次构建约数分钟）"
cd "$REPO_DIR"
$COMPOSE up -d --build || die "docker compose up 失败，请查看上方构建日志"

info "等待数据库 healthy（最多 ${MAX_WAIT_HEALTH}s）"
DB_OK=0
elapsed=0
while [ "$elapsed" -lt "$MAX_WAIT_HEALTH" ]; do
  h=$($COMPOSE ps --format '{{.Service}} {{.Health}}' 2>/dev/null | awk '$1=="db"{print $2}')
  if [ "$h" = "healthy" ]; then DB_OK=1; break; fi
  sleep 5
  elapsed=$((elapsed+5))
done
if [ "$DB_OK" = "1" ]; then
  ok "db 已 healthy"
else
  warn "db 未在 ${MAX_WAIT_HEALTH}s 内 healthy，继续验证（可能失败）"
  $COMPOSE logs --no-color db 2>/dev/null | tail -20 || true
fi
$COMPOSE ps

# ---------- 9. 初始化用户 / 导入 / 授权 ----------
info "9/10 初始化用户、导入示例、授权"
$COMPOSE exec -T backend env PYTHONUTF8=1 python -m app.cli create-user "$ASK_USER" --role admin || true

# 后端镜像只含 app/ 与 config/，examples/ 需先复制进容器再导入
$COMPOSE cp examples/employee-handbook.md backend:/tmp/employee-handbook.md >/dev/null 2>&1 || true
INGEST_OUT=$($COMPOSE exec -T backend env PYTHONUTF8=1 python -m app.cli ingest /tmp/employee-handbook.md --knowledge-base "$KB_NAME" 2>&1)
INGEST_RC=$?
echo "$INGEST_OUT" | tail -4
INGEST_N=$(echo "$INGEST_OUT" | grep -oE 'Imported [0-9]+ chunks' | grep -oE '[0-9]+' | head -1)
if [ "$INGEST_RC" = "0" ] && [ -n "$INGEST_N" ] && [ "$INGEST_N" -gt 0 ]; then
  ok "ingest 成功：Imported ${INGEST_N} chunks"
elif [ "$INGEST_RC" = "0" ] && [ "$INGEST_N" = "0" ]; then
  warn "ingest 返回 0 chunks：文档内容已存在（SHA-256 去重命中）或为空文件，未新增块"
else
  fail "ingest 失败（检查 Embedding API / 外网）"
fi

# 注意：grant 要求知识库已存在，故在 ingest 之后执行
$COMPOSE exec -T backend env PYTHONUTF8=1 python -m app.cli grant "$ASK_USER" --knowledge-base "$KB_NAME" --write || true
ok "用户 / 授权就绪"

# ---------- 10. 端到端验证 ----------
info "10/10 端到端验证"
WEB_PORT="$(get_env WEB_PORT || true)"
WEB_PORT="${WEB_PORT:-8080}"
BASE="http://127.0.0.1:${WEB_PORT}"

if curl -fsS --max-time 10 "$BASE/health" 2>/dev/null | grep -q '"ok"'; then
  ok "GET /health -> ok"
else
  fail "GET /health 失败或未返回 ok"
fi

if curl -fsS --max-time 10 "$BASE/ready" 2>/dev/null | grep -q '"ready"'; then
  ok "GET /ready -> ready"
else
  fail "GET /ready 失败"
fi

ASK_OUT=$($COMPOSE exec -T backend env PYTHONUTF8=1 python -m app.cli ask "$QUESTION" --knowledge-base "$KB_NAME" 2>&1)
ASK_RC=$?
echo "$ASK_OUT" | tail -8
if [ "$ASK_RC" = "0" ] && echo "$ASK_OUT" | grep -q 'Sources:'; then
  ok "CLI ask 返回回答与引用"
elif [ "$ASK_RC" = "0" ]; then
  warn "CLI ask 成功但未带引用（可能被防幻觉门禁判为无依据）"
else
  fail "CLI ask 失败"
fi

if [ "$RERANK" = "1" ]; then
  CFG_OUT=$($COMPOSE exec -T backend env PYTHONUTF8=1 python -c "from app.core.config import retrieval_config as r; print(r().rerank_model, r().rerank_enabled)" 2>/dev/null || true)
  info "retrieval_config: ${CFG_OUT}"
  RERANK_ON=$(echo "$CFG_OUT" | awk '{print $NF}')
  if [ "$RERANK_ON" != "True" ]; then
    fail "RERANK_ENABLED 未生效（retrieval_config 显示 '$RERANK_ON'，期望 'True'）。请确认 .env 的 RERANK_ENABLED=true 并 force-recreate backend"
  else
    DEGRADE_N=$($COMPOSE logs --no-color backend 2>/dev/null | grep -c 'Rerank 不可用' || true)
    if [ "${DEGRADE_N:-0}" -gt 0 ]; then
      fail "检测到 Rerank 降级（$DEGRADE_N 条 'Rerank 不可用' warning）：模型未挂上或加载失败"
    else
      ok "Rerank 已启用且未降级（${CFG_OUT}）"
    fi
  fi
else
  info "RERANK=0：跳过 Rerank 判定（混合检索 dense+BM25+RRF）"
fi

# ---------- 汇总 ----------
echo ""
echo "================================================================"
echo "  验证结果：PASS=$PASSED  FAIL=$FAILED  WARN=$WARNINGS"
if [ "$FAILED" = "0" ]; then
  echo "  结论：全部关键项通过 ✅"
  echo "  访问地址：http://<服务器IP>:${WEB_PORT}"
  echo "================================================================"
  exit 0
else
  echo "  结论：存在失败项 ❌，请根据上方 [FAIL] 逐项排查"
  echo "  排查日志：$COMPOSE logs --no-color backend"
  echo "================================================================"
  exit 1
fi
