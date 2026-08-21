"""EXE 更新检测与用户数据保护模块。

严格遵循阶段 F7 规范：
- 异步/快速非阻塞读取 NAS 或 HTTP update-manifest.json
- 离线或异常时安全降级，绝不阻塞主程序运行
- 语义化版本比对，提供温和更新提醒
- 升级过程严格保护用户数据目录 (case_map, history, evidence, .runtime, .env)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from agent_loop_system.version import __version__


DATA_SAFETY_NOTICE = (
    "数据安全保证：升级版本时仅需替换主程序 Agent-loop.exe、_internal 与前端资源；"
    "您的用户测试用例 (case_map/)、历史测试记录 (history/)、截图证据 (evidence/) 与环境变量 (.env) "
    "均独立存放在数据层，升级时将被完整保留，不受任何影响。"
)


def get_current_system_version() -> str:
    """动态获取当前安装版本：发布清单、环境变量、包版本依次回退。"""
    try:
        from agent_loop_system.runtime_root import resolve_app_root
        manifest_p = resolve_app_root() / "release_manifest.json"
        if manifest_p.is_file():
            data = json.loads(manifest_p.read_text(encoding="utf-8"))
            v = str(data.get("version") or "").strip()
            if v:
                return v
    except Exception:
        pass
    return os.environ.get("AGENT_LOOP_VERSION", __version__).strip() or __version__


CURRENT_SYSTEM_VERSION = get_current_system_version()


def _parse_version_tuple(v_str: str) -> tuple[int, ...]:
    clean = v_str.strip().lstrip("vV")
    parts = []
    for seg in clean.split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_for_updates(
    manifest_source: str | Path | None = None,
    current_version: str | None = None,
    *,
    timeout_sec: float = 2.0,
) -> dict[str, Any]:
    """检测是否存在新版本更新。"""
    cur_version = current_version or get_current_system_version()
    source = str(
        manifest_source
        or os.environ.get("W30_UPDATE_MANIFEST_URL")
        or os.environ.get("W30_NAS_MANIFEST_PATH")
        or ""
    ).strip()

    if not source:
        return {
            "has_update": False,
            "status": "unconfigured",
            "current_version": cur_version,
            "latest_version": cur_version,
            "data_safety_notice": DATA_SAFETY_NOTICE,
        }

    raw_json = ""
    try:
        if source.startswith("http://") or source.startswith("https://"):
            req = urllib.request.Request(
                source,
                headers={"User-Agent": f"AgentLoopSystem/{cur_version}"},
            )
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                raw_json = resp.read().decode("utf-8")
        else:
            p = Path(source)
            if not p.is_file():
                return {
                    "has_update": False,
                    "status": "offline",
                    "error": f"更新清单文件不可访问: {source}",
                    "current_version": cur_version,
                    "latest_version": cur_version,
                    "data_safety_notice": DATA_SAFETY_NOTICE,
                }
            raw_json = p.read_text(encoding="utf-8")

        manifest = json.loads(raw_json)
        latest_ver = str(manifest.get("latest_version") or "").strip()
        if not latest_ver:
            return {
                "has_update": False,
                "status": "invalid_manifest",
                "current_version": cur_version,
                "latest_version": cur_version,
                "data_safety_notice": DATA_SAFETY_NOTICE,
            }

        cur_t = _parse_version_tuple(cur_version)
        lat_t = _parse_version_tuple(latest_ver)

        has_update = lat_t > cur_t
        return {
            "has_update": has_update,
            "status": "update_available" if has_update else "up_to_date",
            "current_version": cur_version,
            "latest_version": latest_ver,
            "release_timestamp": manifest.get("release_timestamp"),
            "min_compatible_version": manifest.get("min_compatible_version", "0.1.0"),
            "changelog": manifest.get("changelog", []),
            "packages": manifest.get("packages", {}),
            "data_safety_notice": DATA_SAFETY_NOTICE,
        }
    except Exception as exc:
        return {
            "has_update": False,
            "status": "offline",
            "error": str(exc),
            "current_version": cur_version,
            "latest_version": cur_version,
            "data_safety_notice": DATA_SAFETY_NOTICE,
        }
