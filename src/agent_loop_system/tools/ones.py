"""ONES 缺陷库只读客户端：按编号获取缺陷，提取 title+description 作为 Agent 输入。

参考原项目 langgraph_test_project.ones.client，精简为最小可用：
只保留 peek + fetch_details + normalize，去掉缓存/队列/轮询/附件/评论/GraphQL 富化。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

DEFAULT_BASE_URL = "https://ones.topstepht.com:8443"

# ONES 固定字段 UUID（验证自 ones-agent-source-mcp v0.3.0）
FIXED_FIELDS = {
    "title": "field001",
    "description": "field002",
    "issueType": "field007",
    "status": "field005",
    "number": "field015",
    "createTime": "field009",
}


class OnesConfigError(Exception):
    pass


class OnesApiError(Exception):
    def __init__(self, message: str, *, status: int | None = None, path: str | None = None):
        super().__init__(message)
        self.status = status
        self.path = path


@dataclass(frozen=True)
class OnesConfig:
    base_url: str
    user_id: str
    auth_token: str
    team_uuid: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "OnesConfig":
        env = env if env is not None else os.environ
        base_url = (env.get("ONES_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        user_id = (env.get("ONES_USER_ID") or "").strip()
        auth_token = (env.get("ONES_AUTH_TOKEN") or "").strip()
        team_uuid = (env.get("ONES_TEAM_UUID") or "").strip()
        if not user_id or not auth_token or not team_uuid:
            raise OnesConfigError(
                "ONES 配置缺失，需设置 ONES_USER_ID/ONES_AUTH_TOKEN/ONES_TEAM_UUID"
            )
        return cls(base_url, user_id, auth_token, team_uuid)


class Defect(BaseModel):
    """缺陷最小模型：只保留闭环需要的字段。"""

    uuid: str
    number: str = ""
    title: str = ""
    description: str = ""
    status_name: str = ""
    desc_rich: str = ""  # 描述富文本 HTML（含内嵌图片 <img data-uuid=...>）


class OnesClient:
    """只读 ONES 客户端，用 urllib 无新依赖。"""

    def __init__(self, config: OnesConfig):
        self.config = config

    def _request(self, path: str, method: str = "GET", body: Any = None) -> Any:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Ones-User-Id": self.config.user_id,
            "Ones-Auth-Token": self.config.auth_token,
            "Referer": self.config.base_url,
            "User-Agent": "w30-agent-loop/0.1.0",
        }
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.config.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise OnesApiError(
                f"ONES API 失败 ({exc.code})", status=exc.code, path=path
            ) from exc
        except urllib.error.URLError as exc:
            raise OnesApiError(f"无法连接 ONES: {exc.reason}", path=path) from exc
        return json.loads(text) if text else None

    def _team_path(self, suffix: str) -> str:
        return f"/project/api/project/team/{urllib.parse.quote(self.config.team_uuid)}{suffix}"

    def _issue_types(self) -> list[dict]:
        payload = self._request(self._team_path("/issue_types"))
        return payload.get("issue_types", []) if isinstance(payload, dict) else []

    def _statuses(self) -> list[dict]:
        payload = self._request(self._team_path("/task_statuses"))
        if isinstance(payload, dict):
            return payload.get("statuses") or payload.get("task_statuses") or []
        return []

    def _peek_defect_ids(self, *, include_completed: bool = False) -> list[str]:
        issue_types = self._issue_types()
        defect_types = [i for i in issue_types if "缺陷" in (i.get("name") or "")]
        if not defect_types:
            raise OnesApiError('未找到"缺陷"工作项类型')
        conditions = [
            {
                "in": {
                    f"field_values.{FIXED_FIELDS['issueType']}": [i["uuid"] for i in defect_types]
                }
            },
        ]
        if not include_completed:
            statuses = self._statuses()
            open_statuses = [s for s in statuses if (s.get("category") or "") != "done"]
            if not open_statuses:
                raise OnesApiError("未找到开放状态")
            conditions.append(
                {
                    "in": {
                        f"field_values.{FIXED_FIELDS['status']}": [s["uuid"] for s in open_statuses]
                    }
                }
            )
        payload = self._request(
            self._team_path("/filters/peek"),
            method="POST",
            body={
                "with_boards": False,
                "boards": None,
                "query": {"must": conditions},
                "group_by": "",
                "sort": [{f"field_values.{FIXED_FIELDS['createTime']}": {"order": "desc"}}],
                "include_subtasks": True,
                "include_status_uuid": True,
                "include_issue_type": True,
                "include_project_uuid": True,
                "is_show_derive": False,
            },
        )
        ids: list[str] = []
        for group in (payload or {}).get("groups", []):
            for entry in group.get("entries", []):
                if entry.get("uuid"):
                    ids.append(entry["uuid"])
        return ids

    def _fetch_details(self, ids: list[str]) -> list[dict]:
        tasks: list[dict] = []
        for start in range(0, len(ids), 100):
            chunk = ids[start : start + 100]
            payload = self._request(
                self._team_path("/tasks/info"), method="POST", body={"ids": chunk}
            )
            if isinstance(payload, dict):
                tasks.extend(payload.get("tasks", []))
        return tasks

    @staticmethod
    def _fixed_value(task: dict, field_key: str):
        field_uuid = FIXED_FIELDS[field_key]
        for entry in task.get("field_values") or []:
            if entry.get("field_uuid") == field_uuid:
                return entry.get("value")
        return None

    def _normalize(self, task: dict, statuses_map: dict[str, dict]) -> Defect:
        status_uuid = task.get("status_uuid") or self._fixed_value(task, "status")
        status = statuses_map.get(status_uuid, {})
        return Defect(
            uuid=task.get("uuid", ""),
            number=str(task.get("number") or self._fixed_value(task, "number") or ""),
            title=task.get("summary") or self._fixed_value(task, "title") or "",
            description=task.get("desc") or self._fixed_value(task, "description") or "",
            status_name=status.get("name", ""),
            desc_rich=task.get("desc_rich") or "",
        )

    def list_defects(self, *, include_completed: bool = False) -> list[Defect]:
        """列出缺陷（默认仅开放缺陷）。"""
        ids = self._peek_defect_ids(include_completed=include_completed)
        if not ids:
            return []
        tasks = self._fetch_details(ids)
        statuses_map = {s["uuid"]: s for s in self._statuses() if s.get("uuid")}
        return [self._normalize(t, statuses_map) for t in tasks]

    def get_defect(self, number: str) -> Defect | None:
        """按编号获取单个缺陷（含已关闭）。"""
        for d in self.list_defects(include_completed=True):
            if d.number == number:
                return d
        return None

    def fetch_attachments(self, issue_uuid: str) -> list[dict]:
        """列出缺陷的附件资源列表。"""
        path = self._team_path(
            f"/task/{urllib.parse.quote(issue_uuid)}/attachments?count=100"
        )
        payload = self._request(path)
        return payload.get("attachments", []) if isinstance(payload, dict) else []

    def download_attachment(self, resource_uuid: str) -> tuple[str, bytes]:
        """下载附件：获取签名URL后下载内容。返回 (mime, content)。"""
        path = self._team_path(
            f"/res/attachment/{urllib.parse.quote(resource_uuid)}"
        )
        detail = self._request(path)
        if not isinstance(detail, dict) or not detail.get("url"):
            raise OnesApiError("附件无下载URL", path=path)
        url = detail["url"]
        mime = detail.get("mime", "application/octet-stream")
        req = urllib.request.Request(
            url, headers={"User-Agent": "w30-agent-loop/0.1.0"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            content = resp.read()
        return mime, content


# 描述富文本内嵌图片提取：<img data-uuid="..." data-mime="...">
_IMG_TAG_RE = re.compile(r"<img[^>]*>", re.IGNORECASE)
_IMG_ATTR_RE = re.compile(r'data-(uuid|mime)="([^"]*)"', re.IGNORECASE)


def extract_desc_image_uuids(desc_rich: str) -> list[tuple[str, str]]:
    """从描述富文本 HTML 提取内嵌图片的 (uuid, mime) 列表。

    ONES 描述图片以 <img data-uuid="XXX" data-mime="image/png"> 存储，
    src 是 1x1 占位 GIF，真实图片需用 uuid 调 download_attachment 下载。
    """
    if not desc_rich:
        return []
    result: list[tuple[str, str]] = []
    for img_tag in _IMG_TAG_RE.findall(desc_rich):
        attrs = dict(_IMG_ATTR_RE.findall(img_tag))
        uuid = attrs.get("uuid", "")
        if uuid:
            mime = attrs.get("mime", "image/png")
            result.append((uuid, mime))
    return result
