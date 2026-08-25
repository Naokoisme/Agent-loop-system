from __future__ import annotations

import unittest

from agent_loop_system.tools.watch_579_ble import (
    Watch579BleUnavailable,
    Watch579Disconnected,
)
from agent_loop_system.tools.watch_579_preflight import (
    run_watch_579_preflight,
    unchecked_watch_579_preflight,
)


class FakeBroker:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = []

    def connect_internal(self, *, lease_token, address, timeout):
        self.calls.append((lease_token, address, timeout))
        if self.error:
            raise self.error
        return {"address": address, "name": "A000 Z1640", "rssi": -54}

    def connect(self, *, address, timeout):
        return self.connect_internal(lease_token=None, address=address, timeout=timeout)

    def check_dependencies(self):
        if isinstance(self.error, Watch579BleUnavailable):
            raise self.error
        return {"bleak": True}


class Watch579PreflightTests(unittest.TestCase):
    def test_unchecked_state_does_not_expose_w30_profile_checks(self) -> None:
        result = unchecked_watch_579_preflight()

        self.assertEqual(result.project, "579_Z1640")
        self.assertEqual(result.readiness_status, "unchecked")
        self.assertFalse(result.execution_ready)
        self.assertFalse(result.observation_ready)
        self.assertEqual(result.checks, ())

    def test_missing_address_blocks_execution_without_w30_dependencies(self) -> None:
        broker = FakeBroker()
        result = run_watch_579_preflight(
            environment={},
            broker=broker,
        )
        self.assertFalse(result.ready)
        self.assertFalse(result.execution_ready)
        self.assertFalse(result.observation_ready)
        self.assertEqual(result.primary_code, "BLE_DEVICE_NOT_FOUND")
        self.assertEqual(broker.calls, [])
        keys = {check.key for check in result.checks}
        self.assertEqual(keys, {"bleak", "address"})

    def test_ready_execution_keeps_observation_explicitly_unavailable(self) -> None:
        broker = FakeBroker()
        result = run_watch_579_preflight(
            environment={"WATCH_579_BLE_ADDRESS": "41:42:72:6A:93:2D"},
            broker=broker,
            lease_token="lease-token",
            connect_timeout=4,
        )
        self.assertTrue(result.ready)
        self.assertTrue(result.execution_ready)
        self.assertFalse(result.observation_ready)
        self.assertEqual(
            broker.calls,
            [("lease-token", "41:42:72:6A:93:2D", 4)],
        )
        checks = {check.key: check for check in result.checks}
        self.assertEqual(
            set(checks),
            {"bleak", "address", "connection", "gatt_profile", "notify", "observation"},
        )
        self.assertEqual(checks["observation"].code, "OBSERVATION_UNAVAILABLE")
        self.assertFalse(checks["observation"].blocking)

    def test_connection_failure_preserves_stable_reason_code(self) -> None:
        broker = FakeBroker(Watch579Disconnected("link dropped"))
        result = run_watch_579_preflight(
            environment={"WATCH_579_BLE_ADDRESS": "41:42:72:6A:93:2D"},
            broker=broker,
            lease_token="lease-token",
        )
        self.assertFalse(result.execution_ready)
        self.assertEqual(result.primary_code, "BLE_DISCONNECTED")

    def test_missing_bleak_blocks_before_address_or_connection_checks(self) -> None:
        broker = FakeBroker(Watch579BleUnavailable("Bleak is unavailable"))
        result = run_watch_579_preflight(
            environment={"WATCH_579_BLE_ADDRESS": "41:42:72:6A:93:2D"},
            broker=broker,
        )
        self.assertFalse(result.execution_ready)
        self.assertEqual(result.primary_code, "BLE_UNAVAILABLE")
        self.assertEqual([check.key for check in result.checks], ["bleak"])
        self.assertEqual(broker.calls, [])


if __name__ == "__main__":
    unittest.main()
