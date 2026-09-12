"""评测 HTTP API。

把离线评测引擎（app/eval.py）暴露为 REST 接口，供前端「直观评测」页面调用：
- GET  /eval/sets              列出 eval/ 目录下的评测集
- GET  /eval/sets/{set_id}     读取单个评测集（题目列表，不执行）
- POST /eval/runs              启动一次评测（后台线程执行，返回 job_id）
- GET  /eval/runs/{job_id}     查询评测进度与结果

仅 admin 角色可调用；评测会消耗模型 API，故采用后台任务 + 轮询，
并限制同一时刻只允许一个评测任务在运行。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.services.audit import log_event
from app.routers.deps import require_admin
from app.models.auth import User
from app.services.eval import EVAL_DIR, list_eval_sets, load_eval_set, run_eval, summarize
from app.core.config import get_provider
from app.schemas import RunEvalRequest

router = APIRouter(prefix="/eval", tags=["eval"])

# 任务并发限制与内存任务表（进程内存储；重启后任务历史清空，不影响评测集文件）
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_MAX_KEPT_JOBS = 50

# 支持的检索方式（与 app/retrieval.py 的 hybrid_search mode 对应）
RETRIEVAL_MODES = ("dense", "hybrid", "rerank")


def _validated_modes(modes: list[str]) -> list[str]:
    """校验并去重检索方式列表（去重保序，非法值抛 400）。"""
    result = list(dict.fromkeys(modes))
    invalid = [m for m in result if m not in RETRIEVAL_MODES]
    if invalid:
        raise HTTPException(status_code=400, detail=f"无效的检索方式：{invalid}。可选：{list(RETRIEVAL_MODES)}")
    if not result:
        raise HTTPException(status_code=400, detail="modes 不能为空。")
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_set_path(set_id: str):
    """安全地把评测集 id 解析为 eval 目录内的文件路径（防目录穿越）。"""
    if not set_id.endswith((".yaml", ".yml")) or set_id != set_id.replace("\\", "/").split("/")[-1]:
        raise HTTPException(status_code=400, detail="无效的评测集 id。")
    path = (EVAL_DIR / set_id).resolve()
    if path.parent != EVAL_DIR.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"评测集 '{set_id}' 不存在。")
    return path


@router.get("/sets")
def get_eval_sets(_: User = Depends(require_admin)) -> list[dict]:
    """列出 eval/ 目录下的全部评测集元信息。"""
    return list_eval_sets()


@router.get("/sets/{set_id}")
def get_eval_set(set_id: str, _: User = Depends(require_admin)) -> dict:
    """读取单个评测集的完整内容（题目列表）。"""
    path = _resolve_set_path(set_id)
    kb_name, cases = load_eval_set(path)
    return {
        "id": path.name,
        "knowledge_base": kb_name,
        "cases": [asdict(c) for c in cases],
    }


def _running_count() -> int:
    with _jobs_lock:
        return sum(1 for job in _jobs.values() if job["status"] in {"queued", "running"})


def _run_job(job_id: str, set_id: str, judge: bool, modes: list[str]) -> None:
    try:
        path = EVAL_DIR / set_id
        kb_name, cases = load_eval_set(path)
        with _jobs_lock:
            _jobs[job_id]["status"] = "running"
            _jobs[job_id]["total"] = len(cases) * len(modes)
            _jobs[job_id]["started_at"] = _now()

        chat_config = get_provider()
        embedding_config = get_provider(for_embeddings=True)

        for mode in modes:
            def on_progress(done: int, result, _mode: str = mode) -> None:
                with _jobs_lock:
                    _jobs[job_id]["results"][_mode].append(asdict(result))
                    _jobs[job_id]["progress"] = sum(len(v) for v in _jobs[job_id]["results"].values())

            results = run_eval(
                kb_name, cases, chat_config, embedding_config,
                judge=judge, retrieval_mode=mode, on_progress=on_progress,
            )
            with _jobs_lock:
                _jobs[job_id]["summary"][mode] = summarize(results)

        with _jobs_lock:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["finished_at"] = _now()
    except Exception as exc:  # noqa: BLE001 - 任务异常记录到 job.error 供前端展示
        with _jobs_lock:
            _jobs[job_id]["status"] = "error"
            _jobs[job_id]["error"] = str(exc)
            _jobs[job_id]["finished_at"] = _now()


def _evict_old_jobs() -> None:
    """只保留最近 _MAX_KEPT_JOBS 个已完成/失败的任务，避免内存无界增长。"""
    finished = sorted(
        [j for j in _jobs.values() if j["status"] in {"done", "error"}],
        key=lambda j: j.get("finished_at") or "",
    )
    for job in finished[: max(0, len(finished) - _MAX_KEPT_JOBS)]:
        _jobs.pop(job["job_id"], None)


@router.post("/runs", status_code=202)
def start_eval_run(body: RunEvalRequest, user: User = Depends(require_admin)) -> dict:
    """启动一次评测，返回 job_id（后台执行，轮询 /eval/runs/{job_id} 取结果）。"""
    # 先解析校验评测集是否存在（同步失败直接返回 4xx，避免启动一个必败的任务）
    _resolve_set_path(body.eval_set)
    modes = _validated_modes(body.modes)
    if _running_count() > 0:
        raise HTTPException(status_code=409, detail="已有一个评测任务在运行，请稍后再试。")

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "set_id": body.eval_set,
            "judge": body.judge,
            "modes": modes,
            "status": "queued",
            "progress": 0,
            "total": 0,
            "summary": {},
            "results": {m: [] for m in modes},
            "error": None,
            "started_at": None,
            "finished_at": None,
        }
        _evict_old_jobs()

    log_event(
        "eval_run",
        user=user.username,
        detail=body.eval_set,
        extra={"judge": body.judge, "modes": modes, "job_id": job_id},
    )
    thread = threading.Thread(target=_run_job, args=(job_id, body.eval_set, body.judge, modes), daemon=True)
    thread.start()
    return {"job_id": job_id}


@router.get("/runs/{job_id}")
def get_eval_run(job_id: str, _: User = Depends(require_admin)) -> dict:
    """查询评测任务的进度与结果。"""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"评测任务 '{job_id}' 不存在。")
        return dict(job)
