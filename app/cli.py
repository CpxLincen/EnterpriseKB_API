"""命令行工具模块（Typer CLI）。

提供三个命令：
- init-db  初始化数据库（pgvector 扩展 + 数据表）
- ingest   导入文档到指定知识库
- ask      向知识库提问并打印回答与引用

调用关系（命令行入口：python -m app.cli <命令>）：
- 每个命令都先 init_db() 确保 pgvector 扩展与数据表存在
- ingest → ingestion.ingest_file()（Embedding 走 get_provider(for_embeddings=True)）
- ask    → rag.ask()（聊天模型走 get_provider()，Embedding 走 get_provider(for_embeddings=True)）
"""

from pathlib import Path

import typer
from sqlalchemy import select

from app.models.auth import KnowledgeBaseAccess, User
from app.core.database import SessionLocal, init_db
from app.services.ingestion import ingest_file_detailed, rebuild_knowledge_base
from app.models.knowledge import KnowledgeBase
from app.services.rag import ask
from app.core.config import get_provider

# 创建 Typer 应用，--help 会显示中文简介
cli = typer.Typer(help="企业知识库助手命令行工具")


@cli.command("init-db")
def initialize_database() -> None:
    """初始化数据库：创建 pgvector 扩展与全部数据表。"""
    init_db()
    typer.echo("Database initialized.")


@cli.command()
def ingest(path: Path, knowledge_base: str = typer.Option("default", "--knowledge-base", "-k")) -> None:
    """导入一个文档到指定知识库（默认知识库名为 default）。

    Embedding 使用配置中的 Embedding 供应商（如 Qwen）。
    """
    init_db()  # 确保数据表已存在
    warnings_list: list[str] = []
    try:
        with SessionLocal() as session:
            # get_provider(for_embeddings=True) 取得 Embedding 配置
            count, stats = ingest_file_detailed(
                session,
                path,
                knowledge_base,
                get_provider(for_embeddings=True),
                warnings_out=warnings_list,
            )
    except (ValueError, RuntimeError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"Imported {count} chunks into knowledge base '{knowledge_base}'.")
    meta = [f"text={stats.text_chunks}", f"table={stats.table_chunks}", f"parse={stats.parse_ms:.0f}ms"]
    if stats.skipped_pages:
        meta.append(f"skipped_pages={stats.skipped_pages}")
    if stats.ocr_pages:
        meta.append(f"ocr_pages={stats.ocr_pages}")
    typer.echo(f"  [{', '.join(meta)}]")
    for warning in warnings_list:
        typer.echo(f"WARNING: {warning}", err=True)


@cli.command("reingest")
def reingest_command(
    source_dir: Path = typer.Argument(..., help="源文档目录（按文件名匹配替换同名文档）"),
    knowledge_base: str = typer.Option(..., "--knowledge-base", "-k", help="知识库名"),
) -> None:
    """按当前解析策略重导知识库（保持 Embedding 配置不变，逐文件替换同名文档）。"""
    init_db()
    warnings_list: list[str] = []
    try:
        result = rebuild_knowledge_base(
            knowledge_base,
            source_dir,
            get_provider(for_embeddings=True),
            replace_embedding=False,
            warnings_out=warnings_list,
        )
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"Reingested '{knowledge_base}': {result.processed} files, {result.chunks} chunks."
    )
    for warning in warnings_list:
        typer.echo(f"WARNING: {warning}", err=True)
    for error in result.errors:
        typer.echo(f"ERROR: {error}", err=True)
    if result.errors:
        raise typer.Exit(1)


@cli.command("rebuild")
def rebuild_command(
    source_dir: Path = typer.Argument(..., help="源文档目录（目录即知识库的完整真相）"),
    knowledge_base: str = typer.Option(..., "--knowledge-base", "-k", help="知识库名"),
) -> None:
    """按当前 Embedding 与解析配置重建知识库索引（清空后从目录全量重导）。"""
    init_db()
    warnings_list: list[str] = []
    try:
        result = rebuild_knowledge_base(
            knowledge_base,
            source_dir,
            get_provider(for_embeddings=True),
            replace_embedding=True,
            warnings_out=warnings_list,
        )
    except RuntimeError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        f"Rebuilt '{knowledge_base}': {result.processed} files, {result.chunks} chunks."
    )
    for warning in warnings_list:
        typer.echo(f"WARNING: {warning}", err=True)
    for error in result.errors:
        typer.echo(f"ERROR: {error}", err=True)
    if result.errors:
        raise typer.Exit(1)


@cli.command("ask")
def ask_command(question: str, knowledge_base: str = typer.Option("default", "--knowledge-base", "-k")) -> None:
    """向指定知识库提问，打印回答与引用来源。

    第一个 get_provider() 取聊天模型，第二个取 Embedding 模型。
    """
    init_db()
    with SessionLocal() as session:
        answer, citations = ask(session, question, knowledge_base, get_provider(), get_provider(for_embeddings=True))
    typer.echo(f"\n{answer}\n")
    if citations:
        typer.echo("Sources:")
        for index, citation in enumerate(citations, 1):
            typer.echo(f"[{index}] {citation.filename} | page {citation.page_number or '-'} | {citation.excerpt}")


@cli.command("create-user")
def create_user(
    username: str,
    role: str = typer.Option("user", help="角色：user 或 admin"),
    display_name: str = typer.Option(None, "--display-name", help="显示名（缺省用用户名）"),
    email: str = typer.Option(None, help="邮箱"),
) -> None:
    """创建或更新本地用户（供认证中间件识别）。"""
    if role not in {"user", "admin"}:
        typer.echo("role 必须是 'user' 或 'admin'", err=True)
        raise typer.Exit(1)
    init_db()
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.username == username))
        if user:
            user.role = role
            if display_name is not None:
                user.display_name = display_name
            if email is not None:
                user.email = email
            session.commit()
            typer.echo(f"Updated user '{username}' (role={user.role}).")
        else:
            user = User(
                username=username,
                display_name=display_name or username,
                email=email,
                role=role,
                is_active=True,
            )
            session.add(user)
            session.commit()
            typer.echo(f"Created user '{username}' (role={user.role}).")


@cli.command("grant")
def grant_access(
    username: str,
    knowledge_base: str = typer.Option(..., "--knowledge-base", "-k", help="知识库名"),
    write: bool = typer.Option(False, "--write", help="是否授予写权限"),
    read: bool = typer.Option(True, "--read/--no-read", help="是否授予读权限"),
) -> None:
    """授予/更新用户对某个知识库的访问权限。"""
    init_db()
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.username == username))
        if not user:
            typer.echo(f"用户 '{username}' 不存在，请先 create-user。", err=True)
            raise typer.Exit(1)
        kb = session.scalar(select(KnowledgeBase).where(KnowledgeBase.name == knowledge_base))
        if not kb:
            typer.echo(f"知识库 '{knowledge_base}' 不存在（请先导入文档创建）。", err=True)
            raise typer.Exit(1)
        access = session.scalar(
            select(KnowledgeBaseAccess).where(
                KnowledgeBaseAccess.user_id == user.id,
                KnowledgeBaseAccess.knowledge_base_id == kb.id,
            )
        )
        if access:
            access.can_read = read
            access.can_write = write
        else:
            session.add(
                KnowledgeBaseAccess(
                    user_id=user.id,
                    knowledge_base_id=kb.id,
                    can_read=read,
                    can_write=write,
                )
            )
        session.commit()
        typer.echo(f"Granted '{username}' -> '{knowledge_base}': read={read}, write={write}.")


@cli.command("list-users")
def list_users() -> None:
    """列出本地用户及其知识库授权情况。"""
    init_db()
    with SessionLocal() as session:
        users = session.scalars(select(User).order_by(User.id)).all()
        for user in users:
            accesses = session.scalars(
                select(KnowledgeBaseAccess).where(KnowledgeBaseAccess.user_id == user.id)
            ).all()
            grants = []
            for access in accesses:
                kb = session.get(KnowledgeBase, access.knowledge_base_id)
                name = kb.name if kb else str(access.knowledge_base_id)
                perms = "rw" if access.can_write else ("r" if access.can_read else "-")
                grants.append(f"{name}({perms})")
            typer.echo(f"{user.username}  role={user.role}  active={user.is_active}  grants={', '.join(grants) or '-'}")


@cli.command("retention")
def retention_command() -> None:
    """执行会话保留策略：自动归档长期不活跃会话，并永久删除过期归档会话。

    阈值来自环境变量 CONVERSATION_ARCHIVE_DAYS / CONVERSATION_RETENTION_DAYS；
    未配置（或 ≤0）的步骤会自动跳过。可挂到系统定时任务定期执行。
    """
    from app.services.conversation import apply_retention

    init_db()
    result = apply_retention()
    typer.echo(
        f"retention done: archived={result['archived']} deleted={result['deleted']} "
        f"(archive_days={result['archive_days']}, retention_days={result['retention_days']})"
    )


@cli.command("eval")
def eval_command(
    eval_set: Path = typer.Option("eval/hr-eval.yaml", "--eval-set", help="评测集 YAML 文件路径"),
    judge: bool = typer.Option(False, "--judge", help="启用 LLM 裁判做语义判分（额外消耗 API）"),
    json_out: Path = typer.Option(None, "--json", help="将逐题结果写入 JSON 文件"),
    markdown_out: Path = typer.Option(None, "--markdown", help="将报告写入 Markdown 文件"),
) -> None:
    """运行知识库问答评测集，输出检索命中 / 事实覆盖 / 拒答正确率。"""
    from app.services.eval import load_eval_set, print_report, run_eval, summarize, write_json, write_markdown

    kb_name, cases = load_eval_set(eval_set)
    results = run_eval(kb_name, cases, get_provider(), get_provider(for_embeddings=True), judge=judge)
    summary = summarize(results)
    print_report(results, summary)
    if json_out is not None:
        write_json(json_out, results, summary)
    if markdown_out is not None:
        write_markdown(markdown_out, results, summary)


if __name__ == "__main__":
    cli()
