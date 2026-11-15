#!/usr/bin/env python3
"""Configure a Xiaomi WiFi Powerline extender (xiaomi.plc.v1 / P01) over local miIO.

Talks to the device on UDP port 54321. No Mi Home account or cloud access is required.
Token and address come from --ip/--token or the PLC_IP / PLC_TOKEN environment variables.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import socket
import struct
import sys
import time
from typing import Any, Optional

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = 0x2131
PORT = 54321
HEADER_LEN = 32
HELLO = struct.pack(">HH", MAGIC, HEADER_LEN) + b"\xff" * 28
CALL_SPACING = 1.5
RESTORE_METHOD = "miIO.restore"
DANGEROUS_METHODS = frozenset(
    {
        "miIO.restore",
        "miIO.ota",
        "set_bind_key",
        "bind_key",
        "local.status",
        "log_upload",
        "set_roam",
    }
)

# Firmware set_wifi accepts these mgmt values (anything else becomes mixed-psk).
MGMT_VALUES = ("WPA2", "WPA", "WPA/WPA2", "NONE")


class MiioError(Exception):
    """Protocol or device error."""


class MiioTimeout(MiioError):
    """No reply from the device."""


class MiioChecksumError(MiioError):
    """Reply checksum failed; usually a wrong token."""


def parse_token(value: str) -> bytes:
    text = value.strip().replace(" ", "").replace("-", "")
    if len(text) == 32 and all(c in "0123456789abcdefABCDEF" for c in text):
        return bytes.fromhex(text)
    raise ValueError("Token must be 16 bytes written as 32 hex characters")


def key_iv(token: bytes) -> tuple[bytes, bytes]:
    key = hashlib.md5(token).digest()
    iv = hashlib.md5(key + token).digest()
    return key, iv


def encrypt(plain: bytes, token: bytes) -> bytes:
    key, iv = key_iv(token)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padded) + enc.finalize()


def decrypt(cipher: bytes, token: bytes) -> bytes:
    if not cipher:
        return b""
    if len(cipher) % 16:
        raise MiioError("Encrypted payload length is not a multiple of 16")
    key, iv = key_iv(token)
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = dec.update(cipher) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).rstrip(b"\x00")


def build_packet(payload: bytes, token: bytes, device_id: int, stamp: int) -> bytes:
    encrypted = encrypt(payload, token)
    length = HEADER_LEN + len(encrypted)
    header = struct.pack(">HHIII", MAGIC, length, 0, device_id & 0xFFFFFFFF, stamp & 0xFFFFFFFF)
    checksum = hashlib.md5(header + token + encrypted).digest()
    return header + checksum + encrypted


def unpack_header(data: bytes) -> tuple[int, int, int, int, int, bytes, bytes]:
    if len(data) < HEADER_LEN:
        raise MiioError(f"Packet too short ({len(data)} bytes)")
    magic, length, unknown, device_id, stamp = struct.unpack(">HHIII", data[:16])
    if magic != MAGIC:
        raise MiioError(f"Bad magic: 0x{magic:04x}")
    checksum = data[16:32]
    payload = data[32:]
    return magic, length, unknown, device_id, stamp, checksum, payload


def parse_hello(data: bytes) -> dict[str, Any]:
    magic, length, unknown, device_id, stamp, checksum, _payload = unpack_header(data)
    token: Optional[bytes]
    if checksum in (b"\x00" * 16, b"\xff" * 16):
        token = None
    else:
        token = checksum
    return {
        "magic": magic,
        "length": length,
        "unknown": unknown,
        "device_id": device_id,
        "stamp": stamp,
        "token": token,
    }


def parse_packet(data: bytes, token: bytes) -> dict[str, Any]:
    magic, length, unknown, device_id, stamp, checksum, encrypted = unpack_header(data)
    header = struct.pack(">HHIII", magic, length, unknown, device_id, stamp)
    expected = hashlib.md5(header + token + encrypted).digest()
    if expected != checksum:
        raise MiioChecksumError("Checksum mismatch (wrong token, or not a miIO reply)")
    plain = decrypt(encrypted, token) if encrypted else b""
    text = plain.decode("utf-8") if plain else ""
    body: Any = None
    if text:
        try:
            body = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MiioError(f"Reply is not JSON: {text!r}") from exc
    return {
        "device_id": device_id,
        "stamp": stamp,
        "payload": body,
        "raw": text,
    }


def format_device_id(device_id: int) -> str:
    return f"{device_id} (0x{device_id:08x})"


def unwrap_result(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    if "error" in payload and payload["error"]:
        return payload
    result = payload.get("result", payload)
    if isinstance(result, list) and len(result) == 1:
        return result[0]
    return result


def mask_secret(value: Any) -> str:
    if value is None or value == "":
        return ""
    return "********"


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_params_arg(raw: Optional[str]) -> Any:
    if raw is None or raw == "":
        return []
    if raw.startswith("@"):
        path = raw[1:]
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return json.loads(raw)


def quality_hint(rx: int, tx: int) -> str:
    phy = min(rx, tx) if rx and tx else max(rx, tx)
    approx = max(phy // 2, 0)
    if phy <= 0:
        level = "no link"
    elif phy < 80:
        level = "poor"
    elif phy < 150:
        level = "fair"
    elif phy < 300:
        level = "good"
    else:
        level = "excellent"
    return (
        f"{level} (PHY {rx}/{tx} Mbps; real TCP throughput is often about half, "
        f"around {approx} Mbps). Good household wiring typically negotiates 150-300 Mbps PHY."
    )


class Client:
    def __init__(
        self,
        ip: str,
        token: Optional[bytes] = None,
        timeout: float = 5.0,
        retries: int = 3,
        spacing: float = CALL_SPACING,
    ) -> None:
        self.ip = ip
        self.token = token
        self.timeout = timeout
        self.retries = max(1, retries)
        self.spacing = spacing
        self.device_id: Optional[int] = None
        self.stamp: Optional[int] = None
        self._last_call = 0.0
        self._msg_id = random.randint(1, 10000)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)

    def close(self) -> None:
        self.sock.close()

    def _next_id(self) -> int:
        self._msg_id += 1
        if self._msg_id > 2_000_000_000:
            self._msg_id = 1
        return self._msg_id

    def _throttle(self) -> None:
        if self._last_call <= 0:
            return
        wait = self.spacing - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)

    def hello(self) -> dict[str, Any]:
        last_error: Optional[Exception] = None
        for _attempt in range(self.retries):
            try:
                self.sock.sendto(HELLO, (self.ip, PORT))
                data, _addr = self.sock.recvfrom(65535)
                info = parse_hello(data)
                self.device_id = info["device_id"]
                self.stamp = info["stamp"]
                return info
            except (OSError, MiioError) as exc:
                last_error = exc
                time.sleep(0.4)
        raise MiioTimeout(f"No hello reply from {self.ip}:{PORT}") from last_error

    def call(
        self,
        method: str,
        params: Any = None,
        *,
        allow_restore: bool = False,
    ) -> dict[str, Any]:
        if method == RESTORE_METHOD and not allow_restore:
            raise SystemExit(
                "Refusing miIO.restore: it factory-resets the extender and leaves "
                "Wi-Fi open. Re-run with --i-know if you really want that."
            )
        if self.token is None:
            raise SystemExit("This command needs a token. Pass --token or set PLC_TOKEN.")
        self._throttle()
        if self.device_id is None or self.stamp is None:
            self.hello()
        if params is None:
            params = []
        request = {
            "id": self._next_id(),
            "method": method,
            "params": params,
        }
        payload = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        last_error: Optional[Exception] = None
        for _attempt in range(self.retries):
            assert self.stamp is not None and self.device_id is not None
            self.stamp = (self.stamp + 1) & 0xFFFFFFFF
            packet = build_packet(payload, self.token, self.device_id, self.stamp)
            try:
                self.sock.sendto(packet, (self.ip, PORT))
                data, _addr = self.sock.recvfrom(65535)
                parsed = parse_packet(data, self.token)
                if parsed["stamp"] is not None:
                    self.stamp = parsed["stamp"]
                self._last_call = time.monotonic()
                body = parsed["payload"]
                if isinstance(body, dict) and body.get("error"):
                    raise MiioError(f"{method} error: {json.dumps(body['error'], ensure_ascii=False)}")
                return body if isinstance(body, dict) else {"result": body}
            except MiioChecksumError:
                raise
            except (OSError, MiioError, TimeoutError, socket.timeout) as exc:
                last_error = exc
                time.sleep(0.4)
        raise MiioTimeout(
            f"No reply to {method} from {self.ip}:{PORT} after {self.retries} tries"
        ) from last_error


def discover(ip: Optional[str], timeout: float) -> list[dict[str, Any]]:
    target = ip or "255.255.255.255"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(HELLO, (target, PORT))
    except OSError as exc:
        raise MiioError(f"Could not send hello to {target}:{PORT}: {exc}") from exc
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            break
        except OSError:
            break
        try:
            info = parse_hello(data)
        except MiioError:
            continue
        key = (addr[0], info["device_id"])
        if key in seen:
            continue
        seen.add(key)
        found.append({"ip": addr[0], "port": addr[1], **info})
    sock.close()
    return found


def require_ip(args: argparse.Namespace) -> str:
    ip = args.ip or os.environ.get("PLC_IP")
    if not ip:
        raise SystemExit("Pass --ip or set the PLC_IP environment variable.")
    return ip


def require_token(args: argparse.Namespace) -> bytes:
    raw = args.token or os.environ.get("PLC_TOKEN")
    if not raw:
        raise SystemExit("Pass --token or set the PLC_TOKEN environment variable.")
    try:
        return parse_token(raw)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def make_client(args: argparse.Namespace, *, need_token: bool = True) -> Client:
    token = require_token(args) if need_token else None
    if token is None and (args.token or os.environ.get("PLC_TOKEN")):
        raw = args.token or os.environ.get("PLC_TOKEN")
        try:
            token = parse_token(raw)  # type: ignore[arg-type]
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    return Client(
        ip=require_ip(args),
        token=token,
        timeout=args.timeout,
        retries=args.retries,
    )


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def cmd_discover(args: argparse.Namespace) -> int:
    ip = args.ip or os.environ.get("PLC_IP")
    devices = discover(ip, args.timeout)
    if not devices:
        target = ip or "255.255.255.255"
        print(f"No device answered hello on {target}:{PORT}.", file=sys.stderr)
        if not ip:
            print(
                "If the extender is in factory-reset AP mode, connect to "
                "xiaomi-plc-v1_miapXXXX and retry with --ip 192.168.100.1",
                file=sys.stderr,
            )
        return 1
    for dev in devices:
        print(f"ip:        {dev['ip']}")
        print(f"device id: {format_device_id(dev['device_id'])}")
        print(f"stamp:     {dev['stamp']}")
        if dev["token"]:
            print(f"token:     {dev['token'].hex()}")
            print("Token is visible because the device is unprovisioned (AP / factory-reset mode). Save it.")
        else:
            print("token:     (not exposed; already provisioned - use the token you saved earlier)")
        print()
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    try:
        params = load_params_arg(args.params)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid JSON params: {exc}") from exc
    if args.method in DANGEROUS_METHODS and args.method != RESTORE_METHOD:
        print(
            f"Warning: {args.method} can disrupt the device or leak data. See docs/methods.md.",
            file=sys.stderr,
        )
    client = make_client(args)
    try:
        result = client.call(args.method, params, allow_restore=args.i_know)
    finally:
        client.close()
    print_json(result)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    client = make_client(args)
    try:
        print_json(client.call("miIO.info"))
    finally:
        client.close()
    return 0


def cmd_devinfo(args: argparse.Namespace) -> int:
    client = make_client(args)
    try:
        print_json(client.call("get_devinfo"))
    finally:
        client.close()
    return 0


def cmd_wifi_get(args: argparse.Namespace) -> int:
    client = make_client(args)
    try:
        payload = client.call("get_wifi")
    finally:
        client.close()
    wifi = unwrap_result(payload)
    if not isinstance(wifi, dict):
        print_json(payload)
        return 0
    shown = dict(wifi)
    if not args.show_password and "pswd" in shown:
        shown["pswd"] = mask_secret(shown["pswd"])
    print_json(shown)
    return 0


def cmd_wifi_set(args: argparse.Namespace) -> int:
    client = make_client(args)
    try:
        current = unwrap_result(client.call("get_wifi"))
        if not isinstance(current, dict):
            raise SystemExit(f"Unexpected get_wifi reply: {current!r}")
        new = {
            "ssid": current.get("ssid", ""),
            "pswd": current.get("pswd", ""),
            "mgmt": current.get("mgmt", "WPA2"),
            "hidden": as_int(current.get("hidden", 0)),
            "channel": as_int(current.get("channel", 0)),
            "bw": as_int(current.get("bw", 0)),
        }
        if args.ssid is not None:
            new["ssid"] = args.ssid
        if args.password is not None:
            new["pswd"] = args.password
        if args.mgmt is not None:
            new["mgmt"] = args.mgmt
        if args.hidden is not None:
            new["hidden"] = args.hidden
        if args.channel is not None:
            new["channel"] = args.channel
        if args.bw is not None:
            new["bw"] = args.bw
        if new["channel"] == 0:
            new["bw"] = 0

        print("set_wifi plan (firmware overwrites every field, not only the ones you pass):")
        keys = ("ssid", "mgmt", "hidden", "channel", "bw", "pswd")
        changed = False
        for key in keys:
            before = current.get(key, "")
            after = new[key]
            if key == "pswd":
                before_s = mask_secret(before) if before else "(empty)"
                after_s = mask_secret(after) if after else "(empty)"
                if not args.show_password:
                    mark = "changed" if before != after else "unchanged"
                    print(f"  pswd:    {before_s} -> {after_s} ({mark})")
                else:
                    mark = "unchanged" if before == after else "CHANGED"
                    print(f"  pswd:    {before!r} -> {after!r} ({mark})")
            else:
                mark = "unchanged" if before == after else "CHANGED"
                if before != after:
                    changed = True
                print(f"  {key + ':':<9} {before!s} -> {after!s} ({mark})")
            if key != "pswd" and before != after:
                changed = True
            if key == "pswd" and before != after:
                changed = True
        if not changed:
            print("No changes.")
            return 0
        if not args.yes:
            print("Dry run. Re-run with --yes to apply. Wi-Fi will restart for a few seconds.")
            return 0
        result = client.call(
            "set_wifi",
            {
                "ssid": new["ssid"],
                "pswd": new["pswd"],
                "mgmt": new["mgmt"],
                "hidden": new["hidden"],
                "channel": new["channel"],
                "bw": new["bw"],
            },
        )
        print_json(result)
        print("Applied. The extender radio restarts for a few seconds; clients will reconnect.")
    finally:
        client.close()
    return 0


def cmd_plc_link(args: argparse.Namespace) -> int:
    client = make_client(args)
    try:
        info = unwrap_result(client.call("avln_info"))
        stas = unwrap_result(client.call("avln_stas"))
    finally:
        client.close()
    if args.json:
        # Keep NMK out of the default JSON view.
        safe_info = dict(info) if isinstance(info, dict) else info
        if isinstance(safe_info, dict):
            safe_info = {k: v for k, v in safe_info.items() if k.lower() != "nmk"}
        safe_stas = dict(stas) if isinstance(stas, dict) else stas
        if isinstance(safe_stas, dict) and isinstance(safe_stas.get("stat"), dict):
            stat = {k: v for k, v in safe_stas["stat"].items() if k.lower() != "nmk"}
            safe_stas = dict(safe_stas)
            safe_stas["stat"] = stat
        print_json({"avln_info": safe_info, "avln_stas": safe_stas})
        return 0

    if not isinstance(info, dict) or not info:
        print("avln_info returned nothing. The local powerline STA may be down.")
        return 1
    rx = as_int(info.get("rxdr"))
    tx = as_int(info.get("txdr"))
    print(f"local mac:  {info.get('mac', '?')}")
    print(f"hfid:       {info.get('hfid', '?')}  (P01_PLW = Wi-Fi unit, P01_PLC = powerline peer)")
    print(f"stat:       {info.get('stat', '?')}")
    print(f"PHY rates:  rx {rx} Mbps / tx {tx} Mbps")
    print(f"quality:    {quality_hint(rx, tx)}")
    print("NMK (powerline network key) omitted; use `call avln_info` if you need the raw payload.")
    if isinstance(stas, dict) and stas:
        peers = stas.get("list") or []
        count = (stas.get("stat") or {}).get("count", len(peers))
        print(f"peers:      {count}")
        for peer in peers:
            print(
                f"  {peer.get('mac', '?')}  rx {peer.get('rxdr', '?')} / "
                f"tx {peer.get('txdr', '?')} Mbps"
            )
    return 0


def cmd_clients(args: argparse.Namespace) -> int:
    client = make_client(args)
    try:
        payload = client.call("extender_stas")
    finally:
        client.close()
    data = unwrap_result(payload)
    if args.json:
        print_json(data)
        return 0
    if not isinstance(data, dict):
        print_json(payload)
        return 0
    sta = data.get("sta") or {}
    policy = {0: "none", 1: "allow", 2: "deny"}.get(as_int(sta.get("access_policy")), sta.get("access_policy"))
    print(f"wifi clients: {sta.get('count', 0)}")
    print(f"access policy: {policy}")
    access_list = data.get("access_list") or ""
    if access_list:
        print(f"access list:  {access_list}")
    for entry in data.get("mat") or []:
        print(f"  {entry.get('mac', '?')}  ip={entry.get('ip') or '-'}  last_time={entry.get('last_time', '-')}")
    return 0


def cmd_provision(args: argparse.Namespace) -> int:
    params = {
        "ssid": args.ssid,
        "passwd": args.password,
        "uid": 0,
        "gmt_offset": args.gmt_offset,
        "tz": args.tz,
        "config_type": "app",
        "country_domain": "cn",
        "wifi_config": {"cc": args.cc},
    }
    print("Provisioning with miIO.config_router:")
    print(f"  ssid:           {args.ssid}")
    print("  passwd:         ********")
    print(f"  tz:             {args.tz}")
    print(f"  gmt_offset:     {args.gmt_offset}")
    print(f"  country_domain: cn  (this hardware is China-only; leave this as cn)")
    print(f"  wifi_config.cc: {args.cc}")
    print(f"The cloned AP will be named {args.ssid}_plus (SSID truncated to 26 chars first if longer).")
    print("Firmware writes key_mgmt=WPA in wifi.conf; you can raise it to WPA2 later with wifi-set.")
    client = make_client(args)
    try:
        result = client.call("miIO.config_router", params)
    finally:
        client.close()
    print_json(result)
    print(
        "The device will join the home Wi-Fi, take a DHCP address, and start "
        "extending as <SSID>_plus. Discover it again on the LAN."
    )
    return 0


def cmd_reboot(args: argparse.Namespace) -> int:
    client = make_client(args)
    try:
        result = client.call("miIO.reboot")
    finally:
        client.close()
    print_json(result)
    print("Reboot requested. Firmware waits about 5 seconds, then reboots.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plc.py",
        description=(
            "Configure a Xiaomi WiFi Powerline extender (xiaomi.plc.v1 / P01, fw 1.0.14) "
            "over the local miIO protocol, without the Mi Home app."
        ),
        epilog=(
            "Credentials: --ip / --token, or environment variables PLC_IP and PLC_TOKEN. "
            "Never commit a real token. On Windows PowerShell, prefer the env vars or "
            "pass JSON params via a file: call set_wifi @params.json"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--ip", help="Device IP (or PLC_IP). In AP mode this is usually 192.168.100.1")
    parser.add_argument("--token", help="16-byte token as 32 hex chars (or PLC_TOKEN)")
    parser.add_argument("--timeout", type=float, default=5.0, help="UDP timeout in seconds")
    parser.add_argument("--retries", type=int, default=3, help="Attempts per packet")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable JSON on commands that otherwise print a summary",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="Send a miIO hello and print device id / token if exposed")
    p_discover.set_defaults(func=cmd_discover)

    p_call = sub.add_parser("call", help="Call a raw method")
    p_call.add_argument("method", help="Method name, for example get_wifi or miIO.info")
    p_call.add_argument(
        "params",
        nargs="?",
        default="[]",
        help='JSON params, or @path.json. Default: []',
    )
    p_call.add_argument(
        "--i-know",
        action="store_true",
        help="Allow miIO.restore (factory reset, opens the Wi-Fi)",
    )
    p_call.set_defaults(func=cmd_call)

    sub.add_parser("info", help="miIO.info").set_defaults(func=cmd_info)
    sub.add_parser("devinfo", help="get_devinfo (mac, ip, firmware, mode)").set_defaults(func=cmd_devinfo)

    p_get = sub.add_parser("wifi-get", help="Read Wi-Fi config; password is masked unless --show-password")
    p_get.add_argument("--show-password", action="store_true")
    p_get.set_defaults(func=cmd_wifi_get)

    p_set = sub.add_parser(
        "wifi-set",
        help="Change Wi-Fi settings. Always reads get_wifi first, merges, prints a diff, and needs --yes",
    )
    p_set.add_argument("--channel", type=int, metavar="N", help="2.4 GHz channel, or 0 for auto")
    p_set.add_argument("--bw", type=int, choices=(20, 40), help="Channel width in MHz")
    p_set.add_argument("--mgmt", choices=MGMT_VALUES, help="Security mode")
    p_set.add_argument("--ssid", help="Extended SSID (usually the home SSID plus _plus)")
    p_set.add_argument("--password", help="Wi-Fi password")
    p_set.add_argument("--hidden", type=int, choices=(0, 1), help="Hide SSID")
    p_set.add_argument("--show-password", action="store_true", help="Show the password in the diff")
    p_set.add_argument("--yes", action="store_true", help="Apply the change (otherwise dry run)")
    p_set.set_defaults(func=cmd_wifi_set)

    sub.add_parser("plc-link", help="Powerline PHY rates (avln_info + avln_stas) and a quality hint").set_defaults(
        func=cmd_plc_link
    )
    sub.add_parser("clients", help="Wi-Fi stations associated to the extender (extender_stas)").set_defaults(
        func=cmd_clients
    )

    p_prov = sub.add_parser(
        "provision",
        help="Join a home Wi-Fi from factory-reset AP mode (miIO.config_router)",
    )
    p_prov.add_argument("--ssid", required=True, help="Home network SSID to join")
    p_prov.add_argument("--password", required=True, help="Home network password")
    p_prov.add_argument("--tz", default="UTC", help="Timezone name")
    p_prov.add_argument("--cc", default="CN", help="ISO country code inside wifi_config.cc")
    p_prov.add_argument("--gmt-offset", type=int, default=0, dest="gmt_offset", help="Offset from GMT in seconds")
    p_prov.set_defaults(func=cmd_provision)

    sub.add_parser("reboot", help="Reboot the extender (miIO.reboot; firmware waits ~5 s)").set_defaults(
        func=cmd_reboot
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (MiioError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
