from __future__ import annotations

import asyncio
import unittest

from agent_loop_system.tools.watch_579_ble import (
    Watch579AckTimeout,
    Watch579BleBroker,
    Watch579Disconnected,
    Watch579GattProfileMismatch,
    Watch579TargetBusy,
)


class FakeDevice:
    def __init__(self, address: str, name: str) -> None:
        self.address = address
        self.name = name


class FakeAdvertisement:
    def __init__(self, name: str, rssi: int) -> None:
        self.local_name = name
        self.rssi = rssi


class FakeScanner:
    def __init__(self, devices: list[tuple[FakeDevice, FakeAdvertisement]]) -> None:
        self.devices = devices
        self.calls = 0

    async def discover(self, **_kwargs):
        self.calls += 1
        return {
            device.address: (device, advertisement)
            for device, advertisement in self.devices
        }


class FakeServices:
    def __init__(self) -> None:
        self.service = object()
        self.write = object()
        self.notify = object()

    def get_service(self, _uuid: str):
        return self.service

    def get_characteristic(self, uuid: str):
        return self.notify if "ff03" in uuid else self.write


class FakeClient:
    def __init__(self, device, *, auto_ack: bool = True, **kwargs) -> None:
        self.device = device
        self.auto_ack = auto_ack
        self.disconnected_callback = kwargs.get("disconnected_callback")
        self.is_connected = False
        self.services = FakeServices()
        self.events: list[str] = []
        self.writes: list[tuple[bytes, bool]] = []
        self.callback = None

    async def connect(self) -> None:
        self.events.append("connect")
        self.is_connected = True

    async def start_notify(self, _characteristic, callback) -> None:
        self.events.append("notify")
        self.callback = callback

    async def stop_notify(self, _characteristic) -> None:
        self.events.append("stop_notify")

    async def write_gatt_char(self, _characteristic, data, *, response: bool) -> None:
        packet = bytes(data)
        self.events.append("write")
        self.writes.append((packet, response))
        if self.auto_ack and packet[:2] == b"\xAB\x00" and self.callback:
            sequence = packet[6:8]
            self.callback(None, bytearray(b"\xAB\x10\x00\x00\x00\x00" + sequence))
            await asyncio.sleep(0)

    async def disconnect(self) -> None:
        self.events.append("disconnect")
        self.is_connected = False


class ClientFactory:
    def __init__(self, *, auto_ack: bool = True) -> None:
        self.auto_ack = auto_ack
        self.instances: list[FakeClient] = []

    def __call__(self, device, **kwargs):
        client = FakeClient(device, auto_ack=self.auto_ack, **kwargs)
        self.instances.append(client)
        return client


class Watch579BleBrokerTests(unittest.TestCase):
    address = "41:42:72:6A:93:2D"

    def make_broker(self, *, auto_ack: bool = True, ack_timeout: float = 0.2):
        own = FakeDevice(self.address, "A000 Z1640")
        other = FakeDevice("53:A0:00:00:00:EF", "A000 Z1640")
        scanner = FakeScanner([
            (other, FakeAdvertisement(other.name, -41)),
            (own, FakeAdvertisement(own.name, -54)),
        ])
        factory = ClientFactory(auto_ack=auto_ack)
        broker = Watch579BleBroker(
            scanner=scanner,
            client_factory=factory,
            ack_timeout=ack_timeout,
        )
        self.addCleanup(broker.shutdown)
        return broker, scanner, factory, own

    def test_exact_mac_native_device_notify_before_single_long_write(self) -> None:
        broker, _scanner, factory, own = self.make_broker()
        status = broker.connect(address=self.address, timeout=1)
        self.assertTrue(status["ready"])
        client = factory.instances[0]
        self.assertIs(client.device, own)

        data = "08 54 4F 50 35 53 54 45 50 00 1C " \
            "54 4F 50 35 53 54 45 50 3A 54 50 5F 43 4C 49 43 4B 3A " \
            "35 31 2C 31 35 36 2C 31 20 3B"
        result = broker.send_manual(cmd="04", key="05", data=data)
        self.assertTrue(result["transport_acked"])
        self.assertFalse(result["effect_verified"])
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(len(client.writes[0][0]), 52)
        self.assertTrue(client.writes[0][1])
        self.assertLess(client.events.index("notify"), client.events.index("write"))

    def test_connection_is_reused(self) -> None:
        broker, scanner, factory, _own = self.make_broker()
        broker.connect(address=self.address, timeout=1)
        broker.connect(address=self.address.lower(), timeout=1)
        self.assertEqual(len(factory.instances), 1)
        self.assertEqual(scanner.calls, 1)

    def test_stale_disconnect_callback_cannot_clobber_replacement_connection(self) -> None:
        broker, _scanner, factory, _own = self.make_broker()
        broker.connect(address=self.address, timeout=1)
        old_client = factory.instances[0]
        replacement_address = "53:A0:00:00:00:EF"
        broker.connect(address=replacement_address, timeout=1)
        self.assertIsNotNone(old_client.disconnected_callback)
        old_client.disconnected_callback(old_client)

        status = broker.status()
        self.assertTrue(status["ready"])
        self.assertEqual(status["address"], replacement_address)
        self.assertEqual(len(factory.instances), 2)

    def test_one_38_case_batch_keeps_one_gatt_connection(self) -> None:
        broker, scanner, factory, _own = self.make_broker()
        token = broker.acquire_lease(owner="calculator-38")
        broker.connect_internal(lease_token=token, address=self.address, timeout=1)
        for _ in range(38):
            result = broker.send_internal(
                lease_token=token,
                cmd="02",
                key="3B",
                data="",
            )
            self.assertTrue(result["transport_acked"])
        self.assertEqual(scanner.calls, 1)
        self.assertEqual(len(factory.instances), 1)
        self.assertEqual(len(factory.instances[0].writes), 38)

    def test_ack_timeout_has_no_retry(self) -> None:
        broker, _scanner, factory, _own = self.make_broker(auto_ack=False)
        broker.connect(address=self.address, timeout=1)
        with self.assertRaises(Watch579AckTimeout):
            broker.send_manual(cmd="02", key="3B", data="")
        self.assertEqual(len(factory.instances[0].writes), 1)

    def test_missing_gatt_characteristic_fails_before_notify_or_write(self) -> None:
        own = FakeDevice(self.address, "A000 Z1640")
        scanner = FakeScanner([(own, FakeAdvertisement(own.name, -54))])
        factory = ClientFactory()

        def missing_profile_factory(device, **kwargs):
            client = factory(device, **kwargs)
            client.services.notify = None
            return client

        broker = Watch579BleBroker(
            scanner=scanner,
            client_factory=missing_profile_factory,
            ack_timeout=0.2,
        )
        self.addCleanup(broker.shutdown)
        with self.assertRaises(Watch579GattProfileMismatch):
            broker.connect(address=self.address, timeout=1)
        client = factory.instances[0]
        self.assertNotIn("notify", client.events)
        self.assertEqual(client.writes, [])

    def test_automation_lease_blocks_page_and_restores_after_release(self) -> None:
        broker, _scanner, _factory, _own = self.make_broker()
        broker.connect(address=self.address, timeout=1)
        token = broker.acquire_lease(owner="job-579")
        with self.assertRaises(Watch579TargetBusy):
            broker.send_manual(cmd="02", key="3B", data="")
        result = broker.send_internal(
            lease_token=token, cmd="02", key="3B", data=""
        )
        self.assertTrue(result["transport_acked"])
        self.assertTrue(broker.release_lease(token))
        self.assertTrue(
            broker.send_manual(cmd="02", key="3B", data="")["transport_acked"]
        )

    def test_device_message_is_reassembled_and_host_acked(self) -> None:
        broker, _scanner, factory, _own = self.make_broker()
        broker.connect(address=self.address, timeout=1)
        client = factory.instances[0]
        assert client.callback is not None
        client.callback(None, bytearray.fromhex("AB0000060AED008C"))
        client.callback(None, bytearray.fromhex("020092000101"))
        # Synchronize with the broker loop through a status call.
        broker.status()
        for _ in range(20):
            if len(client.writes) >= 1:
                break
            asyncio.run(asyncio.sleep(0.01))
        self.assertIn(
            bytes.fromhex("AB1000000000008C"),
            [packet for packet, _response in client.writes],
        )
        kinds = [item["kind"] for item in broker.events(after=0)["items"]]
        self.assertIn("RX_DATA", kinds)
        self.assertIn("TX_HOST_ACK", kinds)

    def test_disconnected_send_fails_without_write(self) -> None:
        broker, _scanner, factory, _own = self.make_broker()
        with self.assertRaises(Watch579Disconnected):
            broker.send_manual(cmd="02", key="3B", data="")
        self.assertEqual(factory.instances, [])


if __name__ == "__main__":
    unittest.main()
