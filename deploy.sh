#!/bin/bash

# VCC Classifier Jock - Cloud Run 部署腳本

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

PROJECT_ID="cobalt-anchor-267306"
SERVICE_NAME="vcc-classifier-jock"
REGION="asia-east1"

ARTIFACT_PROJECT_ID="commeet-artifacts-repo"
ARTIFACT_REGION="asia-east1"
ARTIFACT_REPO="commeet-docker-repo-1"

# 讀取 .env
if [ -f .env ]; then
    echo -e "${BLUE}ℹ 從 .env 檔案讀取設定...${NC}"
    source .env 2>/dev/null
fi

# 驗證必要變數
MISSING=""
[ -z "$ANTHROPIC_API_KEY" ] && MISSING="$MISSING ANTHROPIC_API_KEY"
[ -z "$GAMMA_API_KEY" ] && MISSING="$MISSING GAMMA_API_KEY"

if [ -n "$MISSING" ]; then
    echo -e "${RED}錯誤: .env 缺少必要變數:${MISSING}${NC}"
    exit 1
fi

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}部署設定${NC}"
echo -e "${YELLOW}========================================${NC}"
echo -e "GCP 專案ID: ${GREEN}${PROJECT_ID}${NC}"
echo -e "服務名稱:   ${GREEN}${SERVICE_NAME}${NC}"
echo -e "部署區域:   ${GREEN}${REGION}${NC}"
echo ""

# 設定 GCP
gcloud config set project $PROJECT_ID
gcloud auth configure-docker ${ARTIFACT_REGION}-docker.pkg.dev --quiet

# 構建 Docker 映像
IMAGE_NAME="${ARTIFACT_REGION}-docker.pkg.dev/${ARTIFACT_PROJECT_ID}/${ARTIFACT_REPO}/${SERVICE_NAME}"
echo -e "${YELLOW}構建 Docker 映像 (linux/amd64)...${NC}"
docker build --platform linux/amd64 -t $IMAGE_NAME .

if [ $? -ne 0 ]; then
    echo -e "${RED}錯誤: Docker 映像建構失敗${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker 映像建構成功${NC}"

# 推送映像
echo -e "${YELLOW}推送映像到 Artifact Registry...${NC}"
docker push $IMAGE_NAME

if [ $? -ne 0 ]; then
    echo -e "${RED}錯誤: 映像推送失敗${NC}"
    exit 1
fi
echo -e "${GREEN}✓ 映像推送成功${NC}"

# 部署到 Cloud Run
echo -e "${YELLOW}部署到 Cloud Run...${NC}"

gcloud run deploy $SERVICE_NAME \
    --image $IMAGE_NAME \
    --platform managed \
    --region $REGION \
    --allow-unauthenticated \
    --set-env-vars "^::^ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}::ANTHROPIC_MODEL_FAST=${ANTHROPIC_MODEL_FAST:-claude-sonnet-4-6}::ANTHROPIC_MODEL_STRONG=${ANTHROPIC_MODEL_STRONG:-claude-opus-4-6}::GAMMA_API_KEY=${GAMMA_API_KEY}::GAMMA_THEME_ID=${GAMMA_THEME_ID}::GAMMA_IMAGE_MODEL=${GAMMA_IMAGE_MODEL:-flux-1-quick}::GAMMA_IMAGE_STYLE=${GAMMA_IMAGE_STYLE:-professional, clean, modern business}::GAMMA_PROMPT_FILE=${GAMMA_PROMPT_FILE:-app/prompts/gamma_prompt.txt}" \
    --memory 1Gi \
    --timeout 300s \
    --max-instances 10 \
    --min-instances 0

if [ $? -ne 0 ]; then
    echo -e "${RED}錯誤: Cloud Run 部署失敗${NC}"
    exit 1
fi

SERVICE_URL=$(gcloud run services describe $SERVICE_NAME --region $REGION --format='value(status.url)')

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✓ 部署成功！${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "服務URL: ${YELLOW}$SERVICE_URL${NC}"
echo -e "健康檢查: ${BLUE}curl $SERVICE_URL/health${NC}"
