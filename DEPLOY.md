# 企业知识库助手 —— Docker 部署说明

本文档说明如何把「后端 + 前端」打包成 Docker 镜像，并在其他服务器上部署使用。

## 1. 架构

```text
                        浏览器
                          │  http://<服务器IP>:8080
                          ▼
                 ┌────────────────┐
                 │    frontend    │  Nginx（静态文件 + 反代 API）
                 └───────┬────────┘
                         │  /knowledge-bases、/chat、/auth、/docs
                         ▼
                 ┌────────────────┐
                 │    backend     │  FastAPI（uvicorn，端口 8000，仅内网）
                 └───────┬────────┘
                         │  PostgreSQL + pgvector
                         ▼
                 ┌────────────────┐
                 │       db       │  pgvector/pgvector:pg16
                 └────────────────┘
```

- **frontend**：Nginx 托管前端静态文件，并把 `/knowledge-bases`、`/chat`、`/auth` 等请求
  反向代理到 `backend`。前端使用相对路径请求，因此**同源部署、无需 CORS**。
- **backend**：FastAPI 应用，启动时自动 `init_db`（创建 pgvector 扩展与数据表）；
  除健康检查/文档/登录端点外均需 Bearer Token，并按知识库级权限校验。
  对外不暴露端口，仅被 frontend 访问。
- **db**：PostgreSQL 16 + pgvector，数据保存在命名卷 `kb_db_data`。

## 2. 前置条件

- Docker（建议 24+）与 Docker Compose v2（`docker compose version` 可用）。
- 能访问外网（拉取基础镜像、pip / bun 依赖，以及调用 Qwen / DeepSeek 模型 API）。

## 3. 目录清单

| 文件 | 说明 |
|---|---|
| `E:\EnterpriseKB\Dockerfile` | 后端镜像 |
| `E:\EnterpriseKB\docker-compose.deploy.yml` | 三服务编排 |
| `E:\EnterpriseKB\deploy.env.example` | 环境变量模板 |
| `E:\EnterpriseKBWeb\Dockerfile` | 前端镜像（Bun 构建 + Nginx） |
| `E:\EnterpriseKBWeb\nginx.conf` | 前端反代配置 |

## 4. 部署方式 A：在目标服务器上直接构建并启动（推荐）

把整个目录（`E:\EnterpriseKB` 与 `E:\EnterpriseKBWeb` 两个目录，保持兄弟目录关系）
拷贝到服务器，例如放到 `/opt/kb/EnterpriseKB` 与 `/opt/kb/EnterpriseKBWeb`。

```bash
# 1) 准备环境变量（在 E:\EnterpriseKB 目录下，Linux 用对应路径）
cd /opt/kb/EnterpriseKB
cp deploy.env.example .env
vim .env          # 填写 QWEN_API_KEY（必填）、AUTH_JWT_SECRET（随机长串）、
                  # 保持 AUTH_DEV_MODE=false；如需 SSO 再填 OIDC_*

# 2) 一键构建并启动（首次会自动构建两个镜像 + 拉取 pgvector）
docker compose -f docker-compose.deploy.yml up -d --build

# 3) 查看状态
docker compose -f docker-compose.deploy.yml ps
docker compose -f docker-compose.deploy.yml logs -f backend
```

启动成功后访问：`http://<服务器IP>:8080`

## 5. 部署方式 B：本地打包镜像，传输到服务器

适用于服务器不能直接访问 Docker Hub / npm registry，或需要离线部署的场景。

### 5.1 在本地构建并导出

```bash
cd E:\EnterpriseKB
docker build -t enterprise-kb-backend:latest .
cd ..\EnterpriseKBWeb
docker build -t enterprise-kb-frontend:latest .

# 导出为 tar（两个镜像）
docker save enterprise-kb-backend:latest -o enterprise-kb-backend.tar
docker save enterprise-kb-frontend:latest -o enterprise-kb-frontend.tar
```

### 5.2 在服务器上导入并启动

```bash
docker load -i enterprise-kb-backend.tar
docker load -i enterprise-kb-frontend.tar

# 准备 .env（见第 4 步）后，用“离线版”编排启动
# 服务器上也需要一份 docker-compose.deploy.offline.yml（见 5.3）
docker compose -f docker-compose.deploy.offline.yml up -d
```

### 5.3 离线编排文件（不 build，直接用已导入的镜像）

服务器上新建 `docker-compose.deploy.offline.yml`：

```yaml
name: enterprisekb-deploy
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_DB: enterprise_kb
      POSTGRES_USER: enterprise_kb
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-enterprise_kb}
    volumes:
      - kb_db_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U enterprise_kb -d enterprise_kb"]
      interval: 5s
      timeout: 5s
      retries: 10
    restart: unless-stopped
  backend:
    image: enterprise-kb-backend:latest
    environment:
      DATABASE_URL: postgresql+psycopg://enterprise_kb:${POSTGRES_PASSWORD:-enterprise_kb}@db:5432/enterprise_kb
      QWEN_API_KEY: ${QWEN_API_KEY}
      DEEPSEEK_API_KEY: ${DEEPSEEK_API_KEY:-}
      AUTH_JWT_SECRET: ${AUTH_JWT_SECRET:-}
      AUTH_DEV_MODE: ${AUTH_DEV_MODE:-false}
      AUTH_TOKEN_TTL_MINUTES: ${AUTH_TOKEN_TTL_MINUTES:-480}
      SSO_PROVIDER: ${SSO_PROVIDER:-}
      OIDC_DISCOVERY_URL: ${OIDC_DISCOVERY_URL:-}
      OIDC_AUTHORIZATION_ENDPOINT: ${OIDC_AUTHORIZATION_ENDPOINT:-}
      OIDC_CLIENT_ID: ${OIDC_CLIENT_ID:-}
      OIDC_CLIENT_SECRET: ${OIDC_CLIENT_SECRET:-}
      OIDC_REDIRECT_URI: ${OIDC_REDIRECT_URI:-}
      CORS_ORIGINS: ${CORS_ORIGINS:-}
      MAX_UPLOAD_BYTES: ${MAX_UPLOAD_BYTES:-104857600}
      AUDIT_LOG_FILE: ${AUDIT_LOG_FILE:-}
    depends_on:
      db:
        condition: service_healthy
    restart: unless-stopped
  frontend:
    image: enterprise-kb-frontend:latest
    ports:
      - "${WEB_PORT:-8080}:80"
    depends_on:
      - backend
    restart: unless-stopped
volumes:
  kb_db_data:
```

> 注意：离线模式下 `db` 仍使用 `pgvector/pgvector:pg16` 镜像，需要该镜像也已在
> 服务器上存在。可在有网机器上先 `docker pull pgvector/pgvector:pg16` 再
> `docker save pgvector/pgvector:pg16 -o pgvector.tar` 一并传输导入。

## 6. 部署方式 C：推送到镜像仓库

```bash
docker tag enterprise-kb-backend:latest <registry>/<ns>/enterprise-kb-backend:1.0.0
docker tag enterprise-kb-frontend:latest <registry>/<ns>/enterprise-kb-frontend:1.0.0
docker push <registry>/<ns>/enterprise-kb-backend:1.0.0
docker push <registry>/<ns>/enterprise-kb-frontend:1.0.0
```

然后在服务器上用 5.3 的离线编排，把 `image:` 改成仓库地址即可。

## 7. 配置说明

| 环境变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `QWEN_API_KEY` | 是 | - | 千问 API Key，用于 Embedding 与默认聊天模型 |
| `DEEPSEEK_API_KEY` | 否 | - | 切换聊天模型为 DeepSeek 时需要 |
| `POSTGRES_PASSWORD` | 否 | `enterprise_kb` | 数据库密码 |
| `WEB_PORT` | 否 | `8080` | 前端对外端口 |
| `AUTH_JWT_SECRET` | 是(生产) | - | JWT 签名密钥，随机长字符串 |
| `AUTH_DEV_MODE` | 否 | `false` | 是否开启开发模式登录（生产必须 false） |
| `AUTH_TOKEN_TTL_MINUTES` | 否 | `480` | Token 有效期（分钟） |
| `SSO_PROVIDER` | 否 | - | 设为 `oidc` 启用 SSO |
| `OIDC_DISCOVERY_URL` 等 | 否 | - | SSO 所需配置（见 deploy.env.example） |
| `CORS_ORIGINS` | 否 | 空 | 跨域来源，逗号分隔；同源部署留空 |
| `MAX_UPLOAD_BYTES` | 否 | `104857600` | 上传大小上限（100MB） |
| `AUDIT_LOG_FILE` | 否 | - | 审计日志文件路径（留空仅输出 stdout） |

切换回答模型：编辑 `E:\EnterpriseKB\config\models.yaml` 的 `active_provider`
（`qwen` / `deepseek`）。Embedding 固定走 Qwen；更换 embedding 模型/维度后需重建知识库。

## 8. 验证部署

```bash
# 健康检查（公开）
curl http://<服务器IP>:8080/health

# 登录拿 Token（生产默认关闭开发登录，应走 /auth/sso/login；此处为示例）
TOKEN=$(curl -s -X POST http://<服务器IP>:8080/auth/login \
  -H "Content-Type: application/json" -d '{"username":"admin"}' | jq -r .access_token)

# 列出当前用户可访问的知识库
curl -H "Authorization: Bearer $TOKEN" http://<服务器IP>:8080/knowledge-bases

# Swagger 文档（经前端反代）
curl -I http://<服务器IP>:8080/docs

# 上传文档
curl -H "Authorization: Bearer $TOKEN" -F "file=@/path/to/xxx.md" \
  http://<服务器IP>:8080/knowledge-bases/default/documents

# 提问
curl -X POST http://<服务器IP>:8080/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"question":"年假如何计算？","knowledge_base":"hr"}'
```

> 首次部署后需创建管理员并授权知识库（在后端容器内执行，或经 SSH 到服务器本机）：
>
> ```bash
> docker compose -f docker-compose.deploy.yml exec backend python -m app.cli create-user admin --role admin
> docker compose -f docker-compose.deploy.yml exec backend python -m app.cli grant admin --knowledge-base hr --write
> ```

## 9. 数据持久化与备份

- 数据库数据保存在 Docker 卷 `kb_db_data` 中，重建容器不会丢数据。
- 备份：
  ```bash
  docker run --rm -v enterprisekb-deploy_kb_db_data:/data -v $(pwd):/backup \
    postgres:16-alpine tar czf /backup/kb_db_data.tar.gz -C /data .
  ```
- 恢复（先 `docker compose down`，清空卷后恢复）：
  ```bash
  docker run --rm -v enterprisekb-deploy_kb_db_data:/data -v $(pwd):/backup \
    postgres:16-alpine tar xzf /backup/kb_db_data.tar.gz -C /data
  ```

## 10. 常用运维命令

```bash
docker compose -f docker-compose.deploy.yml ps            # 查看状态
docker compose -f docker-compose.deploy.yml logs -f       # 查看全部日志
docker compose -f docker-compose.deploy.yml down          # 停止并删除容器（保留数据卷）
docker compose -f docker-compose.deploy.yml down -v       # 停止并删除容器+数据卷（清空数据）
docker compose -f docker-compose.deploy.yml restart backend # 重启后端
```

## 11. 注意事项

1. 后端已内置 JWT 认证与知识库级权限；生产环境务必设置 `AUTH_JWT_SECRET` 并保持
   `AUTH_DEV_MODE=false`，且不要直接暴露到公网，建议放在内网或前面加 HTTPS 网关。
2. 前端 Nginx 已设置 `client_max_body_size 200m`，支持较大 PDF 上传。
3. 模型调用需要服务器能访问 `dashscope.aliyuncs.com`（Qwen）或 `api.deepseek.com`。
4. 后端上传另有 `MAX_UPLOAD_BYTES`（默认 100MB）与扩展名白名单校验，请与 Nginx 的
   `client_max_body_size` 保持一致。
5. 审计日志默认输出到容器 stdout，可用 `AUDIT_LOG_FILE` 落盘或接入日志采集。
