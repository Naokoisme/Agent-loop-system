from __future__ import annotations

import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_loop_system.tools.hardware_serial import (
    DEFAULT_BACKGROUND_ERROR_CACHE_CAPACITY,
    DEFAULT_BAUDRATE,
    DEFAULT_EVENT_CACHE_CAPACITY,
    DEFAULT_LINE_CACHE_CAPACITY,
    DEFAULT_PORT,
    DEFAULT_RX_BUFFER_SIZE,
    HardwareSerialCursorExpiredError,
    HardwareSerialSession,
    HardwareSerialTimeoutError,
    SHELL_WRITE_BURST_LIMIT,
    SerialTransportError,
    SuperComPipeTransport,
    UnsafeHardwareCommandError,
    Win32SerialTransport,
    dangerous_command_reason,
    is_dangerous_command,
    strip_ansi,
)
from agent_loop_system.tools.hardware_serial_ports import (
    get_supercom_pipe_name,
    get_supercom_pipe_path,
)


class FakeTransport:
    def __init__(self, on_write=None) -> None:
        self.opened = False
        self.closed = False
        self.writes: list[bytes] = []
        self._chunks: queue.Queue[bytes | None] = queue.Queue()
        self._on_write = on_write

    def open(self) -> None:
        self.opened = True

    def read(self, _size: int) -> bytes:
        try:
            chunk = self._chunks.get(timeout=0.02)
        except queue.Empty:
            return b""
        return b"" if chunk is None else chunk

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if self._on_write is not None:
            self._on_write(self, data)
        return len(data)

    def feed(self, *chunks: bytes) -> None:
        for chunk in chunks:
            self._chunks.put(chunk)

    def close(self) -> None:
        self.closed = True
        self._chunks.put(None)


class HardwareSerialTest(unittest.TestCase):
    def _session(self, root: str, transport: FakeTransport, **kwargs) -> HardwareSerialSession:
        session = HardwareSerialSession(
            log_dir=Path(root) / "serial",
            transport=transport,
            cmd_timeout=0.4,
            **kwargs,
        )
        session.start()
        self.addCleanup(session.stop)
        return session

    @staticmethod
    def _wait_until(predicate, timeout: float = 0.5) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError("condition was not reached")

    @staticmethod
    def _event_line(seq: int, *, request: str = "cache_test") -> bytes:
        return (
            json.dumps(
                {
                    "protocol": "w30_test_bridge",
                    "version": 1,
                    "type": "cache_event",
                    "request": request,
                    "seq": seq,
                    "status": "ok",
                },
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    def test_default_transport_is_com7_1500000(self) -> None:
        session = HardwareSerialSession()
        self.assertEqual(session.port, DEFAULT_PORT)
        self.assertEqual(session.baudrate, DEFAULT_BAUDRATE)
        self.assertIsInstance(session.transport, Win32SerialTransport)
        self.assertEqual(session.transport.port, "COM7")
        self.assertEqual(session.transport.baudrate, 1_500_000)
        self.assertEqual(session.transport.rx_buffer_size, DEFAULT_RX_BUFFER_SIZE)
        self.assertGreaterEqual(DEFAULT_RX_BUFFER_SIZE, 410 * 502 * 3)

    def test_transport_exposes_dtr_rts_configuration(self) -> None:
        transport = Win32SerialTransport("COM7", dtr=False, rts=False)
        self.assertFalse(transport.dtr)
        self.assertFalse(transport.rts)

    def test_supercom_pipe_name_is_derived_from_port(self) -> None:
        transport = SuperComPipeTransport("com7")
        self.assertEqual(transport.pipe_name, "SuperCom.AgentBridge.COM7")
        self.assertEqual(
            transport.pipe_path, r"\\.\pipe\SuperCom.AgentBridge.COM7"
        )

        with self.assertRaisesRegex(ValueError, "positive integer"):
            SuperComPipeTransport("COM7", connect_timeout_ms=0)

    def test_write_shell_line_uses_shared_transport_and_crlf(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport()
            session = self._session(root, transport)

            session.write_shell_line("dal_usb close")

            self.assertEqual(transport.writes, [b"dal_usb close\r\n"])
            for invalid in ("", "bad\nline", "bad\rline", None):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    session.write_shell_line(invalid)
            session.stop()

    def test_shell_wire_boundary_is_atomic_and_long_lines_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport()
            session = self._session(root, transport)

            safe_line = (
                'srv_quick_cmd send "TOP5STEP:SCREENSHOT_CAPTURE_FILE:'
                '9999999;"'
            )
            safe_wire = (safe_line + "\r\n").encode("utf-8")
            self.assertEqual(len(safe_wire), SHELL_WRITE_BURST_LIMIT)
            session.write_shell_line(safe_line)
            self.assertEqual(transport.writes, [safe_wire])

            for sequence in (10_000_000, 999_999_999, 0xFFFFFFFF):
                with self.subTest(sequence=sequence):
                    transport.writes.clear()
                    line = (
                        'srv_quick_cmd send "TOP5STEP:SCREENSHOT_CAPTURE_FILE:'
                        f'{sequence};"'
                    )
                    with self.assertRaisesRegex(
                        SerialTransportError,
                        "maximum safe burst is 64 bytes",
                    ):
                        session.write_shell_line(line)
                    self.assertEqual(transport.writes, [])
            session.stop()

    def test_cache_capacities_have_screenshot_margin_and_validate_positive_ints(self) -> None:
        self.assertGreaterEqual(DEFAULT_EVENT_CACHE_CAPACITY, 1608 * 2)
        self.assertGreater(DEFAULT_LINE_CACHE_CAPACITY, DEFAULT_EVENT_CACHE_CAPACITY)
        self.assertGreater(DEFAULT_BACKGROUND_ERROR_CACHE_CAPACITY, 0)

        for name in (
            "line_cache_capacity",
            "event_cache_capacity",
            "background_error_cache_capacity",
        ):
            for invalid in (0, -1, 1.5, True, "10"):
                with self.subTest(name=name, invalid=invalid):
                    with self.assertRaisesRegex(ValueError, "positive integer"):
                        HardwareSerialSession(**{name: invalid})

    def test_background_error_window_is_absolute_bounded_and_disk_remains_complete(self) -> None:
        raw_lines = [f"watchdog cache error {index}\n".encode() for index in range(4)]
        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport()
            session = self._session(
                root,
                transport,
                background_error_cache_capacity=2,
            )
            transport.feed(*raw_lines)
            self._wait_until(lambda: session.background_error_count == 4)

            self.assertEqual(session.background_error_count, 4)
            self.assertEqual(session.background_error_cache_start_index, 2)
            self.assertEqual(len(session._background_errors), 2)
            self.assertEqual(
                session.background_errors_since(2),
                ["watchdog cache error 2", "watchdog cache error 3"],
            )
            self.assertEqual(
                session.background_errors_since(session.background_error_count), []
            )
            with self.assertRaises(HardwareSerialCursorExpiredError) as caught:
                session.background_errors_since(1)
            self.assertEqual(caught.exception.stream, "background_error")
            self.assertEqual(caught.exception.oldest_available, 2)

            session.stop()
            errors = (Path(root) / "serial" / "errors.log").read_text(
                encoding="utf-8"
            )
            self.assertEqual(errors.count("device_background_error"), 4)
            for index in range(4):
                self.assertIn(f"watchdog cache error {index}", errors)

    def test_background_error_cursor_plus_returned_length_stays_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport()
            session = self._session(
                root,
                transport,
                background_error_cache_capacity=3,
            )
            transport.feed(b"watchdog before monitor\n")
            self._wait_until(lambda: session.background_error_count == 1)

            cursor = len(session.background_errors_since(0))
            self.assertEqual(cursor, session.background_error_count)
            transport.feed(b"ASSERT after monitor\n", b"fatal after monitor\n")
            self._wait_until(lambda: session.background_error_count == 3)

            values = session.background_errors_since(cursor)
            cursor += len(values)
            self.assertEqual(values, ["ASSERT after monitor", "fatal after monitor"])
            self.assertEqual(cursor, session.background_error_count)
            self.assertEqual(session.background_errors_since(cursor), [])
            session.stop()

    def test_memory_windows_evict_but_absolute_counts_and_disk_logs_remain_complete(self) -> None:
        raw_lines = [self._event_line(seq) for seq in range(5)]
        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport()
            session = self._session(
                root,
                transport,
                line_cache_capacity=3,
                event_cache_capacity=2,
            )
            transport.feed(*raw_lines)
            self._wait_until(lambda: session.event_count == 5)

            self.assertEqual(session.line_count, 5)
            self.assertEqual(session.event_count, 5)
            self.assertEqual(session.line_cache_start_index, 2)
            self.assertEqual(session.event_cache_start_index, 3)
            self.assertEqual(len(session._lines), 3)
            self.assertEqual(len(session._events), 2)
            self.assertEqual(
                [json.loads(line)["seq"] for line in session.lines_since(2)],
                [2, 3, 4],
            )
            self.assertEqual(
                [event["seq"] for event in session.events_since(3)],
                [3, 4],
            )
            self.assertEqual(session.lines_since(session.line_count), [])
            self.assertEqual(session.events_since(session.event_count), [])

            with self.assertRaises(HardwareSerialCursorExpiredError) as line_error:
                session.lines_since(1)
            self.assertEqual(line_error.exception.stream, "line")
            self.assertEqual(line_error.exception.oldest_available, 2)
            with self.assertRaises(HardwareSerialCursorExpiredError) as event_error:
                session.events_since(2)
            self.assertEqual(event_error.exception.stream, "event")
            self.assertEqual(event_error.exception.oldest_available, 3)

            session.stop()
            log_dir = Path(root) / "serial"
            self.assertEqual((log_dir / "raw.bin").read_bytes(), b"".join(raw_lines))
            self.assertEqual(
                len((log_dir / "raw.log").read_text(encoding="utf-8").splitlines()),
                5,
            )
            self.assertEqual(
                len((log_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()),
                5,
            )

    def test_wait_for_event_uses_absolute_cursor_across_eviction_and_condition_wakeup(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport()
            session = self._session(
                root,
                transport,
                line_cache_capacity=3,
                event_cache_capacity=2,
            )
            transport.feed(*(self._event_line(seq) for seq in range(4)))
            self._wait_until(lambda: session.event_count == 4)
            cursor = session.event_count

            timer = threading.Timer(0.02, transport.feed, args=(self._event_line(4),))
            timer.start()
            self.addCleanup(timer.join)
            event = session.wait_for_event(
                request="cache_test",
                seq=4,
                event_type="cache_event",
                status="ok",
                start_event_index=cursor,
            )
            self.assertEqual(event["seq"], 4)
            self.assertEqual(session.event_count, 5)
            session.stop()

    def test_wait_for_event_rejects_an_expired_absolute_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport()
            session = self._session(root, transport, event_cache_capacity=2)
            transport.feed(*(self._event_line(seq) for seq in range(3)))
            self._wait_until(lambda: session.event_count == 3)

            with self.assertRaises(HardwareSerialCursorExpiredError) as caught:
                session.wait_for_event(start_event_index=0, timeout=0.2)
            self.assertEqual(caught.exception.oldest_available, 1)
            session.stop()

    def test_send_start_index_stays_absolute_after_prior_line_eviction(self) -> None:
        def respond(transport: FakeTransport, _wire: bytes) -> None:
            transport.feed(
                b'{"protocol":"w30_test_bridge","version":1,"type":"gui_ack",'
                b'"request":"gui_ping","seq":91,"status":"processed"}\n'
            )

        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport(respond)
            session = self._session(
                root,
                transport,
                line_cache_capacity=2,
                event_cache_capacity=2,
            )
            transport.feed(b"old-0\n", b"old-1\n", b"old-2\n")
            self._wait_until(lambda: session.line_count == 3)

            result = session.send(":GUI_PING:91")
            self.assertEqual(result.start_index, 3)
            self.assertEqual(result.lines, [json.dumps(result.raw, separators=(",", ":"))])
            session.stop()

    def test_strip_ansi_handles_csi_osc_and_single_escape(self) -> None:
        text = "\x1b[31mred\x1b[0m \x1b]0;title\x07ok\x1b7done"
        self.assertEqual(strip_ansi(text), "red okdone")

    def test_chunked_protocol_json_is_extracted_and_all_logs_are_saved(self) -> None:
        raw_chunks = (
            b"\x1b[32mshell> \x1b[0m{\"protocol\":\"w30_",
            b"test_bridge\",\"version\":1,\"type\":\"gui_ack\",",
            b"\"request\":\"gui_ping\",\"seq\":7,\"status\":\"processed\"}\r\n",
        )
        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport()
            session = self._session(root, transport)
            transport.feed(*raw_chunks)
            event = session.wait_for_event(
                request="gui_ping",
                seq=7,
                event_type="gui_ack",
                status="processed",
            )
            self.assertEqual(event["seq"], 7)
            self.assertNotIn("\x1b", session.lines_since(0)[0])
            session.stop()

            log_dir = Path(root) / "serial"
            self.assertEqual((log_dir / "raw.bin").read_bytes(), b"".join(raw_chunks))
            self.assertNotIn("\x1b", (log_dir / "raw.log").read_text(encoding="utf-8"))
            events = [
                json.loads(line)
                for line in (log_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events, [event])

    def test_gui_ack_inside_incomplete_ansi_sequence_is_parsed_from_raw_line(self) -> None:
        def respond(transport: FakeTransport, _wire: bytes) -> None:
            transport.feed(
                b'{"protocol":"w30_test_bridge","version":1,'
                b'"type":"command_result","request":"gui_ping",'
                b'"seq":null,"status":"accepted"}\r\n',
                b'\x1b[32m[08-14 16:07:40.530][printf]'
                b'[gui_win_manager.c:709] \x1b[3',
                b'{"protocol":"w30_test_bridge","version":1,'
                b'"type":"gui_ack","request":"gui_ping",'
                b'"seq":1000001,"status":"processed","thread":"gui"}\r\n',
                b'2mmove 2 to center_win\x1b[0m\r\n',
            )

        with tempfile.TemporaryDirectory() as root:
            session = self._session(root, FakeTransport(respond))
            result = session.send(":GUI_PING:1000001")

            self.assertEqual(result.status, "processed")
            self.assertEqual(result.raw["type"], "gui_ack")
            self.assertEqual(result.raw["seq"], 1000001)
            session.stop()

            log_dir = Path(root) / "serial"
            events = [
                json.loads(line)
                for line in (log_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1], result.raw)
            self.assertEqual((log_dir / "errors.log").read_text(encoding="utf-8"), "")
            self.assertNotIn("\x1b", (log_dir / "raw.log").read_text(encoding="utf-8"))

    def test_corrupted_json_is_not_reconstructed_into_a_fake_event(self) -> None:
        corrupted = (
            b'{"protocol":"w30_test_bridge","version":1,"type":"gui_ack",'
            b'LOG_FROM_ISR"request":"gui_ping","seq":8,"status":"processed"}\r\n'
        )
        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport()
            session = self._session(root, transport)
            transport.feed(corrupted)
            with self.assertRaises(HardwareSerialTimeoutError):
                session.wait_for_event(request="gui_ping", seq=8, timeout=0.08)
            session.stop()

            self.assertEqual(
                (Path(root) / "serial" / "events.jsonl").read_text(encoding="utf-8"),
                "",
            )
            self.assertIn(
                "protocol_json_error",
                (Path(root) / "serial" / "errors.log").read_text(encoding="utf-8"),
            )

    def test_known_voice_assistant_banner_inside_command_result_is_recovered(self) -> None:
        def respond(transport: FakeTransport, _wire: bytes) -> None:
            transport.feed(
                b'{"protocol":"w30_test_bridge","version":1,'
                b'"type":"command_resul'
                b'============_gui_comm_voice_assistant_init==============='
                b't","request":"button_press","seq":null,'
                b'"status":"accepted"}\r\n'
            )

        with tempfile.TemporaryDirectory() as root:
            session = self._session(root, FakeTransport(respond))
            result = session.send(
                ":BUTTON_PRESS:1,1,0",
                expected_type="command_result",
                expected_status="accepted",
            )
            self.assertEqual(result.request, "button_press")
            self.assertEqual(result.status, "accepted")
            session.stop()

            errors = (Path(root) / "serial" / "errors.log").read_text(
                encoding="utf-8"
            )
            self.assertIn("protocol_json_recovered", errors)

    def test_known_multiline_voice_assistant_noise_is_recovered(self) -> None:
        def respond(transport: FakeTransport, _wire: bytes) -> None:
            transport.feed(
                b'{"protocol":"w30_test_bridge","version":1,'
                b'"type":"command'
                b'============_gui_comm_voice_assistant_init==============='
                b'current_app[0]: 1\r\n',
                b'current_app[1]: 3\r\n',
                b'current_app[2]: 19\r\n',
                b'gui_list_real_set_header\r\n',
                b'_result","request":"button_press","seq":null,'
                b'"status":"accepted"}\r\n',
            )

        with tempfile.TemporaryDirectory() as root:
            session = self._session(root, FakeTransport(respond))
            result = session.send(
                ":BUTTON_PRESS:1,1,0",
                expected_type="command_result",
                expected_status="accepted",
            )
            self.assertEqual(result.request, "button_press")
            self.assertEqual(result.status, "accepted")
            session.stop()

            events = [
                json.loads(line)
                for line in (Path(root) / "serial" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(events, [result.raw])
            self.assertIn(
                "lines=5",
                (Path(root) / "serial" / "errors.log").read_text(
                    encoding="utf-8"
                ),
            )

    def test_gui_ping_ignores_accepted_and_unrelated_seq_then_waits_for_processed(self) -> None:
        def respond(transport: FakeTransport, _wire: bytes) -> None:
            transport.feed(
                b'{"protocol":"w30_test_bridge","version":1,"type":"command_result",'
                b'"request":"gui_ping","seq":41,"status":"accepted"}\r\n',
                b'{"protocol":"w30_test_bridge","version":1,"type":"gui_ack",'
                b'"request":"gui_ping","seq":40,"status":"processed"}\r\n',
                b'{"protocol":"w30_test_bridge","version":1,"type":"gui_ack",'
                b'"request":"gui_ping","seq":41,"status":"processed"}\r\n',
            )

        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport(respond)
            session = self._session(root, transport)
            result = session.send(":GUI_PING:41")

            self.assertEqual(result.status, "processed")
            self.assertEqual(result.raw["type"], "gui_ack")
            self.assertEqual(result.raw["seq"], 41)
            self.assertEqual(result.start_index, 0)
            self.assertEqual(len(result.lines), 3)
            self.assertEqual(
                transport.writes,
                [b'srv_quick_cmd send "TOP5STEP:GUI_PING:41;"\r\n'],
            )
            session.stop()

    def test_gui_tree_result_keeps_begin_node_and_end_lines(self) -> None:
        events = [
            {
                "protocol": "w30_test_bridge",
                "version": 1,
                "type": "command_result",
                "request": "gui_tree",
                "seq": 51,
                "status": "accepted",
            },
            {
                "protocol": "w30_test_bridge",
                "version": 1,
                "type": "gui_tree_begin",
                "request": "gui_tree",
                "seq": 51,
                "status": "ok",
            },
            {
                "protocol": "w30_test_bridge",
                "version": 1,
                "type": "gui_tree_node",
                "request": "gui_tree",
                "seq": 51,
                "path": "0/0",
            },
            {
                "protocol": "w30_test_bridge",
                "version": 1,
                "type": "gui_tree_end",
                "request": "gui_tree",
                "seq": 51,
                "status": "ok",
                "nodes": 1,
            },
        ]

        def respond(transport: FakeTransport, _wire: bytes) -> None:
            transport.feed(*(json.dumps(event, separators=(",", ":")).encode() + b"\n" for event in events))

        with tempfile.TemporaryDirectory() as root:
            session = self._session(root, FakeTransport(respond))
            result = session.send(":GUI_TREE:51")
            parsed = [json.loads(line) for line in result.lines]

            self.assertEqual(result.raw["type"], "gui_tree_end")
            self.assertEqual([event["type"] for event in parsed], [event["type"] for event in events])
            session.stop()

    def test_terminal_rejection_returns_without_waiting_for_expected_completion(self) -> None:
        def respond(transport: FakeTransport, _wire: bytes) -> None:
            transport.feed(
                b'{"protocol":"w30_test_bridge","version":1,"type":"command_result",'
                b'"request":"gui_state","seq":61,"status":"rejected",'
                b'"reason":"invalid_seq"}\n'
            )

        with tempfile.TemporaryDirectory() as root:
            session = self._session(root, FakeTransport(respond))
            result = session.send(":GUI_STATE:61")
            self.assertEqual(result.status, "rejected")
            self.assertEqual(result.raw["reason"], "invalid_seq")
            session.stop()

    def test_accepted_can_only_be_requested_explicitly_as_a_receipt(self) -> None:
        def respond(transport: FakeTransport, _wire: bytes) -> None:
            transport.feed(
                b'{"protocol":"w30_test_bridge","version":1,"type":"command_result",'
                b'"request":"tp_click","seq":null,"status":"accepted"}\n'
            )

        with tempfile.TemporaryDirectory() as root:
            session = self._session(root, FakeTransport(respond))
            result = session.send(
                ":TP_CLICK:100,120,0",
                expected_type="command_result",
                expected_status="accepted",
            )
            self.assertEqual(result.status, "accepted")
            session.stop()

    def test_dangerous_commands_are_refused_before_any_write(self) -> None:
        dangerous = (
            ":FACTORY_RESET:1",
            ":SYSTEM_SHUTDOWN:1",
            ":DEVICE_REBOOT:1",
            ":WEATHER_CLEAR:",
            ":FLASH_ERASE:all",
            ":POWER_PERCENT_SET:0",
            ":POWER_PERCENT_SET:00",
        )
        for command in dangerous:
            self.assertTrue(is_dangerous_command(command), command)
            self.assertIsNotNone(dangerous_command_reason(command))
        for command in (":GUI_PING:1", ":GUI_STATE:2", ":GUI_TREE:3", ":POWER_PERCENT_SET:1"):
            self.assertFalse(is_dangerous_command(command), command)

        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport()
            session = self._session(root, transport)
            with self.assertRaises(UnsafeHardwareCommandError):
                session.send(":FACTORY_RESET:1")
            self.assertEqual(transport.writes, [])
            session.stop()

    def test_background_faults_are_preserved_but_do_not_replace_protocol_reply(self) -> None:
        def respond(transport: FakeTransport, _wire: bytes) -> None:
            transport.feed(
                b"watchdog warning from an unrelated task\r\n",
                b"ASSERT in sensor worker\r\n",
                b'{"protocol":"w30_test_bridge","version":1,"type":"gui_ack",'
                b'"request":"gui_ping","seq":71,"status":"processed"}\r\n',
            )

        with tempfile.TemporaryDirectory() as root:
            session = self._session(root, FakeTransport(respond))
            result = session.send(":GUI_PING:71")
            self.assertEqual(result.status, "processed")
            self.assertEqual(
                session.background_errors_since(),
                ["watchdog warning from an unrelated task", "ASSERT in sensor worker"],
            )
            session.stop()
            errors = (Path(root) / "serial" / "errors.log").read_text(encoding="utf-8")
            self.assertEqual(errors.count("device_background_error"), 2)

    def test_context_stop_closes_transport_and_flushes_partial_line(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            transport = FakeTransport()
            session = self._session(root, transport)
            transport.feed(b"partial log without newline")
            self._wait_until(lambda: transport._chunks.empty())
            session.stop()
            self.assertTrue(transport.closed)
            self.assertIn("partial log without newline", session.lines_since(0))

    def test_supercom_pipe_transport_shared_normalization(self) -> None:
        t1 = SuperComPipeTransport("com7")
        self.assertEqual(t1.pipe_name, "SuperCom.AgentBridge.COM7")
        self.assertEqual(t1.pipe_path, r"\\.\pipe\SuperCom.AgentBridge.COM7")

        t2 = SuperComPipeTransport("COM10")
        self.assertEqual(t2.pipe_name, "SuperCom.AgentBridge.COM10")
        self.assertEqual(t2.pipe_path, r"\\.\pipe\SuperCom.AgentBridge.COM10")

        self.assertEqual(get_supercom_pipe_name("COM7"), "SuperCom.AgentBridge.COM7")
        self.assertEqual(get_supercom_pipe_path("COM7"), r"\\.\pipe\SuperCom.AgentBridge.COM7")


if __name__ == "__main__":
    unittest.main()
