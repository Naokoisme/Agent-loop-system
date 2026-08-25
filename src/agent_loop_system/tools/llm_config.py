"""统一大模型服务配置与连通性测试模块。

发布包只提供非敏感默认参数；API Key 必须由用户环境显式配置。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import os
import ssl
import time
from typing import Any

# 非敏感默认参数；正式发布物严禁内置 API 凭据。
DEFAULT_OPENAI_API_KEY = ""
DEFAULT_OPENAI_BASE_URL = "https://api.onefaka.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-5.6-sol"
DEFAULT_OPENAI_TIMEOUT = 120.0

LLM_API_KEY_SCOPE_EXPLORATION = "exploration"
LLM_API_KEY_SCOPE_FIXED = "fixed"
_SCOPED_API_KEY_ENV = {
    LLM_API_KEY_SCOPE_EXPLORATION: "OPENAI_API_KEY_EXPLORATION",
    LLM_API_KEY_SCOPE_FIXED: "OPENAI_API_KEY_FIXED",
}
_SCOPED_MODEL_ENV = {
    LLM_API_KEY_SCOPE_EXPLORATION: "OPENAI_EXPLORATION_MODEL",
    LLM_API_KEY_SCOPE_FIXED: "OPENAI_FIXED_MODEL",
}
_CURRENT_API_KEY_SCOPE: ContextVar[str | None] = ContextVar(
    "agent_loop_openai_api_key_scope",
    default=None,
)


def _normalize_api_key_scope(scope: str | None) -> str | None:
    if scope is None:
        return None
    normalized = str(scope).strip().lower()
    if normalized not in _SCOPED_API_KEY_ENV:
        raise ValueError(f"未知大模型 API key 作用域: {scope!r}")
    return normalized


@contextmanager
def llm_api_key_scope(scope: str) -> Iterator[None]:
    """在当前调用上下文中选择专用密钥和模型，不修改进程环境。"""

    token = _CURRENT_API_KEY_SCOPE.set(_normalize_api_key_scope(scope))
    try:
        yield
    finally:
        _CURRENT_API_KEY_SCOPE.reset(token)


def get_llm_ca_bundle() -> str | None:
    """返回显式 CA bundle；留空时使用系统信任链。"""

    value = os.environ.get("OPENAI_CA_BUNDLE", "").strip()
    return value or None


def _create_compatible_ssl_context() -> ssl.SSLContext:
    """创建默认验证证书的 SSL 上下文，并允许显式配置企业 CA。"""

    ca_bundle = get_llm_ca_bundle()
    ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
    if os.environ.get("OPENAI_TLS_ALLOW_LEGACY_CIPHERS", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    return ctx


def _create_http_client(timeout: float | None = None) -> Any:
    """创建并返回显式启用证书验证的 httpx.Client 实例。"""
    try:
        import httpx
    except ImportError:
        return None
    ctx = _create_compatible_ssl_context()
    t = timeout if timeout is not None else get_llm_timeout()
    return httpx.Client(verify=ctx, timeout=t)


def get_llm_api_key(scope: str | None = None) -> str:
    """获取当前生效的 API Key；专用 key 缺失时兼容旧配置。"""

    selected_scope = _normalize_api_key_scope(scope)
    if selected_scope is None:
        selected_scope = _CURRENT_API_KEY_SCOPE.get()
    if selected_scope is not None:
        scoped_key = os.environ.get(_SCOPED_API_KEY_ENV[selected_scope], "").strip()
        if scoped_key and not scoped_key.startswith("暂时"):
            return scoped_key

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    return "" if key.startswith("暂时") else key


def get_llm_base_url() -> str:
    """获取当前生效的 Base URL。"""
    url = os.environ.get("OPENAI_BASE_URL", "").strip()
    if not url:
        return DEFAULT_OPENAI_BASE_URL
    return url


def get_llm_model(scope: str | None = None) -> str:
    """获取当前生效的模型名；专用模型缺失时兼容旧配置。"""
    selected_scope = _normalize_api_key_scope(scope)
    if selected_scope is None:
        selected_scope = _CURRENT_API_KEY_SCOPE.get()
    if selected_scope is not None:
        scoped_model = os.environ.get(_SCOPED_MODEL_ENV[selected_scope], "").strip()
        if scoped_model:
            return scoped_model

    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not model:
        return DEFAULT_OPENAI_MODEL
    return model


def get_llm_timeout() -> float:
    """获取大模型请求超时时间。"""
    raw = os.environ.get("OPENAI_REQUEST_TIMEOUT") or os.environ.get("OPENAI_TIMEOUT")
    if raw:
        try:
            return float(raw)
        except (ValueError, TypeError):
            pass
    return DEFAULT_OPENAI_TIMEOUT


def get_llm_config() -> dict[str, Any]:
    """返回全套已解析的大模型运行时配置。"""
    return {
        "api_key": get_llm_api_key(),
        "base_url": get_llm_base_url(),
        "model": get_llm_model(),
        "timeout": get_llm_timeout(),
        "ca_bundle": get_llm_ca_bundle(),
        "is_builtin": bool(DEFAULT_OPENAI_API_KEY)
        and get_llm_api_key() == DEFAULT_OPENAI_API_KEY,
    }


def create_chat_llm(
    *,
    model: str | None = None,
    api_key: str | None = None,
    api_key_scope: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
):
    """创建并返回 ChatOpenAI 实例。若缺少依赖则返回 None。"""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        return None

    client = _create_http_client(timeout)
    kwargs: dict[str, Any] = {
        "model": model or get_llm_model(api_key_scope),
        "api_key": api_key or get_llm_api_key(api_key_scope),
        "base_url": (base_url or get_llm_base_url()) or None,
        "timeout": timeout if timeout is not None else get_llm_timeout(),
    }
    if client is not None:
        kwargs["http_client"] = client

    return ChatOpenAI(**kwargs)


def test_llm_connectivity(timeout: float = 20.0) -> dict[str, Any]:
    """测试当前大模型服务的网络与鉴权连通性，并返回详细的诊断信息。"""
    start_time = time.time()
    cfg = get_llm_config()
    model_name = cfg["model"]
    base_url = cfg["base_url"]
    api_key = cfg["api_key"]

    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage

        client = _create_http_client(timeout)
        kwargs: dict[str, Any] = {
            "model": model_name,
            "api_key": api_key,
            "base_url": base_url or None,
            "timeout": timeout,
            "max_retries": 1,
            "max_tokens": 10,
        }
        if client is not None:
            kwargs["http_client"] = client

        llm = ChatOpenAI(**kwargs)
        response = llm.invoke([HumanMessage(content="Hello! Respond with: pong")])
        latency_ms = int((time.time() - start_time) * 1000)
        reply = str(response.content if hasattr(response, "content") else response).strip()
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "model": model_name,
            "base_url": base_url,
            "message": f"连接成功！响应耗时 {latency_ms}ms",
            "sample_reply": reply[:100],
        }
    except Exception as exc:
        latency_ms = int((time.time() - start_time) * 1000)
        exc_str = str(exc)
        exc_type = type(exc).__name__

        # 细化错误诊断
        error_category = "请求异常"
        error_code = "LLM_REQUEST_FAILED"
        suggestion = "请检查网络连接或稍后重试"

        if "UNEXPECTED_EOF" in exc_str or "SSL" in exc_str:
            error_category = "SSL/TLS 握手失败"
            error_code = "LLM_TLS_ERROR"
            suggestion = "服务端连接协商异常或证书握手被中断"
        elif "timeout" in exc_str.lower() or "timed out" in exc_str.lower():
            error_category = "网络连接超时"
            error_code = "LLM_TIMEOUT"
            suggestion = f"在 {timeout}s 内未收到服务端响应，请检查外网连通性或当前网关负载"
        elif "401" in exc_str or "Unauthorized" in exc_str or "auth" in exc_str.lower():
            error_category = "API Key 鉴权失败"
            error_code = "LLM_AUTH_FAILED"
            suggestion = "API Key 凭据失效或未授权访问该模型"
        elif "404" in exc_str or "NotFound" in exc_str or ("model" in exc_str.lower() and "exist" in exc_str.lower()):
            error_category = "模型不存在"
            error_code = "LLM_MODEL_NOT_FOUND"
            suggestion = f"网关 {base_url} 未部署或未提供模型 {model_name}"
        elif "429" in exc_str or "rate limit" in exc_str.lower() or "quota" in exc_str.lower():
            error_category = "额度不足或频次超限"
            error_code = "LLM_QUOTA_EXCEEDED"
            suggestion = "网关余额不足或请求过于频繁"

        return {
            "ok": False,
            "latency_ms": latency_ms,
            "model": model_name,
            "base_url": base_url,
            "error_type": exc_type,
            "error_category": error_category,
            "error_code": error_code,
            "reason_code": error_code,
            "error": exc_str,
            "suggestion": suggestion,
            "message": f"[{error_category}] {exc_str}",
        }
