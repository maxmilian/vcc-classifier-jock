# VCC Classifier Jock

VCC（Virtual Credit Card）刷卡適性分類工具。上傳企業費用項目 CSV，透過 Claude AI 以 5 級制判斷各項目的 VCC 適用等級，並可自動生成 Gamma 簡報。

## 功能

1. **VCC 適性分析** — 上傳 CSV，AI 以 5 級制判斷每筆費用項目：絕對適合 / 高度適合 / 條件適合 / 需釐清 / 不適合
   - 分析模式採「多段批次」執行，支援進度輪詢與快取紀錄下載
   - 前端支援中斷續查（localStorage 記錄 `job_id`）與手動輸入 `job_id` 查詢
   - 結果新增 `VCC判定狀態`（模型判定 / 外部匯入 / 系統回退 / 未分析 / 分析失敗），避免未分析資料混入 `需釐清`
2. **費用分類** — 將 VCC 候選項目分為：高頻次、固定支出、高單價、其他
3. **兩階段簡報生成** — 先用 Claude 生成可編輯 Markdown，確認後再送 Gamma API 產生 PPTX
4. **雙模式上傳** —
   - 分析模式：上傳未標註 CSV，先做 VCC 判斷
   - PPT 模式：上傳已標註 CSV（含 `VCC適用等級`），直接進入簡報流程
5. **多公司支援** — 單一 CSV 含多間公司時，可切換處理各公司的簡報

## Tech Stack

- Python 3.12 / FastAPI / uvicorn
- Anthropic Claude API（分類與簡報文案）
- Gamma API（簡報生成）
- Docker / GCP Cloud Run

## 快速開始

```bash
# 安裝依賴
uv sync

# 設定環境變數
cp .env.example .env
# 編輯 .env 填入 API keys

# 啟動開發伺服器
uv run uvicorn app.main:app --reload --port 8080
```

開啟 http://localhost:8080 使用 Web UI。

## CSV 格式

上傳的 CSV 必須包含以下欄位：

| 欄位 | 說明 |
|------|------|
| `費用項目名稱` | 費用項目名稱 |
| `金額累計` | 該項目的累計金額 |
| `交易筆數` | 交易總筆數 |
| `交易日期起` | 最早交易日期 |
| `交易日期迄` | 最晚交易日期 |

PPT 模式（上傳已標註檔）另外需要：

| 欄位 | 說明 |
|------|------|
| `VCC適用等級` | 已判斷結果（絕對適合/高度適合/條件適合/需釐清/不適合） |

## API

| Method | Path | 說明 |
|--------|------|------|
| `GET` | `/` | Web UI |
| `GET` | `/health` | 健康檢查 |
| `POST` | `/api/analyze` | 上傳 CSV 建立分析任務（回傳 `job_id`） |
| `GET` | `/api/analyze-jobs/{job_id}` | 查詢多段分析進度與結果 |
| `GET` | `/api/analyze-jobs/{job_id}/cache` | 下載任務快取紀錄 JSON |
| `POST` | `/api/prepare-presentation-csv` | 上傳已標註 CSV，準備簡報資料 |
| `GET` | `/api/download/{filename}` | 下載分析結果 CSV |
| `POST` | `/api/generate-markdown` | 生成簡報 Markdown（第一階段） |
| `POST` | `/api/generate-gamma` | 將 Markdown 送 Gamma 生成簡報（第二階段） |
| `POST` | `/api/generate-presentation` | 相容舊流程：一步到位生成簡報 |
| `GET` | `/api/gamma-status/{generation_id}` | 查詢簡報生成狀態 |

API 錯誤回傳統一格式：

```json
{
  "error_code": "TOKEN_LIMIT_INPUT",
  "message": "模型輸入過長，請縮小批次或精簡內容。",
  "retryable": false,
  "provider_status": 400
}
```

常見 `error_code`：`TOKEN_LIMIT_INPUT`、`TOKEN_LIMIT_OUTPUT`、`CONTENT_BLOCKED`、`RATE_LIMITED`、`UPSTREAM_TIMEOUT`、`GAMMA_INPUT_TOO_LARGE`。

## 參數調整

- `ANALYZE_BATCH_SIZE`：分析批次大小（預設 `120`）
- `ANALYZE_MAX_TOKENS`：分析 LLM `max_tokens`（預設 `8192`）
- `PPT_MARKDOWN_MAX_TOKENS`：簡報 Markdown LLM `max_tokens`（預設 `4096`）
- `GAMMA_INPUT_MAX_CHARS`：送 Gamma 前 Markdown 字元上限（預設 `32000`；`0` 代表不限制）
- 註：input token 沒有可直接設定上限，只能透過縮短 prompt、調整輸出格式與 batch 大小控制有效規模。

## 部署

```bash
# 確保 .env 中已設定 GCP 相關變數
./deploy.sh
```

部署至 GCP Cloud Run（asia-east1）。需要在 `.env` 中設定 `GCP_PROJECT_ID`、`ARTIFACT_PROJECT_ID`、`ARTIFACT_REPO`。
