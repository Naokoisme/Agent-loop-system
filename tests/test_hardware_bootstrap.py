from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_loop_system.tools import hardware_bootstrap, real_device
from agent_loop_system.tools.hardware_serial import (
    HardwareCommandResult,
    HardwareSerialTimeoutError,
)
from agent_loop_system.tools.real_device import (
    TestSessionBootstrapError as BootstrapError,
    TestSessionBootstrapResult as BootstrapResult,
    TestSessionStatus as SessionStatus,
    bootstrap_test_session,
)


def _result(
    request: str,
    status: str,
    event_type: str,
    **extra: object,
) -> HardwareCommandResult:
    raw: dict[str, object] = {
        "type": event_type,
        "request": request,
        "status": status,
        **extra,
    }
    return HardwareCommandResult(
        request=request,
        status=status,
        raw=raw,
        lines=[],
        start_index=0,
    )


class FakeBootstrapSerial:
    def __init__(
        self,
        *responses: HardwareCommandResult | BaseException,
        ready_event: dict[str, object] | BaseException | None = None,
    ) -> None:
        self.responses = list(responses)
        self.ready_event = ready_event
        self.send_calls: list[tuple[str, dict[str, object]]] = []
        self.wait_calls: list[dict[str, object]] = []
        self.start_calls = 0
        self.stop_calls = 0

    @property
    def event_count(self) -> int:
        return 0

    def start(self) -> None:
        self.start_calls += 1

    def send(self, command: str, **kwargs: object) -> HardwareCommandResult:
        self.send_calls.append((command, dict(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def wait_for_event(self, **kwargs: object) -> dict[str, object]:
        self.wait_calls.append(dict(kwargs))
        if isinstance(self.ready_event, BaseException):
            raise self.ready_event
        assert self.ready_event is not None
        return dict(self.ready_event)

    def stop(self) -> None:
        self.stop_calls += 1


class BootstrapTestSessionTest(unittest.TestCase):
    def test_owned_session_refuses_direct_com_transport(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ,
            {"W30_HARDWARE_TRANSPORT": "serial"},
            clear=True,
        ), mock.patch.object(real_device, "_create_hardware_serial_session") as create:
            with self.assertRaisesRegex(ValueError, "supercom"):
                bootstrap_test_session(evidence_dir=root)

        create.assert_not_called()

    def test_ping_then_sends_exactly_one_start_without_status_or_stop(self) -> None:
        serial = FakeBootstrapSerial(
            _result("gui_ping", "processed", "gui_ack", seq=41),
            _result(
                "test_session",
                "active",
                "test_session",
                lease_seconds=86400,
            ),
        )
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_device, "_positive_handshake_sequence", return_value=41
        ):
            result = bootstrap_test_session(
                evidence_dir=root,
                serial_session=serial,
            )

        self.assertTrue(result.status.active)
        self.assertEqual(result.status.lease_seconds, 86400)
        self.assertTrue(result.start_sent)
        self.assertEqual(result.gui_ping_attempts, 1)
        self.assertFalse(result.bootstrap_event_seen)
        self.assertEqual(
            [command for command, _kwargs in serial.send_calls],
            [":GUI_PING:41", ":TEST_SESSION:START"],
        )
        self.assertNotIn(":TEST_SESSION:STATUS", [call[0] for call in serial.send_calls])
        self.assertEqual(serial.start_calls, 1)
        self.assertEqual(serial.stop_calls, 1)

    def test_waits_for_bootstrap_ready_then_retries_only_gui_ping(self) -> None:
        serial = FakeBootstrapSerial(
            HardwareSerialTimeoutError("not ready"),
            _result("gui_ping", "processed", "gui_ack", seq=52),
            _result(
                "test_session",
                "active",
                "test_session",
                lease_seconds=86399,
            ),
            ready_event={
                "type": "test_bootstrap",
                "request": "test_bootstrap",
                "status": "ready",
                "grace_seconds": 180,
            },
        )
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_device, "_positive_handshake_sequence", side_effect=(51, 52)
        ):
            result = bootstrap_test_session(
                evidence_dir=root,
                serial_session=serial,
            )

        self.assertEqual(result.gui_ping_attempts, 2)
        self.assertTrue(result.bootstrap_event_seen)
        self.assertEqual(
            [command for command, _kwargs in serial.send_calls],
            [":GUI_PING:51", ":GUI_PING:52", ":TEST_SESSION:START"],
        )
        self.assertEqual(len(serial.wait_calls), 1)
        self.assertEqual(serial.stop_calls, 1)

    def test_expired_bootstrap_never_sends_start(self) -> None:
        serial = FakeBootstrapSerial(
            HardwareSerialTimeoutError("asleep"),
            ready_event={
                "type": "test_bootstrap",
                "request": "test_bootstrap",
                "status": "expired",
                "reason": "grace_expired",
            },
        )
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(BootstrapError) as raised:
                bootstrap_test_session(
                    evidence_dir=root,
                    serial_session=serial,
                )

        self.assertEqual(raised.exception.code, "BLOCKED_LOW_POWER_WAKE")
        self.assertEqual(len(serial.send_calls), 1)
        self.assertTrue(serial.send_calls[0][0].startswith(":GUI_PING:"))
        self.assertEqual(serial.stop_calls, 1)

    def test_already_active_reply_is_success_without_renewal_protocol(self) -> None:
        serial = FakeBootstrapSerial(
            _result("gui_ping", "processed", "gui_ack", seq=61),
            _result(
                "test_session",
                "active",
                "test_session",
                lease_seconds=43210,
                reason="already_active",
            ),
        )
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            real_device, "_positive_handshake_sequence", return_value=61
        ):
            result = bootstrap_test_session(
                evidence_dir=root,
                serial_session=serial,
            )

        self.assertEqual(result.status.lease_seconds, 43210)
        self.assertEqual(result.status.raw["reason"], "already_active")
        self.assertEqual([call[0] for call in serial.send_calls].count(":TEST_SESSION:START"), 1)


class HardwareBootstrapCliTest(unittest.TestCase):
    def test_environment_requires_the_shared_workspace_and_supercom(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = SimpleNamespace(
                project="6202_W5230",
                source_root=Path(root).resolve(),
            )
            environment = {
                "W30_SOURCE_ROOT": root,
                "W30_AGENT_WORKSPACE_ROOT": root,
                "W30_PROJECT": "6202_W5230",
                "W30_HARDWARE_PROJECT": "6202_W5230",
                "W30_HARDWARE_TRANSPORT": "supercom",
                "W30_HARDWARE_PORT": "COM7",
            }
            with mock.patch.object(
                hardware_bootstrap.HardwareTargetConfig,
                "from_env",
                return_value=config,
            ), mock.patch.dict(os.environ, environment, clear=True):
                validated, port = hardware_bootstrap._validate_environment()

        self.assertIs(validated, config)
        self.assertEqual(port, "COM7")

    def test_environment_rejects_direct_serial_transport(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = SimpleNamespace(
                project="6202_W5230",
                source_root=Path(root).resolve(),
            )
            environment = {
                "W30_SOURCE_ROOT": root,
                "W30_AGENT_WORKSPACE_ROOT": root,
                "W30_PROJECT": "6202_W5230",
                "W30_HARDWARE_PROJECT": "6202_W5230",
                "W30_HARDWARE_TRANSPORT": "serial",
                "W30_HARDWARE_PORT": "COM7",
            }
            with mock.patch.object(
                hardware_bootstrap.HardwareTargetConfig,
                "from_env",
                return_value=config,
            ), mock.patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, "supercom"):
                    hardware_bootstrap._validate_environment()

    def test_cli_prints_machine_readable_success(self) -> None:
        bootstrap_result = BootstrapResult(
            status=SessionStatus(
                active=True,
                lease_seconds=86400,
                raw={"status": "active", "lease_seconds": 86400},
            ),
            start_sent=True,
            gui_ping_attempts=1,
            bootstrap_event_seen=False,
        )
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            hardware_bootstrap,
            "_validate_environment",
            return_value=(SimpleNamespace(project="6202_W5230"), "COM7"),
        ), mock.patch.object(
            hardware_bootstrap,
            "bootstrap_test_session",
            return_value=bootstrap_result,
        ) as bootstrap, redirect_stdout(output):
            exit_code = hardware_bootstrap.main(
                ["--evidence-dir", str(Path(root) / "evidence")]
            )

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "active")
        self.assertEqual(payload["port"], "COM7")
        bootstrap.assert_called_once()


if __name__ == "__main__":
    unittest.main()
