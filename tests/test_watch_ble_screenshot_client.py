from __future__ import annotations

import asyncio
import struct
import tempfile
import unittest
from pathlib import Path

from agent_loop_system.tools.watch_app_protocol import (
    WatchAppFrame,
    WatchAppFrameDecoder,
    decode_frame,
    encode_frame,
)
from agent_loop_system.tools.watch_ble import (
    WatchBleClient,
    WatchBleConnectionError,
    WatchBleDisconnectedError,
    WatchBleProtocolError,
    _build_parser,
)
from agent_loop_system.tools.watch_ble_screenshot import (
    BLE_SCREENSHOT_ACK,
    BLE_SCREENSHOT_COMMAND,
    BLE_SCREENSHOT_COMPLETE_ACK,
    BLE_SCREENSHOT_PROTOCOL_VERSION,
    encode_screenshot_request,
)
from tests.test_watch_ble import FakeClientFactory
from tests.test_watch_ble_screenshot import make_watch_bmp, screenshot_messages


class WatchBleScreenshotClientTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _written_frames(fake) -> list[WatchAppFrame]:
        return [decode_frame(data) for data, _ in fake.writes]

    @staticmethod
    def _decode_ack(frame: WatchAppFrame) -> tuple[int, int, int, int]:
        return struct.unpack("<BBIH", frame.payload)

    def test_960_byte_data_frame_reassembles_from_c400_safe_notifications(
        self,
    ) -> None:
        capture_sequence = 0x11223344
        data_payload = screenshot_messages(
            capture_sequence,
            make_watch_bmp(),
        )[1]
        frame = WatchAppFrame(
            BLE_SCREENSHOT_COMMAND,
            321,
            data_payload,
        )
        wire = encode_frame(frame)
        notifications = [
            wire[offset : offset + 244]
            for offset in range(0, len(wire), 244)
        ]

        self.assertEqual(len(data_payload), 14 + 960)
        self.assertEqual(len(wire), 984)
        self.assertEqual(
            [len(part) for part in notifications],
            [244, 244, 244, 244, 8],
        )

        decoder = WatchAppFrameDecoder()
        for part in notifications[:-1]:
            self.assertEqual(decoder.feed(part), [])
        self.assertEqual(decoder.feed(notifications[-1]), [frame])

    async def test_data_ack_waits_for_fifth_c400_safe_notification(self) -> None:
        capture_sequence = 0x22334455
        messages = screenshot_messages(capture_sequence, make_watch_bmp())
        factory = FakeClientFactory(chunk_size=20)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        original_write = fake.write_gatt_char
        request_outer_sequence: int | None = None
        start_ack_seen = asyncio.Event()
        data_ack_seen = asyncio.Event()

        async def observe_ack(characteristic, data, *, response) -> None:
            nonlocal request_outer_sequence
            frame = decode_frame(bytes(data))
            await original_write(characteristic, data, response=response)
            if request_outer_sequence is None:
                self.assertEqual(
                    frame.payload,
                    encode_screenshot_request(capture_sequence),
                )
                request_outer_sequence = frame.sequence
                self.assertIsNotNone(fake.notify_callback)
                fake.notify_callback(
                    fake.services.notify,
                    bytearray(
                        encode_frame(
                            WatchAppFrame(
                                BLE_SCREENSHOT_COMMAND,
                                1000,
                                messages[0],
                            )
                        )
                    ),
                )
                return

            acknowledgement = self._decode_ack(frame)
            self.assertEqual(frame.sequence, request_outer_sequence)
            if acknowledgement[-1] == 0:
                start_ack_seen.set()
            elif acknowledgement[-1] == 1:
                data_ack_seen.set()

        fake.write_gatt_char = observe_ack
        capture: asyncio.Task | None = None
        try:
            with tempfile.TemporaryDirectory() as temporary:
                capture = asyncio.create_task(
                    wrapper.capture_screenshot(
                        Path(temporary) / "partial.bmp",
                        sequence=capture_sequence,
                        timeout=5,
                    )
                )
                await asyncio.wait_for(start_ack_seen.wait(), 1)

                data_wire = encode_frame(
                    WatchAppFrame(
                        BLE_SCREENSHOT_COMMAND,
                        1001,
                        messages[1],
                    )
                )
                notifications = [
                    data_wire[offset : offset + 244]
                    for offset in range(0, len(data_wire), 244)
                ]
                self.assertEqual(
                    [len(part) for part in notifications],
                    [244, 244, 244, 244, 8],
                )

                for part in notifications[:-1]:
                    self.assertIsNotNone(fake.notify_callback)
                    fake.notify_callback(fake.services.notify, bytearray(part))
                    await asyncio.sleep(0)
                    self.assertFalse(data_ack_seen.is_set())

                fake.notify_callback(
                    fake.services.notify,
                    bytearray(notifications[-1]),
                )
                await asyncio.wait_for(data_ack_seen.wait(), 1)

                acknowledgements = [
                    self._decode_ack(frame)
                    for frame in self._written_frames(fake)[1:]
                ]
                self.assertEqual(
                    [acknowledgement[-1] for acknowledgement in acknowledgements],
                    [0, 1],
                )

                await wrapper.close()
                with self.assertRaisesRegex(
                    WatchBleDisconnectedError,
                    "BLE client closed",
                ):
                    await capture
        finally:
            await wrapper.close()
            if capture is not None and not capture.done():
                capture.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await capture

    async def test_client_uses_ff02_request_and_ff03_stream_to_save_verified_bmp(
        self,
    ) -> None:
        capture_sequence = 0x12345678
        bmp = make_watch_bmp()
        factory = FakeClientFactory(chunk_size=20)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        messages = screenshot_messages(capture_sequence, bmp)
        fake.response_bytes = b"".join(
            encode_frame(
                WatchAppFrame(
                    command=BLE_SCREENSHOT_COMMAND,
                    sequence=index & 0xFFFF,
                    payload=payload,
                )
            )
            for index, payload in enumerate(messages, start=100)
        )
        fake.response_slices = (7, 13, 233, 1024, 4096)
        fake.respond_on_write = True

        try:
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "ble" / "watch.bmp"
                result = await wrapper.capture_screenshot(
                    output,
                    sequence=capture_sequence,
                    timeout=5,
                )

                self.assertEqual(output.read_bytes(), bmp)
                self.assertEqual(result.sequence, capture_sequence)
                self.assertEqual(result.file_size, 618_518)
                self.assertEqual(result.chunks, 645)
                self.assertEqual(result.path, str(output.resolve()))

            written = self._written_frames(fake)
            request, acknowledgements = written[0], written[1:]
            self.assertEqual(request.command, BLE_SCREENSHOT_COMMAND)
            self.assertEqual(
                request.payload,
                encode_screenshot_request(capture_sequence),
            )
            decoded_acks = [self._decode_ack(frame) for frame in acknowledgements]
            self.assertEqual(
                decoded_acks,
                [
                    (
                        BLE_SCREENSHOT_PROTOCOL_VERSION,
                        BLE_SCREENSHOT_ACK,
                        capture_sequence,
                        next_chunk,
                    )
                    for next_chunk in (
                        [0]
                        + list(range(1, result.chunks + 1))
                        + [BLE_SCREENSHOT_COMPLETE_ACK]
                    )
                ],
            )
            self.assertTrue(
                all(frame.sequence == request.sequence for frame in acknowledgements)
            )
            self.assertTrue(all(response is False for _, response in fake.writes))
            self.assertIsNone(wrapper._screenshot_ack_queue)
            self.assertIsNone(wrapper._screenshot_ack_task)
        finally:
            await wrapper.close()

    async def test_stop_and_wait_advances_only_after_each_ack(self) -> None:
        capture_sequence = 0x23456789
        bmp = make_watch_bmp()
        messages = screenshot_messages(capture_sequence, bmp)
        expected_next_chunks = (
            [0]
            + list(range(1, len(messages) - 1))
            + [BLE_SCREENSHOT_COMPLETE_ACK]
        )
        factory = FakeClientFactory(chunk_size=20)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        original_write = fake.write_gatt_char
        events: list[tuple[str, int]] = []
        acknowledgements: list[tuple[int, int, int, int]] = []
        request_outer_sequence: int | None = None
        complete_ack_seen = asyncio.Event()

        def emit_message(index: int) -> None:
            self.assertIsNotNone(fake.notify_callback)
            events.append(("notify", index))
            fake.notify_callback(
                fake.services.notify,
                bytearray(
                    encode_frame(
                        WatchAppFrame(
                            BLE_SCREENSHOT_COMMAND,
                            (1000 + index) & 0xFFFF,
                            messages[index],
                        )
                    )
                ),
            )

        async def stop_and_wait_write(characteristic, data, *, response) -> None:
            nonlocal request_outer_sequence
            frame = decode_frame(bytes(data))
            if request_outer_sequence is None:
                self.assertEqual(
                    frame.payload,
                    encode_screenshot_request(capture_sequence),
                )
                request_outer_sequence = frame.sequence
                events.append(("request", request_outer_sequence))
                await original_write(characteristic, data, response=response)
                emit_message(0)
                return

            acknowledgement = self._decode_ack(frame)
            acknowledgement_index = len(acknowledgements)
            self.assertLess(acknowledgement_index, len(messages))
            self.assertEqual(
                acknowledgement,
                (
                    BLE_SCREENSHOT_PROTOCOL_VERSION,
                    BLE_SCREENSHOT_ACK,
                    capture_sequence,
                    expected_next_chunks[acknowledgement_index],
                ),
            )
            self.assertEqual(frame.sequence, request_outer_sequence)
            await original_write(characteristic, data, response=response)
            acknowledgements.append(acknowledgement)
            events.append(("ack", acknowledgement[-1]))
            if len(acknowledgements) < len(messages):
                emit_message(len(acknowledgements))
            else:
                complete_ack_seen.set()

        fake.write_gatt_char = stop_and_wait_write
        try:
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "stop-and-wait.bmp"
                result = await wrapper.capture_screenshot(
                    output,
                    sequence=capture_sequence,
                    timeout=5,
                )

                self.assertEqual(output.read_bytes(), bmp)

            self.assertTrue(complete_ack_seen.is_set())
            self.assertEqual(result.sequence, capture_sequence)
            self.assertEqual(result.chunks, 645)
            expected_events = [("request", request_outer_sequence)]
            for index, next_chunk in enumerate(expected_next_chunks):
                expected_events.extend(
                    [("notify", index), ("ack", next_chunk)]
                )
            self.assertEqual(events, expected_events)
            self.assertEqual(len(fake.writes), 1 + len(messages))
            self.assertIsNone(wrapper._screenshot_ack_queue)
            self.assertIsNone(wrapper._screenshot_ack_task)
            self.assertFalse(
                any(
                    task.get_name().startswith("watch-ble-screenshot-ack-")
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                )
            )
        finally:
            await wrapper.close()

    async def test_capture_waits_until_complete_ack_write_finishes(self) -> None:
        capture_sequence = 0x10203040
        bmp = make_watch_bmp()
        factory = FakeClientFactory(chunk_size=20)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        fake.response_bytes = b"".join(
            encode_frame(
                WatchAppFrame(BLE_SCREENSHOT_COMMAND, index, payload)
            )
            for index, payload in enumerate(
                screenshot_messages(capture_sequence, bmp),
                start=200,
            )
        )
        fake.respond_on_write = True
        original_write = fake.write_gatt_char
        complete_ack_started = asyncio.Event()
        release_complete_ack = asyncio.Event()

        async def blocked_complete_ack(characteristic, data, *, response) -> None:
            frame = decode_frame(bytes(data))
            if len(frame.payload) == 8:
                _, message_type, _, next_chunk = self._decode_ack(frame)
                if (
                    message_type == BLE_SCREENSHOT_ACK
                    and next_chunk == BLE_SCREENSHOT_COMPLETE_ACK
                ):
                    complete_ack_started.set()
                    await release_complete_ack.wait()
            await original_write(characteristic, data, response=response)

        fake.write_gatt_char = blocked_complete_ack
        try:
            with tempfile.TemporaryDirectory() as temporary:
                capture = asyncio.create_task(
                    wrapper.capture_screenshot(
                        Path(temporary) / "watch.bmp",
                        sequence=capture_sequence,
                        timeout=5,
                    )
                )
                await asyncio.wait_for(complete_ack_started.wait(), 2)
                self.assertFalse(capture.done())

                release_complete_ack.set()
                result = await asyncio.wait_for(capture, 2)

            self.assertEqual(result.sequence, capture_sequence)
            final_frame = self._written_frames(fake)[-1]
            self.assertEqual(
                self._decode_ack(final_frame)[-1],
                BLE_SCREENSHOT_COMPLETE_ACK,
            )
        finally:
            release_complete_ack.set()
            await wrapper.close()

    async def test_duplicate_end_is_acknowledged_again_before_cleanup(self) -> None:
        capture_sequence = 0x44556677
        bmp = make_watch_bmp()
        factory = FakeClientFactory(chunk_size=20)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        messages = screenshot_messages(capture_sequence, bmp)
        messages.append(messages[-1])
        fake.response_bytes = b"".join(
            encode_frame(WatchAppFrame(BLE_SCREENSHOT_COMMAND, index, payload))
            for index, payload in enumerate(messages, start=300)
        )
        fake.respond_on_write = True

        try:
            with tempfile.TemporaryDirectory() as temporary:
                await wrapper.capture_screenshot(
                    Path(temporary) / "watch.bmp",
                    sequence=capture_sequence,
                    timeout=5,
                )

            final_acks = [
                frame
                for frame in self._written_frames(fake)[1:]
                if self._decode_ack(frame)[-1] == BLE_SCREENSHOT_COMPLETE_ACK
            ]
            self.assertEqual(len(final_acks), 2)
        finally:
            await wrapper.close()

    async def test_ack_writer_failure_fails_capture_and_is_cleaned_up(self) -> None:
        capture_sequence = 0x55667788
        factory = FakeClientFactory(chunk_size=20)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        fake.response_bytes = encode_frame(
            WatchAppFrame(
                BLE_SCREENSHOT_COMMAND,
                400,
                screenshot_messages(capture_sequence, make_watch_bmp())[0],
            )
        )
        fake.respond_on_write = True
        original_write = fake.write_gatt_char

        async def fail_ack(characteristic, data, *, response) -> None:
            if fake.writes:
                raise RuntimeError("simulated ACK failure")
            await original_write(characteristic, data, response=response)

        fake.write_gatt_char = fail_ack
        try:
            with self.assertRaisesRegex(
                WatchBleConnectionError,
                "BLE screenshot ACK write failed: simulated ACK failure",
            ):
                await wrapper.capture_screenshot(
                    "unused.bmp",
                    sequence=capture_sequence,
                    timeout=2,
                )

            self.assertIsNone(wrapper._screenshot_ack_queue)
            self.assertIsNone(wrapper._screenshot_ack_task)
            self.assertFalse(
                any(
                    task.get_name().startswith("watch-ble-screenshot-ack-")
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                )
            )
        finally:
            await wrapper.close()

    async def test_close_cancels_active_ack_writer_without_leaking_task(self) -> None:
        factory = FakeClientFactory(chunk_size=20)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        capture = asyncio.create_task(
            wrapper.capture_screenshot(
                "unused.bmp",
                sequence=0x66778899,
                timeout=5,
            )
        )
        await asyncio.sleep(0)

        await wrapper.close()

        with self.assertRaises(WatchBleDisconnectedError):
            await capture
        self.assertIsNone(wrapper._screenshot_ack_queue)
        self.assertIsNone(wrapper._screenshot_ack_task)
        self.assertFalse(
            any(
                task.get_name().startswith("watch-ble-screenshot-ack-")
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
            )
        )

    async def test_disconnect_fails_capture_and_cleans_up_ack_writer(self) -> None:
        factory = FakeClientFactory(chunk_size=20)
        wrapper = WatchBleClient("AA:01", client_factory=factory)
        await wrapper.connect()
        fake = factory.instances[0]
        capture = asyncio.create_task(
            wrapper.capture_screenshot(
                "unused.bmp",
                sequence=0x778899AA,
                timeout=5,
            )
        )
        await asyncio.sleep(0)

        fake.disconnected_callback(fake)

        with self.assertRaisesRegex(
            WatchBleDisconnectedError,
            "watch disconnected during screenshot",
        ):
            await capture
        self.assertIsNone(wrapper._screenshot_ack_queue)
        self.assertIsNone(wrapper._screenshot_ack_task)
        await wrapper.close()

    def test_screenshot_cli_has_explicit_output_and_long_transfer_timeout(self) -> None:
        args = _build_parser().parse_args(
            ["screenshot", "--address", "AA:01", "--output", "watch.bmp"]
        )
        self.assertEqual(args.output, "watch.bmp")
        self.assertEqual(args.timeout, 180.0)
        self.assertIsNone(args.sequence)

    async def test_client_maps_invalid_capture_sequence_to_public_protocol_error(
        self,
    ) -> None:
        wrapper = WatchBleClient("AA:01", client_factory=FakeClientFactory())

        with self.assertRaisesRegex(
            WatchBleProtocolError,
            "sequence must be between 0 and 4294967295",
        ):
            await wrapper.capture_screenshot("watch.bmp", sequence=-1)


if __name__ == "__main__":
    unittest.main()
