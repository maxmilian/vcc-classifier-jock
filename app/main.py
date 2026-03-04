import logging
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.services import classifier, categorizer
from app.services.gamma import check_status, generate_presentation

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="VCC Classifier Jock", version="0.1.0")

# 暫存目錄
TEMP_DIR = Path(tempfile.gettempdir()) / "vcc-classifier"
TEMP_DIR.mkdir(exist_ok=True)

# 靜態檔案
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path("app/static/index.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/analyze")
async def analyze(
    company_name: str = Form(...),
    tax_id: str = Form(...),
    file: UploadFile = File(...),
):
    """上傳 CSV，進行 VCC 適性分析。"""
    content = (await file.read()).decode("utf-8-sig")
    rows = classifier.parse_csv(content)

    missing = classifier.validate_csv_columns(rows)
    if missing:
        raise HTTPException(400, detail=f"CSV 缺少必要欄位: {', '.join(missing)}")

    if not rows:
        raise HTTPException(400, detail="CSV 無資料列")

    # 呼叫 Claude API 分類
    classifications = await classifier.classify_items(rows, company_name)

    # 合併結果
    merged = classifier.merge_results(rows, classifications)

    # 產生 CSV 檔案
    csv_content = classifier.to_csv_string(merged)
    filename = f"{company_name}_費用項目分析.csv"
    filepath = TEMP_DIR / filename
    filepath.write_text(csv_content, encoding="utf-8-sig")

    # 統計
    total = len(merged)
    vcc_yes = sum(1 for r in merged if r.get("VCC判斷") == "可以")
    vcc_no = sum(1 for r in merged if r.get("VCC判斷") == "不行")
    vcc_maybe = sum(1 for r in merged if r.get("VCC判斷") == "不確定")

    return {
        "company_name": company_name,
        "tax_id": tax_id,
        "filename": filename,
        "total_items": total,
        "vcc_yes": vcc_yes,
        "vcc_no": vcc_no,
        "vcc_maybe": vcc_maybe,
        "items": merged,
    }


@app.get("/api/download/{filename}")
async def download(filename: str):
    """下載分析 CSV。"""
    filepath = TEMP_DIR / filename
    if not filepath.exists():
        raise HTTPException(404, detail="檔案不存在")
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
    """接收 VCC 可行項目，分類後生成 Gamma 簡報。"""
    company_name = data.get("company_name", "")
    vcc_items = data.get("vcc_items", [])

    if not company_name:
        raise HTTPException(400, detail="缺少 company_name")
    if not vcc_items:
        raise HTTPException(400, detail="無 VCC 可行項目")

    # 用 Claude 分類為四類
    categories = await categorizer.categorize_items(vcc_items)

    # 讀取 vcc_ppt.txt 模板
    ppt_prompt_path = Path("app/prompts/vcc_ppt.txt")
    ppt_prompt = ppt_prompt_path.read_text(encoding="utf-8").strip()

    # 組合數據摘要
    summary_lines = [f"客戶名稱：{company_name}\n"]

    for cat_name in ["高頻次", "固定支出", "高單價", "其他"]:
        items = categories.get(cat_name, [])
        if items:
            summary_lines.append(f"\n### {cat_name}類別：")
            for item in items:
                summary_lines.append(
                    f"- {item['itemName']} (金額: {item.get('totalAmount', 'N/A')}, "
                    f"筆數: {item.get('txCount', 'N/A')}, "
                    f"均額: {item.get('avgAmount', 'N/A')})"
                )

    # 用 Claude 根據模板生成簡報 Markdown
    import anthropic
    from app.config import settings

    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    response = await client.messages.create(
        model=settings.anthropic_model_strong,
        max_tokens=4096,
        system=ppt_prompt,
        messages=[
            {
                "role": "user",
                "content": f"請根據以下數據生成簡報 Markdown：\n\n{''.join(summary_lines)}",
            }
        ],
    )
    presentation_markdown = response.content[0].text.strip()

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


@app.get("/api/gamma-status/{generation_id}")
async def gamma_status(generation_id: str):
    """查詢 Gamma 生成狀態。"""
    result = await check_status(generation_id)
    return result
