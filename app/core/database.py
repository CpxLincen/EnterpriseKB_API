"""数据库初始化模块。

创建 SQLAlchemy 引擎与会话工厂，定义声明式基类 Base，
并提供初始化 pgvector 扩展和数据表的 init_db()。

调用关系：
- 模块加载时调用 settings.database_url()，据此生成 engine 与 SessionLocal
- init_db() ← 被 cli.py（init-db/ingest/ask 执行前）与 api.py（lifespan 启动时）调用
- Base      ← 被 models.py 继承，是三张表 ORM 模型的声明式基类
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.core.config import database_url

# 创建数据库引擎；pool_pre_ping 在每次取连接前先探测连接是否有效，
# 避免数据库重启后遗留的失效连接导致报错
# connect_args 的 connect_timeout 让数据库不可达时快速失败（默认无限等待，
# 评测/启动脚本会因此卡住），仅对 psycopg 生效，不影响其他驱动。
engine = create_engine(
    database_url(),
    pool_pre_ping=True,
    connect_args={"connect_timeout": 10},
)

# 会话工厂：后续所有数据库操作通过 SessionLocal() 打开会话；
# expire_on_commit=False 表示提交后对象属性仍可直接读取，无需重新查询
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """SQLAlchemy 2.0 声明式映射基类，所有数据模型都继承它。"""

    pass


def init_db() -> None:
    """初始化数据库：启用 pgvector 扩展并创建全部数据表（若不存在）。"""
    from app.models import audit as audit_models  # noqa: F401  # 审计日志表
    from app.models import auth as auth_models  # noqa: F401  # 认证表（users / knowledge_base_access）
    from app.models import conversation as conversation_models  # noqa: F401  # 会话/消息表
    from app.models import knowledge as knowledge_models  # noqa: F401  # 知识库/文档/文本块表
    from app.models import review as review_models  # noqa: F401  # 人工复核表

    with engine.begin() as connection:
        # 启用 pgvector 扩展（幂等操作，已存在则跳过）
        connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
    # 依据所有继承 Base 的模型创建缺失的数据表
    Base.metadata.create_all(engine)
    # 轻量迁移：create_all 不会给「已存在的表」新增列。这里幂等补齐
    # conversations 表后续新增的生命周期字段（status / archived_at）与索引，
    # 避免因直接改模型而导致旧库缺列报错。新库由 create_all 直接建好，此处为空操作。
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'"
        )
        connection.exec_driver_sql(
            "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ NULL"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_conversations_status ON conversations (status)"
        )
