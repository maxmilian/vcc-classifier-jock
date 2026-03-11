import asyncio
import json
import logging
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TEMP_DIR = Path(tempfile.gettempdir()) / "vcc-classifier"
TEMP_DIR.mkdir(exist_ok=True)
JOB_CACHE_DIR = TEMP_DIR / "analyze-jobs"
JOB_CACHE_DIR.mkdir(exist_ok=True)

ANALYZE_JOB_TTL_SECONDS = 3600
TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
MAX_STAGE_LOGS = 200

ANALYZE_JOBS: dict[str, dict[str, Any]] = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_cache_path(job_id: str) -> Path:
    return JOB_CACHE_DIR / f"{job_id}.json"


def analyze_job_cache_api_path(job_id: str) -> str:
    return f"/api/analyze-jobs/{job_id}/cache"


def save_job_cache(job: dict[str, Any]) -> None:
    serializable = dict(job)
    serializable.pop("_task", None)
    job_cache_path(job["job_id"]).write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_stage_log(
    job: dict[str, Any],
    phase: str,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    log = {
        "time": now_iso(),
        "phase": phase,
        "message": message,
    }
    if extra:
        log["extra"] = extra
    stage_logs = job.setdefault("stage_logs", [])
    stage_logs.append(log)
    if len(stage_logs) > MAX_STAGE_LOGS:
        job["stage_logs"] = stage_logs[-MAX_STAGE_LOGS:]


def public_job_payload(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "phase": job["phase"],
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "updated_at": job["updated_at"],
        "total_rows": job["total_rows"],
        "unique_items": job["unique_items"],
        "total_batches": job["total_batches"],
        "processed_batches": job["processed_batches"],
        "progress_pct": job["progress_pct"],
        "estimated_seconds": job["estimated_seconds"],
        "estimated_seconds_remaining": job.get("estimated_seconds_remaining", 0),
        "stage_logs": job.get("stage_logs", []),
        "batch_logs": job.get("batch_logs", []),
        "error": job.get("error"),
        # Always expose API route instead of server filesystem path.
        "cache_file": analyze_job_cache_api_path(job["job_id"]),
        "result": job.get("result"),
    }


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _is_expired(job: dict[str, Any], ttl_seconds: int, now: datetime) -> bool:
    status = str(job.get("status", "")).strip()
    if status not in TERMINAL_STATUSES:
        return False

    finished_at = _parse_iso(job.get("finished_at"))
    if finished_at is None:
        return False
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    return now >= (finished_at + timedelta(seconds=ttl_seconds))


def cleanup_expired_jobs(ttl_seconds: int = ANALYZE_JOB_TTL_SECONDS) -> int:
    now = datetime.now(timezone.utc)
    expired_ids: list[str] = []
    for job_id, job in ANALYZE_JOBS.items():
        task = job.get("_task")
        if isinstance(task, asyncio.Task) and not task.done():
            continue
        if _is_expired(job=job, ttl_seconds=ttl_seconds, now=now):
            expired_ids.append(job_id)

    for job_id in expired_ids:
        ANALYZE_JOBS.pop(job_id, None)
    return len(expired_ids)


def find_job(job_id: str) -> dict[str, Any] | None:
    cleanup_expired_jobs()
    job = ANALYZE_JOBS.get(job_id)
    if job is not None:
        return job
    path = job_cache_path(job_id)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def create_analyze_job(
    *,
    total_rows: int,
    unique_items: int,
    total_batches: int,
    estimated_seconds: int,
) -> dict[str, Any]:
    cleanup_expired_jobs()
    job_id = uuid.uuid4().hex[:12]
    created_at = now_iso()
    job = {
        "job_id": job_id,
        "status": "queued",
        "phase": "queued",
        "created_at": created_at,
        "started_at": None,
        "finished_at": None,
        "updated_at": created_at,
        "total_rows": total_rows,
        "unique_items": unique_items,
        "total_batches": total_batches,
        "processed_batches": 0,
        "progress_pct": 0.0,
        "estimated_seconds": estimated_seconds,
        "estimated_seconds_remaining": estimated_seconds,
        "batch_logs": [],
        "stage_logs": [],
        "result": None,
        "error": None,
        "cache_file": analyze_job_cache_api_path(job_id),
    }
    append_stage_log(
        job,
        "queued",
        "任務已建立，等待開始分析",
        {
            "total_rows": total_rows,
            "unique_items": unique_items,
            "total_batches": total_batches,
            "estimated_seconds": estimated_seconds,
        },
    )

    ANALYZE_JOBS[job_id] = job
    save_job_cache(job)
    return job


def get_active_job(job_id: str) -> dict[str, Any] | None:
    return ANALYZE_JOBS.get(job_id)


def set_job_task(job_id: str, task: asyncio.Task[Any]) -> None:
    job = ANALYZE_JOBS.get(job_id)
    if job is not None:
        job["_task"] = task


def mark_job_unhandled_exception(job_id: str, exc: BaseException) -> None:
    job = ANALYZE_JOBS.get(job_id)
    if job is None:
        return
    if str(job.get("status", "")).strip() in TERMINAL_STATUSES:
        return

    finished_at = now_iso()
    job["status"] = "failed"
    job["phase"] = "failed"
    job["progress_pct"] = 100.0
    job["updated_at"] = finished_at
    job["finished_at"] = finished_at
    job["error"] = f"Unhandled task exception: {exc}"
    append_stage_log(job, "failed", "背景任務異常結束", {"error": str(exc)})
    save_job_cache(job)


def on_analyze_task_done(job_id: str, task: asyncio.Task[Any]) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        logger.warning("Analyze task cancelled: %s", job_id)
        return
    except Exception:  # noqa: BLE001
        logger.exception("Unable to inspect analyze task exception: %s", job_id)
        return

    if exc is None:
        return

    logger.error(
        "Unhandled exception in analyze task %s",
        job_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    mark_job_unhandled_exception(job_id, exc)
