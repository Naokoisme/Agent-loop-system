from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from agent_loop_system.tools.watch_app_protocol import (
    BIND_COMMAND,
    WatchAppFrame,
    WatchAuthRequest,
    encode_frame,
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
    WatchBleTimeoutError,
    _build_parser,
    _error_payload,
    _run_cli,
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
