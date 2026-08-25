from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_loop_system.platforms.contracts import RunRequest
from agent_loop_system.platforms.platform_579 import (
    AppBleTransport,
    Platform579Catalog,
    Platform579Gateway,
    Platform579TransportConfig,
    WatchSerialReadonly,
)
from agent_loop_system.platforms.registry import PlatformRegistry
from agent_loop_system.projects.registry import ProjectRegistry


def test_platform_registry_declares_579_read_only_observation() -> None:
    registry = PlatformRegistry()
    target = registry.target("579.o2")
    assert target["transport"] == "app_ble"
    assert target["serial_provider"] == "com3_readonly"
    assert "serial_write_provider" not in target
    assert target["capture_provider"] == "o2"


def test_w30_hardware_target_owns_a_reusable_runtime_profile() -> None:
    target = PlatformRegistry().target("w30.6202.hardware")
    assert target["runtime_profile_id"] == "6202_W5230"


def test_new_logical_project_inherits_runtime_profile_from_target(
    tmp_path: Path,
) -> None:
    registry = ProjectRegistry(tmp_path, PlatformRegistry())
    registry.create({
        "project_id": "new_6202_project",
        "project_name": "新 6202 项目",
        "allowed_platforms": ["w30"],
        "default_platform": "w30",
        "allowed_targets": ["w30.6202.hardware"],
        "default_target": "w30.6202.hardware",
    })

    resolved = registry.resolve("new_6202_project")
    assert resolved["project"] == "new_6202_project"
    assert resolved["runtime_profile_id"] == "6202_W5230"


def test_project_registry_creates_atomically_and_archives_without_deleting(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path, PlatformRegistry())
    project = registry.create({
        "project_id": "watch_regression",
        "project_name": "手表回归",
        "allowed_platforms": ["w30", "579"],
        "default_platform": "579",
        "allowed_targets": ["w30.620c.simulator", "579.o2"],
        "default_target": "579.o2",
    })
    project_dir = tmp_path / "project_data" / "watch_regression"
    assert project["case_catalog"]["type"] == "unified"
    assert (project_dir / "project.json").is_file()
    assert all((project_dir / name).is_dir() for name in ("cases", "imports", "artifacts"))

    archived = registry.archive("watch_regression")
    assert archived["status"] == "archived"
    assert project_dir.is_dir()
    assert "watch_regression" not in {item["project_id"] for item in registry.list()}


def test_project_registry_rejects_mismatched_default_platform_target(tmp_path: Path) -> None:
    registry = ProjectRegistry(tmp_path, PlatformRegistry())
    with pytest.raises(ValueError, match="default_target 必须属于 default_platform"):
        registry.create({
            "project_id": "bad_route",
            "project_name": "错误路由",
            "allowed_platforms": ["w30", "579"],
            "default_platform": "579",
            "allowed_targets": ["w30.620c.simulator", "579.o2"],
            "default_target": "w30.620c.simulator",
        })
    assert not (tmp_path / "project_data" / "bad_route").exists()


def test_579_catalog_is_frozen_and_has_expected_maturity_counts() -> None:
    audit = Platform579Catalog().audit()
    assert audit["ok"] is True
    assert audit["case_count"] == 59
    assert audit["runnable_count"] == 37
    assert audit["source_file_count"] == 7


def test_com3_observer_exposes_no_write_send_or_flush_api() -> None:
    for forbidden in ("write", "send", "flush"):
        assert not hasattr(WatchSerialReadonly, forbidden)


def test_579_transport_is_default_off_and_does_not_spawn_adb() -> None:
    calls: list[object] = []
    config = Platform579TransportConfig(
        enabled=False,
        device_actions_enabled=False,
        adb_path="adb",
        adb_serial="",
        app_package="bridge.app",
        bridge_component="",
        bridge_action="bridge.ACTION",
        timeout_seconds=1,
    )
    transport = AppBleTransport(config, runner=lambda *args, **kwargs: calls.append(args))
    result = transport.raw_command(4, 5, "00", logical_action="test")
    assert result["delivery_status"] == "BLOCKED"
    assert result["device_action_count"] == 0
    assert calls == []


class _Catalog:
    def private_plan(self, case_id: str) -> dict:
        return {
            "automation_case_id": "AC-TEST",
            "automation_status": "AUTO_READY",
            "execution_policy": {"evidence_eligible": True},
            "setup": [{
                "type": "step",
                "device": "app_ble",
                "action": "raw_command",
                "args": {"cmd_id": "0x04", "key_id": "0x05", "data_hex": "00"},
            }],
            "steps": [{
                "type": "assert",
                "src": "watch_screen",
                "op": "ai_visual_match",
                "expected": "秒表页面可见",
                "evidence_name": "screen",
            }],
            "assertions": [{"expected": "秒表页面可见"}],
            "teardown": [],
            "_plan_sha256": "A" * 64,
        }


class _Health:
    def inspect(self) -> dict:
        return {"readiness_status": "ready", "checks": []}


class _Transport:
    def raw_command(self, *args, **kwargs) -> dict:
        return {
            "ok": True,
            "delivery_status": "CONFIRMED",
            "device_action_count": 1,
            "raw_response": "private",
            "parsed": {"ok": True, "cmd_id": 4, "data_hex": "00"},
        }

    def trigger_screenshot(self) -> dict:
        return {"ok": True, "delivery_status": "CONFIRMED", "device_action_count": 1}


class _Observer:
    def __init__(self, *, capture_ok: bool = True):
        self.capture_ok = capture_ok

    def observe_o1(self, *, ready_event, **kwargs) -> dict:
        ready_event.set()
        return {"ok": True, "kind": "o1_log_observation", "channel": "com3_readonly"}

    def capture_o2(self, *, artifact_dir: Path, ready_event, **kwargs) -> dict:
        ready_event.set()
        if not self.capture_ok:
            return {
                "ok": False,
                "kind": "o2_screenshot",
                "channel": "com3_readonly",
                "freshness_verified": False,
                "reason": "no fresh frame",
            }
        path = artifact_dir / "screen.bmp"
        path.write_bytes(b"BM-fake")
        return {
            "ok": True,
            "kind": "o2_screenshot",
            "channel": "com3_readonly",
            "freshness_verified": True,
            "image_path": str(path),
        }


def _gateway(observer: _Observer) -> Platform579Gateway:
    return Platform579Gateway(
        catalog=_Catalog(),
        transport=_Transport(),
        observer=observer,
        health=_Health(),
        judge=lambda *args: SimpleNamespace(verdict="PASS", reason="视觉证据通过"),
        sleeper=lambda _: None,
    )


def test_579_fake_gateway_passes_only_with_o1_and_o2_evidence(tmp_path: Path) -> None:
    gateway = _gateway(_Observer())
    preflight = gateway.preflight(RunRequest("p", "579", "579.o2", ("CASE-1",)))
    assert preflight.ready is True
    result = gateway.run_case(
        case={"case_id": "CASE-1", "sheet": "秒表"},
        artifact_dir=tmp_path,
        run_id="run-1",
    )
    assert result["verdict"] == "PASS"
    assert result["infrastructure_status"] == "READY"
    assert result["evidence_contract"]["serial_write"] is False
    rendered = repr(result)
    assert "raw_response" not in rendered
    assert "data_hex" not in rendered
    assert "cmd_id" not in rendered


def test_app_ack_alone_cannot_prove_product_pass(tmp_path: Path) -> None:
    result = _gateway(_Observer(capture_ok=False)).run_case(
        case={"case_id": "CASE-1", "sheet": "秒表"},
        artifact_dir=tmp_path,
        run_id="run-2",
    )
    assert result["verdict"] == "CANNOT_VERIFY"
    assert result["infrastructure_status"] == "OBSERVATION_INCOMPLETE"
    assert result["evidence_contract"]["complete"] is False
