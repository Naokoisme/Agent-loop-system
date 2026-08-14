from __future__ import annotations

import dataclasses
import unittest

from agent_loop_system.protocol import watch_app_pb2
from agent_loop_system.tools.watch_app_protocol import (
    BIND_COMMAND,
    MAX_PAYLOAD_LENGTH,
    WatchAppFrame,
    WatchAppFrameDecoder,
    WatchAppProtocolError,
    WatchAuthRequest,
    WatchAuthResponse,
    WatchUserProfile,
    crc16_ibm,
    decode_auth_response,
    decode_frame,
    encode_auth_request,
    encode_frame,
)


class WatchAppFrameTests(unittest.TestCase):
    def test_known_crc16_ibm_vector(self) -> None:
        self.assertEqual(crc16_ibm(b"123456789"), 0xBB3D)

    def test_frame_header_uses_firmware_big_endian_fields(self) -> None:
        frame = WatchAppFrame(
            command=BIND_COMMAND,
            sequence=0x1234,
            payload=b"abc",
            flags=0x80,
        )
        encoded = encode_frame(frame)

        self.assertEqual(encoded[:6], bytes.fromhex("AB 03 01 80 00 03"))
        self.assertEqual(encoded[6:8], crc16_ibm(b"abc").to_bytes(2, "big"))
        self.assertEqual(encoded[8:10], bytes.fromhex("12 34"))
        self.assertEqual(decode_frame(encoded), frame)

    def test_decoder_handles_garbage_fragmentation_and_multiple_frames(self) -> None:
        first = WatchAppFrame(BIND_COMMAND, 1, b"first")
        second = WatchAppFrame(0x0302, 2, b"second", flags=0x20)
        first_raw = encode_frame(first)
        second_raw = encode_frame(second)
        decoder = WatchAppFrameDecoder()

        self.assertEqual(decoder.feed(b"garbage" + first_raw[:7]), [])
        self.assertEqual(
            decoder.feed(first_raw[7:] + second_raw),
            [first, second],
        )

    def test_crc_error_is_reported(self) -> None:
        corrupted = bytearray(encode_frame(WatchAppFrame(BIND_COMMAND, 7, b"ok")))
        corrupted[-1] ^= 0x01

        with self.assertRaisesRegex(WatchAppProtocolError, "CRC mismatch"):
            WatchAppFrameDecoder().feed(corrupted)

    def test_decode_is_strict_and_payload_is_bounded(self) -> None:
        encoded = encode_frame(WatchAppFrame(BIND_COMMAND, 1, b""))
        with self.assertRaisesRegex(WatchAppProtocolError, "frame length"):
            decode_frame(encoded + b"trailing")
        with self.assertRaisesRegex(WatchAppProtocolError, "firmware limit"):
            WatchAppFrame(BIND_COMMAND, 1, b"x" * (MAX_PAYLOAD_LENGTH + 1))

    def test_frame_is_immutable(self) -> None:
        frame = WatchAppFrame(BIND_COMMAND, 1, b"")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            frame.sequence = 2  # type: ignore[misc]


class WatchAuthProtocolTests(unittest.TestCase):
    def test_auth_request_round_trip_has_exact_firmware_fields(self) -> None:
        request = WatchAuthRequest(
            user_id="agent-user",
            auth_code="123456",
            version="Windows",
            brand="Agent-loop",
            model="PC",
            user=WatchUserProfile(gender=0, age=28, height_cm=165, weight_kg=52.5),
            timestamp_2000=123_456,
            zone_offset_seconds=28_800,
        )

        message = watch_app_pb2._AuthRequest()
        message.ParseFromString(encode_auth_request(request))

        self.assertEqual(message.userId, "agent-user")
        self.assertEqual(message.system, 0)
        self.assertEqual(message.version, "Windows")
        self.assertEqual(message.brand, "Agent-loop")
        self.assertEqual(message.model, "PC")
        self.assertEqual(message.authCode, "123456")
        self.assertEqual(message.user.gender, 0)
        self.assertEqual(message.user.age, 28)
        self.assertEqual(message.user.height, 165)
        self.assertAlmostEqual(message.user.weight, 52.5)
        self.assertEqual(message.time.timestamp, 123_456)
        self.assertEqual(message.time.zoneOffset, 28_800)

    def test_utf8_firmware_field_boundaries(self) -> None:
        request = WatchAuthRequest(
            user_id="\u4eba" * 10 + "ab",
            auth_code="1" * 16,
            version="\u00e9" * 8,
            brand="b" * 16,
            model="m" * 16,
            timestamp_2000=0,
            zone_offset_seconds=0,
        )
        self.assertTrue(encode_auth_request(request))

        with self.assertRaisesRegex(WatchAppProtocolError, "user_id.*33"):
            WatchAuthRequest(user_id="\u4eba" * 11, auth_code="123456")
        with self.assertRaisesRegex(WatchAppProtocolError, "version.*18"):
            WatchAuthRequest(
                user_id="u",
                auth_code="123456",
                version="\u00e9" * 9,
            )
        with self.assertRaisesRegex(WatchAppProtocolError, "valid UTF-8"):
            WatchAuthRequest(user_id="\ud800", auth_code="123456")

    def test_user_profile_rejects_values_that_would_corrupt_device_data(self) -> None:
        invalid_profiles = (
            {"gender": 2},
            {"age": 0},
            {"age": 121},
            {"height_cm": 49},
            {"height_cm": 251},
            {"weight_kg": 19.9},
            {"weight_kg": 300.1},
        )
        for values in invalid_profiles:
            with self.subTest(values=values):
                with self.assertRaises(WatchAppProtocolError):
                    WatchUserProfile(**values)

    def test_auth_response_decode(self) -> None:
        message = watch_app_pb2._AuthResponse(
            result=0,
            everBind=1,
            bindTime=1_725_000_000,
        )
        self.assertEqual(
            decode_auth_response(message.SerializeToString()),
            WatchAuthResponse(result=0, ever_bound=1, bind_time=1_725_000_000),
        )

    def test_invalid_response_is_rejected(self) -> None:
        with self.assertRaisesRegex(WatchAppProtocolError, "invalid auth response"):
            decode_auth_response(b"\x08")

    def test_auth_response_values_are_validated(self) -> None:
        for response in (
            {"result": False, "ever_bound": 0, "bind_time": 0},
            {"result": 8, "ever_bound": 0, "bind_time": 0},
            {"result": 0, "ever_bound": 2, "bind_time": 0},
            {"result": 0, "ever_bound": 0, "bind_time": -1},
        ):
            with self.subTest(response=response):
                with self.assertRaises(WatchAppProtocolError):
                    WatchAuthResponse(**response)


if __name__ == "__main__":
    unittest.main()
