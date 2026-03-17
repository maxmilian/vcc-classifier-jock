# VCC Classifier Jock

## 專案概述

VCC 刷卡適性分類工具。FastAPI 應用，透過 Claude API 以 5 級制分析企業費用項目的 VCC 適性，並可透過 Gamma API 生成提案簡報。

## 專案結構

```
app/
├── main.py              # FastAPI 路由 + 業務邏輯（analyze, download, generate-markdown, generate-gamma 等）
├── config.py            # pydantic-settings 設定（從 .env 讀取）
├── errors.py            # 統一錯誤處理（AppError + HTTP error code 映射）
├── services/
│   ├── classifier.py    # CSV 解析 + Claude 批次分類（5 級 VCC 適性判斷）
│   ├── categorizer.py   # Claude 將 VCC 候選項目分四類（高頻次/固定支出/高單價/其他）
│   ├── gamma.py         # Gamma API 簡報生成 + polling
│   ├── job_manager.py   # 非同步分析任務管理（狀態、快取、TTL 清理）
│   └── llm.py           # Anthropic Claude API 封裝（統一 client 管理）
├── prompts/
│   ├── vcc_filter.txt   # VCC 適性判斷 system prompt（5 級制）
│   ├── vcc_ppt.txt      # 簡報 Markdown 生成 prompt
│   └── gamma_prompt.txt # Gamma additional instructions
└── static/
    └── index.html       # Web UI（含多公司切換、Markdown 編輯器、進度輪詢）
```

## 開發指令

```bash
uv sync                                          # 安裝依賴
uv run uvicorn app.main:app --reload --port 8080  # 開發伺服器
./deploy.sh                                       # 部署到 Cloud Run
```

## 重要慣例

- 套件管理使用 `uv`，不使用 pip
- 環境變數放 `.env`（已 gitignore），範本見 `.env.example`
- Claude API 使用兩個 model tier：`fast`（分類）和 `strong`（簡報文案），統一由 `llm.py` 管理
- VCC 適性使用 5 級制：絕對適合、高度適合、條件適合、需釐清、不適合
- 分析任務為非同步執行，透過 `job_manager.py` 管理狀態與 TTL 清理
- 簡報生成分兩階段：先 generate-markdown（可編輯），再 generate-gamma（送出）
- `.env` 包含 GCP 部署設定（`GCP_PROJECT_ID` 等），不可硬編碼於程式碼中
- 分析參數（`ANALYZE_BATCH_SIZE`、`ANALYZE_MAX_TOKENS`、`PPT_MARKDOWN_MAX_TOKENS`、`GAMMA_INPUT_MAX_CHARS`）透過環境變數設定，`deploy.sh` 會檢查這些變數是否存在
- `/health` 端點回傳 hardcoded 版本號與最後更新日期，版本更新時需手動修改 `main.py`
