import csv
import io
import json
import logging
from pathlib import Path

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

_filter_prompt_cache: str | None = None


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

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.anthropic_model_fast,
        max_tokens=4096,
        system=_load_filter_prompt(),
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    # 處理可能被 markdown 包裹的 JSON
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    results = json.loads(raw)
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
        new_row["VCC判斷"] = lookup.get(item_name, "")
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
