# VCC Classifier Jock

## 專案概述

VCC 刷卡適性分類工具。FastAPI 應用，透過 Claude API 分析企業費用項目的 VCC 適性，並可透過 Gamma API 生成提案簡報。

## 專案結構

```
app/
├── main.py              # FastAPI 路由（analyze, download, generate-presentation, gamma-status）
├── config.py            # pydantic-settings 設定（從 .env 讀取）
├── services/
│   ├── classifier.py    # CSV 解析 + Claude 分類（VCC 適性判斷）
│   ├── categorizer.py   # Claude 將 VCC 可行項目分四類（高頻次/固定支出/高單價/其他）
│   └── gamma.py         # Gamma API 簡報生成 + polling
├── prompts/
│   ├── vcc_filter.txt   # VCC 適性判斷 system prompt
│   ├── vcc_ppt.txt      # 簡報 Markdown 生成 prompt
│   └── gamma_prompt.txt # Gamma additional instructions
└── static/
    └── index.html       # Web UI
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
- Claude API 使用兩個 model：fast（分類）和 strong（簡報文案）
- `.env` 包含 GCP 部署設定（`GCP_PROJECT_ID` 等），不可硬編碼於程式碼中
