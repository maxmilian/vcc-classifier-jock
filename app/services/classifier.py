import csv
import io
import json
import logging
import re
import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.config import settings
from app.services.llm import complete as llm_complete

logger = logging.getLogger(__name__)

_filter_prompt_cache: str | None = None
ALLOWED_LABELS_5 = {"絕對適合", "高度適合", "條件適合", "需釐清", "不適合"}
ALLOWED_LABELS = ALLOWED_LABELS_5
DEFAULT_BATCH_SIZE = max(1, int(settings.analyze_batch_size))
DEFAULT_ANALYZE_MAX_TOKENS = max(256, int(settings.analyze_max_tokens))
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_WAIT_SECONDS = 1.5
JUDGE_STATUS_MODEL = "模型判定"
JUDGE_STATUS_FALLBACK = "系統回退"
JUDGE_STATUS_UNANALYZED = "未分析"


def _load_filter_prompt() -> str:
    global _filter_prompt_cache
    if _filter_prompt_cache is not None:
        return _filter_prompt_cache
    path = Path("app/prompts/vcc_filter.txt")
    _filter_prompt_cache = path.read_text(encoding="utf-8").strip()
    return _filter_prompt_cache


def parse_csv(file_content: str) -> list[dict]:
    """解析 CSV 內容，回傳 list of dict。"""
    reader = csv.DictReader(io.StringIO(file_content))
    return list(reader)


def validate_csv_columns(rows: list[dict]) -> list[str]:
    """驗證必要欄位是否存在，回傳缺少的欄位列表。"""
    required = {"費用項目名稱", "金額累計", "交易筆數", "交易日期起", "交易日期迄"}
    if not rows:
        return list(required)
    existing = set(rows[0].keys())
    return sorted(required - existing)


def _strip_markdown_json_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _parse_model_json(raw: str) -> dict | list:
    text = _strip_markdown_json_fence(raw)
    if not text:
        raise ValueError("模型回傳空字串")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    try:
        parsed, _ = decoder.raw_decode(text)
        return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError("模型回傳不是合法 JSON")


def _validate_batch_result(
    parsed: dict | list, expected_items: list[str]
) -> tuple[dict[str, str], list[str], dict[str, str], list[str]]:
    cleaned: dict[str, str] = {}
    invalid_values: dict[str, str] = {}
    extra_keys: list[str] = []

    expected_set = set(expected_items)

    if isinstance(parsed, dict):
        for key, value in parsed.items():
            item = str(key).strip()
            label = _to_level_label(str(value).strip())
            if item not in expected_set:
                extra_keys.append(item)
                continue
            if label not in ALLOWED_LABELS:
                invalid_values[item] = str(value).strip()
                continue
            cleaned[item] = label
    elif isinstance(parsed, list):
        for obj in parsed:
            if not isinstance(obj, dict):
                continue
            item = str(
                obj.get("itemName")
                or obj.get("費用項目名稱")
                or obj.get("item")
                or ""
            ).strip()
            label = str(
                obj.get("isVccSuitable")
                or obj.get("VCC判斷")
                or obj.get("vccSuitability")
                or ""
            ).strip()
            level_label = _to_level_label(label)
            if not item:
                continue
            if item not in expected_set:
                extra_keys.append(item)
                continue
            if level_label not in ALLOWED_LABELS:
                invalid_values[item] = label
                continue
            cleaned[item] = level_label
    else:
        raise ValueError("模型輸出不是 dict/list")

    missing_items = [item for item in expected_items if item not in cleaned]
    return cleaned, missing_items, invalid_values, extra_keys


def _build_batch_user_prompt(items: list[str], batch_index: int, total_batches: int) -> str:
    payload = {
        "task": "請判斷每個費用項目是否適合 VCC，僅回傳合法 JSON object。",
        "batch_index": batch_index,
        "total_batches": total_batches,
        "items": items,
        "allowed_values": ["絕對適合", "高度適合", "條件適合", "需釐清", "不適合"],
        "output_format": {"交通費": "高度適合", "房租": "不適合", "雜費": "需釐清"},
    }
    return json.dumps(payload, ensure_ascii=False)


def _normalize_vcc_label(raw_label: str) -> str:
    level = _to_level_label(str(raw_label).strip())
    if level in ALLOWED_LABELS:
        return level
    return ""


def _to_level_label(raw_label: str) -> str:
    label = str(raw_label).strip()
    if label in ALLOWED_LABELS_5:
        return label
    if label == "可以":
        return "高度適合"
    if label == "不行":
        return "不適合"
    if label == "不確定":
        return "需釐清"
    return ""


async def _classify_single_batch(
    items: list[str],
    batch_index: int,
    total_batches: int,
    max_retries: int,
    max_tokens: int,
) -> tuple[dict[str, str], dict]:
    attempt_logs = []
    last_error = ""

    for attempt in range(1, max_retries + 1):
        started = time.time()
        try:
            raw = await llm_complete(
                system_prompt=_load_filter_prompt(),
                user_prompt=_build_batch_user_prompt(items, batch_index, total_batches),
                tier="fast",
                max_tokens=max_tokens,
            )
            parsed = _parse_model_json(raw)
            cleaned, missing, invalid, extra = _validate_batch_result(parsed, items)
            if missing or invalid:
                raise ValueError(
                    f"批次校驗失敗: missing={len(missing)} invalid={len(invalid)}"
                )

            return cleaned, {
                "batch_index": batch_index,
                "total_batches": total_batches,
                "status": "success",
                "attempts": attempt,
                "item_count": len(items),
                "fallback_count": 0,
                "extra_keys": extra,
                "latency_sec": round(time.time() - started, 3),
                "attempt_logs": attempt_logs,
            }
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            attempt_logs.append(
                {
                    "attempt": attempt,
                    "error": last_error,
                    "latency_sec": round(time.time() - started, 3),
                }
            )
            if attempt < max_retries:
                await asyncio.sleep(min(DEFAULT_RETRY_WAIT_SECONDS * attempt, 5.0))

    return {}, {
        "batch_index": batch_index,
        "total_batches": total_batches,
        "status": "failed_fallback",
        "attempts": max_retries,
        "item_count": len(items),
        "fallback_count": len(items),
        "last_error": last_error,
        "attempt_logs": attempt_logs,
    }


async def classify_items_in_batches(
    rows: list[dict],
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_tokens: int = DEFAULT_ANALYZE_MAX_TOKENS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    progress_callback: Callable[[dict], Awaitable[None] | None] | None = None,
) -> tuple[list[dict], dict]:
    """去重後分批判斷費用項目，並回填每列 VCC適用等級。"""
    if batch_size <= 0:
        raise ValueError("batch_size 必須大於 0")
    if max_retries <= 0:
        raise ValueError("max_retries 必須大於 0")
    if max_tokens <= 0:
        raise ValueError("max_tokens 必須大於 0")

    unique_items: list[str] = []
    seen: set[str] = set()
    for row in rows:
        item = str(row.get("費用項目名稱", "")).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        unique_items.append(item)

    if not unique_items:
        merged = []
        for row in rows:
            new_row = dict(row)
            new_row["VCC適用等級"] = ""
            new_row["VCC判定狀態"] = JUDGE_STATUS_UNANALYZED
            merged.append(new_row)
        return merged, {
            "summary": {
                "total_rows": len(rows),
                "unique_items": 0,
                "total_batches": 0,
                "processed_batches": 0,
                "fallback_items": 0,
            },
            "batch_records": [],
        }

    batches = [
        unique_items[i : i + batch_size] for i in range(0, len(unique_items), batch_size)
    ]

    lookup: dict[str, str] = {}
    batch_records: list[dict] = []
    fallback_items = 0
    fallback_item_names: set[str] = set()

    for idx, batch_items in enumerate(batches, start=1):
        batch_lookup, record = await _classify_single_batch(
            items=batch_items,
            batch_index=idx,
            total_batches=len(batches),
            max_retries=max_retries,
            max_tokens=max_tokens,
        )
        lookup.update(batch_lookup)
        batch_records.append(record)
        fallback_count = int(record.get("fallback_count", 0))
        fallback_items += fallback_count
        if fallback_count > 0:
            fallback_item_names.update(batch_items)

        if progress_callback is not None:
            payload = {
                "batch_index": idx,
                "total_batches": len(batches),
                "processed_items": len(lookup),
                "unique_items": len(unique_items),
                "record": record,
            }
            maybe_awaitable = progress_callback(payload)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable

    merged = []
    status_counts = {
        JUDGE_STATUS_MODEL: 0,
        JUDGE_STATUS_FALLBACK: 0,
        JUDGE_STATUS_UNANALYZED: 0,
    }
    for row in rows:
        new_row = dict(row)
        item_name = str(row.get("費用項目名稱", "")).strip()
        raw_label = lookup.get(item_name, "")
        level = _normalize_vcc_label(raw_label)
        if level:
            status = JUDGE_STATUS_MODEL
        elif item_name and item_name in fallback_item_names:
            status = JUDGE_STATUS_FALLBACK
        else:
            status = JUDGE_STATUS_UNANALYZED
        new_row["VCC適用等級"] = level
        new_row["VCC判定狀態"] = status
        new_row.pop("VCC判斷", None)
        merged.append(new_row)
        status_counts[status] = int(status_counts.get(status, 0)) + 1

    summary = {
        "total_rows": len(rows),
        "unique_items": len(unique_items),
        "total_batches": len(batches),
        "processed_batches": len(batches),
        "classified_items": len(lookup),
        "fallback_items": fallback_items,
        "judge_status_counts": status_counts,
    }
    logger.info(
        "批次分類完成: rows=%d unique=%d batches=%d fallback=%d",
        summary["total_rows"],
        summary["unique_items"],
        summary["total_batches"],
        summary["fallback_items"],
    )
    return merged, {"summary": summary, "batch_records": batch_records}


async def classify_items(
    rows: list[dict], company_name: str
) -> list[dict]:
    """呼叫 Claude API 為每筆費用判斷 VCC 適性。"""
    items_text = "\n".join(
        f"- {row['費用項目名稱']} (金額累計: {row['金額累計']}, 交易筆數: {row['交易筆數']})"
        for row in rows
    )

    user_prompt = (
        f"公司名稱：{company_name}\n\n"
        f"以下是該公司的費用項目列表，請為每一筆判斷 VCC 刷卡適性：\n\n{items_text}"
    )

    raw = await llm_complete(
        system_prompt=_load_filter_prompt(),
        user_prompt=user_prompt,
        tier="fast",
        max_tokens=DEFAULT_ANALYZE_MAX_TOKENS,
    )
    parsed = _parse_model_json(raw)
    if not isinstance(parsed, list):
        raise ValueError("模型輸出格式錯誤：預期 JSON Array")
    results = parsed
    logger.info("Claude 分類完成: %d 筆", len(results))
    return results


def merge_results(
    rows: list[dict], classifications: list[dict]
) -> list[dict]:
    """將分類結果合併回原始 CSV 資料。"""
    # 建立 itemName -> isVccSuitable 對照表
    lookup = {c["itemName"]: c["isVccSuitable"] for c in classifications}

    merged = []
    for row in rows:
        new_row = dict(row)
        item_name = row.get("費用項目名稱", "")
        raw_label = lookup.get(item_name, "")
        level = _normalize_vcc_label(raw_label)
        new_row["VCC適用等級"] = level
        new_row["VCC判定狀態"] = JUDGE_STATUS_MODEL if level else JUDGE_STATUS_UNANALYZED
        new_row.pop("VCC判斷", None)
        merged.append(new_row)
    return merged


def to_csv_string(rows: list[dict]) -> str:
    """將 list of dict 轉為 CSV 字串。"""
    if not rows:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()
