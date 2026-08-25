from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_loop_system.tools.case_map import CaseEntry, run_case
from agent_loop_system.tools.simulator import CommandResult
from agent_loop_system.tools.test import judge_case_result, save_evidence
from agent_loop_system.tools.watch_579_case import Watch579CaseSession, run_watch_579_case


class ExecutionOnlySession:
    requires_gui_ping_barrier = False
    observation_available = False
    observation_unavailable_reason = "579 BLE screenshot unavailable"

    def __init__(self) -> None:
        self.commands: list[str] = []

    def send(self, command: str, **_kwargs):
        self.commands.append(command)
        return CommandResult(
            request=command,
            status="accepted",
            raw={"transport_acked": True},
            lines=[],
        )

    def capture_screenshot(self, _output_path: str) -> bool:
        raise AssertionError("579 execution-only session must not request screenshots")


class Watch579CaseTests(unittest.TestCase):
    def test_blocked_ota_entry_route_is_rejected_before_starting_session(self) -> None:
        case = CaseEntry(
            case_id="CALC-001",
            sheet="计算器",
            actions=[":BUTTON_PRESS:1,1,0"],
            mapping_status="BLOCKED",
            block_reason_code="BUTTON_PRESS_UNSUPPORTED_AFTER_OTA",
        )
        with self.assertRaisesRegex(ValueError, "BUTTON_PRESS_UNSUPPORTED_AFTER_OTA"):
            run_watch_579_case(case, "unused.bmp")

    def test_semantic_commands_translate_to_04_05_top5step(self) -> None:
        click = Watch579CaseSession._raw_payload(":TP_CLICK:51,156,1")
        button = Watch579CaseSession._raw_payload(":BUTTON_PRESS:1,1,0")
        raw = Watch579CaseSession._raw_payload(":W579_RAW:0x02,3b,")
        self.assertEqual((click["cmd"], click["key"]), ("04", "05"))
        self.assertIn("54 50 5F 43 4C 49 43 4B", click["data"])
        self.assertEqual((button["cmd"], button["key"]), ("04", "05"))
        self.assertEqual(raw, {"cmd": "0x02", "key": "3b", "data": ""})

    def test_session_calls_loopback_internal_api_with_lease_token(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return json.dumps({
                    "tx_id": "tx-579",
                    "transport_acked": True,
                    "effect_verified": False,
                }).encode("utf-8")

        session = Watch579CaseSession(
            base_url="http://127.0.0.1:8768",
            lease_token="random-task-token",
        )
        session.start()
        try:
            with patch(
                "agent_loop_system.tools.watch_579_case.urlopen",
                return_value=FakeResponse(),
            ) as opener:
                result = session.send(":BUTTON_PRESS:1,1,0")
        finally:
            session.stop()

        request = opener.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8768/api/internal/hardware/579/send",
        )
        self.assertEqual(
            request.get_header("X-agent-loop-579-lease"),
            "random-task-token",
        )
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual((body["cmd"], body["key"]), ("04", "05"))
        self.assertEqual(result.status, "accepted")
        self.assertTrue(result.raw["transport_acked"])
        self.assertFalse(result.raw["effect_verified"])

    def test_execution_ready_case_has_ok_execution_and_incomplete_evidence(self) -> None:
        case = CaseEntry(
            case_id="CALC-001",
            sheet="计算器",
            steps_text="点击数字 1",
            expected_text="显示 1",
            verification_points=["计算器显示 1"],
            actions=[":TP_CLICK:51,156,1", ":HOST_WAIT:50"],
            mapping_status="EXECUTION_READY",
        )
        session = ExecutionOnlySession()
        with tempfile.TemporaryDirectory() as temporary:
            result = run_case(
                session,
                case,
                screenshot_path=Path(temporary) / "screenshot.bmp",
            )
            self.assertEqual(result.execution_mode, "execution_ready_mapping")
            self.assertEqual(session.commands, [":TP_CLICK:51,156,1"])
            self.assertNotIn("GUI_PING", " ".join(session.commands))
            self.assertEqual(result.screenshots, [])
            self.assertEqual(result.evidence_contract["status"], "INCOMPLETE")
            self.assertFalse(result.evidence_contract["complete"])
            self.assertEqual(
                result.evidence_contract["issues"][-1]["code"],
                "observation_unavailable",
            )
            decision = judge_case_result(result)
            self.assertEqual(decision.verdict, "CANNOT_VERIFY")
            output = Path(temporary) / "result.json"
            save_evidence(result, decision, output)
            saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved["workflow_status"], "completed")
        self.assertEqual(saved["execution_status"], "OK")
        self.assertEqual(saved["evidence_status"], "INCOMPLETE")
        self.assertEqual(saved["reason_code"], "OBSERVATION_UNAVAILABLE")
        self.assertEqual(saved["verdict"], "CANNOT_VERIFY")
        self.assertEqual(saved["mapping_status"], "NOT_RECORDED")

    def test_command_failure_remains_execution_error(self) -> None:
        class FailedSession(ExecutionOnlySession):
            def send(self, command: str, **_kwargs):
                raise RuntimeError("BLE_ACK_TIMEOUT: no L1 ACK")

        case = CaseEntry(
            case_id="CALC-002",
            sheet="计算器",
            actions=[":BUTTON_PRESS:1,1,0"],
            mapping_status="EXECUTION_READY",
        )
        result = run_case(FailedSession(), case)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            save_evidence(result, judge_case_result(result), output)
            saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(saved["workflow_status"], "failed")
        self.assertEqual(saved["execution_status"], "ERROR")
        self.assertEqual(saved["verdict"], "CANNOT_VERIFY")


if __name__ == "__main__":
    unittest.main()
