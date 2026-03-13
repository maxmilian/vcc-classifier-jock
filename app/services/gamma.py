import asyncio
import logging
from pathlib import Path

import httpx

from app.config import settings
from app.errors import AppError

logger = logging.getLogger(__name__)

GAMMA_API_BASE = "https://public-api.gamma.app/v1.0"
GAMMA_GENERATE_ENDPOINT = f"{GAMMA_API_BASE}/generations"

HTTP_TIMEOUT_DEFAULT = 30.0
HTTP_TIMEOUT_LONG = 60.0
GAMMA_MAX_WAIT_SECONDS = 120
GAMMA_POLL_INTERVAL = 3.0

_gamma_prompt_cache: str | None = None


def _load_gamma_prompt() -> str:
    global _gamma_prompt_cache
    if _gamma_prompt_cache is not None:
        return _gamma_prompt_cache

    prompt_path = Path(settings.gamma_prompt_file)
    if not prompt_path.exists():
        return ""

    _gamma_prompt_cache = prompt_path.read_text(encoding="utf-8").strip()
    return _gamma_prompt_cache


def _parse_response(result: dict) -> dict:
    return {
        "generation_id": result.get("generationId"),
        "status": result.get("status"),
        "gamma_url": result.get("gammaUrl"),
        "export_url": result.get("exportUrl"),
    }


def _map_gamma_http_error(exc: Exception, default_message: str) -> AppError:
    if isinstance(exc, httpx.TimeoutException):
        return AppError(
            status_code=504,
            error_code="UPSTREAM_TIMEOUT",
            message="Gamma API 逾時，請稍後再試。",
            retryable=True,
        )

    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        status = response.status_code
        body = (response.text or "").lower()

        if status == 429:
            return AppError(
                status_code=429,
                error_code="RATE_LIMITED",
                message="Gamma API 請求過於頻繁，請稍後再試。",
                retryable=True,
                provider_status=status,
            )
        if status in {408, 504}:
            return AppError(
                status_code=504,
                error_code="UPSTREAM_TIMEOUT",
                message="Gamma API 回應逾時，請稍後再試。",
                retryable=True,
                provider_status=status,
            )
        if status in {401, 403}:
            return AppError(
                status_code=502,
                error_code="UPSTREAM_AUTH_FAILED",
                message="Gamma API 認證失敗，請檢查 API Key。",
                retryable=False,
                provider_status=status,
            )
        if "blocked" in body or "policy" in body or "safety" in body:
            return AppError(
                status_code=400,
                error_code="CONTENT_BLOCKED",
                message="內容觸發 Gamma 安全政策，請調整內容後重試。",
                retryable=False,
                provider_status=status,
            )
        if "too long" in body or "token" in body:
            return AppError(
                status_code=400,
                error_code="TOKEN_LIMIT_INPUT",
                message="送至 Gamma 的內容過長，請精簡後再送出。",
                retryable=False,
                provider_status=status,
            )
        if status >= 500:
            return AppError(
                status_code=502,
                error_code="UPSTREAM_ERROR",
                message="Gamma API 服務異常，請稍後再試。",
                retryable=True,
                provider_status=status,
            )

        return AppError(
            status_code=502,
            error_code="UPSTREAM_ERROR",
            message=default_message,
            retryable=False,
            provider_status=status,
        )

    return AppError(
        status_code=502,
        error_code="UPSTREAM_ERROR",
        message=default_message,
        retryable=True,
    )


async def generate_presentation(
    text: str,
    company_name: str,
    num_cards: int | None = None,
    export_format: str = "pptx",
) -> dict:
    """使用 Gamma API 從純文字生成簡報並等待完成。"""
    if not settings.gamma_api_key:
        raise AppError(
            status_code=500,
            error_code="MISSING_PROVIDER_KEY",
            message="Gamma API Key 未設定",
            retryable=False,
        )

    payload = {
        "inputText": text,
        "textMode": "generate",
        "format": "presentation",
        "textOptions": {"amount": "medium", "language": "zh-tw"},
        "imageOptions": {
            "source": "aiGenerated",
            "model": settings.gamma_image_model,
            "style": settings.gamma_image_style,
        },
        "cardOptions": {
            "headerFooter": {
                "bottomLeft": {
                    "type": "image",
                    "source": "themeLogo",
                    "size": "md",
                }
            }
        },
        "sharingOptions": {"externalAccess": "view"},
    }

    if num_cards and num_cards > 0:
        payload["numCards"] = num_cards
    if settings.gamma_theme_id:
        payload["themeId"] = settings.gamma_theme_id
    if export_format in ("pptx", "pdf"):
        payload["exportAs"] = export_format

    additional = _load_gamma_prompt()
    if additional:
        payload["additionalInstructions"] = additional

    headers = {"X-API-KEY": settings.gamma_api_key, "Content-Type": "application/json"}

    logger.info("發送 Gamma API 請求: %d 字元", len(text))

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_LONG) as client:
            response = await client.post(GAMMA_GENERATE_ENDPOINT, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Gamma generate request failed")
        raise _map_gamma_http_error(exc, "Gamma 生成請求失敗") from exc

    warnings = result.get("warnings")
    if warnings:
        logger.info("Gamma API 警告: %s", warnings)

    parsed = _parse_response(result)

    if parsed.get("status") == "completed":
        return parsed

    generation_id = parsed.get("generation_id")
    if not generation_id:
        return parsed

    # Polling
    elapsed = 0.0
    while elapsed < GAMMA_MAX_WAIT_SECONDS:
        await asyncio.sleep(GAMMA_POLL_INTERVAL)
        elapsed += GAMMA_POLL_INTERVAL

        status_result = await check_status(generation_id)
        logger.info("Gamma 生成狀態: %s (%.0fs)", status_result.get("status"), elapsed)

        if status_result.get("status") in ("completed", "failed"):
            return status_result

    raise AppError(
        status_code=504,
        error_code="UPSTREAM_TIMEOUT",
        message=f"Gamma 簡報生成超時 ({GAMMA_MAX_WAIT_SECONDS}s)",
        retryable=True,
    )


async def check_status(generation_id: str) -> dict:
    """查詢 Gamma 簡報生成狀態。"""
    if not settings.gamma_api_key:
        raise AppError(
            status_code=500,
            error_code="MISSING_PROVIDER_KEY",
            message="Gamma API Key 未設定",
            retryable=False,
        )

    headers = {"X-API-KEY": settings.gamma_api_key}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_DEFAULT) as client:
            response = await client.get(
                f"{GAMMA_GENERATE_ENDPOINT}/{generation_id}", headers=headers
            )
            response.raise_for_status()
            result = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Gamma status request failed")
        raise _map_gamma_http_error(exc, "Gamma 狀態查詢失敗") from exc

    parsed = _parse_response(result)
    parsed["generation_id"] = generation_id
    return parsed
