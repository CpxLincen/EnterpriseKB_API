"""评测集与评测工具模块（离线回归评测）。

对 RAG 问答做离线评测，回答三个核心问题：
1. 检索命中率（Citation Recall）：回答引用的文档里是否包含预期来源文件；
2. 事实覆盖（Fact Coverage）：回答是否包含预期关键事实（关键词/短句子串匹配）；
3. 拒答正确性（No-Answer Accuracy）：知识库本无依据的问题是否被正确拒答。

可选 LLM-as-Judge（--judge）：用聊天模型对「助手回答 vs 参考答案」做 0/1 语义判分，
用于校验语义等价但措辞不同的回答，额外消耗 API。

评测集格式与构建方法见 eval/README.md，示例见 eval/hr-eval.yaml。

命令行入口：
    python -m app.eval eval/hr-eval.yaml [--judge] [--json out.json] [--markdown out.md]
    python -m app.cli eval --eval-set eval/hr-eval.yaml [--judge]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from app.core.database import SessionLocal, init_db
from app.services.providers import OpenAICompatibleProvider
from app.services.rag import ask
from app.core.config import ROOT, get_provider

# 与 rag.py 的拒答文案保持一致，用于识别“未找到相关依据”的回答
NO_ANSWER_MARKER = "未找到相关依据"

# 评测集目录：存放所有 *.yaml / *.yml 评测集文件
EVAL_DIR = ROOT / "eval"


@dataclass
class EvalCase:
    """评测集里的一道题。"""

    id: str  # 题目唯一编号，如 hr-001
    question: str  # 用户问题
    reference_answer: str | None = None  # 参考答案（供 LLM 裁判使用）
    expected_sources: list[str] = field(default_factory=list)  # 预期命中的来源文件名
    expected_facts: list[str] = field(default_factory=list)  # 回答必须包含的关键事实（子串）
    expect_no_answer: bool = False  # 是否期望“知识库无依据、应拒答”
    category: str = "factual"  # 分类：factual / negative / ...（自由填写，仅用于统计分组）
    knowledge_base: str | None = None  # 该题使用的知识库；None 表示用评测集顶层的 knowledge_base
    expected_chunk: str | None = None  # 答案所在正确块的特征子串（用于块级检索命中与 hit@k/MRR）
    difficulty: str = "L1"  # 难度标签：L1 原文直问 / L2 同义改写 / L3 推理边界


@dataclass
class CaseResult:
    """一道题的评测结果。"""

    id: str
    category: str
    difficulty: str
    knowledge_base: str
    question: str
    answer: str
    citations: list[dict]
    is_no_answer: bool
    retrieval_hit: bool | None  # None=未配置预期来源，不参与该项统计
    expected_sources: list[str]
    fact_hit: bool | None  # None=未配置预期事实，不参与该项统计
    fact_matched: list[str]
    fact_missing: list[str]
    no_answer_correct: bool | None  # None=非“应拒答”题，不参与该项统计
    expected_chunk: str | None = None  # 该题标注的正确块特征子串
    chunk_hit: bool | None = None  # None=未配置 expected_chunk；否则是否命中正确块
    chunk_rank: int | None = None  # 正确块在 Top5 中的位次（1 起）；未命中为 None
    judge_score: int | None = None  # 仅 --judge 时有值
    judge_reason: str | None = None


def _reconfigure_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 Windows 控制台 GBK 乱码。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_eval_set(path: Path) -> tuple[str, list[EvalCase]]:
    """读取评测集 YAML，返回 (知识库名, 题目列表)。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError(f"Invalid eval set '{path}': expected a dict with a 'cases' list.")
    kb_name = str(data.get("knowledge_base", "default"))
    cases: list[EvalCase] = []
    for index, raw in enumerate(data["cases"]):
        if not isinstance(raw, dict) or "question" not in raw:
            raise ValueError(f"Case #{index} must be a dict with at least a 'question' field.")
        cases.append(
            EvalCase(
                id=str(raw.get("id") or f"case-{index + 1}"),
                question=str(raw["question"]),
                reference_answer=raw.get("reference_answer"),
                expected_sources=[str(s) for s in raw.get("expected_sources", [])],
                expected_facts=[str(s) for s in raw.get("expected_facts", [])],
                expect_no_answer=bool(raw.get("expect_no_answer", False)),
                category=str(raw.get("category", "factual")),
                knowledge_base=raw.get("knowledge_base"),
                expected_chunk=raw.get("expected_chunk"),
                difficulty=str(raw.get("difficulty", "L1")),
            )
        )
    return kb_name, cases


def _normalize(text: str) -> str:
    """去除全部空白、转小写，并做 NFKC 归一化，便于子串匹配。

    入库侧（app/services/ingestion.py 的 normalize_text）会把文本 NFKC 归一化，
    因此全角标点（如 “，” “（）”）在库里已变成半角。这里同样做 NFKC，
    保证 expected_chunk / expected_facts 与已入库文本在相同规则下比较。
    """
    return unicodedata.normalize("NFKC", re.sub(r"\s+", "", text)).lower()


def _fact_matching(answer: str, facts: list[str]) -> tuple[list[str], list[str]]:
    """返回 (命中的事实, 未命中的事实)。"""
    normalized = _normalize(answer)
    matched: list[str] = []
    missing: list[str] = []
    for fact in facts:
        (matched if _normalize(fact) in normalized else missing).append(fact)
    return matched, missing


JUDGE_SYSTEM = (
    "你是企业知识库助手的评测裁判。请判断助手回答是否包含参考答案的关键信息。"
    '只输出一行 JSON：{"score": 1 或 0, "reason": "一句话理由"}。不要输出任何其他内容。'
)


def judge_answer(chat_config, question: str, reference_answer: str, answer: str) -> tuple[int, str]:
    """用聊天模型对单个回答做 0/1 判分，返回 (得分, 理由)。"""
    user = (
        f"问题：{question}\n"
        f"参考答案：{reference_answer}\n"
        f"助手回答：{answer}\n"
        "请判断助手回答是否满足参考答案的关键信息（1=正确，0=错误），并给出理由。"
    )
    raw = OpenAICompatibleProvider(chat_config).chat(JUDGE_SYSTEM, user).strip()
    try:
        parsed = json.loads(raw)
        return int(parsed["score"]), str(parsed.get("reason", ""))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # 模型偶尔输出非严格 JSON（如 "1" 或 "score: 1"），做一次兜底解析
        match = re.search(r'"score"\s*:\s*([01])', raw)
        if match:
            return int(match.group(1)), raw[:200]
        return 0, f"judge 输出不可解析：{raw[:200]}"


def run_eval(
    kb_name: str,
    cases: list[EvalCase],
    chat_config,
    embedding_config,
    judge: bool = False,
    retrieval_mode: str | None = None,
    on_progress: Callable[[int, CaseResult], None] | None = None,
) -> list[CaseResult]:
    """逐题执行 RAG 问答并计算指标。

    参数:
        kb_name:          目标知识库名。
        cases:            评测题目列表。
        chat_config:      聊天模型配置（回答与可选 LLM 裁判共用）。
        embedding_config: Embedding 模型配置。
        judge:            是否启用 LLM-as-Judge。
        retrieval_mode:   检索方式（dense/hybrid/rerank）；None 表示用默认配置。
        on_progress:      每完成一道题时回调 (已完成的题数, 该题结果)，用于实时进度展示。
    返回:
        与 cases 一一对应的结果列表。
    """
    init_db()
    results: list[CaseResult] = []
    with SessionLocal() as session:
        for case in cases:
            kb = case.knowledge_base or kb_name  # 允许单题指定知识库，否则用评测集默认
            # 单题异常不应中断整场评测：记为异常回答，继续跑后续题目
            try:
                answer, citations, chunks = ask(
                    session, case.question, kb, chat_config, embedding_config, retrieval_mode,
                    return_chunks=True,
                )
            except Exception as exc:  # noqa: BLE001
                answer = f"<评测执行异常: {exc}>"
                citations = []
                chunks = []

            is_no_answer = (not citations) or (NO_ANSWER_MARKER in answer)
            cited_files = [c.filename for c in citations]

            # 1) 检索命中：预期来源文件中是否有任一出现在引用里（Top5）
            retrieval_hit: bool | None = None
            if case.expected_sources:
                retrieval_hit = any(src in cited_files for src in case.expected_sources)

            # 1.5) 块级检索：正确块（内容包含 expected_chunk）在 Top5 中的位次
            chunk_hit: bool | None = None
            chunk_rank: int | None = None
            if case.expected_chunk:
                needle = _normalize(case.expected_chunk)
                for rank, chunk in enumerate(chunks, start=1):
                    if needle and needle in _normalize(chunk.content):
                        chunk_rank = rank
                        break
                chunk_hit = chunk_rank is not None

            # 2) 事实覆盖：全部预期事实都命中才算通过（拒答时直接判失败）
            fact_matched: list[str] = []
            fact_missing: list[str] = []
            fact_hit: bool | None = None
            if case.expected_facts:
                if is_no_answer:
                    fact_missing = list(case.expected_facts)
                    fact_hit = False
                else:
                    fact_matched, fact_missing = _fact_matching(answer, case.expected_facts)
                    fact_hit = not fact_missing

            # 3) 拒答正确性：期望拒答的题，是否真的拒答
            no_answer_correct: bool | None = None
            if case.expect_no_answer:
                no_answer_correct = is_no_answer

            # 4) 可选 LLM 裁判（仅对有参考答案且已作答的题）
            judge_score: int | None = None
            judge_reason: str | None = None
            if judge and case.reference_answer and not is_no_answer:
                try:
                    judge_score, judge_reason = judge_answer(
                        chat_config, case.question, case.reference_answer, answer
                    )
                except Exception as exc:  # noqa: BLE001
                    judge_score, judge_reason = 0, f"judge 调用失败：{exc}"

            results.append(
                CaseResult(
                    id=case.id,
                    category=case.category,
                    difficulty=case.difficulty,
                    knowledge_base=kb,
                    question=case.question,
                    answer=answer,
                    citations=[asdict(c) for c in citations],
                    is_no_answer=is_no_answer,
                    retrieval_hit=retrieval_hit,
                    expected_sources=list(case.expected_sources),
                    fact_hit=fact_hit,
                    fact_matched=fact_matched,
                    fact_missing=fact_missing,
                    no_answer_correct=no_answer_correct,
                    expected_chunk=case.expected_chunk,
                    chunk_hit=chunk_hit,
                    chunk_rank=chunk_rank,
                    judge_score=judge_score,
                    judge_reason=judge_reason,
                )
            )
            if on_progress is not None:
                on_progress(len(results), results[-1])
    return results


def list_eval_sets() -> list[dict]:
    """扫描评测集目录，返回每个评测集的元信息（不执行评测）。"""
    if not EVAL_DIR.exists():
        return []
    found: list[dict] = []
    for path in sorted([*EVAL_DIR.glob("*.yaml"), *EVAL_DIR.glob("*.yml")]):
        try:
            kb_name, cases = load_eval_set(path)
            name = path.stem
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("name"):
                name = str(data["name"])
            found.append(
                {
                    "id": path.name,
                    "name": name,
                    "knowledge_base": kb_name,
                    "cases": len(cases),
                }
            )
        except Exception as exc:  # noqa: BLE001 - 单个评测集损坏不应影响整体列表
            found.append(
                {
                    "id": path.name,
                    "name": path.stem,
                    "knowledge_base": None,
                    "cases": 0,
                    "error": str(exc),
                }
            )
    return found


def _rate(numerator: int, denominator: int) -> float | None:
    """计算通过率；分母为 0 时返回 None（表示该项无样本、不参与统计）。"""
    return round(numerator / denominator, 4) if denominator else None


def summarize(results: list[CaseResult]) -> dict:
    """汇总整场评测指标。"""
    total = len(results)
    answered = sum(1 for r in results if not r.is_no_answer)
    retrieval_cases = [r for r in results if r.retrieval_hit is not None]
    fact_cases = [r for r in results if r.fact_hit is not None]
    na_cases = [r for r in results if r.no_answer_correct is not None]
    judge_cases = [r for r in results if r.judge_score is not None]
    chunk_cases = [r for r in results if r.chunk_hit is not None]
    chunk_n = len(chunk_cases)
    ranked = [r.chunk_rank for r in chunk_cases if r.chunk_rank is not None]

    def _pct(num: int, den: int) -> float | None:
        return round(num / den, 4) if den else None

    difficulty_breakdown: dict[str, dict] = {}
    for d in sorted({r.difficulty for r in chunk_cases}):
        sub = [r for r in chunk_cases if r.difficulty == d]
        difficulty_breakdown[d] = {
            "n": len(sub),
            "hit": sum(1 for r in sub if r.chunk_hit),
            "rate": _pct(sum(1 for r in sub if r.chunk_hit), len(sub)),
        }

    return {
        "total": total,
        "answered": answered,
        "no_answer": total - answered,
        "retrieval_recall": {
            "n": len(retrieval_cases),
            "hit": sum(1 for r in retrieval_cases if r.retrieval_hit),
            "rate": _rate(sum(1 for r in retrieval_cases if r.retrieval_hit), len(retrieval_cases)),
        },
        "fact_accuracy": {
            "n": len(fact_cases),
            "hit": sum(1 for r in fact_cases if r.fact_hit),
            "rate": _rate(sum(1 for r in fact_cases if r.fact_hit), len(fact_cases)),
        },
        "no_answer_accuracy": {
            "n": len(na_cases),
            "correct": sum(1 for r in na_cases if r.no_answer_correct),
            "rate": _rate(sum(1 for r in na_cases if r.no_answer_correct), len(na_cases)),
        },
        "judge_accuracy": {
            "n": len(judge_cases),
            "correct": sum(1 for r in judge_cases if r.judge_score == 1),
            "rate": _rate(sum(1 for r in judge_cases if r.judge_score == 1), len(judge_cases)),
        }
        if judge_cases
        else None,
        "chunk_retrieval": {
            "n": chunk_n,
            "recall_at_5": _pct(sum(1 for r in chunk_cases if r.chunk_hit), chunk_n),
            "hit_at_1": _pct(sum(1 for r in chunk_cases if r.chunk_rank == 1), chunk_n),
            "hit_at_3": _pct(sum(1 for r in chunk_cases if r.chunk_rank is not None and r.chunk_rank <= 3), chunk_n),
            "hit_at_5": _pct(sum(1 for r in chunk_cases if r.chunk_hit), chunk_n),
            "mrr": round(sum(1 / r for r in ranked) / chunk_n, 4) if chunk_n else None,
        },
        "difficulty_breakdown": difficulty_breakdown,
    }


def _status_text(result: CaseResult) -> str:
    """计算单题通过状态：FAIL > SKIP > PASS（有任一可判项未过即 FAIL）。"""
    checks: list[bool] = []
    if result.retrieval_hit is not None:
        checks.append(result.retrieval_hit)
    if result.chunk_hit is not None:
        checks.append(result.chunk_hit)
    if result.fact_hit is not None:
        checks.append(result.fact_hit)
    if result.no_answer_correct is not None:
        checks.append(result.no_answer_correct)
    if result.judge_score is not None:
        checks.append(result.judge_score == 1)
    if not checks:
        return "SKIP"
    return "PASS" if all(checks) else "FAIL"


def print_report(results: list[CaseResult], summary: dict) -> None:
    """把逐题结果与汇总打印到控制台。"""
    print("=" * 72)
    print(f"评测结果汇总：共 {summary['total']} 题，作答 {summary['answered']} 题，拒答 {summary['no_answer']} 题")
    rr, fa = summary["retrieval_recall"], summary["fact_accuracy"]
    na, ju = summary["no_answer_accuracy"], summary["judge_accuracy"]
    print(f"  检索命中率（Citation Recall）: {rr['hit']}/{rr['n']} = {rr['rate']}")
    print(f"  事实覆盖率（Fact Accuracy）  : {fa['hit']}/{fa['n']} = {fa['rate']}")
    print(f"  拒答正确率（No-Answer Acc）  : {na['correct']}/{na['n']} = {na['rate']}")
    if ju:
        print(f"  LLM 裁判准确率（Judge Acc）  : {ju['correct']}/{ju['n']} = {ju['rate']}")
    cr = summary.get("chunk_retrieval") or {}
    if cr.get("n"):
        print(f"  块级检索（Chunk Retrieval） : Recall@5={cr['recall_at_5']}  Hit@1={cr['hit_at_1']}  "
              f"Hit@3={cr['hit_at_3']}  Hit@5={cr['hit_at_5']}  MRR={cr['mrr']}  (n={cr['n']})")
    db = summary.get("difficulty_breakdown") or {}
    if db:
        print("  难度分层 Recall@5          : " + "  ".join(f"{d}:{v['hit']}/{v['n']}" for d, v in db.items()))
    print("=" * 72)
    for result in results:
        print(f"\n[{_status_text(result)}] {result.id} ({result.category} / {result.difficulty} / kb={result.knowledge_base}) {result.question}")
        print(f"  回答: {result.answer[:160]}{'...' if len(result.answer) > 160 else ''}")
        if result.citations:
            print(f"  引用: {', '.join(c['filename'] for c in result.citations)}")
        if result.retrieval_hit is not None:
            print(f"  检索: {'命中' if result.retrieval_hit else '未命中'}（预期 {result.expected_sources}）")
        if result.chunk_hit is not None:
            rank_txt = f"命中@{result.chunk_rank}" if result.chunk_hit else "未命中"
            feat = f"（特征：{result.expected_chunk}）" if result.expected_chunk else ""
            print(f"  块级: {rank_txt}{feat}")
        if result.fact_hit is not None:
            matched = f"命中 {result.fact_matched}" if result.fact_matched else ""
            missing = f"缺失 {result.fact_missing}" if result.fact_missing else ""
            print(f"  事实: {'通过' if result.fact_hit else '未通过'}  {matched} {missing}".rstrip())
        if result.no_answer_correct is not None:
            print(f"  拒答: {'正确' if result.no_answer_correct else '错误（应拒答但作答）'}")
        if result.judge_score is not None:
            print(f"  裁判: {result.judge_score}  {result.judge_reason or ''}")


# 检索方式（与 config/models.yaml 的 retrieval 三种 mode 对齐）
VALID_MODES = ("dense", "hybrid", "rerank")


def parse_modes(value: str) -> list[str]:
    """把 --modes 参数解析为模式列表（逗号 / 空白分隔），非法值报错。"""
    modes = [m.strip().lower() for m in re.split(r"[,，\s]+", value) if m.strip()]
    invalid = [m for m in modes if m not in VALID_MODES]
    if invalid:
        raise ValueError(f"Invalid retrieval mode(s): {invalid}. Valid: {', '.join(VALID_MODES)}")
    if not modes:
        raise ValueError("--modes requires at least one of: dense, hybrid, rerank")
    seen: set[str] = set()
    unique: list[str] = []
    for m in modes:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    return unique


def run_eval_modes(
    kb_name: str,
    cases: list[EvalCase],
    chat_config,
    embedding_config,
    modes: list[str],
    judge: bool = False,
) -> dict[str, list[CaseResult]]:
    """按多种检索方式各跑一遍评测，返回 {mode: results}。"""
    return {
        mode: run_eval(kb_name, cases, chat_config, embedding_config, judge=judge, retrieval_mode=mode)
        for mode in modes
    }


def print_comparison(mode_results: dict[str, list[CaseResult]]) -> None:
    """打印多方式对比表（指标 × 方式）。"""
    summaries = {mode: summarize(results) for mode, results in mode_results.items()}
    modes = list(mode_results.keys())
    width = 24 + 16 * len(modes)
    print("=" * width)
    print(f"评测结果对比（共 {summaries[modes[0]]['total']} 题 × {len(modes)} 方式）")
    print("指标".ljust(24) + "".join(m.rjust(16) for m in modes))
    print("-" * width)

    def row(label: str, getter) -> None:
        cells = []
        for mode in modes:
            value = getter(summaries[mode])
            cells.append("-" if value is None else str(value))
        print(label.ljust(24) + "".join(c.rjust(16) for c in cells))

    row("检索命中率", lambda s: s["retrieval_recall"]["rate"])
    row("事实覆盖率", lambda s: s["fact_accuracy"]["rate"])
    row("拒答正确率", lambda s: s["no_answer_accuracy"]["rate"])
    if any(summaries[m].get("judge_accuracy") for m in modes):
        row("LLM 裁判准确率", lambda s: (s.get("judge_accuracy") or {}).get("rate"))
    cr = "chunk_retrieval"
    row("块级 Recall@5", lambda s: (s.get(cr) or {}).get("recall_at_5"))
    row("块级 Hit@1", lambda s: (s.get(cr) or {}).get("hit_at_1"))
    row("块级 Hit@3", lambda s: (s.get(cr) or {}).get("hit_at_3"))
    row("块级 MRR", lambda s: (s.get(cr) or {}).get("mrr"))
    print("=" * width)
    for mode in modes:
        s = summaries[mode]
        print(
            f"[{mode}] 检索 {s['retrieval_recall']['hit']}/{s['retrieval_recall']['n']}  "
            f"事实 {s['fact_accuracy']['hit']}/{s['fact_accuracy']['n']}  "
            f"拒答 {s['no_answer_accuracy']['correct']}/{s['no_answer_accuracy']['n']}"
        )


def write_json(path: Path, results: list[CaseResult], summary: dict) -> None:
    """把逐题结果与汇总写入 JSON 文件。"""
    payload = {"summary": summary, "cases": [asdict(r) for r in results]}
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 JSON 结果：{path}")


def write_json_modes(path: Path, mode_results: dict[str, list[CaseResult]]) -> None:
    """把多方式评测结果写入 JSON 文件（{"modes": {mode: {...}}}）。"""
    payload = {
        "modes": {
            mode: {"summary": summarize(results), "cases": [asdict(r) for r in results]}
            for mode, results in mode_results.items()
        }
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 JSON 结果：{path}")


def _markdown_case_table(results: list[CaseResult]) -> list[str]:
    """生成逐题结果的 Markdown 表格行。"""
    lines = ["| 状态 | ID | 分类/难度 | 知识库 | 问题 | 回答 | 块级 | 引用 |", "|---|---|---|---|---|---|---|---|"]
    for result in results:
        answer = result.answer.replace("|", "\\|").replace("\n", " ")
        cited = ", ".join(c["filename"] for c in result.citations) or "-"
        chunk_txt = f"@{result.chunk_rank}" if result.chunk_hit else ("未命中" if result.chunk_hit is not None else "-")
        lines.append(
            f"| {_status_text(result)} | {result.id} | {result.category}/{result.difficulty} | {result.knowledge_base} | "
            f"{result.question} | {answer[:120]} | {chunk_txt} | {cited} |"
        )
    return lines


def write_markdown(path: Path, results: list[CaseResult], summary: dict) -> None:
    """把报告写入 Markdown 文件。"""
    lines = [
        "# 知识库问答评测报告",
        "",
        f"- 总题数：{summary['total']}",
        f"- 作答：{summary['answered']}，拒答：{summary['no_answer']}",
    ]
    rr, fa = summary["retrieval_recall"], summary["fact_accuracy"]
    na, ju = summary["no_answer_accuracy"], summary["judge_accuracy"]
    lines.append(f"- 检索命中率：{rr['hit']}/{rr['n']} = {rr['rate']}")
    lines.append(f"- 事实覆盖率：{fa['hit']}/{fa['n']} = {fa['rate']}")
    lines.append(f"- 拒答正确率：{na['correct']}/{na['n']} = {na['rate']}")
    if ju:
        lines.append(f"- LLM 裁判准确率：{ju['correct']}/{ju['n']} = {ju['rate']}")
    cr = summary.get("chunk_retrieval") or {}
    if cr.get("n"):
        lines.append(f"- 块级检索：Recall@5={cr['recall_at_5']}，Hit@1={cr['hit_at_1']}，Hit@3={cr['hit_at_3']}，MRR={cr['mrr']}（n={cr['n']}）")
    db = summary.get("difficulty_breakdown") or {}
    if db:
        lines.append("- 难度分层 Recall@5：" + "  ".join(f"{d}:{v['hit']}/{v['n']}" for d, v in db.items()))
    lines += ["", * _markdown_case_table(results)]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入 Markdown 报告：{path}")


def write_markdown_modes(path: Path, mode_results: dict[str, list[CaseResult]]) -> None:
    """把多方式对比报告写入 Markdown 文件。"""
    summaries = {mode: summarize(results) for mode, results in mode_results.items()}
    modes = list(mode_results.keys())
    lines = ["# 知识库问答评测报告（多方式对比）", ""]
    lines.append("| 指标 | " + " | ".join(modes) + " |")
    lines.append("|" + "---|" * (len(modes) + 1))

    def row(label: str, getter) -> None:
        cells = []
        for mode in modes:
            value = getter(summaries[mode])
            cells.append("-" if value is None else str(value))
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    row("检索命中率", lambda s: s["retrieval_recall"]["rate"])
    row("事实覆盖率", lambda s: s["fact_accuracy"]["rate"])
    row("拒答正确率", lambda s: s["no_answer_accuracy"]["rate"])
    if any(summaries[m].get("judge_accuracy") for m in modes):
        row("LLM 裁判准确率", lambda s: (s.get("judge_accuracy") or {}).get("rate"))
    cr = "chunk_retrieval"
    row("块级 Recall@5", lambda s: (s.get(cr) or {}).get("recall_at_5"))
    row("块级 Hit@1", lambda s: (s.get(cr) or {}).get("hit_at_1"))
    row("块级 Hit@3", lambda s: (s.get(cr) or {}).get("hit_at_3"))
    row("块级 MRR", lambda s: (s.get(cr) or {}).get("mrr"))
    for mode in modes:
        lines.append("")
        lines.append(f"## {mode}")
        lines.extend(_markdown_case_table(mode_results[mode]))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入 Markdown 报告：{path}")


def main(argv: list[str] | None = None) -> int:
    """命令行入口：python -m app.eval [eval_set] [--judge] [--modes ...] [--json ...] [--markdown ...]"""
    parser = argparse.ArgumentParser(description="运行企业知识库问答评测集")
    parser.add_argument(
        "eval_set",
        nargs="?",
        default=str(EVAL_DIR / "hr-eval.yaml"),
        help="评测集 YAML 路径（默认 eval/hr-eval.yaml）",
    )
    parser.add_argument("--judge", action="store_true", help="启用 LLM 裁判做语义判分（额外消耗 API）")
    parser.add_argument(
        "--modes",
        dest="modes",
        metavar="dense,hybrid,rerank",
        help="按逗号/空白分隔的检索方式跑多方式对比（dense/hybrid/rerank）；缺省用当前配置单跑",
    )
    parser.add_argument("--json", dest="json_out", metavar="PATH", help="把逐题结果写入 JSON 文件")
    parser.add_argument("--markdown", dest="md_out", metavar="PATH", help="把报告写入 Markdown 文件")
    args = parser.parse_args(argv)

    _reconfigure_stdio()
    kb_name, cases = load_eval_set(Path(args.eval_set))
    chat_config = get_provider()
    embedding_config = get_provider(for_embeddings=True)
    modes = parse_modes(args.modes) if args.modes else None
    if modes:
        mode_results = run_eval_modes(kb_name, cases, chat_config, embedding_config, modes, judge=args.judge)
        print_comparison(mode_results)
        if args.json_out:
            write_json_modes(Path(args.json_out), mode_results)
        if args.md_out:
            write_markdown_modes(Path(args.md_out), mode_results)
    else:
        results = run_eval(kb_name, cases, chat_config, embedding_config, judge=args.judge)
        summary = summarize(results)
        print_report(results, summary)
        if args.json_out:
            write_json(Path(args.json_out), results, summary)
        if args.md_out:
            write_markdown(Path(args.md_out), results, summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
