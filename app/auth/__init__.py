"""认证与授权模块。

提供：
- models：User（用户）、KnowledgeBaseAccess（知识库级访问权限）
- security：JWT 签发与校验（PyJWT / HS256）
- dependencies：get_current_user / require_kb_access / ensure_kb_access
- middleware：HTTP 认证中间件（进入路由前校验 Bearer Token）
- sso：SSO（OIDC）登录的预留接口与配置
- routes：/auth/* 路由（开发登录、当前用户、SSO 预留、登出）
"""
