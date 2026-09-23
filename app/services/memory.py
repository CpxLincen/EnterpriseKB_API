"""会话内短期记忆（方案 D：查询改写 + 短窗口历史 + 引用复用判定）。

职责：在会话内每条新消息到来时，结合该会话最近的历史，把「当前问题」规划为
三类执行计划（TurnPlan）之一，并输出改写后的检索问题：

  - new      ：与历史无关的全新问题（或首问）→ 用原问题重新检索；
  - followup ：承接前文、省略/指代了前文实体、需要新证据 → 用改写后的完整问题重新检索；
  - reuse    ：不引入新证据需求，仅对上一轮回答本身做加工（展开/举例/总结/翻译等）
               → 不检索，复用上一轮已落库的 context_chunks 与 citations 作答。

「何时复用引用」的判定规则见 `项目技术文档.md`「记忆策略与方案」章节第 6.3 节，核心判据是
「是否引入了新的证据需求」，并辅以三条不依赖 LLM 的确定性兜底。

调用关系：被 app/routers/conversations.py 的会话内问答（非流式/流式）调用；
依赖 providers.py（chat）、config.py（memory_config）、models/conversation.py。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import ProviderConfig, memory_config
from app.models.conversation import ConversationMessage
from app.services.providers import OpenAICompatibleProvider


MEMORY_NEW = "new"
MEMORY_FOLLOWUP = "followup"
MEMORY_REUSE = "reuse"


_REWRITE_SYSTEM = """你是企业知识库助手的「对话记忆」模块。请根据下面的历史对话，判断用户当前问题属于哪一类，并输出改写后的检索问题。

三类定义：
- new：与历史无关的全新问题（或本会话首问）。query = 原问题（可修正错别字）。
- followup：承接前文的追问，省略或指代了前文实体，需要新的检索证据才能回答。query = 补全指代后的完整独立问题。
- reuse：不引入新证据需求，仅对上一轮回答本身做加工（如：展开讲讲、再详细点、举例说明、总结、翻译、换个说法、解释其中某一条）。query = 空字符串。

判定 reuse 的唯一标准：只用「上一轮已经检索到的资料」就能回答当前问题。
只要引入了新的事实、对象、金额、条件、时间等新证据需求，就应判为 followup（需要重新检索），而不是 reuse。

仅输出一个 JSON 对象，不要输出任何其他文字，不要用代码块包裹：
{"type":"new|followup|reuse","query":"改写后的检索问题（reuse 时为空字符串）"}
"""


@dataclass
class TurnPlan:
    """一条会话内消息的执行计划：检索方式 + 改写问题 +（reuse 时）上一轮证据。"""

    type: str  # new / followup / reuse
    query: str  # 实际用于检索的问题（new=原问题，followup=改写问题，reuse=空）
    reuse_chunks: list[dict] | None = None  # reuse：上一轮已检索到的完整文本块（context_chunks）
    reuse_decision: str | None = None  # reuse：沿用上一轮的门禁判定
    history: list[dict] = field(default_factory=list)  # 本次回看的历史（role/content，供调试/审计）


def _recent_messages(
    session: Session,
    conversation_id: int,
    limit: int,
    before_id: int | None = None,
) -> list[dict]:
    """返回会话内最近 `limit` 条消息（时间正序），排除 `before_id` 及之后的记录。"""
    stmt = select(ConversationMessage).where(
        ConversationMessage.conversation_id == conversation_id
    )
    if before_id is not None:
        stmt = stmt.where(ConversationMessage.id < before_id)
    rows = (
        session.execute(stmt.order_by(ConversationMessage.id.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


def _last_assistant(session: Session, conversation_id: int) -> ConversationMessage | None:
    """返回会话内最近一条 assistant 消息（用于复用上一轮证据）。"""
    return session.execute(
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.role == "assistant",
        )
        .order_by(ConversationMessage.id.desc())
        .limit(1)
    ).scalars().first()


def _history_text(history: list[dict]) -> str:
    """把历史消息格式化为给改写模型的文本。"""
    lines = []
    for item in history:
        who = "用户" if item["role"] == "user" else "助手"
        lines.append(f"{who}：{item['content']}")
    return "\n".join(lines)


def _parse_rewrite(raw: str) -> dict:
    """从 LLM 返回文本中稳健解析 JSON 对象；失败返回空 dict（由调用方兜底降级）。"""
    if not raw:
        return {}
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rewrite(chat_config: ProviderConfig, history: list[dict], question: str) -> dict:
    """调用聊天模型做一次查询改写/意图判定，返回 {type, query}；失败返回空 dict。"""
    user_prompt = f"历史对话：\n{_history_text(history)}\n\n当前问题：{question}"
    try:
        raw = OpenAICompatibleProvider(chat_config).chat(_REWRITE_SYSTEM, user_prompt)
    except Exception:  # noqa: BLE001 - 改写失败不阻断问答，降级为单轮
        return {}
    return _parse_rewrite(raw)


def _chunk_key(item: dict) -> tuple:
    """证据块去重键：同一知识库内「同文件 + 同块序号」视为同一块。"""
    return (
        item.get("knowledge_base", ""),
        item.get("filename", ""),
        int(item.get("chunk_index", 0)),
    )


def merge_chunk_lists(chunk_lists: list[list[dict]]) -> list[dict]:
    """合并多轮证据块并按块去重，保持「最近一轮在前」的传入顺序。"""
    merged: list[dict] = []
    seen: set[tuple] = set()
    for chunk_list in chunk_lists:
        for item in chunk_list or []:
            if not isinstance(item, dict):
                continue
            key = _chunk_key(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _recent_evidence_windows(session: Session, conversation_id: int, rounds: int) -> list[list[dict]]:
    """返回最近 `rounds` 条 assistant 消息的 context_chunks（最新在前，空值跳过）。"""
    rows = (
        session.execute(
            select(ConversationMessage)
            .where(
                ConversationMessage.conversation_id == conversation_id,
                ConversationMessage.role == "assistant",
            )
            .order_by(ConversationMessage.id.desc())
            .limit(rounds)
        )
        .scalars()
        .all()
    )
    return [m.context_chunks for m in rows if m.context_chunks]


def build_turn_plan(
    session: Session,
    conversation_id: int,
    question: str,
    chat_config: ProviderConfig,
    before_id: int | None = None,
) -> TurnPlan:
    """为会话内的一条新消息生成执行计划（查询改写 + 引用复用判定）。

    参数:
        before_id: 当前用户消息的 id。历史仅回看 id 小于它的消息，
                   避免把「当前问题本身」也当作历史喂给改写模型。
    """
    cfg = memory_config()
    if not cfg.enabled:
        return TurnPlan(type=MEMORY_NEW, query=question)

    history = _recent_messages(session, conversation_id, cfg.max_history_messages, before_id=before_id)
    # 确定性兜底 1：无历史（首问）→ new
    if not history:
        return TurnPlan(type=MEMORY_NEW, query=question, history=history)

    parsed = _rewrite(chat_config, history, question)
    plan_type = parsed.get("type") if parsed.get("type") in {MEMORY_NEW, MEMORY_FOLLOWUP, MEMORY_REUSE} else MEMORY_NEW
    query = (parsed.get("query") or "").strip()

    if plan_type == MEMORY_REUSE:
        previous = _last_assistant(session, conversation_id)
        if previous is None or not previous.context_chunks:
            # 确定性兜底 2：最近一轮无证据可复用 → 降级为 followup 重新检索
            plan_type = MEMORY_FOLLOWUP
            query = query or question
        else:
            # 方案 A：证据窗口——不只复用上一轮，而是合并最近 N 轮的检索块（按块去重），
            # 以支持「对比 / 综合上面几轮」类追问。
            windows = _recent_evidence_windows(session, conversation_id, cfg.evidence_window_rounds)
            merged = merge_chunk_lists(windows)
            return TurnPlan(
                type=MEMORY_REUSE,
                query="",
                reuse_chunks=merged,
                reuse_decision=previous.decision or "answer",
                history=history,
            )
    elif plan_type == MEMORY_FOLLOWUP:
        query = query or question
    else:
        # new（或解析失败降级）→ 直接用原问题，保证检索行为稳定
        plan_type = MEMORY_NEW
        query = question

    return TurnPlan(type=plan_type, query=query, history=history)
