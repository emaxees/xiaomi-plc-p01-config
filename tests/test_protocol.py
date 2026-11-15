#!/usr/bin/env python3
"""Encrypt/decrypt and packet round-trip tests. No network, no real token."""

from __future__ import annotations

import json
import os
import struct
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import plc  # noqa: E402

DUMMY_TOKEN = bytes.fromhex("00112233445566778899aabbccddeeff")
DEVICE_ID = 0x11223344
STAMP = 123456


class ProtocolTests(unittest.TestCase):
    def test_hello_packet_is_32_bytes(self) -> None:
        self.assertEqual(len(plc.HELLO), 32)
        magic, length = struct.unpack(">HH", plc.HELLO[:4])
        self.assertEqual(magic, 0x2131)
        self.assertEqual(length, 32)
        self.assertEqual(plc.HELLO[4:], b"\xff" * 28)

    def test_token_parse(self) -> None:
        self.assertEqual(plc.parse_token("00112233445566778899aabbccddeeff"), DUMMY_TOKEN)
        self.assertEqual(plc.parse_token("0011-2233-4455-6677-8899-AABB-CCDD-EEFF"), DUMMY_TOKEN)
        with self.assertRaises(ValueError):
            plc.parse_token("not-a-token")
        with self.assertRaises(ValueError):
            plc.parse_token("00112233")

    def test_encrypt_decrypt_roundtrip(self) -> None:
        plain = b'{"id":1,"method":"get_wifi","params":[]}'
        cipher = plc.encrypt(plain, DUMMY_TOKEN)
        self.assertEqual(len(cipher) % 16, 0)
        self.assertNotEqual(cipher, plain)
        self.assertEqual(plc.decrypt(cipher, DUMMY_TOKEN), plain)

    def test_encrypt_is_not_ecb(self) -> None:
        # Same plaintext with a different token must not collide.
        other = bytes.fromhex("ffeeddccbbaa99887766554433221100")
        plain = b"hello-plc-p01-payload"
        self.assertNotEqual(plc.encrypt(plain, DUMMY_TOKEN), plc.encrypt(plain, other))

    def test_build_and_parse_packet(self) -> None:
        payload = {"id": 7, "method": "get_wifi", "params": []}
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        packet = plc.build_packet(raw, DUMMY_TOKEN, DEVICE_ID, STAMP)
        self.assertEqual(packet[:2], b"\x21\x31")
        length = struct.unpack(">H", packet[2:4])[0]
        self.assertEqual(length, len(packet))
        parsed = plc.parse_packet(packet, DUMMY_TOKEN)
        self.assertEqual(parsed["device_id"], DEVICE_ID)
        self.assertEqual(parsed["stamp"], STAMP)
        self.assertEqual(parsed["payload"], payload)

    def test_wrong_token_fails_checksum(self) -> None:
        raw = b'{"id":1,"method":"miIO.info","params":[]}'
        packet = plc.build_packet(raw, DUMMY_TOKEN, DEVICE_ID, STAMP)
        wrong = bytes.fromhex("ffeeddccbbaa99887766554433221100")
        with self.assertRaises(plc.MiioChecksumError):
            plc.parse_packet(packet, wrong)

    def test_parse_hello_exposes_token(self) -> None:
        # Unprovisioned devices put the token in the checksum field of the hello reply.
        reply = struct.pack(">HHIII", 0x2131, 32, 0, DEVICE_ID, STAMP) + DUMMY_TOKEN
        info = plc.parse_hello(reply)
        self.assertEqual(info["device_id"], DEVICE_ID)
        self.assertEqual(info["stamp"], STAMP)
        self.assertEqual(info["token"], DUMMY_TOKEN)

    def test_parse_hello_hides_zero_token(self) -> None:
        reply = struct.pack(">HHIII", 0x2131, 32, 0, DEVICE_ID, STAMP) + (b"\x00" * 16)
        info = plc.parse_hello(reply)
        self.assertIsNone(info["token"])
        reply_ff = struct.pack(">HHIII", 0x2131, 32, 0, DEVICE_ID, STAMP) + (b"\xff" * 16)
        self.assertIsNone(plc.parse_hello(reply_ff)["token"])

    def test_quality_hint(self) -> None:
        self.assertIn("poor", plc.quality_hint(53, 59))
        self.assertIn("good", plc.quality_hint(180, 200))
        self.assertIn("excellent", plc.quality_hint(320, 350))

    def test_unwrap_and_mask(self) -> None:
        self.assertEqual(plc.unwrap_result({"result": [{"ssid": "Home_plus"}]}), {"ssid": "Home_plus"})
        self.assertEqual(plc.unwrap_result({"result": ["ok"]}), "ok")
        self.assertEqual(plc.mask_secret("super-secret"), "********")

    def test_load_params_arg(self) -> None:
        self.assertEqual(plc.load_params_arg(None), [])
        self.assertEqual(plc.load_params_arg('{"channel":6,"bw":20}'), {"channel": 6, "bw": 20})

    def test_cli_help(self) -> None:
        parser = plc.build_parser()
        help_text = parser.format_help()
        for name in (
            "discover",
            "call",
            "info",
            "wifi-get",
            "wifi-set",
            "plc-link",
            "devinfo",
            "clients",
            "provision",
            "reboot",
        ):
            self.assertIn(name, help_text)

    def test_restore_is_refused(self) -> None:
        client = plc.Client("127.0.0.1", token=DUMMY_TOKEN, timeout=0.1, retries=1)
        try:
            with self.assertRaises(SystemExit) as ctx:
                client.call("miIO.restore")
            self.assertIn("miIO.restore", str(ctx.exception))
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
