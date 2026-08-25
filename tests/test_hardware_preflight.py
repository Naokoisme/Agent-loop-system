from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_loop_system.tools.hardware_preflight import (
    HardwarePreflightFailed,
    internal_error_preflight,
    load_cached_hardware_preflight,
    persist_hardware_preflight,
    require_hardware_preflight,
    run_hardware_preflight,
    target_busy_preflight,
    unchecked_hardware_preflight,
)


class FakeMtpSystem:
    def __init__(
        self,
        *,
        devices: int = 1,
        status: str = "OK",
        namespace_error: Exception | None = None,
    ):
        self.devices = devices
        self.status = status
        self.namespace_error = namespace_error
        self.calls: list[str] = []

    def inspect_usb_devices(self, *, timeout: float = 5.0):
        self.calls.append("pnp")
        return [
            {
                "instance_id": f"USB\\VID_301A&PID_6808\\SERIAL-{index}",
                "status": self.status,
                "friendly_name": "ZORA",
                "class": "WPD",
            }
            for index in range(self.devices)
        ]

    def probe_namespace(self, *, timeout: float = 10.0):
        self.calls.append("wpd")
        if self.namespace_error is not None:
            raise self.namespace_error
        return {"device": "ZORA", "storage": "storage", "folder": "download"}


class FakeSerialSession:
    def __init__(self, *, failure: Exception | None = None):
        self.failure = failure
        self.commands: list[str] = []
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def send(self, command: str, **_kwargs):
        self.commands.append(command)
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(status="processed", raw={"type": "gui_ack"})

    def stop(self) -> None:
        self.stopped = True


class HardwarePreflightTest(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = {
            "W30_HARDWARE_PORT": "COM7",
            "W30_HARDWARE_TRANSPORT": "supercom",
            "W30_HARDWARE_CAPTURE_PROVIDER": "mtp",
        }

    @staticmethod
    def _profile(**_kwargs):
        return SimpleNamespace(
            version="1.0.0",
            firmware_version="30",
            project="6202_W5230",
        )

    @staticmethod
    def _ports(_port: str):
        return {
            "items": [{
                "port": "COM7",
                "present": True,
                "supercom_open": True,
                "pipe_path": r"\\.\pipe\SuperCom.AgentBridge.COM7",
            }]
        }

    @staticmethod
    def _llm(**_kwargs):
        return {
            "ok": True,
            "model": "vision-test",
            "base_url": "https://example.invalid/v1",
            "latency_ms": 12,
        }

    def _run(
        self,
        *,
        environment=None,
        profile_loader=None,
        serial_ports_probe=None,
        mtp_system=None,
        serial=None,
        llm_probe=None,
    ):
        session = serial or FakeSerialSession()
        with tempfile.TemporaryDirectory() as root:
            result = run_hardware_preflight(
                evidence_dir=Path(root),
                environment=environment or self.environment,
                profile_loader=profile_loader or self._profile,
                serial_ports_probe=serial_ports_probe or self._ports,
                mtp_system=mtp_system or FakeMtpSystem(),
                serial_session_factory=lambda **_kwargs: session,
                llm_probe=llm_probe or self._llm,
            )
        return result, session

    def test_ready_probe_sends_only_one_gui_ping(self) -> None:
        result, session = self._run()

        self.assertTrue(result.ready)
        self.assertEqual(result.readiness_status, "ready")
        self.assertEqual([check.status for check in result.checks], ["pass"] * 6)
        self.assertEqual(len(session.commands), 1)
        self.assertTrue(session.commands[0].startswith(":GUI_PING:"))
        forbidden = (
            "SYSTEM_REBOOT",
            "TEST_SESSION",
            "dal_usb",
            "BUTTON_PRESS",
            "ENTER_PAGE",
            "SCREENSHOT",
        )
        self.assertFalse(any(token in session.commands[0] for token in forbidden))
        self.assertTrue(session.stopped)

    def test_profile_failure_stops_before_any_device_probe(self) -> None:
        mtp = FakeMtpSystem()
        result, session = self._run(
            profile_loader=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("bad profile")),
            mtp_system=mtp,
        )

        self.assertFalse(result.ready)
        self.assertEqual(result.primary_code, "PROFILE_INVALID")
        self.assertEqual(session.commands, [])
        self.assertEqual(mtp.calls, [])

    def test_port_not_selected(self) -> None:
        result, session = self._run(environment={
            **self.environment,
            "W30_HARDWARE_PORT": "",
        })
        self.assertEqual(result.primary_code, "PORT_NOT_SELECTED")
        self.assertEqual(session.commands, [])

    def test_pipe_unavailable(self) -> None:
        result, session = self._run(serial_ports_probe=lambda _port: {"items": []})
        self.assertEqual(result.primary_code, "SUPERCOM_PIPE_UNAVAILABLE")
        self.assertEqual(session.commands, [])

    def test_usb_absent_and_ambiguous_are_distinct(self) -> None:
        absent, absent_session = self._run(mtp_system=FakeMtpSystem(devices=0))
        ambiguous, ambiguous_session = self._run(mtp_system=FakeMtpSystem(devices=2))

        self.assertEqual(absent.primary_code, "USB_DEVICE_NOT_PRESENT")
        self.assertEqual(ambiguous.primary_code, "USB_TARGET_AMBIGUOUS")
        self.assertEqual(absent_session.commands, [])
        self.assertEqual(ambiguous_session.commands, [])

    def test_usb_instance_must_be_online(self) -> None:
        result, session = self._run(
            mtp_system=FakeMtpSystem(status="Error")
        )

        self.assertEqual(result.primary_code, "USB_DEVICE_NOT_PRESENT")
        self.assertEqual(session.commands, [])
        usb = next(check for check in result.checks if check.key == "usb_pnp")
        self.assertEqual(usb.diagnostics["online_count"], 0)

    def test_usb_probe_failure_is_internal_not_device_absence(self) -> None:
        class BrokenPnp(FakeMtpSystem):
            def inspect_usb_devices(self, *, timeout: float = 5.0):
                raise RuntimeError("Get-PnpDevice unavailable")

        result, session = self._run(mtp_system=BrokenPnp())

        self.assertEqual(result.primary_code, "PREFLIGHT_INTERNAL_ERROR")
        self.assertEqual(result.readiness_status, "blocked")
        self.assertEqual(session.commands, [])

    def test_mtp_namespace_failure_stops_before_uart(self) -> None:
        result, session = self._run(
            mtp_system=FakeMtpSystem(namespace_error=RuntimeError("download missing"))
        )
        self.assertEqual(result.primary_code, "MTP_NAMESPACE_NOT_READY")
        self.assertEqual(session.commands, [])

    def test_pipe_exists_but_uart_has_no_data(self) -> None:
        result, session = self._run(serial=FakeSerialSession(failure=TimeoutError("zero bytes")))
        self.assertEqual(result.primary_code, "SUPERCOM_NO_UART")
        self.assertEqual(len(session.commands), 1)
        self.assertTrue(session.commands[0].startswith(":GUI_PING:"))

    def test_llm_connectivity_is_blocking(self) -> None:
        result, session = self._run(
            llm_probe=lambda **_kwargs: {"ok": False, "message": "401"}
        )
        self.assertEqual(result.primary_code, "LLM_NOT_READY")
        self.assertEqual(session.commands[0].split(":")[1], "GUI_PING")
        self.assertFalse(result.ready)
        llm_check = next(check for check in result.checks if check.key == "llm")
        self.assertEqual(llm_check.diagnostics["scopes"][0]["scope"], "current")

    def test_requested_llm_scopes_are_each_probed(self) -> None:
        with tempfile.TemporaryDirectory() as root, patch(
            "agent_loop_system.tools.llm_config.test_llm_connectivity",
            side_effect=[
                {"ok": True, "model": "fixed", "latency_ms": 1},
                {"ok": True, "model": "exploration", "latency_ms": 2},
            ],
        ) as llm_probe:
            result = run_hardware_preflight(
                evidence_dir=root,
                environment=self.environment,
                profile_loader=self._profile,
                serial_ports_probe=self._ports,
                mtp_system=FakeMtpSystem(),
                serial_session_factory=lambda **_kwargs: FakeSerialSession(),
                llm_scopes=("fixed", "exploration"),
            )

        self.assertTrue(result.ready)
        self.assertEqual(llm_probe.call_count, 2)
        llm_check = next(check for check in result.checks if check.key == "llm")
        self.assertEqual(
            [item["scope"] for item in llm_check.diagnostics["scopes"]],
            ["fixed", "exploration"],
        )

    def test_require_raises_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            persisted = Path(root) / "preflight.json"
            with self.assertRaises(HardwarePreflightFailed) as raised:
                require_hardware_preflight(
                    evidence_dir=root,
                    environment=self.environment,
                    profile_loader=self._profile,
                    serial_ports_probe=lambda _port: {"items": []},
                    mtp_system=FakeMtpSystem(),
                    serial_session_factory=lambda **_kwargs: FakeSerialSession(),
                    llm_probe=self._llm,
                    persist_paths=(persisted,),
                )
            self.assertTrue(persisted.is_file())
            self.assertIn(
                "SUPERCOM_PIPE_UNAVAILABLE",
                persisted.read_text(encoding="utf-8"),
            )
        self.assertEqual(raised.exception.code, "SUPERCOM_PIPE_UNAVAILABLE")

    def test_cache_round_trip_and_unchecked_default(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "preflight.json"
            missing = load_cached_hardware_preflight(path)
            self.assertEqual(missing.readiness_status, "unchecked")
            self.assertIsNone(missing.checked_at)

            ready, _ = self._run()
            persist_hardware_preflight(ready, path)
            cached = load_cached_hardware_preflight(path)
            self.assertTrue(cached.ready)
            self.assertEqual(cached.primary_code, None)

    def test_target_busy_is_stable_and_does_not_probe(self) -> None:
        result = target_busy_preflight(job_id="abc123")
        self.assertFalse(result.ready)
        self.assertEqual(result.readiness_status, "blocked")
        self.assertEqual(result.primary_code, "TARGET_BUSY")

    def test_internal_error_is_stable(self) -> None:
        result = internal_error_preflight(RuntimeError("probe exploded"))
        self.assertFalse(result.ready)
        self.assertEqual(result.readiness_status, "blocked")
        self.assertEqual(result.primary_code, "PREFLIGHT_INTERNAL_ERROR")

    def test_failure_actions_are_user_facing_and_actionable(self) -> None:
        profile, _ = self._run(
            profile_loader=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("bad profile"))
        )
        port, _ = self._run(environment={**self.environment, "W30_HARDWARE_PORT": ""})
        pipe, _ = self._run(serial_ports_probe=lambda _port: {"items": []})
        usb, _ = self._run(mtp_system=FakeMtpSystem(devices=0))
        screenshot, _ = self._run(
            mtp_system=FakeMtpSystem(namespace_error=RuntimeError("download missing"))
        )
        watch, _ = self._run(serial=FakeSerialSession(failure=TimeoutError("zero bytes")))
        judge, _ = self._run(llm_probe=lambda **_kwargs: {"ok": False, "message": "401"})
        busy = target_busy_preflight(job_id="abc123")
        internal = internal_error_preflight(RuntimeError("probe exploded"))

        expected = {
            "PROFILE_INVALID": "重新选择测试项目；如仍失败，联系维护人员",
            "PORT_NOT_SELECTED": "打开系统设置，选择当前手表使用的串口后重新检查",
            "SUPERCOM_PIPE_UNAVAILABLE": "打开 SuperCom，连接当前手表对应的串口后重新检查",
            "USB_DEVICE_NOT_PRESENT": "重新插拔 USB，确认电脑能够识别手表后重试",
            "MTP_NAMESPACE_NOT_READY": "重新连接 USB，并确认电脑能够打开手表存储后重试",
            "SUPERCOM_NO_UART": "确认 SuperCom 连接的是当前手表，并唤醒手表屏幕后重试",
            "LLM_NOT_READY": "检查网络和判定服务设置后重试",
            "TARGET_BUSY": "等待该任务结束或停止后再检查",
            "PREFLIGHT_INTERNAL_ERROR": "重新检查；如再次出现，展开诊断信息并联系维护人员",
        }
        for result in (profile, port, pipe, usb, screenshot, watch, judge, busy, internal):
            with self.subTest(code=result.primary_code):
                check = next(item for item in result.checks if item.code == result.primary_code)
                self.assertEqual(check.action, expected[result.primary_code])
                for forbidden in ("PnP", "MTP", "UART", "VID", "PID", "preflight.json"):
                    self.assertNotIn(forbidden, check.action)

    def test_unchecked_result_is_not_ready(self) -> None:
        result = unchecked_hardware_preflight()
        self.assertFalse(result.ready)
        self.assertEqual(result.readiness_status, "unchecked")


if __name__ == "__main__":
    unittest.main()
