import json
import logging

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

CATEGORIZE_PROMPT = """你是 COMMEET 的費用分析專家。請將以下 VCC 可行的費用項目分成四類：

1. 高頻次：交易筆數高的項目（燃料費、停車費、計程車、捷運等交通移動類）
2. 固定支出：固定週期支出（電話費、SaaS、管理費、水電費等）
3. 高單價：單筆平均金額高的項目（機票、住宿、活動成本、員工旅遊等）
4. 其他：不屬於上述三類的

判斷依據：
- 「高頻次」優先看交易筆數，通常 > 50 筆且與外勤/交通相關
- 「固定支出」看是否為每月固定發生的營運費用
- 「高單價」看單筆平均金額，通常 > 5000 元
- 同一項目只歸入一類，優先順序：高頻次 > 固定支出 > 高單價 > 其他

只輸出合法 JSON，格式如下：
{
  "高頻次": [{"itemName": "...", "totalAmount": ..., "txCount": ..., "avgAmount": ...}, ...],
  "固定支出": [{"itemName": "...", "totalAmount": ..., "txCount": ..., "avgAmount": ...}, ...],
  "高單價": [{"itemName": "...", "totalAmount": ..., "txCount": ..., "avgAmount": ...}, ...],
  "其他": [{"itemName": "...", "totalAmount": ..., "txCount": ..., "avgAmount": ...}, ...]
}
"""


async def categorize_items(vcc_items: list[dict]) -> dict:
    """將 VCC 可行項目分成四類。"""
    items_text = "\n".join(
        f"- {item['費用項目名稱']} (金額累計: {item['金額累計']}, "
        f"交易筆數: {item['交易筆數']}, "
        f"單筆平均: {item.get('費用項目單筆平均金額', 'N/A')})"
        for item in vcc_items
    )

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.anthropic_model_strong,
        max_tokens=4096,
        system=CATEGORIZE_PROMPT,
        messages=[{"role": "user", "content": f"以下是 VCC 可行的費用項目：\n\n{items_text}"}],
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    result = json.loads(raw)
    logger.info(
        "分類完成: 高頻次=%d, 固定支出=%d, 高單價=%d, 其他=%d",
        len(result.get("高頻次", [])),
        len(result.get("固定支出", [])),
        len(result.get("高單價", [])),
        len(result.get("其他", [])),
    )
    return result
