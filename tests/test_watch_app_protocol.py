from __future__ import annotations

import dataclasses
import unittest

from agent_loop_system.protocol import (
    pb_b2b_hsd_pb2,
    pb_config_pb2,
    pb_data_pb2,
    pb_env_pb2,
    pb_notice_pb2,
    pb_setting_pb2,
    pb_stream_pb2,
    watch_app_pb2,
)
from agent_loop_system.protocol.pb_commands import (
    ConfigKey,
    DataKey,
    EnvironmentKey,
    NoticeKey,
    PbCommandGroup,
    SettingKey,
    StreamKey,
    TopstepFileKey,
    command_id,
)
from agent_loop_system.protocol.pb_schema import (
    message_class,
    message_names,
    new_message,
)
from agent_loop_system.tools.watch_app_protocol import (
    APP_MAX_DATA_LENGTH,
    BIND_COMMAND,
    MULTIPART_FLAG,
    MAX_PAYLOAD_LENGTH,
    RETRANSMISSION_FLAG,
    WatchAppFrame,
    WatchAppFrameDecoder,
    WatchAppMessageAssembler,
    WatchAppMessageDecoder,
    WatchAppProtocolError,
    WatchAuthRequest,
    WatchAuthResponse,
    WatchUserProfile,
    crc16_ibm,
    decode_auth_response,
    decode_frame,
    decode_multipart_fragment,
    decode_protobuf,
    encode_multipart_chunks,
    encode_auth_request,
    encode_frame,
    encode_protobuf,
    encode_protobuf_chunks,
    encode_protobuf_frames,
    fragment_payload,
    make_command,
    split_protobuf_list,
    split_protobuf_object,
    split_command,
    validate_protobuf,
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

    def test_command_helpers_and_frame_flag_properties(self) -> None:
        command = make_command(0x06, 0x21)
        self.assertEqual(command, 0x0621)
        self.assertEqual(split_command(command), (0x06, 0x21))

        frame = WatchAppFrame(
            command,
            9,
            b"",
            flags=MULTIPART_FLAG | RETRANSMISSION_FLAG,
        )
        self.assertEqual(frame.command_group, 0x06)
        self.assertEqual(frame.key, 0x21)
        self.assertTrue(frame.is_multipart)
        self.assertTrue(frame.is_retransmission)


class WatchAppMultipartTests(unittest.TestCase):
    def test_outbound_payload_uses_app_limit_and_big_endian_fragment_header(self) -> None:
        payload = bytes(range(251)) * 10
        frames = fragment_payload(0x0240, 0x1234, payload)

        self.assertEqual(len(frames), 3)
        self.assertTrue(all(frame.is_multipart for frame in frames))
        self.assertTrue(
            all(
                len(decode_multipart_fragment(frame.payload).data)
                <= APP_MAX_DATA_LENGTH
                for frame in frames
            )
        )
        for index, frame in enumerate(frames):
            fragment = decode_multipart_fragment(frame.payload)
            self.assertEqual(fragment.total_packets, 3)
            self.assertEqual(fragment.packet_index, index)
            self.assertEqual(frame.payload[:8], (3).to_bytes(4, "big") + index.to_bytes(4, "big"))

        assembler = WatchAppMessageAssembler()
        completed = None
        for frame in frames:
            completed = assembler.feed(frame)
        self.assertIsNotNone(completed)
        self.assertEqual(completed.payload, payload)
        self.assertEqual(completed.fragment_count, 3)

    def test_reassembly_is_ordered_and_isolated_by_transport_channel(self) -> None:
        a_frames = fragment_payload(0x0201, 7, b"a" * 1500)
        b_frames = fragment_payload(0x0201, 7, b"b" * 1100)
        assembler = WatchAppMessageAssembler()

        self.assertIsNone(assembler.feed(a_frames[1], channel="gatt"))
        self.assertIsNone(assembler.feed(b_frames[0], channel="spp"))
        repeat = WatchAppFrame(
            b_frames[0].command,
            b_frames[0].sequence,
            b_frames[0].payload,
            flags=b_frames[0].flags | RETRANSMISSION_FLAG,
        )
        self.assertIsNone(assembler.feed(repeat, channel="spp"))

        a_message = assembler.feed(a_frames[0], channel="gatt")
        b_message = assembler.feed(b_frames[1], channel="spp")
        self.assertEqual(a_message.payload, b"a" * 1500)
        self.assertEqual(b_message.payload, b"b" * 1100)
        self.assertFalse(a_message.is_retransmission)
        self.assertTrue(b_message.is_retransmission)
        self.assertEqual(assembler.pending_count, 0)

    def test_conflicting_retransmission_is_rejected_and_state_is_dropped(self) -> None:
        frames = fragment_payload(0x0201, 2, b"x" * 1500)
        assembler = WatchAppMessageAssembler()
        self.assertIsNone(assembler.feed(frames[0]))
        fragment = decode_multipart_fragment(frames[0].payload)
        conflicting = WatchAppFrame(
            frames[0].command,
            frames[0].sequence,
            type(fragment)(
                fragment.total_packets,
                fragment.packet_index,
                b"y" + fragment.data[1:],
            ).encode(),
            flags=frames[0].flags | RETRANSMISSION_FLAG,
        )

        with self.assertRaisesRegex(WatchAppProtocolError, "changed"):
            assembler.feed(conflicting)
        self.assertEqual(assembler.pending_count, 0)

    def test_message_decoder_keeps_partial_byte_streams_separate_per_channel(self) -> None:
        gatt = b"".join(
            encode_frame(frame)
            for frame in fragment_payload(0x0240, 8, b"g" * 1200)
        )
        spp = encode_frame(WatchAppFrame(0x0309, 8, b"spp"))
        decoder = WatchAppMessageDecoder()

        self.assertEqual(decoder.feed(gatt[:13], channel="gatt"), [])
        spp_messages = decoder.feed(spp, channel="spp")
        self.assertEqual([message.payload for message in spp_messages], [b"spp"])
        gatt_messages = decoder.feed(gatt[13:], channel="gatt")
        self.assertEqual([message.payload for message in gatt_messages], [b"g" * 1200])

    def test_list_fragments_merge_with_normal_protobuf_semantics(self) -> None:
        first = pb_setting_pb2._AlarmList()
        first.items.add(id=1, hour=1, minute=0, isEnabled=True)
        second = pb_setting_pb2._AlarmList()
        second.items.add(id=2, hour=2, minute=0, isEnabled=False)
        frames = encode_multipart_chunks(
            0x0239,
            11,
            (encode_protobuf(first), encode_protobuf(second)),
        )
        assembler = WatchAppMessageAssembler()

        self.assertIsNone(assembler.feed(frames[1]))
        message = assembler.feed(frames[0])
        decoded = decode_protobuf(message.payload, pb_setting_pb2._AlarmList)
        self.assertEqual([item.id for item in decoded.items], [1, 2])

    def test_protobuf_requires_semantic_chunks_instead_of_raw_byte_slicing(self) -> None:
        oversized = pb_setting_pb2._DialList()
        for index in range(100):
            oversized.items.add(
                id=(1 << 63) + index,
                is_builtin=True,
                is_select=True,
            )
        self.assertGreater(len(encode_protobuf(oversized)), 1024)
        with self.assertRaisesRegex(
            WatchAppProtocolError, "complete object/list chunks"
        ):
            encode_protobuf_frames(0x0622, 5, oversized)

        first = pb_setting_pb2._AlarmList()
        first.items.add(id=1, hour=1)
        second = pb_setting_pb2._AlarmList()
        second.items.add(id=2, hour=2)
        frames = encode_protobuf_chunks(0x0239, 5, (first, second))
        self.assertEqual(len(frames), 2)
        for frame in frames:
            chunk = decode_multipart_fragment(frame.payload).data
            # Every protocol fragment is independently decodable by firmware.
            self.assertIsInstance(
                decode_protobuf(chunk, pb_setting_pb2._AlarmList),
                pb_setting_pb2._AlarmList,
            )

    def test_object_and_list_split_helpers_emit_valid_mergeable_messages(self) -> None:
        configs = pb_config_pb2._Configs()
        configs.functionConfig.flags = b"\x03"
        configs.unitConfig.flags = b"\x02"
        configs.goalConfig.steps = 12_345
        object_chunks = split_protobuf_object(configs, max_data_length=9)
        self.assertGreater(len(object_chunks), 1)
        self.assertTrue(
            all(len(encode_protobuf(chunk)) <= 9 for chunk in object_chunks)
        )
        merged_configs = decode_protobuf(
            b"".join(encode_protobuf(chunk) for chunk in object_chunks),
            pb_config_pb2._Configs,
        )
        self.assertEqual(merged_configs, configs)

        alarms = pb_setting_pb2._AlarmList()
        for index in range(12):
            alarms.items.add(
                id=index,
                hour=index,
                minute=index * 2,
                label=f"alarm-{index:02d}-" + "x" * 20,
            )
        list_chunks = split_protobuf_list(
            alarms,
            "items",
            max_data_length=100,
        )
        self.assertGreater(len(list_chunks), 1)
        self.assertTrue(
            all(len(encode_protobuf(chunk)) <= 100 for chunk in list_chunks)
        )
        self.assertTrue(all(len(chunk.items) <= 10 for chunk in list_chunks))
        merged_alarms = decode_protobuf(
            b"".join(encode_protobuf(chunk) for chunk in list_chunks),
            pb_setting_pb2._AlarmList,
        )
        self.assertEqual(merged_alarms, alarms)

    def test_document_field_limits_are_enforced_on_outbound_chunks(self) -> None:
        with self.assertRaisesRegex(
            WatchAppProtocolError, "_DeviceInfo.project.*17 bytes"
        ):
            validate_protobuf(pb_config_pb2._DeviceInfo(project="x" * 17))
        with self.assertRaisesRegex(
            WatchAppProtocolError, "_AppNotice.content.*801 bytes"
        ):
            validate_protobuf(
                pb_notice_pb2._AppNotice(content="a" * 801)
            )
        with self.assertRaisesRegex(
            WatchAppProtocolError, "_HsdIce.labels.*4 items"
        ):
            validate_protobuf(
                pb_b2b_hsd_pb2._HsdIce(labels=["a", "b", "c", "d"])
            )


class CompletePbSchemaTests(unittest.TestCase):
    def test_all_document_schema_groups_are_generated_and_importable(self) -> None:
        modules_and_messages = (
            (pb_config_pb2, "_DeviceInfo"),
            (pb_setting_pb2, "_AiResult"),
            (pb_env_pb2, "_BluetoothInfo"),
            (pb_notice_pb2, "_AppNotice"),
            (pb_data_pb2, "_SportRecord"),
            (pb_stream_pb2, "_FileHead"),
            (pb_b2b_hsd_pb2, "_HsdHabitList"),
        )
        for module, message_name in modules_and_messages:
            with self.subTest(module=module.__name__, message=message_name):
                self.assertIn(message_name, module.DESCRIPTOR.message_types_by_name)

    def test_v012_declared_fields_round_trip(self) -> None:
        device = pb_config_pb2._DeviceInfo(
            productType=1,
            platform=2,
            dialFeatures=1,
        )
        device.video.width = 240
        device.video.height = 240
        decoded_device = decode_protobuf(
            encode_protobuf(device), pb_config_pb2._DeviceInfo
        )
        self.assertEqual(decoded_device.video.width, 240)
        self.assertEqual(decoded_device.platform, 2)

        weather = pb_setting_pb2._WeatherDay(
            pressure=1009,
            windAngle=225.5,
            visibility=12.25,
        )
        decoded_weather = decode_protobuf(
            encode_protobuf(weather), pb_setting_pb2._WeatherDay
        )
        self.assertEqual(decoded_weather.pressure, 1009)
        self.assertAlmostEqual(decoded_weather.windAngle, 225.5)
        self.assertAlmostEqual(decoded_weather.visibility, 12.25)

        location = pb_env_pb2._LocationInfo(
            longitude=113000000,
            latitude=23000000,
            snr=42,
        )
        self.assertEqual(
            decode_protobuf(
                encode_protobuf(location), pb_env_pb2._LocationInfo
            ).snr,
            42,
        )

    def test_command_catalog_and_name_based_message_lookup(self) -> None:
        self.assertEqual(
            command_id(PbCommandGroup.ENVIRONMENT, EnvironmentKey.BIND),
            BIND_COMMAND,
        )
        self.assertEqual(
            command_id(PbCommandGroup.SETTING, SettingKey.GET_EPO_TIME),
            0x026B,
        )
        self.assertIs(message_class("_DeviceInfo"), pb_config_pb2._DeviceInfo)
        self.assertIs(
            message_class("agent_loop_system.protocol._AiResult"),
            pb_setting_pb2._AiResult,
        )
        notice = new_message("_AppNotice", type=2, title="title", content="body")
        self.assertIsInstance(notice, pb_notice_pb2._AppNotice)
        self.assertIn("_HsdGameRecordList", message_names())
        with self.assertRaisesRegex(KeyError, "unknown PB message"):
            message_class("_DoesNotExist")
        with self.assertRaisesRegex(TypeError, "group"):
            command_id(True, SettingKey.GET_EPO_TIME)

        self.assertEqual({int(key) for key in ConfigKey}, set(range(0x01, 0x24)))
        self.assertEqual({int(key) for key in SettingKey}, set(range(0x30, 0x6F)))
        self.assertEqual(
            {int(key) for key in EnvironmentKey}, set(range(0x01, 0x14))
        )
        self.assertEqual({int(key) for key in NoticeKey}, set(range(0x01, 0x07)))
        self.assertEqual(
            {int(key) for key in DataKey},
            {*range(0x01, 0x08), 0x21, 0x22, 0x30, 0x31, 0x32},
        )
        self.assertEqual(
            {int(key) for key in StreamKey},
            {
                *range(0x01, 0x05),
                *range(0x11, 0x17),
                *range(0x21, 0x29),
                *range(0x31, 0x37),
                0x41,
                0x42,
                0x51,
                0x52,
            },
        )
        self.assertEqual([int(key) for key in TopstepFileKey], [0x01])


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
            {"gender": 3},
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

        request = WatchAuthRequest(
            user_id="unknown-gender",
            auth_code="123456",
            user=WatchUserProfile(gender=2),
            timestamp_2000=0,
            zone_offset_seconds=0,
        )
        message = watch_app_pb2._AuthRequest()
        message.ParseFromString(encode_auth_request(request))
        self.assertEqual(message.user.gender, 2)

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
