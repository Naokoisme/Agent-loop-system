from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_loop_system.exploration_core import (
    BindingStatus,
    ExplorationActionRegistry,
    audit_registry,
)


def _registry(tmp_path: Path) -> tuple[Path, Path]:
    evidence = tmp_path / "evidence" / "timer-start.json"
    evidence.parent.mkdir()
    evidence.write_text('{"status":"PASS"}\n', encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    payload = {
        "kind": "ExplorationActionRegistry",
        "schema_version": 1,
        "registry_id": "shared-test-v1",
        "source_revision": "unit-test",
        "actions": [{
            "alias": "timer.start",
            "description": "启动计时器",
            "platform_bindings": {
                "w30": {
                    "status": "verified",
                    "transport": "w30_session",
                    "payload": {"command": ":TP_CLICK:100,100,1"},
                    "evidence": [{
                        "evidence_id": "w30-timer-start",
                        "path": "evidence/timer-start.json",
                        "sha256": digest,
                    }],
                },
                "579": {
                    "status": "verified",
                    "transport": "app_ble",
                    "payload": {
                        "action": "raw_command",
                        "cmd_id": "0x04",
                        "key_id": "0x05",
                        "data_hex": "01 02",
                        "o1_seconds": 1.0,
                    },
                    "required_capabilities": ["APP_RAW_HEX_RELAY", "O2_SCREENSHOT_OBSERVE"],
                    "constraints": {
                        "serial_write": False,
                        "allow_adb_recovery": False,
                    },
                    "evidence": [{
                        "evidence_id": "579-timer-start",
                        "path": "evidence/timer-start.json",
                        "sha256": digest,
                    }],
                },
            },
        }],
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path, evidence


def test_one_semantic_action_exposes_separate_platform_bindings(tmp_path: Path) -> None:
    path, _ = _registry(tmp_path)
    registry = ExplorationActionRegistry.load(path)
    assert registry.platform_action_index("w30")["timer.start"][1].transport == "w30_session"
    binding_579 = registry.platform_action_index("579")["timer.start"][1]
    assert binding_579.transport == "app_ble"
    assert binding_579.payload["cmd_id"] == "0x04"
    knowledge = registry.capability_knowledge("579")
    assert ":CAP_ACTION:timer.start" in knowledge
    assert "0x04" not in knowledge and "01 02" not in knowledge


def test_registry_audit_verifies_evidence_hash_without_device_action(tmp_path: Path) -> None:
    path, evidence = _registry(tmp_path)
    registry = ExplorationActionRegistry.load(path)
    audit = audit_registry(
        registry,
        platform_id="579",
        evidence_root=tmp_path,
        verify_evidence=True,
    )
    assert audit["status"] == "PASS"
    assert audit["metrics"]["verified_binding_count"] == 1
    assert audit["device_action_count"] == 0

    evidence.write_text('{"status":"tampered"}\n', encoding="utf-8")
    audit = audit_registry(
        registry,
        platform_id="579",
        evidence_root=tmp_path,
        verify_evidence=True,
    )
    assert audit["status"] == "BLOCKED"
    assert audit["issues"][0]["code"] == "EVIDENCE_SHA256_MISMATCH"


def test_verified_binding_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="必须提供 evidence"):
        ExplorationActionRegistry.model_validate({
            "registry_id": "bad",
            "source_revision": "test",
            "actions": [{
                "alias": "timer.start",
                "description": "启动",
                "platform_bindings": {
                    "579": {
                        "status": BindingStatus.VERIFIED,
                        "transport": "app_ble",
                        "payload": {},
                    },
                },
            }],
        })


def test_duplicate_alias_is_rejected(tmp_path: Path) -> None:
    path, _ = _registry(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["actions"].append(payload["actions"][0])
    with pytest.raises(ValidationError, match="动作别名重复"):
        ExplorationActionRegistry.model_validate(payload)
