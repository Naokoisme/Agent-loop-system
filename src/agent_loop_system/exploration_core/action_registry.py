"""Shared semantic-action registry owned by the W30 Agent exploration core.

The Agent sees a stable semantic alias.  A selected platform adapter receives
only that platform's opaque binding payload.  This keeps the W30 decision loop
shared without leaking W30 transport commands into another platform.
"""

from __future__ import annotations

from enum import StrEnum
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_ALIAS_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}")
_PLATFORM_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class BindingStatus(StrEnum):
    DRAFT = "draft"
    PROBE_REQUIRED = "probe_required"
    VERIFIED = "verified"
    REVOKED = "revoked"


class EvidenceReference(BaseModel):
    """Immutable reference used to justify a verified platform binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1, max_length=160)
    path: str = Field(min_length=1)
    sha256: str

    @field_validator("evidence_id", "path")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("sha256 必须是 64 位十六进制")
        return normalized


class PlatformActionBinding(BaseModel):
    """One platform implementation for a shared semantic action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: BindingStatus
    transport: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    required_capabilities: tuple[str, ...] = ()
    constraints: dict[str, Any] = Field(default_factory=dict)
    evidence: tuple[EvidenceReference, ...] = ()

    @field_validator("transport")
    @classmethod
    def strip_transport(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("required_capabilities")
    @classmethod
    def normalize_capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
        return normalized

    @model_validator(mode="after")
    def verified_requires_evidence(self) -> "PlatformActionBinding":
        if self.status == BindingStatus.VERIFIED and not self.evidence:
            raise ValueError("verified 平台绑定必须提供 evidence")
        return self


class SemanticAction(BaseModel):
    """Agent-visible action with platform-specific binding implementations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: str
    description: str = Field(min_length=1, max_length=300)
    platform_bindings: dict[str, PlatformActionBinding]

    @field_validator("alias")
    @classmethod
    def validate_alias(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _ALIAS_RE.fullmatch(normalized):
            raise ValueError(f"动作别名非法: {value!r}")
        return normalized

    @field_validator("description")
    @classmethod
    def strip_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("platform_bindings")
    @classmethod
    def validate_platforms(
        cls, values: dict[str, PlatformActionBinding],
    ) -> dict[str, PlatformActionBinding]:
        if not values:
            raise ValueError("语义动作至少需要一个平台绑定")
        normalized: dict[str, PlatformActionBinding] = {}
        for platform_id, binding in values.items():
            platform = str(platform_id).strip().lower()
            if not _PLATFORM_RE.fullmatch(platform):
                raise ValueError(f"platform_id 非法: {platform_id!r}")
            if platform in normalized:
                raise ValueError(f"platform_id 重复: {platform}")
            normalized[platform] = binding
        return normalized


class ExplorationActionRegistry(BaseModel):
    """Versioned shared registry consumed by every platform adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["ExplorationActionRegistry"] = "ExplorationActionRegistry"
    schema_version: Literal[1] = 1
    registry_id: str = Field(min_length=1, max_length=160)
    source_revision: str = Field(min_length=1, max_length=200)
    actions: tuple[SemanticAction, ...]

    @field_validator("registry_id", "source_revision")
    @classmethod
    def strip_identity(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def aliases_are_unique(self) -> "ExplorationActionRegistry":
        aliases = [action.alias for action in self.actions]
        duplicate = sorted({alias for alias in aliases if aliases.count(alias) > 1})
        if duplicate:
            raise ValueError(f"动作别名重复: {', '.join(duplicate)}")
        return self

    @classmethod
    def load(cls, path: str | Path) -> "ExplorationActionRegistry":
        source = Path(path)
        payload = json.loads(source.read_text(encoding="utf-8"))
        return cls.model_validate(payload)

    def action_index(self) -> dict[str, SemanticAction]:
        return {action.alias: action for action in self.actions}

    def platform_action_index(
        self, platform_id: str, *, verified_only: bool = True,
    ) -> dict[str, tuple[SemanticAction, PlatformActionBinding]]:
        platform = normalize_platform_id(platform_id)
        result: dict[str, tuple[SemanticAction, PlatformActionBinding]] = {}
        for action in self.actions:
            binding = action.platform_bindings.get(platform)
            if binding is None:
                continue
            if verified_only and binding.status != BindingStatus.VERIFIED:
                continue
            result[action.alias] = (action, binding)
        return result

    def capability_knowledge(self, platform_id: str) -> str:
        """Return Agent-readable aliases without transport payload values."""

        platform = normalize_platform_id(platform_id)
        lines = [
            f"平台 {platform} 已验证语义动作。",
            "command 必须原样使用 :CAP_ACTION:<alias>；不得编造别名或底层参数。",
        ]
        for alias, (action, binding) in sorted(
            self.platform_action_index(platform).items()
        ):
            required = ",".join(binding.required_capabilities) or "none"
            lines.append(
                f"- :CAP_ACTION:{alias} | {action.description} | "
                f"transport={binding.transport} | requires={required}"
            )
        return "\n".join(lines)


def normalize_platform_id(value: str) -> str:
    platform = str(value or "").strip().lower()
    if not _PLATFORM_RE.fullmatch(platform):
        raise ValueError(f"platform_id 非法: {value!r}")
    return platform


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_registry(
    registry: ExplorationActionRegistry,
    *,
    platform_id: str | None = None,
    evidence_root: str | Path | None = None,
    verify_evidence: bool = False,
) -> dict[str, Any]:
    """Audit lifecycle and optional evidence files without executing a device."""

    issues: list[dict[str, Any]] = []
    platform = normalize_platform_id(platform_id) if platform_id else None
    root = Path(evidence_root).resolve() if evidence_root is not None else None
    if verify_evidence and root is None:
        issues.append({
            "severity": "blocking",
            "code": "EVIDENCE_ROOT_REQUIRED",
            "message": "verify_evidence=true 时必须提供 evidence_root",
        })

    verified_count = 0
    platform_binding_count = 0
    for action in registry.actions:
        for binding_platform, binding in action.platform_bindings.items():
            if platform is not None and binding_platform != platform:
                continue
            platform_binding_count += 1
            if binding.status != BindingStatus.VERIFIED:
                continue
            verified_count += 1
            for evidence in binding.evidence:
                if not verify_evidence or root is None:
                    continue
                relative = Path(evidence.path)
                if relative.is_absolute() or ".." in relative.parts:
                    issues.append({
                        "severity": "blocking",
                        "code": "EVIDENCE_PATH_UNSAFE",
                        "alias": action.alias,
                        "platform": binding_platform,
                        "path": evidence.path,
                    })
                    continue
                candidate = (root / relative).resolve()
                if not candidate.is_relative_to(root):
                    issues.append({
                        "severity": "blocking",
                        "code": "EVIDENCE_PATH_ESCAPE",
                        "alias": action.alias,
                        "platform": binding_platform,
                        "path": evidence.path,
                    })
                elif not candidate.is_file():
                    issues.append({
                        "severity": "blocking",
                        "code": "EVIDENCE_FILE_MISSING",
                        "alias": action.alias,
                        "platform": binding_platform,
                        "path": evidence.path,
                    })
                elif sha256_file(candidate) != evidence.sha256:
                    issues.append({
                        "severity": "blocking",
                        "code": "EVIDENCE_SHA256_MISMATCH",
                        "alias": action.alias,
                        "platform": binding_platform,
                        "path": evidence.path,
                    })

    if platform is not None and platform_binding_count == 0:
        issues.append({
            "severity": "blocking",
            "code": "PLATFORM_BINDINGS_MISSING",
            "platform": platform,
        })
    blocking = sum(item["severity"] == "blocking" for item in issues)
    return {
        "kind": "ExplorationActionRegistryAudit",
        "schema_version": 1,
        "registry_id": registry.registry_id,
        "platform": platform,
        "status": "PASS" if blocking == 0 else "BLOCKED",
        "blocking_count": blocking,
        "issues": issues,
        "metrics": {
            "semantic_action_count": len(registry.actions),
            "platform_binding_count": platform_binding_count,
            "verified_binding_count": verified_count,
        },
        "device_action_count": 0,
    }
