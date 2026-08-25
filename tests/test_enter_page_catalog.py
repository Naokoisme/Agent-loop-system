from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent_loop_system.tools.enter_page_catalog import (
    EnterPageCatalogError,
    load_enter_page_catalog,
)


def test_bundled_6202_catalog_is_complete_and_business_searchable() -> None:
    catalog = load_enter_page_catalog("6202_W5230")

    assert catalog is not None
    assert len(catalog.entries) == 248
    assert len(catalog.window_names) == 213
    assert [
        (entry.business_name, entry.command)
        for entry in catalog.entries_for("SHORTCUT")
    ] == [("控制中心", ":ENTER_PAGE:SHORTCUT,0")]
    assert [
        (entry.business_name, entry.param)
        for entry in catalog.entries_for("ACTIVE_GOAL")
    ] == [
        ("活动步数目标弹窗", 1),
        ("活动卡路里目标弹窗", 2),
        ("活动时长目标弹窗", 3),
    ]


def test_catalog_requires_exact_current_source_coverage() -> None:
    catalog = load_enter_page_catalog("6202_W5230")
    assert catalog is not None

    catalog.validate_source(
        registered_windows=catalog.window_names,
        windows_version="5803_SBYH_FP410X502",
    )
    with pytest.raises(EnterPageCatalogError, match="目录缺少=NEW_WINDOW"):
        catalog.validate_source(
            registered_windows=(*catalog.window_names, "NEW_WINDOW"),
            windows_version="5803_SBYH_FP410X502",
        )
    with pytest.raises(EnterPageCatalogError, match="WINDOWS_VERSION 不匹配"):
        catalog.validate_source(
            registered_windows=catalog.window_names,
            windows_version="NEXT_WINDOWS_VERSION",
        )


def test_catalog_rejects_duplicate_window_param_pairs() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "catalog.json"
        payload = {
            "kind": "EnterPageCapabilityCatalog",
            "schema_version": 1,
            "catalog_id": "test.enter_page",
            "project": "TEST",
            "windows_version": "TEST_WINDOWS",
            "source_method": "test",
            "expected_window_count": 1,
            "expected_entry_count": 2,
            "entries": [
                {"business_name": "页面 A", "window_name": "DEMO", "param": 0},
                {"business_name": "页面 B", "window_name": "DEMO", "param": 0},
            ],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        with pytest.raises(EnterPageCatalogError, match="重复入口: DEMO,0"):
            load_enter_page_catalog("TEST", catalog_path=path)


def test_projects_without_a_catalog_keep_source_only_fallback() -> None:
    assert load_enter_page_catalog("PROJECT_WITHOUT_CATALOG") is None
