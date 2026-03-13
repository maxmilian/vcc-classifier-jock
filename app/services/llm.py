import logging

from app.errors import AppError
from app.config import settings

logger = logging.getLogger(__name__)

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None


def _extract_provider_status(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _extract_provider_message(exc: Exception) -> str:
    message = str(exc).strip() or "LLM 供應商回傳錯誤"
    response = getattr(exc, "response", None)
    response_text = getattr(response, "text", None)
    if isinstance(response_text, str) and response_text.strip():
        return f"{message} | {response_text.strip()}"
    return message


def _map_provider_error(exc: Exception) -> AppError:
    status = _extract_provider_status(exc)
    raw_message = _extract_provider_message(exc)
    message_lower = raw_message.lower()

    if status == 429 or "rate limit" in message_lower:
        return AppError(
            status_code=429,
            error_code="RATE_LIMITED",
            message="LLM 請求過於頻繁，請稍後再試。",
            retryable=True,
            provider_status=status,
        )

    if status in {408, 504} or "timeout" in message_lower:
        return AppError(
            status_code=504,
            error_code="UPSTREAM_TIMEOUT",
            message="LLM 回應逾時，請稍後再試。",
            retryable=True,
            provider_status=status,
        )

    if any(token in message_lower for token in ["policy", "safety", "blocked", "disallowed"]):
        return AppError(
            status_code=400,
            error_code="CONTENT_BLOCKED",
            message="內容觸發供應商安全政策，請調整後重試。",
            retryable=False,
            provider_status=status,
        )

    if any(token in message_lower for token in ["max_tokens", "output token", "output too long"]):
        return AppError(
            status_code=400,
            error_code="TOKEN_LIMIT_OUTPUT",
            message="模型輸出超過上限，請縮小批次或降低輸出內容。",
            retryable=False,
            provider_status=status,
        )

    if any(token in message_lower for token in ["prompt is too long", "input too long", "context length"]):
        return AppError(
            status_code=400,
            error_code="TOKEN_LIMIT_INPUT",
            message="模型輸入過長，請縮小批次或精簡內容。",
            retryable=False,
            provider_status=status,
        )

    if isinstance(status, int) and status >= 500:
        return AppError(
            status_code=502,
            error_code="UPSTREAM_ERROR",
            message="LLM 供應商服務異常，請稍後再試。",
            retryable=True,
            provider_status=status,
        )

    if status == 400:
        return AppError(
            status_code=400,
            error_code="UPSTREAM_BAD_REQUEST",
            message="LLM 請求參數不正確，請調整設定後重試。",
            retryable=False,
            provider_status=status,
        )

    return AppError(
        status_code=502,
        error_code="UPSTREAM_ERROR",
        message="LLM 供應商回傳未知錯誤。",
        retryable=True,
        provider_status=status,
    )


def _resolve_anthropic_key() -> str:
    anthropic_key = (settings.anthropic_api_key or "").strip()
    if not anthropic_key:
        raise AppError(
            status_code=500,
            error_code="MISSING_PROVIDER_KEY",
            message="ANTHROPIC_API_KEY 未設定",
            retryable=False,
        )
    return anthropic_key


def _pick_model(tier: str) -> str:
    normalized = tier.strip().lower()
    if normalized == "strong":
        return settings.anthropic_model_strong
    return settings.anthropic_model_fast


async def complete(
    *,
    system_prompt: str,
    user_prompt: str,
    tier: str = "fast",
    max_tokens: int = 4096,
) -> str:
    key = _resolve_anthropic_key()
    model = _pick_model(tier)

    if anthropic is None:  # pragma: no cover
        raise AppError(
            status_code=500,
            error_code="PROVIDER_SDK_MISSING",
            message="缺少 anthropic 套件，無法使用 Anthropic provider",
            retryable=False,
        )
    client = anthropic.AsyncAnthropic(api_key=key)
    try:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM request failed")
        raise _map_provider_error(exc) from exc
    return response.content[0].text if response.content else ""
