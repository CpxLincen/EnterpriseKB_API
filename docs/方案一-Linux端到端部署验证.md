# 方案一 · Linux 端到端部署验证清单

> 目标：在线上 Linux 服务器把「后端 + 前端 + pgvector + BGE Rerank（宿主机卷挂载）」
> 完整跑起来，并验证 Rerank 确实生效（未降级）。适用 `docker compose` v2。

## 0. 前置条件

```bash
docker --version          # >= 24
docker compose version    # 需为 v2（compose 插件）
free -h                   # 内存建议 >= 8G（Rerank 模型约 2.3GB，CPU 推理更吃内存）
df -h                     # 磁盘预留 >= 20G（模型 + 两个镜像 + 数据库数据卷）
```

> **关于 GPU**：Dockerfile 默认装 PyPI 的 CPU 版 torch。**没有 GPU 也能跑通**，
> 只是 Rerank 用 CPU 推理、首次加载模型较慢、单次提问可能延迟几秒~几十秒，属正常。
> 若要 GPU 加速，需额外装 CUDA 版 torch 并在 compose 加 `gpus` 配置（见文末「可选」）。

> **一键验证脚本**：仓库提供 `scripts/verify-linux-deploy.sh`，可自动完成下方第 2~7 节
> （下载模型 → 生成 override → 构建启动 → 初始化 → 端到端验证）并输出 PASS/FAIL 汇总。
> 完整验证：`bash scripts/verify-linux-deploy.sh`；低配关 Rerank：`RERANK=0 bash scripts/verify-linux-deploy.sh`。

## 1. 放置代码（两个仓库必须为兄弟目录，目录名固定）

`docker-compose.deploy.yml` 的 frontend 构建上下文是 `../EnterpriseKBWeb`，
因此两个目录必须同名、同父目录：

```bash
mkdir -p /opt/kb && cd /opt/kb

# 方式 A：git clone（推荐）
git clone https://github.com/CpxLincen/EnterpriseKB_API.git EnterpriseKB
git clone <前端仓库地址> EnterpriseKBWeb

# 方式 B：scp 上传（无 git 时），保证解压后目录名为 EnterpriseKB / EnterpriseKBWeb
```

预期结构：

```text
/opt/kb/
├── EnterpriseKB/          # 后端（含 docker-compose.deploy.yml、scripts/、config/）
└── EnterpriseKBWeb/       # 前端（含 Dockerfile、nginx.conf、src/）
```

## 2. 下载 BGE Rerank 模型（约 2.3GB）

```bash
cd /opt/kb/EnterpriseKB

# 安装下载工具（二选一；国内推荐 ModelScope）
pip3 install modelscope
# 或：pip3 install -U huggingface_hub

# 下载到宿主机固定目录（推荐路径，与 override 默认一致）
./scripts/download-rerank-model.sh /opt/kb/models/bge-reranker-v2-m3
# 走 HuggingFace（自动用 hf-mirror 镜像）：
# ./scripts/download-rerank-model.sh /opt/kb/models/bge-reranker-v2-m3 huggingface
```

校验模型完整性（应能看到 `config.json`、`pytorch_model.bin` 或 `model.safetensors`、
tokenizer 相关文件）：

```bash
ls -lh /opt/kb/models/bge-reranker-v2-m3
du -sh /opt/kb/models/bge-reranker-v2-m3    # 约 2.3G
test -f /opt/kb/models/bge-reranker-v2-m3/config.json && echo "模型 OK" || echo "模型缺失"
```

## 3. 配置环境变量

```bash
cd /opt/kb/EnterpriseKB
cp deploy.env.example .env
vim .env
```

关键项：

| 变量 | 值 | 说明 |
|---|---|---|
| `QWEN_API_KEY` | `sk-你的密钥` | **必填**，Embedding + 默认聊天模型 |
| `AUTH_JWT_SECRET` | 随机长串 | **生产必填**；可用 `openssl rand -hex 32` 生成 |
| `AUTH_DEV_MODE` | `false` | 保持 false |
| `POSTGRES_PASSWORD` | `enterprise_kb` | 可改（改了无需其它改动，compose 内一致引用） |
| `WEB_PORT` | `8080` | 前端对外端口 |
| 其余 | 留空 | — |

生成随机密钥：

```bash
openssl rand -hex 32
```

## 4. 生成 Rerank 卷挂载 override（方案一）

```bash
cd /opt/kb/EnterpriseKB
cp docker-compose.deploy.rerank.example.yml docker-compose.deploy.rerank.yml
vim docker-compose.deploy.rerank.yml
```

确认内容（宿主机路径 = 第 2 步实际下载位置）：

```yaml
services:
  backend:
    environment:
      RERANK_MODEL_PATH: /models/bge-reranker-v2-m3   # 容器内路径，不要改成宿主机路径
    volumes:
      - /opt/kb/models/bge-reranker-v2-m3:/models/bge-reranker-v2-m3:ro
```

## 5. 构建并启动（首次构建约数分钟）

```bash
cd /opt/kb/EnterpriseKB
docker compose -f docker-compose.deploy.yml -f docker-compose.deploy.rerank.yml up -d --build
docker compose -f docker-compose.deploy.yml -f docker-compose.deploy.rerank.yml ps
```

预期三个服务均 `running` / `healthy`：`db`（healthy）、`backend`、`frontend`。

## 6. 初始化管理员、导入示例文档、授权

```bash
cd /opt/kb/EnterpriseKB
C="docker compose -f docker-compose.deploy.yml -f docker-compose.deploy.rerank.yml"

$C exec backend python -m app.cli create-user admin --role admin
# 后端镜像只含 app/ 与 config/，examples/ 需先复制进容器
$C cp examples/employee-handbook.md backend:/tmp/employee-handbook.md
$C exec backend python -m app.cli ingest /tmp/employee-handbook.md --knowledge-base hr
$C exec backend python -m app.cli grant admin --knowledge-base hr --write
```

> 顺序说明：`ingest` 会自动创建知识库 `hr`；`grant` 要求知识库已存在，
> 因此放在 `ingest` 之后执行。

## 7. 端到端验证

### 7.1 健康/就绪探针

```bash
curl http://127.0.0.1:8080/health    # 期望 {"status":"ok"}
curl http://127.0.0.1:8080/ready     # 期望 {"status":"ready"}
```

### 7.2 提问（CLI，不经过 HTTP 认证，直接触发检索 + Rerank）

```bash
$C exec backend python -m app.cli ask '年假如何计算？' --knowledge-base hr
```

期望：返回带 `[1]` 引用标记的回答 + 引用列表（文件名、页码、块序号）。

### 7.3 判定 Rerank 是否生效（关键）

```bash
$C logs backend | grep -i rerank
```

- **生效**：日志里**没有** `Rerank 不可用，本次及后续将降级为混合检索` 这类 warning。
- **降级（问题）**：出现了该 warning，说明模型没挂上，按第 8 节排查。

辅助确认模型在容器内可见、配置生效：

```bash
$C exec backend ls /models/bge-reranker-v2-m3          # 应看到 config.json 等
$C exec backend python -c "from app.core.config import retrieval_config as r; print(r().rerank_model, r().rerank_enabled)"
# 期望输出：/models/bge-reranker-v2-m3 True
```

> 首次提问会加载 2.3GB 模型到内存：backend 内存明显上升、首次延迟较长属正常。

## 8. 常见问题排查

| 现象 | 排查 |
|---|---|
| `Rerank 不可用...降级` warning | ① override 是否与 `-f` 一起传；② 宿主机路径是否真实存在模型；③ `RERANK_MODEL_PATH` 是否指向容器内路径；④ `exec backend ls /models/...` 是否有文件 |
| `Permission denied` 读模型 | 模型目录权限给读：`chmod -R a+r /opt/kb/models/bge-reranker-v2-m3` |
| backend 起不来 / 连不上 db | `$C logs backend` 看 DATABASE_URL；确认 db healthy 后再看 backend |
| `/ready` 失败 | db 容器未 healthy，等 `$C ps` 显示 healthy |
| 端口 8080 被占 | 改 `.env` 的 `WEB_PORT` |
| 提问返回「未找到依据」 | 先确认第 6 步 ingest 成功（输出 chunks 数 > 0） |

## 9. 可选项：GPU 加速 Rerank

1. 后端镜像改装 CUDA 版 torch（`requirements-rerank.txt` 加 `--index-url https://download.pytorch.org/whl/cu124`）；
2. compose 的 backend 加：
   ```yaml
   deploy:
     resources:
       reservations:
         devices: [{ driver: nvidia, count: 1, capabilities: [gpu] }]
   ```
3. 宿主机装 nvidia-container-toolkit。

> 无 GPU 也能完整验证「方案一」本身；GPU 只是性能问题，不阻塞验证。

## 10. 低配服务器（2G 内存 / 50G SSD）适配

**结论**：2G 内存**跑不动本地 BGE Rerank**（模型 2.3GB，CPU 加载即需 ~2.5GB+ 内存，
必然 OOM）；50G SSD 磁盘足够。建议**关闭本地 Rerank，退回混合检索（dense+BM25+RRF）**。

### 10.1 低配部署步骤（替代上文第 2 / 4 / 5 步）

`.env` 关键设置：

```bash
INSTALL_RERANK=false     # 镜像内不装 torch（省 ~1.5GB+ 镜像体积与构建时间）
RERANK_ENABLED=false     # 运行时跳过 Rerank 分支，退回混合检索
# RERANK_MODEL_PATH 留空即可（无需下载 2.3GB 模型）
```

启动（**不需要** `-f docker-compose.deploy.rerank.yml`，也不需要下载模型）：

```bash
cd /opt/kb/EnterpriseKB
docker compose -f docker-compose.deploy.yml up -d --build
docker compose -f docker-compose.deploy.yml ps
```

用户初始化 / 文档导入 / 提问验证与第 6 / 7 节相同（compose 文件名不带 rerank override）。

### 10.2 内存与磁盘预期

- 关 Rerank 后三服务合计约 1~1.5GB 内存：PostgreSQL + FastAPI（单 worker）+ Nginx；
- **强烈建议给宿主机加 swap**（4G）：
  ```bash
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  ```
- 若 db 启动报内存不足，可在 compose 给 db 加：
  ```yaml
  command: postgres -c shared_buffers=64MB -c max_connections=20 -c work_mem=2MB
  ```

### 10.3 取舍

- 少一层 BGE 精排，检索精度略降；防幻觉门禁退化为「稠密距离单阈值」；
- 2G 内存只适合**小规模 / 验证**；正式生产建议内存 ≥8G，或把 Rerank 换成云端 API（后续可选改造）。
