"""统一大模型服务配置与连通性测试模块。

为发布包提供统一内置的大模型服务凭据（开箱即用，无需测试人员手动配置）。
"""
from __future__ import annotations

import os
import time
from typing import Any

# 统一内置默认服务凭据
DEFAULT_OPENAI_API_KEY = "sk-Q7ltq1BNR1ouXNdKCDAPj1hWm3lYEvVsK7ok48g74AyBGN9Q"
DEFAULT_OPENAI_BASE_URL = "https://api.onefaka.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-5.6-sol"
DEFAULT_OPENAI_TIMEOUT = 120.0


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

    return ChatOpenAI(
        model=model or get_llm_model(),
        api_key=api_key or get_llm_api_key(),
        base_url=(base_url or get_llm_base_url()) or None,
        timeout=timeout if timeout is not None else get_llm_timeout(),
    )


def test_llm_connectivity(timeout: float = 15.0) -> dict[str, Any]:
    """测试当前大模型服务的网络与鉴权连通性。"""
    start_time = time.time()
    cfg = get_llm_config()
    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage

        llm = ChatOpenAI(
            model=cfg["model"],
            api_key=cfg["api_key"],
            base_url=cfg["base_url"] or None,
            timeout=timeout,
            max_retries=1,
        )
        response = llm.invoke([HumanMessage(content="ping")])
        latency_ms = int((time.time() - start_time) * 1000)
        reply = str(response.content if hasattr(response, "content") else response).strip()
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "model": cfg["model"],
            "base_url": cfg["base_url"],
            "message": f"连接成功 (响应耗时 {latency_ms}ms)",
            "sample_reply": reply[:100],
        }
    except Exception as exc:
        latency_ms = int((time.time() - start_time) * 1000)
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "model": cfg["model"],
            "base_url": cfg["base_url"],
            "error": str(exc),
            "message": f"连接失败: {exc}",
        }
