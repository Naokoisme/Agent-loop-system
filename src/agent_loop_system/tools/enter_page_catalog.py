"""Versioned, project-level ``ENTER_PAGE`` capability catalogs.

The catalog adds reviewed business names and parameter semantics to the window
names extracted from firmware source.  Source extraction remains authoritative
for which windows exist; a catalog is accepted only when it covers that source
set exactly.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Iterable


_WINDOW_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\Z")
_UINT32_MAX = 0xFFFFFFFF


class EnterPageCatalogError(ValueError):
    """Raised when a platform page catalog is malformed or has drifted."""


@dataclass(frozen=True, slots=True)
class EnterPageCapability:
    business_name: str
    window_name: str
    param: int

    @property
    def command(self) -> str:
        return f":ENTER_PAGE:{self.window_name},{self.param}"


@dataclass(frozen=True, slots=True)
class EnterPageCapabilityCatalog:
    catalog_id: str
    project: str
    windows_version: str
    source_method: str
    entries: tuple[EnterPageCapability, ...]

    @property
    def window_names(self) -> frozenset[str]:
        return frozenset(entry.window_name for entry in self.entries)

    def entries_for(self, window_name: str) -> tuple[EnterPageCapability, ...]:
        return tuple(
            entry for entry in self.entries if entry.window_name == window_name
        )

    def validate_source(
        self,
        *,
        registered_windows: Iterable[str],
        windows_version: str,
    ) -> None:
        """Require exact coverage of the current firmware window registry."""

        if self.windows_version != windows_version:
            raise EnterPageCatalogError(
                "ENTER_PAGE 能力目录的 WINDOWS_VERSION 不匹配: "
                f"目录={self.windows_version}, 源码={windows_version}"
            )
        registered = frozenset(str(name).strip() for name in registered_windows)
        missing = sorted(registered - self.window_names)
        extra = sorted(self.window_names - registered)
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append("目录缺少=" + ",".join(missing))
            if extra:
                details.append("源码未注册=" + ",".join(extra))
            raise EnterPageCatalogError(
                "ENTER_PAGE 能力目录与当前固件注册窗口不一致: "
                + "；".join(details)
            )


def _default_catalog_path(project: str) -> Path:
    return Path(
        str(
            files("agent_loop_system.platform_data").joinpath(
                "w30",
                project,
                "enter_page_capabilities.v1.json",
            )
        )
    )


def _positive_count(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EnterPageCatalogError(f"ENTER_PAGE 能力目录 {field} 必须是正整数")
    return value


def load_enter_page_catalog(
    project: str,
    *,
    catalog_path: str | Path | None = None,
) -> EnterPageCapabilityCatalog | None:
    """Load one bundled project catalog, or return ``None`` when not provided."""

    normalized_project = str(project or "").strip()
    if not _PROJECT_NAME.fullmatch(normalized_project):
        if catalog_path is None:
            return None
        raise EnterPageCatalogError(f"ENTER_PAGE 能力目录项目名非法: {project!r}")

    path = Path(catalog_path) if catalog_path is not None else _default_catalog_path(
        normalized_project
    )
    if not path.is_file():
        if catalog_path is None:
            return None
        raise EnterPageCatalogError(f"ENTER_PAGE 能力目录不存在: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EnterPageCatalogError(f"ENTER_PAGE 能力目录无法读取: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EnterPageCatalogError("ENTER_PAGE 能力目录必须是 JSON 对象")
    if (
        payload.get("kind") != "EnterPageCapabilityCatalog"
        or payload.get("schema_version") != 1
    ):
        raise EnterPageCatalogError("ENTER_PAGE 能力目录 kind 或 schema_version 不兼容")
    if payload.get("project") != normalized_project:
        raise EnterPageCatalogError(
            "ENTER_PAGE 能力目录项目不匹配: "
            f"请求={normalized_project}, 目录={payload.get('project')!r}"
        )

    catalog_id = str(payload.get("catalog_id") or "").strip()
    windows_version = str(payload.get("windows_version") or "").strip()
    source_method = str(payload.get("source_method") or "").strip()
    if not catalog_id or not windows_version or not source_method:
        raise EnterPageCatalogError(
            "ENTER_PAGE 能力目录缺少 catalog_id、windows_version 或 source_method"
        )
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise EnterPageCatalogError("ENTER_PAGE 能力目录 entries 必须是非空数组")

    entries: list[EnterPageCapability] = []
    seen: set[tuple[str, int]] = set()
    for index, raw in enumerate(raw_entries, 1):
        if not isinstance(raw, dict):
            raise EnterPageCatalogError(f"ENTER_PAGE 能力目录第 {index} 项不是对象")
        business_name = str(raw.get("business_name") or "").strip()
        window_name = str(raw.get("window_name") or "").strip()
        param = raw.get("param")
        if not business_name:
            raise EnterPageCatalogError(f"ENTER_PAGE 能力目录第 {index} 项业务名为空")
        if not _WINDOW_NAME.fullmatch(window_name):
            raise EnterPageCatalogError(
                f"ENTER_PAGE 能力目录第 {index} 项窗口名非法: {window_name!r}"
            )
        if (
            isinstance(param, bool)
            or not isinstance(param, int)
            or not 0 <= param <= _UINT32_MAX
        ):
            raise EnterPageCatalogError(
                f"ENTER_PAGE 能力目录第 {index} 项 param 不是 uint32: {param!r}"
            )
        key = (window_name, param)
        if key in seen:
            raise EnterPageCatalogError(
                f"ENTER_PAGE 能力目录包含重复入口: {window_name},{param}"
            )
        seen.add(key)
        entries.append(
            EnterPageCapability(
                business_name=business_name,
                window_name=window_name,
                param=param,
            )
        )

    expected_entries = _positive_count(
        payload.get("expected_entry_count"), "expected_entry_count"
    )
    expected_windows = _positive_count(
        payload.get("expected_window_count"), "expected_window_count"
    )
    actual_windows = len({entry.window_name for entry in entries})
    if len(entries) != expected_entries or actual_windows != expected_windows:
        raise EnterPageCatalogError(
            "ENTER_PAGE 能力目录声明数量与内容不一致: "
            f"entries={len(entries)}/{expected_entries}, "
            f"windows={actual_windows}/{expected_windows}"
        )
    return EnterPageCapabilityCatalog(
        catalog_id=catalog_id,
        project=normalized_project,
        windows_version=windows_version,
        source_method=source_method,
        entries=tuple(entries),
    )


__all__ = [
    "EnterPageCapability",
    "EnterPageCapabilityCatalog",
    "EnterPageCatalogError",
    "load_enter_page_catalog",
]
