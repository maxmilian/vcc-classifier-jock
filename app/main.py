import asyncio
import json
import logging
import math
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.errors import AppError, map_http_error_code
from app.services import categorizer, classifier, job_manager
from app.services.gamma import check_status, generate_presentation
from app.services.llm import complete as llm_complete

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="VCC Classifier Jock", version="0.1.0")

# 靜態檔案
app.mount("/static", StaticFiles(directory="app/static"), name="static")

ANALYZE_BATCH_SIZE = max(1, int(settings.analyze_batch_size))
ANALYZE_MAX_TOKENS = max(256, int(settings.analyze_max_tokens))
PPT_MARKDOWN_MAX_TOKENS = max(512, int(settings.ppt_markdown_max_tokens))
GAMMA_INPUT_MAX_CHARS = max(0, int(settings.gamma_input_max_chars))
ESTIMATED_SECONDS_PER_BATCH = 12

VCC_LEVELS = {"絕對適合", "高度適合", "條件適合", "需釐清", "不適合"}
PPT_ELIGIBLE_LEVELS = {"絕對適合", "高度適合", "條件適合"}
VCC_JUDGE_STATUSES = {
    classifier.JUDGE_STATUS_MODEL,
    "外部匯入",
    classifier.JUDGE_STATUS_FALLBACK,
    classifier.JUDGE_STATUS_UNANALYZED,
    "分析失敗",
}


@app.exception_handler(AppError)
async def app_error_handler(_, exc: AppError):
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload())


@app.exception_handler(HTTPException)
async def http_exception_handler(_, exc: HTTPException):
    if isinstance(exc.detail, dict):
        detail = exc.detail
        if "error_code" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail)
        message = str(detail.get("message", "")).strip() or "請求失敗"
    else:
        message = str(exc.detail).strip() if exc.detail else "請求失敗"

    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": map_http_error_code(exc.status_code),
            "message": message,
            "retryable": exc.status_code in {408, 429, 502, 503, 504},
        },
    )


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(_, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error_code": "INVALID_REQUEST",
            "message": f"請求格式不正確: {exc.errors()}",
            "retryable": False,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_, exc: Exception):
    logger.exception("Unhandled API exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_ERROR",
            "message": "系統發生未預期錯誤，請稍後再試。",
            "retryable": True,
        },
    )


def _count_vcc_metrics(rows: list[dict]) -> tuple[dict[str, int], dict[str, int]]:
    vcc_level_counts = {
        "絕對適合": 0,
        "高度適合": 0,
        "條件適合": 0,
        "需釐清": 0,
        "不適合": 0,
    }
    vcc_status_counts = {
        classifier.JUDGE_STATUS_MODEL: 0,
        "外部匯入": 0,
        classifier.JUDGE_STATUS_FALLBACK: 0,
        classifier.JUDGE_STATUS_UNANALYZED: 0,
        "分析失敗": 0,
    }

    for row in rows:
        level = str(row.get("VCC適用等級", "")).strip()
        if level in vcc_level_counts:
            vcc_level_counts[level] += 1

        status = str(row.get("VCC判定狀態", "")).strip()
        if not status:
            status = (
                classifier.JUDGE_STATUS_MODEL
                if level in VCC_LEVELS
                else classifier.JUDGE_STATUS_UNANALYZED
            )
        if status not in vcc_status_counts:
            status = classifier.JUDGE_STATUS_UNANALYZED
        vcc_status_counts[status] += 1

    return vcc_level_counts, vcc_status_counts


def _validate_gamma_input_length(markdown_content: str) -> None:
    if GAMMA_INPUT_MAX_CHARS <= 0:
        return
    actual = len(markdown_content)
    if actual <= GAMMA_INPUT_MAX_CHARS:
        return
    raise AppError(
        status_code=413,
        error_code="GAMMA_INPUT_TOO_LARGE",
        message=(
            f"Gamma 輸入長度 {actual} 超過上限 {GAMMA_INPUT_MAX_CHARS} 字元，"
            "請精簡內容或分段生成。"
        ),
        retryable=False,
    )


def _normalize_row_vcc_fields(row: dict) -> dict:
    new_row = dict(row)
    level = str(new_row.get("VCC適用等級", "")).strip()
    status = str(new_row.get("VCC判定狀態", "")).strip()

    if level in VCC_LEVELS:
        new_row["VCC適用等級"] = level
        if status in VCC_JUDGE_STATUSES:
            new_row["VCC判定狀態"] = status
        else:
            new_row["VCC判定狀態"] = "外部匯入"
    else:
        new_row["VCC適用等級"] = ""
        if status in VCC_JUDGE_STATUSES:
            new_row["VCC判定狀態"] = status
        else:
            new_row["VCC判定狀態"] = classifier.JUDGE_STATUS_UNANALYZED

    new_row.pop("VCC判斷", None)
    return new_row


def _is_ppt_candidate(row: dict) -> bool:
    level = str(row.get("VCC適用等級", "")).strip()
    return level in PPT_ELIGIBLE_LEVELS


def _build_analyze_result(merged_rows: list[dict], filename: str) -> dict:
    company_values = sorted(
        {
            str(r.get("公司名稱", "")).strip()
            for r in merged_rows
            if str(r.get("公司名稱", "")).strip()
        }
    )
    if len(company_values) == 1:
        company_name = company_values[0]
    elif len(company_values) > 1:
        company_name = f"多公司（{len(company_values)}）"
    else:
        company_name = "未命名公司"

    vcc_level_counts, vcc_status_counts = _count_vcc_metrics(merged_rows)

    ppt_candidate_items = sum(1 for row in merged_rows if _is_ppt_candidate(row))

    return {
        "company_name": company_name,
        "tax_id": "",
        "filename": filename,
        "total_items": len(merged_rows),
        "vcc_level_counts": vcc_level_counts,
        "vcc_status_counts": vcc_status_counts,
        "ppt_candidate_items": ppt_candidate_items,
        "items": merged_rows,
        "source_mode": "analyze",
    }


async def _run_analyze_job(job_id: str, rows: list[dict]) -> None:
    job = job_manager.get_active_job(job_id)
    if job is None:
        return

    try:
        job["status"] = "running"
        job["phase"] = "preparing"
        job["started_at"] = job_manager.now_iso()
        job["updated_at"] = job_manager.now_iso()
        job_manager.append_stage_log(job, "preparing", "開始前處理與批次初始化")
        job_manager.save_job_cache(job)

        async def on_batch_progress(progress: dict) -> None:
            active = job_manager.get_active_job(job_id)
            if active is None:
                return

            batch_idx = int(progress["batch_index"])
            total_batches = int(progress["total_batches"])
            record = progress["record"]

            active["phase"] = "classifying"
            active["processed_batches"] = batch_idx
            active["progress_pct"] = round((batch_idx / max(total_batches, 1)) * 90, 1)
            active["estimated_seconds_remaining"] = max(
                0, (total_batches - batch_idx) * ESTIMATED_SECONDS_PER_BATCH
            )
            active["batch_logs"].append(record)
            active["updated_at"] = job_manager.now_iso()

            job_manager.append_stage_log(
                active,
                "classifying",
                f"批次 {batch_idx}/{total_batches} 完成",
                {
                    "status": record.get("status"),
                    "attempts": record.get("attempts"),
                    "item_count": record.get("item_count"),
                    "fallback_count": record.get("fallback_count", 0),
                },
            )
            job_manager.save_job_cache(active)

        merged, meta = await classifier.classify_items_in_batches(
            rows=rows,
            batch_size=ANALYZE_BATCH_SIZE,
            max_tokens=ANALYZE_MAX_TOKENS,
            progress_callback=on_batch_progress,
        )

        job["phase"] = "merging"
        job["progress_pct"] = 95.0
        job["updated_at"] = job_manager.now_iso()
        job_manager.append_stage_log(job, "merging", "批次分析完成，開始彙整輸出")
        job_manager.save_job_cache(job)

        csv_content = classifier.to_csv_string(merged)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"VCC分析結果_{ts}.csv"
        filepath = job_manager.TEMP_DIR / filename
        filepath.write_text(csv_content, encoding="utf-8-sig")

        result_payload = _build_analyze_result(merged, filename)
        result_payload["analysis_summary"] = meta.get("summary", {})

        job["status"] = "completed"
        job["phase"] = "completed"
        job["progress_pct"] = 100.0
        job["estimated_seconds_remaining"] = 0
        job["updated_at"] = job_manager.now_iso()
        job["finished_at"] = job_manager.now_iso()
        job["result"] = result_payload
        job_manager.append_stage_log(
            job,
            "completed",
            "分析完成，可下載 CSV 或進入簡報流程",
            {"filename": filename},
        )
        job_manager.save_job_cache(job)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Analyze job failed: %s", job_id)
        job["status"] = "failed"
        job["phase"] = "failed"
        job["progress_pct"] = 100.0
        job["updated_at"] = job_manager.now_iso()
        job["finished_at"] = job_manager.now_iso()
        job["error"] = str(exc)
        job_manager.append_stage_log(job, "failed", "分析失敗", {"error": str(exc)})
        job_manager.save_job_cache(job)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path("app/static/index.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/health")
async def health():
    from importlib.metadata import version

    return {"status": "ok", "version": version("vcc-classifier-jock")}


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    """上傳 CSV，啟動多段批次分析任務。"""
    content = (await file.read()).decode("utf-8-sig")
    rows = classifier.parse_csv(content)

    missing = classifier.validate_csv_columns(rows)
    if missing:
        raise AppError(
            status_code=400,
            error_code="INVALID_CSV_COLUMNS",
            message=f"CSV 缺少必要欄位: {', '.join(missing)}",
            retryable=False,
        )
    if not rows:
        raise AppError(
            status_code=400,
            error_code="EMPTY_CSV",
            message="CSV 無資料列",
            retryable=False,
        )

    unique_items = {
        str(row.get("費用項目名稱", "")).strip()
        for row in rows
        if str(row.get("費用項目名稱", "")).strip()
    }
    unique_count = len(unique_items)
    if unique_count == 0:
        raise AppError(
            status_code=400,
            error_code="EMPTY_ANALYZE_ITEMS",
            message="CSV 沒有有效的費用項目名稱",
            retryable=False,
        )

    total_batches = math.ceil(unique_count / ANALYZE_BATCH_SIZE)
    estimated_seconds = max(10, total_batches * ESTIMATED_SECONDS_PER_BATCH)
    job = job_manager.create_analyze_job(
        total_rows=len(rows),
        unique_items=unique_count,
        total_batches=total_batches,
        estimated_seconds=estimated_seconds,
    )
    job_id = job["job_id"]

    task = asyncio.create_task(_run_analyze_job(job_id=job_id, rows=rows))
    task.add_done_callback(
        lambda finished_task, current_job_id=job_id: job_manager.on_analyze_task_done(
            current_job_id, finished_task
        )
    )
    job_manager.set_job_task(job_id, task)

    return {
        "job_id": job_id,
        "status": job["status"],
        "phase": job["phase"],
        "created_at": job["created_at"],
        "total_rows": job["total_rows"],
        "unique_items": job["unique_items"],
        "batch_size": ANALYZE_BATCH_SIZE,
        "total_batches": job["total_batches"],
        "estimated_seconds": job["estimated_seconds"],
        "cache_file": job["cache_file"],
    }


@app.get("/api/analyze-jobs/{job_id}")
async def analyze_job_status(job_id: str):
    """查詢分析任務狀態與逐段快取紀錄。"""
    job = job_manager.find_job(job_id)
    if job is None:
        raise AppError(
            status_code=404,
            error_code="ANALYZE_JOB_NOT_FOUND",
            message="找不到分析任務",
            retryable=False,
        )
    return job_manager.public_job_payload(job)


@app.get("/api/analyze-jobs/{job_id}/cache")
async def download_analyze_job_cache(job_id: str):
    """下載分析任務快取紀錄(JSON)。"""
    cache_path = job_manager.job_cache_path(job_id)
    if not cache_path.exists():
        raise AppError(
            status_code=404,
            error_code="ANALYZE_JOB_CACHE_NOT_FOUND",
            message="快取紀錄不存在",
            retryable=False,
        )
    filename = f"analyze_job_{job_id}.json"
    encoded = quote(filename)
    return FileResponse(
        cache_path,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"},
    )


@app.post("/api/prepare-presentation-csv")
async def prepare_presentation_csv(
    company_name: str = Form(""),
    file: UploadFile = File(...),
):
    """上傳已標註 CSV，準備簡報所需資料。"""
    content = (await file.read()).decode("utf-8-sig")
    rows = classifier.parse_csv(content)

    if not rows:
        raise AppError(
            status_code=400,
            error_code="EMPTY_CSV",
            message="CSV 無資料列",
            retryable=False,
        )

    required = {"費用項目名稱", "金額累計", "交易筆數"}
    existing = set(rows[0].keys())
    missing = sorted(required - existing)
    if missing:
        raise AppError(
            status_code=400,
            error_code="INVALID_CSV_COLUMNS",
            message=f"CSV 缺少必要欄位: {', '.join(missing)}",
            retryable=False,
        )
    if "VCC適用等級" not in existing:
        raise AppError(
            status_code=400,
            error_code="INVALID_CSV_COLUMNS",
            message="CSV 需包含 VCC適用等級 欄位",
            retryable=False,
        )

    rows = [_normalize_row_vcc_fields(row) for row in rows]

    # 若 company_name 未填，嘗試從 CSV 內推斷
    if not company_name.strip():
        if "公司名稱" in rows[0]:
            for row in rows:
                guessed = (row.get("公司名稱") or "").strip()
                if guessed:
                    company_name = guessed
                    break
        if not company_name.strip():
            company_name = "未命名公司"

    total = len(rows)
    vcc_level_counts, vcc_status_counts = _count_vcc_metrics(rows)
    ppt_candidate_items = sum(1 for r in rows if _is_ppt_candidate(r))

    if ppt_candidate_items == 0:
        raise AppError(
            status_code=400,
            error_code="NO_PPT_CANDIDATES",
            message="此檔案沒有可產生簡報的項目（需要 VCC適用等級為絕對適合/高度適合/條件適合）",
            retryable=False,
        )

    return {
        "company_name": company_name,
        "tax_id": "",
        "filename": "",
        "total_items": total,
        "vcc_level_counts": vcc_level_counts,
        "vcc_status_counts": vcc_status_counts,
        "ppt_candidate_items": ppt_candidate_items,
        "items": rows,
        "source_mode": "ppt_ready",
    }


@app.get("/api/download/{filename}")
async def download(filename: str):
    """下載分析 CSV。"""
    filepath = (job_manager.TEMP_DIR / filename).resolve()
    if not filepath.is_relative_to(job_manager.TEMP_DIR.resolve()):
        raise AppError(
            status_code=400,
            error_code="INVALID_FILENAME",
            message="無效的檔案名稱",
            retryable=False,
        )
    if not filepath.exists():
        raise AppError(
            status_code=404,
            error_code="FILE_NOT_FOUND",
            message="檔案不存在",
            retryable=False,
        )
    encoded = quote(filename)
    return FileResponse(
        filepath,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
        },
    )


@app.post("/api/generate-presentation")
async def generate_ppt(data: dict):
    """相容舊流程：接收資料後直接生成 Markdown 並送 Gamma。"""
    company_name = data.get("company_name", "")
    vcc_items = data.get("vcc_items", [])

    if not company_name:
        raise AppError(
            status_code=400,
            error_code="INVALID_REQUEST",
            message="缺少 company_name",
            retryable=False,
        )
    if not vcc_items:
        raise AppError(
            status_code=400,
            error_code="NO_PPT_CANDIDATES",
            message="無 VCC 可行項目",
            retryable=False,
        )

    generated = await _build_presentation_markdown(company_name=company_name, vcc_items=vcc_items)
    presentation_markdown = generated["markdown_preview"]
    categories = generated["categories"]
    _validate_gamma_input_length(presentation_markdown)

    # 呼叫 Gamma API
    result = await generate_presentation(
        text=presentation_markdown,
        company_name=company_name,
        num_cards=7,
    )

    return {
        "status": result.get("status"),
        "generation_id": result.get("generation_id"),
        "gamma_url": result.get("gamma_url"),
        "export_url": result.get("export_url"),
        "categories": categories,
        "markdown_preview": presentation_markdown,
    }


async def _build_presentation_markdown(company_name: str, vcc_items: list[dict]) -> dict:
    normalized_items = [
        _normalize_row_vcc_fields(item) for item in vcc_items if isinstance(item, dict)
    ]
    eligible_items = [item for item in normalized_items if _is_ppt_candidate(item)]
    if not eligible_items:
        raise AppError(
            status_code=400,
            error_code="NO_PPT_CANDIDATES",
            message="無可產生簡報的候選項目",
            retryable=False,
        )

    categories = await categorizer.categorize_items(eligible_items)
    level_lookup = {
        str(item.get("費用項目名稱", "")).strip(): str(item.get("VCC適用等級", "")).strip()
        for item in eligible_items
        if str(item.get("費用項目名稱", "")).strip()
    }
    for cat_name in ["高頻次", "固定支出", "高單價", "其他"]:
        for item in categories.get(cat_name, []):
            item_name = str(item.get("itemName", "")).strip()
            if item_name and item_name in level_lookup:
                item["VCC適用等級"] = level_lookup[item_name]

    ppt_prompt_path = Path("app/prompts/vcc_ppt.txt")
    ppt_prompt = ppt_prompt_path.read_text(encoding="utf-8").strip()

    summary_lines = [f"客戶名稱：{company_name}\n"]
    summary_lines.append(f"VCC候選項目數：{len(eligible_items)}\n")
    level_counts = {
        "絕對適合": 0,
        "高度適合": 0,
        "條件適合": 0,
        "需釐清": 0,
        "不適合": 0,
    }
    for item in normalized_items:
        level = str(item.get("VCC適用等級", "")).strip()
        if level in level_counts:
            level_counts[level] += 1
    summary_lines.append(f"適用等級統計：{json.dumps(level_counts, ensure_ascii=False)}\n")

    for cat_name in ["高頻次", "固定支出", "高單價", "其他"]:
        items = categories.get(cat_name, [])
        if items:
            summary_lines.append(f"\n### {cat_name}類別：")
            for item in items:
                level = item.get("VCC適用等級", "N/A")
                summary_lines.append(
                    f"- {item['itemName']} (金額: {item.get('totalAmount', 'N/A')}, "
                    f"筆數: {item.get('txCount', 'N/A')}, "
                    f"均額: {item.get('avgAmount', 'N/A')}, "
                    f"等級: {level})"
                )

    presentation_markdown = await llm_complete(
        system_prompt=ppt_prompt,
        user_prompt=f"請根據以下數據生成簡報 Markdown：\n\n{''.join(summary_lines)}",
        tier="strong",
        max_tokens=PPT_MARKDOWN_MAX_TOKENS,
    )
    presentation_markdown = presentation_markdown.strip()
    return {
        "categories": categories,
        "markdown_preview": presentation_markdown,
        "eligible_item_count": len(eligible_items),
    }


@app.post("/api/generate-markdown")
async def generate_markdown(data: dict):
    """第一段：先生成可編輯 Markdown（不呼叫 Gamma）。"""
    company_name = data.get("company_name", "")
    vcc_items = data.get("vcc_items", [])

    if not company_name:
        raise AppError(
            status_code=400,
            error_code="INVALID_REQUEST",
            message="缺少 company_name",
            retryable=False,
        )
    if not vcc_items:
        raise AppError(
            status_code=400,
            error_code="NO_PPT_CANDIDATES",
            message="無 VCC 可行項目",
            retryable=False,
        )

    generated = await _build_presentation_markdown(company_name=company_name, vcc_items=vcc_items)
    return {
        "status": "ready",
        "company_name": company_name,
        "categories": generated["categories"],
        "markdown_preview": generated["markdown_preview"],
        "eligible_item_count": generated["eligible_item_count"],
    }


@app.post("/api/generate-gamma")
async def generate_gamma_from_markdown(data: dict):
    """第二段：吃使用者確認後的 Markdown，送去 Gamma。"""
    company_name = str(data.get("company_name", "")).strip()
    markdown_content = str(data.get("markdown_content", "")).strip()
    num_cards = data.get("num_cards", 7)

    if not company_name:
        raise AppError(
            status_code=400,
            error_code="INVALID_REQUEST",
            message="缺少 company_name",
            retryable=False,
        )
    if not markdown_content:
        raise AppError(
            status_code=400,
            error_code="INVALID_REQUEST",
            message="缺少 markdown_content",
            retryable=False,
        )
    _validate_gamma_input_length(markdown_content)

    result = await generate_presentation(
        text=markdown_content,
        company_name=company_name,
        num_cards=int(num_cards) if str(num_cards).strip() else 7,
    )
    return {
        "status": result.get("status"),
        "generation_id": result.get("generation_id"),
        "gamma_url": result.get("gamma_url"),
        "export_url": result.get("export_url"),
    }


@app.get("/api/gamma-status/{generation_id}")
async def gamma_status(generation_id: str):
    """查詢 Gamma 生成狀態。"""
    result = await check_status(generation_id)
    return result
