from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agent_loop_system.exploration_core.cli import main


def _inputs(tmp_path: Path) -> tuple[Path, Path]:
    evidence = tmp_path / "evidence.json"
    evidence.write_text('{"result":"PASS"}\n', encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "kind": "ExplorationActionRegistry",
        "schema_version": 1,
        "registry_id": "cli-test",
        "source_revision": "test",
        "actions": [{
            "alias": "timer.start",
            "description": "启动计时器",
            "platform_bindings": {
                "w30": {
                    "status": "verified",
                    "transport": "w30_session",
                    "payload": {"command": ":BUTTON:1"},
                    "evidence": [{"evidence_id": "w30", "path": "evidence.json", "sha256": digest}],
                },
                "579": {
                    "status": "verified",
                    "transport": "app_ble",
                    "payload": {"action": "raw_command", "cmd_id": 4, "key_id": 5},
                    "constraints": {"serial_write": False, "allow_adb_recovery": False},
                    "evidence": [{"evidence_id": "579", "path": "evidence.json", "sha256": digest}],
                },
            },
        }],
    }, ensure_ascii=False), encoding="utf-8")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({
        "kind": "FunctionalCaseManifest",
        "cases": [{
            "case_no": "TIMER-001",
            "title": "启动计时器",
            "steps": "打开计时器并启动",
            "expected": "计时器进入运行状态",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    return registry, cases


def test_common_cli_lists_both_platforms_from_one_registry(tmp_path: Path, capsys) -> None:
    registry, _ = _inputs(tmp_path)
    for platform in ("w30", "579"):
        assert main([
            "list-actions", "--registry", str(registry), "--platform", platform,
        ]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["platform"] == platform
        assert payload["actions"][0]["command"] == ":CAP_ACTION:timer.start"
        assert payload["device_action_count"] == 0


def test_explore_case_creates_offline_shadow_request(tmp_path: Path, capsys) -> None:
    registry, cases = _inputs(tmp_path)
    output = tmp_path / "shadow"
    assert main([
        "explore-case",
        "--platform", "579",
        "--registry", str(registry),
        "--case-source", str(cases),
        "--case-id", "TIMER-001",
        "--out", str(output),
    ]) == 0
    summary = json.loads(capsys.readouterr().out)
    request = json.loads((output / "shadow-request.json").read_text(encoding="utf-8"))
    assert summary["device_action_count"] == 0
    assert request["status"] == "SHADOW_READY"
    assert request["device_actions_enabled"] is False
    assert request["formal_evidence_eligible"] is False
    assert request["promotion_status"] == "EXPLORING"
    assert request["available_actions"][0]["alias"] == "timer.start"
    assert "payload" not in request["available_actions"][0]


def test_shadow_refuses_missing_evidence_or_output_overwrite(tmp_path: Path, capsys) -> None:
    registry, cases = _inputs(tmp_path)
    (tmp_path / "evidence.json").unlink()
    assert main([
        "explore-case", "--platform", "579", "--registry", str(registry),
        "--case-source", str(cases), "--case-id", "TIMER-001",
        "--out", str(tmp_path / "blocked"),
    ]) == 1
    audit = json.loads(capsys.readouterr().out)
    assert audit["issues"][0]["code"] == "EVIDENCE_FILE_MISSING"


def test_shadow_rejects_platform_ir_case_source(tmp_path: Path, capsys) -> None:
    registry, cases = _inputs(tmp_path)
    payload = json.loads(cases.read_text(encoding="utf-8"))
    payload["cases"][0]["setup"] = [{
        "action": "raw_command",
        "args": {"cmd_id": "0x04", "key_id": "0x05", "data_hex": "01 02"},
    }]
    cases.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert main([
        "explore-case", "--platform", "579", "--registry", str(registry),
        "--case-source", str(cases), "--case-id", "TIMER-001",
        "--out", str(tmp_path / "must-not-exist"),
    ]) == 2
    error = json.loads(capsys.readouterr().err)
    assert "平台执行 IR" in error["reason"]
    assert not (tmp_path / "must-not-exist").exists()
