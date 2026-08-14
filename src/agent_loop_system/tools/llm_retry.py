"""LLM 调用重试：指数退避，供 agent/test/defect_store 共用。

背景：第三方 LLM 代理空响应率约 67%，单次调用不可靠。
策略：最多 12 次实际调用，失败后按 1/2/4/8/16/32/60x5 秒退避，
耗尽后抛 LLMRetryError，由调用方映射为 CANNOT_VERIFY。
"""
from __future__ import annotations

import os
import time

_RETRY_DELAYS = (1, 2, 4, 8, 16, 32, 60, 60, 60, 60, 60)
_NON_RETRYABLE_ERROR_MARKERS = (
    "insufficient account balance",
    "insufficient_quota",
    "invalid_api_key",
    "incorrect api key",
)


class LLMRetryError(RuntimeError):
    """12 次重试全部失败。"""


def _is_non_retryable_error(exc: Exception) -> bool:
    message = f"{type(exc).__name__}: {exc}".casefold()
    return any(marker in message for marker in _NON_RETRYABLE_ERROR_MARKERS)


def get_llm_request_timeout() -> float:
    """Return the per-request timeout shared by every LLM caller."""
    default_timeout = 120.0
    try:
        timeout = float(os.environ.get("OPENAI_REQUEST_TIMEOUT", default_timeout))
    except (TypeError, ValueError):
        return default_timeout
    return timeout if timeout > 0 else default_timeout


def invoke_with_retry(
    invoke,
    *,
    is_valid=lambda value: value is not None,
    max_attempts: int = 12,
):
    """调用 invoke，异常或无效结果时指数退避重试；耗尽后抛 LLMRetryError。

    - 第 1 次立即调用。
    - 失败后按 _RETRY_DELAYS 递增等待；最后一次失败后不再等待。
    - 调用抛异常、返回无效值（由 is_valid 判定）都算本次失败。
    - 错误消息只保留最后一次失败摘要，不包含 API key。
    """
    last_error = "LLM 调用失败"
    for attempt in range(max_attempts):
        try:
            value = invoke()
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            if _is_non_retryable_error(exc):
                raise LLMRetryError(last_error) from exc
            if attempt < max_attempts - 1:
                time.sleep(_RETRY_DELAYS[attempt])
            continue
        if not is_valid(value):
            last_error = f"LLM 返回无效结果: {type(value).__name__}"
            if attempt < max_attempts - 1:
                time.sleep(_RETRY_DELAYS[attempt])
            continue
        return value
    raise LLMRetryError(last_error)
