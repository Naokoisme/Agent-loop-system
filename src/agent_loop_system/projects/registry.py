from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_loop_system.platforms.registry import PlatformRegistry


PROJECT_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


class ProjectRegistry:
    """Owns user projects while keeping built-in compatibility seeds immutable."""

    def __init__(self, root: Path, platforms: PlatformRegistry):
        self.root = Path(root).resolve()
        self.platforms = platforms
        self.path = self.root / "config" / "projects.v1.json"
        self.project_data = self.root / "project_data"
        self._ensure_registry()

    def _seed_projects(self) -> list[dict[str, Any]]:
        return [
            self._seed("620C_W6830", "620C W6830", "w30.620c.simulator", "case_map/620C_simulator_case_map", "620C_W6830"),
            self._seed("6202_W5230_SIMULATOR", "6202 W5230", "w30.6202.simulator", "case_map/6202_simulator_case_map", "6202_W5230_SIMULATOR"),
            self._seed("6202_W5230", "6202 W5230", "w30.6202.hardware", "case_map/6202_case_map", "6202_W5230"),
            self._seed("579_O2", "579 O2 真机", "579.o2", "case_map/579_case_map", "579_O2"),
        ]

    def _seed(
        self,
        project_id: str,
        name: str,
        target_id: str,
        catalog_path: str,
        profile: str,
    ) -> dict[str, Any]:
        target = self.platforms.target(target_id)
        timestamp = _now()
        return {
            "project_id": project_id,
            "project_name": name,
            "case_catalog": {
                "type": target["case_catalog_adapter"],
                "root": catalog_path,
                "profile": profile,
            },
            "allowed_platforms": [target["platform_id"]],
            "default_platform": target["platform_id"],
            "allowed_targets": [target_id],
            "default_target": target_id,
            "status": "active",
            "built_in": True,
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def _ensure_registry(self) -> None:
        if self.path.is_file():
            return
        _atomic_json(self.path, {"version": "1", "projects": self._seed_projects()})

    def _load(self) -> dict[str, Any]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != "1" or not isinstance(payload.get("projects"), list):
            raise ValueError("项目注册表格式不合法")
        return payload

    def _validated(self, raw: dict[str, Any]) -> dict[str, Any]:
        project = dict(raw)
        project_id = str(project.get("project_id") or "").strip()
        if not PROJECT_ID.fullmatch(project_id) or project_id in {".", ".."}:
            raise ValueError("项目 ID 只能包含字母、数字、点、下划线和连字符")
        name = str(project.get("project_name") or "").strip()
        if not name:
            raise ValueError("项目名称必填")
        allowed_platforms = [str(value).strip() for value in project.get("allowed_platforms", [])]
        if not allowed_platforms or len(set(allowed_platforms)) != len(allowed_platforms):
            raise ValueError("allowed_platforms 必须非空且不能重复")
        for platform_id in allowed_platforms:
            self.platforms.platform(platform_id)
        default_platform = str(project.get("default_platform") or "").strip()
        if default_platform not in allowed_platforms:
            raise ValueError("default_platform 必须属于 allowed_platforms")
        allowed_targets = [str(value).strip() for value in project.get("allowed_targets", [])]
        if not allowed_targets:
            raise ValueError("allowed_targets 必须非空")
        for target_id in allowed_targets:
            target = self.platforms.target(target_id)
            if target["platform_id"] not in allowed_platforms:
                raise ValueError(f"执行目标 {target_id} 不属于项目允许的平台")
        default_target = str(project.get("default_target") or allowed_targets[0]).strip()
        if default_target not in allowed_targets:
            raise ValueError("default_target 必须属于 allowed_targets")
        if self.platforms.target(default_target)["platform_id"] != default_platform:
            raise ValueError("default_target 必须属于 default_platform")
        catalog = project.get("case_catalog")
        if not isinstance(catalog, dict):
            raise ValueError("case_catalog 必须是对象")
        root_text = str(catalog.get("root") or "").replace("\\", "/").strip("/")
        if not root_text:
            raise ValueError("case_catalog.root 必填")
        catalog_path = (self.root / root_text).resolve()
        catalog_path.relative_to(self.root)
        project.update({
            "project_id": project_id,
            "project_name": name,
            "allowed_platforms": allowed_platforms,
            "default_platform": default_platform,
            "allowed_targets": allowed_targets,
            "default_target": default_target,
            "case_catalog": {**catalog, "root": root_text},
            "status": str(project.get("status") or "active"),
        })
        return project

    def list(self, *, include_archived: bool = False, query: str = "") -> list[dict[str, Any]]:
        words = [item.casefold() for item in query.split() if item]
        result: list[dict[str, Any]] = []
        for raw in self._load()["projects"]:
            project = self._validated(raw)
            if not include_archived and project["status"] == "archived":
                continue
            haystack = f"{project['project_id']} {project['project_name']}".casefold()
            if words and not all(word in haystack for word in words):
                continue
            result.append(project)
        return result

    def get(self, project_id: str, *, include_archived: bool = True) -> dict[str, Any]:
        project_id = str(project_id).strip()
        for project in self.list(include_archived=include_archived):
            if project["project_id"] == project_id:
                return project
        raise ValueError(f"PROJECT_NOT_FOUND: {project_id or '空'}")

    def resolve(
        self,
        project_id: str,
        *,
        platform_id: str | None = None,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        project = self.get(project_id, include_archived=False)
        requested_platform = str(platform_id or project["default_platform"]).strip()
        if requested_platform not in project["allowed_platforms"]:
            raise ValueError("PLATFORM_NOT_ALLOWED_FOR_PROJECT")
        candidates = [
            value for value in project["allowed_targets"]
            if self.platforms.target(value)["platform_id"] == requested_platform
        ]
        requested_target = str(target_id or "").strip()
        if not requested_target:
            default_target = str(project.get("default_target") or "")
            requested_target = default_target if default_target in candidates else (candidates[0] if candidates else "")
        if requested_target not in candidates:
            raise ValueError("TARGET_NOT_ALLOWED_FOR_PROJECT")
        target = self.platforms.target(requested_target)
        catalog = project["case_catalog"]
        return {
            **project,
            **target,
            "project": project["project_id"],
            "project_label": project["project_name"],
            "case_catalog_path": catalog["root"],
            "case_map_dir": Path(catalog["root"]).name,
            "case_map_profile": str(catalog.get("profile") or project["project_id"]),
            "requested_platform_id": requested_platform,
            "profile_version": self.platforms.version,
        }

    def create(self, raw: dict[str, Any]) -> dict[str, Any]:
        project_id = str(raw.get("project_id") or "").strip()
        now = _now()
        target_ids = list(raw.get("allowed_targets") or [])
        project = self._validated({
            **raw,
            "project_id": project_id,
            "case_catalog": {
                "type": "unified",
                "root": f"project_data/{project_id}/cases",
                "profile": project_id,
            },
            "default_target": raw.get("default_target") or (target_ids[0] if target_ids else ""),
            "status": "active",
            "built_in": False,
            "created_at": now,
            "updated_at": now,
        })
        payload = self._load()
        if any(item.get("project_id") == project_id for item in payload["projects"]):
            raise ValueError("PROJECT_ALREADY_EXISTS")
        target_dir = (self.project_data / project_id).resolve()
        target_dir.relative_to(self.project_data.resolve())
        if target_dir.exists():
            raise ValueError("PROJECT_ALREADY_EXISTS")
        temporary = self.project_data / f".{project_id}.{os.getpid()}.tmp"
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            for child in ("cases", "imports", "artifacts"):
                (temporary / child).mkdir()
            _atomic_json(temporary / "project.json", project)
            os.replace(temporary, target_dir)
            payload["projects"].append(project)
            _atomic_json(self.path, payload)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary)
            if target_dir.exists() and not any(item.get("project_id") == project_id for item in self._load()["projects"]):
                shutil.rmtree(target_dir)
            raise
        return project

    def update(self, project_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        payload = self._load()
        for index, raw in enumerate(payload["projects"]):
            if raw.get("project_id") != project_id:
                continue
            if raw.get("built_in") and any(key in changes for key in ("allowed_platforms", "allowed_targets", "default_platform", "default_target")):
                raise ValueError("内置项目的平台身份字段不可修改")
            updated = self._validated({
                **raw,
                **{key: value for key, value in changes.items() if key not in {"project_id", "created_at", "built_in", "case_catalog"}},
                "updated_at": _now(),
            })
            payload["projects"][index] = updated
            _atomic_json(self.path, payload)
            data_file = self.project_data / project_id / "project.json"
            if data_file.parent.is_dir():
                _atomic_json(data_file, updated)
            return updated
        raise ValueError(f"PROJECT_NOT_FOUND: {project_id}")

    def archive(self, project_id: str) -> dict[str, Any]:
        project = self.get(project_id)
        if project.get("built_in"):
            raise ValueError("内置项目不能归档")
        return self.update(project_id, {"status": "archived"})
