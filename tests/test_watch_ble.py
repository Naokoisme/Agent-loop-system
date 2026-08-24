from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from agent_loop_system.protocol import (
    pb_config_pb2,
    pb_env_pb2,
    pb_notice_pb2,
    pb_setting_pb2,
)
from agent_loop_system.tools.watch_app_protocol import (
    BIND_COMMAND,
    WatchAppFrame,
    WatchAppMessageDecoder,
    WatchAuthRequest,
    encode_frame,
    encode_protobuf,
    fragment_payload,
)
from agent_loop_system.tools.watch_ble import (
    BLOCKED_BY_CLASSIC_BT_GATE,
    WATCH_NOTIFY_UUID,
    WATCH_SERVICE_UUID,
    WATCH_WRITE_UUID,
    WatchBleAmbiguousDeviceError,
    WatchBleAuthError,
    WatchBleClassicGateTimeoutError,
    WatchBleClient,
    WatchBleDevice,
    WatchBlePairingError,
    WatchBlePairingTimeoutError,
    WatchBleProtocolError,
    WatchBleTimeoutError,
    _build_parser,
    _error_payload,
    _run_cli,
    discover_ble_devices,
    scan_watches,
    select_watch,
)


class FakeScanner:
    discovered: dict[str, tuple[object, object]] = {}
    calls: list[dict[str, object]] = []

    @classmethod
    async def discover(cls, **kwargs):
        cls.calls.append(kwargs)
        return cls.discovered


class FakeServices:
    def __init__(self, *, chunk_size: int = 20) -> None:
        self.service = SimpleNamespace(uuid=WATCH_SERVICE_UUID)
        self.write = SimpleNamespace(
            uuid=WATCH_WRITE_UUID,
            max_write_without_response_size=chunk_size,
        )
        self.notify = SimpleNamespace(uuid=WATCH_NOTIFY_UUID)

    def get_service(self, uuid: str):
        return self.service if uuid.lower() == WATCH_SERVICE_UUID else None

    def get_characteristic(self, uuid: str):
        if uuid.lower() == WATCH_WRITE_UUID:
            return self.write
        if uuid.lower() == WATCH_NOTIFY_UUID:
            return self.notify
        return None


class FakeClient:
    def __init__(
        self,
        target,
        *,
        disconnected_callback,
        timeout,
        pair,
        chunk_size: int = 20,
    ) -> None:
        self.target = target
        self.disconnected_callback = disconnected_callback
        self.timeout = timeout
        self.constructor_pair = pair
        self.is_connected = False
        self.services = FakeServices(chunk_size=chunk_size)
        self.events: list[str] = []
        self.writes: list[tuple[bytes, bool]] = []
        self.notify_callback = None
        self.disconnect_calls = 0
        self.response_bytes: bytes | None = None
        self.response_slices: tuple[int, ...] = ()
        self.stop_notify_calls = 0
        self.respond_on_write = False
        self.respond_on_pair = True
        self.pair_calls = 0
        self.pair_error: BaseException | None = None
        self.pair_started = asyncio.Event()
        self.pair_release: asyncio.Event | None = None

    async def connect(self) -> None:
        self.events.append("connect")
        self.is_connected = True

    async def start_notify(self, characteristic, callback) -> None:
        self.events.append("notify")
        self.notify_callback = callback

    async def write_gatt_char(self, characteristic, data, *, response) -> None:
        self.events.append("write")
        self.writes.append((bytes(data), response))
        if self.respond_on_write:
            self._emit_response(characteristic)

    def _emit_response(self, characteristic) -> None:
        if self.response_bytes is None or self.notify_callback is None:
            return
        wire = self.response_bytes
        self.response_bytes = None
        offset = 0
        for size in self.response_slices:
            self.notify_callback(
                characteristic,
                bytearray(wire[offset : offset + size]),
            )
            offset += size
        if offset < len(wire):
            self.notify_callback(characteristic, bytearray(wire[offset:]))

    async def pair(self) -> None:
        self.events.append("pair_start")
        self.pair_calls += 1
        self.pair_started.set()
        if self.pair_error is not None:
            raise self.pair_error
        if self.pair_release is not None:
            await self.pair_release.wait()
        if self.respond_on_pair:
            self._emit_response(self.services.write)
        self.events.append("pair_end")

    async def disconnect(self) -> None:
        self.events.append("disconnect")
        self.disconnect_calls += 1
        self.is_connected = False

    async def stop_notify(self, characteristic) -> None:
        self.events.append("stop_notify")
        self.stop_notify_calls += 1


class FakeClientFactory:
    def __init__(self, *, chunk_size: int = 20) -> None:
        self.chunk_size = chunk_size
        self.instances: list[FakeClient] = []

    def __call__(self, *args, **kwargs) -> FakeClient:
        client = FakeClient(*args, **kwargs, chunk_size=self.chunk_size)
        self.instances.append(client)
        return client


def auth_response_payload(
    *, result: int = 0, ever_bound: int = 1, bind_time: int = 123
) -> bytes:
    # _AuthResponse protobuf: int32 result=1, int32 everBind=2, int64 bindTime=3.
    values = bytearray()
    for field, value in ((1, result), (2, ever_bound), (3, bind_time)):
        if value == 0:
            continue
        values.append(field << 3)
        while value >= 0x80:
            values.append((value & 0x7F) | 0x80)
            value >>= 7
        values.append(value)
    return bytes(values)


class WatchBleScanTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        FakeScanner.calls = []
        FakeScanner.discovered = {}

    async def test_discovery_returns_devices_without_watch_compatibility_filter(self) -> None:
        watch = SimpleNamespace(address="AA:01", name="fallback")
        other = SimpleNamespace(address="AA:02", name=None)
        FakeScanner.discovered = {
            "watch": (
                watch,
                SimpleNamespace(
                    local_name="oraimo Watch Pro X_1215",
                    rssi=-52,
                    service_uuids=[],
                ),
            ),
            "other": (
                other,
                SimpleNamespace(local_name=None, rssi=-70, service_uuids=[]),
            ),
        }

        devices = await discover_ble_devices(timeout=1.5, scanner=FakeScanner)

        self.assertEqual([item.address for item in devices], ["AA:01", "AA:02"])
        self.assertEqual(devices[0].name, "oraimo Watch Pro X_1215")
        self.assertIsNone(devices[1].name)
        self.assertEqual(FakeScanner.calls, [{"timeout": 1.5, "return_adv": True}])

    async def test_scan_accepts_name_when_advertised_services_are_empty(self) -> None:
        named = SimpleNamespace(address="AA:01", name="fallback")
        by_service = SimpleNamespace(address="AA:02", name="other")
        ignored = SimpleNamespace(address="AA:03", name="other")
        FakeScanner.discovered = {
            "one": (
                named,
                SimpleNamespace(
                    local_name="oraimo Watch Tank N_0A02",
                    rssi=-66,
                    service_uuids=[],
                ),
            ),
            "two": (
                by_service,
                SimpleNamespace(
                    local_name=None,
                    rssi=-70,
                    service_uuids=[WATCH_SERVICE_UUID.upper()],
                ),
            ),
            "three": (
                ignored,
                SimpleNamespace(local_name=None, rssi=-40, service_uuids=[]),
            ),
        }

        devices = await scan_watches(timeout=1.5, scanner=FakeScanner)

        self.assertEqual([item.address for item in devices], ["AA:01", "AA:02"])
        self.assertEqual(devices[0].name, "oraimo Watch Tank N_0A02")
        self.assertEqual(devices[0].rssi, -66)
        self.assertEqual(FakeScanner.calls, [{"timeout": 1.5, "return_adv": True}])

    async def test_scan_accepts_explicit_address_without_name_or_service(self) -> None:
        target = SimpleNamespace(address="54:C8:D4:D9:29:06", name=None)
        ignored = SimpleNamespace(address="AA:03", name=None)
        FakeScanner.discovered = {
            "target": (
                target,
                SimpleNamespace(local_name=None, rssi=-54, service_uuids=[]),
            ),
            "ignored": (
                ignored,
                SimpleNamespace(local_name=None, rssi=-40, service_uuids=[]),
            ),
        }

        devices = await scan_watches(
            timeout=1.5,
            address="54:c8:d4:d9:29:06",
            scanner=FakeScanner,
        )

        self.assertEqual(devices, [WatchBleDevice("54:C8:D4:D9:29:06", None, -54)])

    async def test_selection_requires_exactly_one_match(self) -> None:
        devices = [
            WatchBleDevice("AA:01", "same"),
            WatchBleDevice("AA:02", "same"),
        ]
        with self.assertRaises(WatchBleAmbiguousDeviceError):
            select_watch(devices, name="same")
        self.assertEqual(select_watch(devices, address="aa:02").address, "AA:02")


class WatchBleClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_bind_writes_before_pair_with_response_pending(self) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        fake.pair_release = asyncio.Event()
        fake.response_bytes = encode_frame(
            WatchAppFrame(BIND_COMMAND, 0, auth_response_payload())
        )

        bind_task = asyncio.create_task(
            wrapper.bind(WatchAuthRequest(user_id="u", auth_code="123456"))
        )
        await asyncio.wait_for(fake.pair_started.wait(), 1.0)

        self.assertFalse(fake.constructor_pair)
        self.assertLess(fake.events.index("notify"), fake.events.index("write"))
        self.assertLess(fake.events.index("write"), fake.events.index("pair_start"))
        self.assertIsNotNone(wrapper._pending)
        self.assertFalse(wrapper._pending.done())

        fake.pair_release.set()
        result = await bind_task

        self.assertEqual(result.result, 0)
        self.assertEqual(result.transport, "ble_gatt")
        self.assertEqual(result.classic_bluetooth_status, "unknown")
        self.assertLess(fake.events.index("pair_start"), fake.events.index("pair_end"))

    async def test_write_is_chunked_and_notification_is_reassembled(self) -> None:
        factory = FakeClientFactory(chunk_size=7)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect(pair=False)
        fake = factory.instances[0]
        fake.response_bytes = encode_frame(
            WatchAppFrame(BIND_COMMAND, 0, auth_response_payload(bind_time=987))
        )
        fake.response_slices = (3, 2, 1)

        result = await wrapper.bind(
            WatchAuthRequest(
                user_id="a-longer-user-id",
                auth_code="123456",
                version="Windows-long",
            )
        )

        self.assertGreater(len(fake.writes), 1)
        self.assertTrue(all(len(data) <= 7 for data, _ in fake.writes))
        self.assertTrue(all(response is False for _, response in fake.writes))
        self.assertEqual(result.bind_time, 987)

    async def test_exchange_sends_semantic_chunks_and_reassembles_response(self) -> None:
        factory = FakeClientFactory(chunk_size=37)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        command = 0x0201
        response_payload = b"r" * 2200
        fake.response_bytes = b"".join(
            encode_frame(frame)
            for frame in fragment_payload(
                command,
                0,
                response_payload,
                max_data_length=700,
            )
        )
        fake.response_slices = (5, 11, 3, 97)
        fake.respond_on_write = True

        request_payload = b"q" * 2500
        response = await wrapper.exchange_multipart(
            command,
            (
                request_payload[:900],
                request_payload[900:1800],
                request_payload[1800:],
            ),
        )

        self.assertEqual(response.payload, response_payload)
        self.assertEqual(response.fragment_count, 4)
        request_decoder = WatchAppMessageDecoder()
        request_messages = []
        for chunk, without_response in fake.writes:
            self.assertFalse(without_response)
            request_messages.extend(request_decoder.feed(chunk))
        self.assertEqual(len(request_messages), 1)
        self.assertEqual(request_messages[0].payload, request_payload)
        self.assertEqual(request_messages[0].fragment_count, 3)

        with self.assertRaisesRegex(WatchBleProtocolError, "single-frame limit"):
            await wrapper.exchange(command, request_payload, timeout=0.01)

    async def test_send_and_exchange_protobuf_cover_nonblocking_and_blocking_commands(self) -> None:
        factory = FakeClientFactory(chunk_size=64)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]

        sequence = await wrapper.send_protobuf(
            0x0219,
            pb_config_pb2._FunctionConfig(flags=b"\x03"),
        )
        self.assertEqual(sequence, 0)

        fake.response_bytes = encode_frame(
            WatchAppFrame(
                0x0204,
                1,
                encode_protobuf(pb_config_pb2._CommonResponse(result=0)),
            )
        )
        fake.respond_on_write = True
        response = await wrapper.exchange_protobuf(
            0x0204,
            pb_config_pb2._FunctionConfig(flags=b"\x01"),
            pb_config_pb2._CommonResponse,
        )
        self.assertEqual(response.result, 0)

        write_count = len(fake.writes)
        with self.assertRaisesRegex(
            WatchBleProtocolError, "_AppNotice.content.*801 bytes"
        ):
            await wrapper.send_protobuf(
                0x0401,
                pb_notice_pb2._AppNotice(content="x" * 801),
            )
        self.assertEqual(len(fake.writes), write_count)

    async def test_exchange_protobuf_chunks_keeps_each_fragment_decodable(self) -> None:
        factory = FakeClientFactory(chunk_size=19)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        first = pb_setting_pb2._AlarmList()
        first.items.add(id=1, hour=7)
        second = pb_setting_pb2._AlarmList()
        second.items.add(id=2, hour=8)
        fake.response_bytes = encode_frame(
            WatchAppFrame(
                0x0239,
                0,
                encode_protobuf(pb_config_pb2._CommonResponse(result=0)),
            )
        )
        fake.respond_on_write = True

        response = await wrapper.exchange_protobuf_chunks(
            0x0239,
            (first, second),
            pb_config_pb2._CommonResponse,
        )
        self.assertEqual(response.result, 0)

        decoder = WatchAppMessageDecoder()
        outbound = []
        for chunk, _ in fake.writes:
            outbound.extend(decoder.feed(chunk))
        self.assertEqual(len(outbound), 1)
        merged = pb_setting_pb2._AlarmList()
        merged.ParseFromString(outbound[0].payload)
        self.assertEqual([item.id for item in merged.items], [1, 2])

    async def test_concurrent_exchanges_are_routed_by_command_and_sequence(self) -> None:
        factory = FakeClientFactory(chunk_size=13)
        wrapper = WatchBleClient("AA:01", client_factory=factory, timeout=1.0)
        await wrapper.connect()
        fake = factory.instances[0]

        first_task = asyncio.create_task(
            wrapper.exchange_multipart(0x0203, (b"a" * 650, b"a" * 650))
        )
        second_task = asyncio.create_task(
            wrapper.exchange_multipart(0x0309, (b"b" * 700, b"b" * 700))
        )
        for _ in range(100):
            if wrapper.pending_exchange_count == 2 and fake.writes:
                break
            await asyncio.sleep(0)
        self.assertEqual(wrapper.pending_exchange_count, 2)
        pending_keys = tuple(wrapper._pending_requests)
        self.assertEqual({command for command, _ in pending_keys}, {0x0203, 0x0309})

        for command, sequence in reversed(pending_keys):
            fake.response_bytes = encode_frame(
                WatchAppFrame(command, sequence, f"reply-{command:04x}".encode())
            )
            fake._emit_response(fake.services.write)

        first, second = await asyncio.gather(first_task, second_task)
        self.assertEqual(first.payload, b"reply-0203")
        self.assertEqual(second.payload, b"reply-0309")
        self.assertEqual(wrapper.pending_exchange_count, 0)

        decoder = WatchAppMessageDecoder()
        outbound = []
        for chunk, _ in fake.writes:
            outbound.extend(decoder.feed(chunk))
        self.assertEqual(
            {(message.command, len(message.payload)) for message in outbound},
            {(0x0203, 1300), (0x0309, 1400)},
        )

    async def test_unsolicited_device_message_can_be_received_and_decoded(self) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        payload = encode_protobuf(pb_env_pb2._BatteryInfo(level=87, charging=1))

        fake.notify_callback(
            fake.services.notify,
            bytearray(encode_frame(WatchAppFrame(0x030A, 77, payload))),
        )
        envelope, battery = await wrapper.receive_protobuf(
            pb_env_pb2._BatteryInfo,
            timeout=0.1,
        )

        self.assertEqual(envelope.command, 0x030A)
        self.assertEqual(envelope.sequence, 77)
        self.assertEqual(battery.level, 87)
        self.assertEqual(battery.charging, 1)

    async def test_ignores_wrong_command_and_sequence_then_times_out(self) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory, timeout=0.05)
        await wrapper.connect()
        factory.instances[0].response_bytes = encode_frame(
            WatchAppFrame(BIND_COMMAND, 99, auth_response_payload())
        )

        with self.assertRaises(WatchBleTimeoutError):
            await wrapper.bind(WatchAuthRequest(user_id="u", auth_code="1"))

    async def test_auth_error_exposes_watch_result(self) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        factory.instances[0].response_bytes = encode_frame(
            WatchAppFrame(BIND_COMMAND, 0, auth_response_payload(result=2))
        )

        with self.assertRaises(WatchBleAuthError) as caught:
            await wrapper.bind(WatchAuthRequest(user_id="u", auth_code="bad"))
        self.assertEqual(caught.exception.response.result, 2)

    async def test_bind_result_four_does_not_infer_classic_status(self) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        factory.instances[0].response_bytes = encode_frame(
            WatchAppFrame(BIND_COMMAND, 0, auth_response_payload(result=4))
        )

        result = await wrapper.bind(WatchAuthRequest(user_id="u", auth_code="1"))

        self.assertEqual(result.result, 4)
        self.assertEqual(result.status, "success_result_4")
        self.assertEqual(result.transport, "ble_gatt")
        self.assertEqual(result.classic_bluetooth_status, "unknown")

    async def test_result_zero_does_not_claim_classic_bluetooth(self) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        factory.instances[0].response_bytes = encode_frame(
            WatchAppFrame(BIND_COMMAND, 0, auth_response_payload())
        )

        result = await wrapper.bind(WatchAuthRequest(user_id="u", auth_code="1"))

        self.assertEqual(result.status, "success")
        self.assertEqual(result.classic_bluetooth_status, "unknown")

    async def test_pair_failure_is_explicit_and_clears_pending(self) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        fake.pair_error = RuntimeError("rejected")

        with self.assertRaisesRegex(WatchBlePairingError, "rejected"):
            await wrapper.bind(WatchAuthRequest(user_id="u", auth_code="1"))

        self.assertLess(fake.events.index("write"), fake.events.index("pair_start"))
        self.assertIsNone(wrapper._pending)

    async def test_pair_timeout_is_distinct_from_response_timeout(self) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory, timeout=0.01)
        await wrapper.connect()
        fake = factory.instances[0]
        fake.pair_release = asyncio.Event()

        with self.assertRaises(WatchBlePairingTimeoutError) as caught:
            await wrapper.bind(WatchAuthRequest(user_id="u", auth_code="1"))

        self.assertEqual(caught.exception.code, "PAIR_TIMEOUT")
        self.assertIsNone(wrapper._pending)

    async def test_login_timeout_identifies_possible_classic_gate(self) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory, timeout=0.05)
        await wrapper.connect(pair=True)
        factory.instances[0].respond_on_pair = False

        with self.assertRaises(WatchBleClassicGateTimeoutError) as caught:
            await wrapper.login(WatchAuthRequest(user_id="u", auth_code=""))

        self.assertEqual(caught.exception.code, BLOCKED_BY_CLASSIC_BT_GATE)
        payload = _error_payload(caught.exception)
        self.assertEqual(
            payload["error"]["code"],
            BLOCKED_BY_CLASSIC_BT_GATE,
        )
        self.assertIn("may still be closed", payload["error"]["message"])

    async def test_prepaired_bind_timeout_identifies_possible_classic_gate(
        self,
    ) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory, timeout=0.05)
        await wrapper.connect(pair=True)

        with self.assertRaises(WatchBleClassicGateTimeoutError):
            await wrapper.bind(WatchAuthRequest(user_id="u", auth_code="1"))
        self.assertEqual(factory.instances[0].pair_calls, 0)

    async def test_first_bind_response_timeout_is_not_mislabeled_as_gate(
        self,
    ) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory, timeout=0.05)
        await wrapper.connect()

        with self.assertRaises(WatchBleTimeoutError) as caught:
            await wrapper.bind(WatchAuthRequest(user_id="u", auth_code="1"))

        self.assertNotIsInstance(
            caught.exception,
            WatchBleClassicGateTimeoutError,
        )
        self.assertEqual(caught.exception.code, "RESPONSE_TIMEOUT")

    async def test_close_is_idempotent(self) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()

        await wrapper.close()
        await wrapper.close()

        self.assertEqual(factory.instances[0].disconnect_calls, 1)
        self.assertEqual(factory.instances[0].stop_notify_calls, 1)

    async def test_closed_client_can_reconnect(self) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        await wrapper.close()

        await wrapper.connect()
        await wrapper.close()

        self.assertEqual(len(factory.instances), 2)

    async def test_connect_without_pair_is_passed_to_backend(self) -> None:
        factory = FakeClientFactory()
        wrapper = WatchBleClient("AA:01", client_factory=factory)

        await wrapper.connect(pair=False)

        self.assertFalse(factory.instances[0].constructor_pair)

    async def test_status_cli_never_pairs(self) -> None:
        FakeScanner.calls = []
        device = SimpleNamespace(address="AA:01", name="oraimo Watch Tank N_test")
        FakeScanner.discovered = {
            "one": (
                device,
                SimpleNamespace(
                    local_name=device.name,
                    rssi=-50,
                    service_uuids=[],
                ),
            )
        }
        factory = FakeClientFactory()
        args = _build_parser().parse_args(
            [
                "status",
                "--address",
                "AA:01",
                "--scan-timeout",
                "0.2",
                "--timeout",
                "1",
            ]
        )

        result = await _run_cli(
            args,
            scanner=FakeScanner,
            client_factory=factory,
        )

        self.assertTrue(result["connected"])
        self.assertFalse(result["pairing_attempted"])
        self.assertEqual(FakeScanner.calls, [{"timeout": 0.2, "return_adv": True}])
        self.assertFalse(factory.instances[0].constructor_pair)
        self.assertEqual(factory.instances[0].pair_calls, 0)


if __name__ == "__main__":
    unittest.main()
