from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import sys
from threading import Barrier
from types import ModuleType
from unittest import mock

import pytest

from agent_loop_system.tools.llm_config import (
    LLM_API_KEY_SCOPE_EXPLORATION,
    LLM_API_KEY_SCOPE_FIXED,
    create_chat_llm,
    get_llm_api_key,
    llm_api_key_scope,
)


def _keys() -> dict[str, str]:
    return {
        "OPENAI_API_KEY": "shared-key",
        "OPENAI_API_KEY_EXPLORATION": "exploration-key",
        "OPENAI_API_KEY_FIXED": "fixed-key",
    }


def test_scoped_keys_override_the_shared_key_and_restore_nested_scope() -> None:
    with mock.patch.dict(os.environ, _keys(), clear=True):
        assert get_llm_api_key() == "shared-key"
        assert get_llm_api_key(LLM_API_KEY_SCOPE_EXPLORATION) == "exploration-key"
        assert get_llm_api_key(LLM_API_KEY_SCOPE_FIXED) == "fixed-key"

        with llm_api_key_scope(LLM_API_KEY_SCOPE_EXPLORATION):
            assert get_llm_api_key() == "exploration-key"
            with llm_api_key_scope(LLM_API_KEY_SCOPE_FIXED):
                assert get_llm_api_key() == "fixed-key"
            assert get_llm_api_key() == "exploration-key"

        assert get_llm_api_key() == "shared-key"


@pytest.mark.parametrize(
    ("scope", "scoped_name"),
    [
        (LLM_API_KEY_SCOPE_EXPLORATION, "OPENAI_API_KEY_EXPLORATION"),
        (LLM_API_KEY_SCOPE_FIXED, "OPENAI_API_KEY_FIXED"),
    ],
)
def test_missing_scoped_key_falls_back_to_shared_key(scope: str, scoped_name: str) -> None:
    environment = _keys()
    environment.pop(scoped_name)
    with mock.patch.dict(os.environ, environment, clear=True):
        assert get_llm_api_key(scope) == "shared-key"


def test_unknown_scope_is_rejected() -> None:
    with pytest.raises(ValueError, match="未知大模型 API key 作用域"):
        get_llm_api_key("unexpected")


def test_worker_threads_bind_their_own_scope_without_mutating_environment() -> None:
    barrier = Barrier(2)

    def resolve(scope: str) -> str:
        with llm_api_key_scope(scope):
            barrier.wait(timeout=5)
            return get_llm_api_key()

    with mock.patch.dict(os.environ, _keys(), clear=True):
        with ThreadPoolExecutor(max_workers=2) as executor:
            exploration = executor.submit(resolve, LLM_API_KEY_SCOPE_EXPLORATION)
            fixed = executor.submit(resolve, LLM_API_KEY_SCOPE_FIXED)
            assert exploration.result(timeout=5) == "exploration-key"
            assert fixed.result(timeout=5) == "fixed-key"
        assert get_llm_api_key() == "shared-key"


def test_create_chat_llm_resolves_the_requested_scope() -> None:
    constructor = mock.Mock()
    fake_langchain_openai = ModuleType("langchain_openai")
    fake_langchain_openai.ChatOpenAI = constructor
    with (
        mock.patch.dict(os.environ, _keys(), clear=True),
        mock.patch.dict(sys.modules, {"langchain_openai": fake_langchain_openai}),
        mock.patch(
            "agent_loop_system.tools.llm_config._create_http_client",
            return_value=None,
        ),
    ):
        create_chat_llm(api_key_scope=LLM_API_KEY_SCOPE_FIXED)

    assert constructor.call_args.kwargs["api_key"] == "fixed-key"
