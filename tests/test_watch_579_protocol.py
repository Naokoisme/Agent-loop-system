from __future__ import annotations

import unittest

from agent_loop_system.tools.watch_579_protocol import (
    Watch579FrameDecoder,
    Watch579Message,
    Watch579ProtocolError,
    Watch579TransportAck,
    build_raw_packet,
    crc16_arc,
    encode_raw_command,
    encode_transport_ack,
    format_hex,
    normalize_raw_command,
    parse_data_hex,
    top5step_command,
)


FIND_WATCH = bytes.fromhex("AB0000050D08000202003B000000")
CALCULATOR_CLICK = bytes.fromhex(
    "AB00002C545500020400050027"
    "08544F503553544550001C"
    "544F5035535445503A54505F434C49434B3A35312C3135362C31203B"
)
BUTTON_PRESS = bytes.fromhex(
    "AB00002CC02600020400050027"
    "08544F503553544550001C"
    "544F5035535445503A425554544F4E5F50524553533A312C312C303B"
)


class Watch579ProtocolTests(unittest.TestCase):
    def test_crc16_arc_reference(self) -> None:
        self.assertEqual(crc16_arc(b"123456789"), 0xBB3D)

    def test_find_watch_legacy_simple_packet(self) -> None:
        self.assertEqual(build_raw_packet("02", "3b", ""), FIND_WATCH)
        self.assertEqual(len(FIND_WATCH), 14)

    def test_calculator_click_golden_packet(self) -> None:
        packet = encode_raw_command(top5step_command(":TP_CLICK:51,156,1 ;"))
        self.assertEqual(packet, CALCULATOR_CLICK)
        self.assertEqual(len(packet), 52)

    def test_button_press_golden_packet(self) -> None:
        packet = encode_raw_command(top5step_command(":BUTTON_PRESS:1,1,0"))
        self.assertEqual(packet, BUTTON_PRESS)

    def test_raw_input_is_case_insensitive_and_accepts_separators(self) -> None:
        command = normalize_raw_command("0X0a", "fF", "01, 0x02:03-04")
        self.assertEqual((command.cmd, command.key, command.data), (10, 255, b"\x01\x02\x03\x04"))
        self.assertEqual(parse_data_hex("01020304"), command.data)
        self.assertEqual(format_hex(command.data), "01 02 03 04")

    def test_invalid_and_oversized_data_are_rejected(self) -> None:
        with self.assertRaises(Watch579ProtocolError):
            parse_data_hex("ABC")
        with self.assertRaises(Watch579ProtocolError):
            parse_data_hex("GG")
        with self.assertRaises(Watch579ProtocolError):
            parse_data_hex("00" * 500)

    def test_decoder_handles_split_header_and_l2_body(self) -> None:
        incoming = bytes.fromhex("AB0000060AED008C020092000101")
        decoder = Watch579FrameDecoder()
        self.assertEqual(decoder.feed(incoming[:8]), [])
        frames = decoder.feed(incoming[8:])
        self.assertEqual(len(frames), 1)
        message = frames[0]
        self.assertIsInstance(message, Watch579Message)
        assert isinstance(message, Watch579Message)
        self.assertEqual(message.sequence, 0x008C)
        self.assertEqual(message.command.cmd, 0x02)
        self.assertEqual(message.command.key, 0x92)
        self.assertEqual(message.command.data, b"\x01")

    def test_decoder_handles_coalesced_ack_and_message(self) -> None:
        ack = encode_transport_ack(2)
        incoming = bytes.fromhex("AB0000060BAD008D020092000102")
        frames = Watch579FrameDecoder().feed(ack + incoming)
        self.assertEqual(len(frames), 2)
        self.assertIsInstance(frames[0], Watch579TransportAck)
        self.assertIsInstance(frames[1], Watch579Message)

    def test_decoder_accepts_legacy_zero_data_placeholder_in_a_later_chunk(self) -> None:
        decoder = Watch579FrameDecoder()
        frames = decoder.feed(FIND_WATCH[:-1])
        self.assertEqual(len(frames), 1)
        self.assertIsInstance(frames[0], Watch579Message)
        self.assertEqual(decoder.feed(FIND_WATCH[-1:]), [])
        trailing = decoder.feed(encode_transport_ack(2))
        self.assertEqual(len(trailing), 1)
        self.assertIsInstance(trailing[0], Watch579TransportAck)

    def test_decoder_rejects_bad_length_and_crc(self) -> None:
        with self.assertRaises(Watch579ProtocolError):
            Watch579FrameDecoder().feed(bytes.fromhex("AB00000400000002"))
        bad = bytearray.fromhex("AB0000060AED008C020092000101")
        bad[-1] ^= 1
        with self.assertRaises(Watch579ProtocolError):
            Watch579FrameDecoder().feed(bad)


if __name__ == "__main__":
    unittest.main()
