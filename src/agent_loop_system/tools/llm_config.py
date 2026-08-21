"""统一大模型服务配置与连通性测试模块。

为发布包提供统一内置的大模型服务凭据（开箱即用，无需测试人员手动配置）。
"""
from __future__ import annotations

import os
import ssl
import time
from typing import Any

# 统一内置默认服务凭据
DEFAULT_OPENAI_API_KEY = "sk-Q7ltq1BNR1ouXNdKCDAPj1hWm3lYEvVsK7ok48g74AyBGN9Q"
DEFAULT_OPENAI_BASE_URL = "https://api.onefaka.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-5.6-sol"
DEFAULT_OPENAI_TIMEOUT = 120.0


def _create_compatible_ssl_context() -> ssl.SSLContext:
    """创建高兼容性 SSL 上下文（解决部分网关 renegotiation 与 SECLEVEL=2 导致的 UNEXPECTED_EOF 握手失败）。"""
    ctx = ssl.create_default_context()
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    except Exception:
        pass
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _create_http_client(timeout: float | None = None) -> Any:
    """创建并返回配置了高兼容性 SSL 上下文的 httpx.Client 实例。"""
    try:
        import httpx
        ctx = _create_compatible_ssl_context()
        t = timeout if timeout is not None else get_llm_timeout()
        return httpx.Client(verify=ctx, timeout=t)
    except Exception:
        return None


def get_llm_api_key() -> str:
    """获取当前生效的 API Key（优先环境变量，其次内置默认）。"""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key or key.startswith("暂时"):
        return DEFAULT_OPENAI_API_KEY
    return key


def get_llm_base_url() -> str:
    """获取当前生效的 Base URL。"""
    url = os.environ.get("OPENAI_BASE_URL", "").strip()
    if not url:
        return DEFAULT_OPENAI_BASE_URL
    return url


def get_llm_model() -> str:
    """获取当前生效的模型名。"""
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
        "is_builtin": (get_llm_api_key() == DEFAULT_OPENAI_API_KEY),
    }


def create_chat_llm(
    *,
    model: str | None = None,
    api_key: str | None = None,
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
        "model": model or get_llm_model(),
        "api_key": api_key or get_llm_api_key(),
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
        suggestion = "请检查网络连接或稍后重试"

        if "UNEXPECTED_EOF" in exc_str or "SSL" in exc_str:
            error_category = "SSL/TLS 握手失败"
            suggestion = "服务端连接协商异常或证书握手被中断"
        elif "timeout" in exc_str.lower() or "timed out" in exc_str.lower():
            error_category = "网络连接超时"
            suggestion = f"在 {timeout}s 内未收到服务端响应，请检查外网连通性或当前网关负载"
        elif "401" in exc_str or "Unauthorized" in exc_str or "auth" in exc_str.lower():
            error_category = "API Key 鉴权失败"
            suggestion = "内置 Token 凭据失效或未授权访问该模型"
        elif "404" in exc_str or "NotFound" in exc_str or ("model" in exc_str.lower() and "exist" in exc_str.lower()):
            error_category = "模型不存在"
            suggestion = f"网关 {base_url} 未部署或未提供模型 {model_name}"
        elif "429" in exc_str or "rate limit" in exc_str.lower() or "quota" in exc_str.lower():
            error_category = "额度不足或频次超限"
            suggestion = "网关余额不足或请求过于频繁"

        return {
            "ok": False,
            "latency_ms": latency_ms,
            "model": model_name,
            "base_url": base_url,
            "error_type": exc_type,
            "error_category": error_category,
            "error": exc_str,
            "suggestion": suggestion,
            "message": f"[{error_category}] {exc_str}",
        }
