#!/usr/bin/env python3
"""Auto-disconnect idle Bluetooth gamepads on Linux (SteamOS Desktop Mode).

Monitors Bluetooth evdev gamepad nodes via sysfs + raw Linux input events
using only the Python standard library, and disconnects idle devices via
BlueZ over D-Bus (busctl).
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import logging
import os
import re
import selectors
import signal
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 fallback
    tomllib = None  # type: ignore[assignment]

SYSFS_INPUT = Path("/sys/class/input")
SYSFS_BLUETOOTH = Path("/sys/class/bluetooth")
DEV_INPUT = Path("/dev/input")

BUS_BLUETOOTH = "0005"
INPUT_PROP_ACCELEROMETER = 1 << 6

# Linux input event constants (64-bit / 32-bit struct input_event)
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)
EV_KEY = 0x01
EV_ABS = 0x03

# BTN_JOYSTICK (0x120..0x12f) and BTN_GAMEPAD (0x130..0x13f)
GAMEPAD_KEY_RANGE = range(0x120, 0x140)

# EVIOCGABS(abs): _IOR('E', 0x40 + code, struct input_absinfo[24 bytes])
ABSINFO_FORMAT = "6i"
ABSINFO_SIZE = struct.calcsize(ABSINFO_FORMAT)

MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
WORD_BITS = 64 if sys.maxsize > 2**32 else 32

LOG = logging.getLogger("gamepad-sleep")


@dataclass
class MonitoredDevice:
    """State for an open Bluetooth gamepad evdev node."""

    event_name: str
    mac: str
    name: str
    fd: int
    abs_thresholds: dict[int, int] = field(default_factory=dict)
    last_abs: dict[int, int] = field(default_factory=dict)


def _read_sysfs_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def _has_any_key_bit(key_mask_hex: str, bits: range) -> bool:
    """Return True if any bit in `bits` is set in a sysfs capabilities/key bitmap."""
    if not key_mask_hex:
        return False
    # Sysfs prints words from most-significant to least-significant.
    words = [int(w, 16) for w in reversed(key_mask_hex.split())]
    for bit in bits:
        word_idx = bit // WORD_BITS
        if word_idx < len(words) and (words[word_idx] & (1 << (bit % WORD_BITS))):
            return True
    return False


def _is_bt_gamepad(event_dir: Path) -> tuple[str, str] | None:
    """Return (mac, name) if `event_dir` is a Bluetooth gamepad, else None."""
    dev_dir = event_dir / "device"
    if _read_sysfs_text(dev_dir / "id" / "bustype").lower() != BUS_BLUETOOTH:
        return None

    mac = _read_sysfs_text(dev_dir / "uniq").upper()
    if not MAC_RE.match(mac):
        return None

    props_hex = _read_sysfs_text(dev_dir / "properties")
    if props_hex and (int(props_hex, 16) & INPUT_PROP_ACCELEROMETER):
        return None

    key_mask = _read_sysfs_text(dev_dir / "capabilities" / "key")
    if not _has_any_key_bit(key_mask, GAMEPAD_KEY_RANGE):
        return None

    name = _read_sysfs_text(dev_dir / "name") or "Bluetooth Gamepad"
    return mac, name


def _query_abs_info(fd: int, abs_mask_hex: str) -> tuple[dict[int, int], dict[int, int]]:
    """Query EVIOCGABS for supported ABS axes to compute deadzones and initial anchors."""
    thresholds: dict[int, int] = {}
    anchors: dict[int, int] = {}
    if not abs_mask_hex:
        return thresholds, anchors

    words = [int(w, 16) for w in reversed(abs_mask_hex.split())]
    for word_idx, word in enumerate(words):
        for bit in range(WORD_BITS):
            if not (word & (1 << bit)):
                continue
            code = word_idx * WORD_BITS + bit
            if code >= 0x40:
                continue
            req = (2 << 30) | (ABSINFO_SIZE << 16) | (ord("E") << 8) | (0x40 + code)
            try:
                buf = fcntl.ioctl(fd, req, bytes(ABSINFO_SIZE))
                val, mn, mx, _fuzz, flat, _res = struct.unpack(ABSINFO_FORMAT, buf)
            except OSError:
                continue
            span = max(0, mx - mn)
            # 5% of axis span filters stick drift while keeping 1-step D-pads (-1..1) responsive.
            thresholds[code] = max(1, flat, span // 20)
            anchors[code] = val
    return thresholds, anchors


def _find_bt_adapters() -> list[str]:
    if not SYSFS_BLUETOOTH.exists():
        return ["hci0"]
    adapters = sorted(
        p.name for p in SYSFS_BLUETOOTH.iterdir() if p.name.startswith("hci")
    )
    return adapters or ["hci0"]


def disconnect_bluetooth_device(mac: str, dry_run: bool = False) -> bool:
    """Disconnect a Bluetooth device via BlueZ D-Bus using busctl."""
    dev_node = f"dev_{mac.replace(':', '_')}"
    if dry_run:
        LOG.info("[dry-run] Would disconnect %s", mac)
        return True

    for adapter in _find_bt_adapters():
        obj_path = f"/org/bluez/{adapter}/{dev_node}"
        cmd = [
            "busctl",
            "--system",
            "call",
            "org.bluez",
            obj_path,
            "org.bluez.Device1",
            "Disconnect",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            LOG.info("Disconnected idle gamepad %s via %s", mac, adapter)
            return True
        LOG.debug("busctl %s failed: %s", obj_path, res.stderr.strip())

    # Fallback to bluetoothctl if adapter path differed
    res = subprocess.run(
        ["bluetoothctl", "disconnect", mac],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode == 0:
        LOG.info("Disconnected idle gamepad %s via bluetoothctl", mac)
        return True

    LOG.warning("Failed to disconnect %s: %s", mac, res.stderr.strip())
    return False


class GamepadTimeoutDaemon:
    """Monitors connected Bluetooth gamepads and disconnects them after inactivity."""

    def __init__(self, timeout_seconds: float, scan_interval: float, dry_run: bool) -> None:
        self.timeout_seconds = timeout_seconds
        self.scan_interval = scan_interval
        self.dry_run = dry_run
        self.selector = selectors.DefaultSelector()
        self.devices_by_event: dict[str, MonitoredDevice] = {}
        self.last_activity_by_mac: dict[str, float] = {}
        self.cooldown_until_by_mac: dict[str, float] = {}
        self._running = True

    def stop(self) -> None:
        self._running = False

    def scan_devices(self, now: float) -> None:
        """Discover newly connected Bluetooth gamepads and register them."""
        if not SYSFS_INPUT.exists():
            return

        for entry in sorted(SYSFS_INPUT.iterdir()):
            name = entry.name
            if not name.startswith("event") or name in self.devices_by_event:
                continue

            info = _is_bt_gamepad(entry)
            if info is None:
                continue
            mac, dev_name = info

            dev_path = DEV_INPUT / name
            try:
                fd = os.open(dev_path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                LOG.debug("Cannot open %s (%s): %s", dev_path, mac, exc)
                continue

            abs_mask = _read_sysfs_text(entry / "device" / "capabilities" / "abs")
            thresholds, anchors = _query_abs_info(fd, abs_mask)
            monitored = MonitoredDevice(
                event_name=name,
                mac=mac,
                name=dev_name,
                fd=fd,
                abs_thresholds=thresholds,
                last_abs=anchors,
            )
            self.devices_by_event[name] = monitored
            self.selector.register(fd, selectors.EVENT_READ, monitored)
            self.last_activity_by_mac.setdefault(mac, now)
            LOG.info("Monitoring %s (%s) on %s", dev_name, mac, dev_path)

    def _remove_device(self, dev: MonitoredDevice) -> None:
        try:
            self.selector.unregister(dev.fd)
        except (KeyError, ValueError):
            pass
        try:
            os.close(dev.fd)
        except OSError:
            pass
        self.devices_by_event.pop(dev.event_name, None)

        still_tracked = any(d.mac == dev.mac for d in self.devices_by_event.values())
        if not still_tracked:
            self.last_activity_by_mac.pop(dev.mac, None)
            self.cooldown_until_by_mac.pop(dev.mac, None)
        LOG.info("Stopped monitoring %s (%s)", dev.name, dev.mac)

    def _handle_readable(self, dev: MonitoredDevice, now: float) -> None:
        while True:
            try:
                chunk = os.read(dev.fd, EVENT_SIZE * 32)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in (errno.ENODEV, errno.EIO, errno.EBADF):
                    self._remove_device(dev)
                else:
                    LOG.debug("Read error on %s: %s", dev.event_name, exc)
                break

            if not chunk:
                self._remove_device(dev)
                break

            usable = len(chunk) - (len(chunk) % EVENT_SIZE)
            for offset in range(0, usable, EVENT_SIZE):
                _sec, _usec, ev_type, code, value = struct.unpack_from(
                    EVENT_FORMAT, chunk, offset
                )
                if ev_type == EV_KEY:
                    self.last_activity_by_mac[dev.mac] = now
                elif ev_type == EV_ABS:
                    threshold = dev.abs_thresholds.get(code, 1)
                    prev = dev.last_abs.get(code)
                    if prev is None or abs(value - prev) >= threshold:
                        dev.last_abs[code] = value
                        self.last_activity_by_mac[dev.mac] = now

    def _check_timeouts(self, now: float) -> None:
        for mac, last_seen in list(self.last_activity_by_mac.items()):
            if now - last_seen < self.timeout_seconds:
                continue
            if now < self.cooldown_until_by_mac.get(mac, 0.0):
                continue

            LOG.info(
                "Gamepad %s idle for %.0fs (limit %.0fs), disconnecting",
                mac,
                now - last_seen,
                self.timeout_seconds,
            )
            if disconnect_bluetooth_device(mac, dry_run=self.dry_run):
                # Reset timer / apply cooldown so dry-run or slow teardown does not spam D-Bus.
                self.last_activity_by_mac[mac] = now
                self.cooldown_until_by_mac[mac] = now + 30.0
            else:
                self.cooldown_until_by_mac[mac] = now + 30.0

    def run(self) -> None:
        now = time.monotonic()
        self.scan_devices(now)
        last_scan = now

        try:
            while self._running:
                events = self.selector.select(timeout=self.scan_interval)
                now = time.monotonic()

                for key, _mask in events:
                    dev: MonitoredDevice = key.data
                    self._handle_readable(dev, now)

                if now - last_scan >= self.scan_interval:
                    self.scan_devices(now)
                    last_scan = now

                self._check_timeouts(now)
        finally:
            for dev in list(self.devices_by_event.values()):
                self._remove_device(dev)
            self.selector.close()


DEFAULT_TIMEOUT_MINUTES = 15.0
DEFAULT_SCAN_INTERVAL = 10.0


def _default_config_path() -> Path:
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return xdg_config / "gamepad-sleep" / "config.toml"


def load_config(config_path: Path) -> dict[str, float]:
    """Load timeout configuration from TOML, returning defaults if absent."""
    cfg = {
        "timeout_seconds": DEFAULT_TIMEOUT_MINUTES * 60.0,
        "scan_interval": DEFAULT_SCAN_INTERVAL,
    }
    if not config_path.is_file():
        return cfg

    if tomllib is None:
        LOG.warning("tomllib unavailable on Python < 3.11; ignoring %s", config_path)
        return cfg

    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOG.warning("Failed to parse %s (%s); using defaults", config_path, exc)
        return cfg

    if "timeout_seconds" in data:
        cfg["timeout_seconds"] = float(data["timeout_seconds"])
    elif "timeout_minutes" in data:
        cfg["timeout_seconds"] = float(data["timeout_minutes"]) * 60.0

    if "scan_interval_seconds" in data:
        cfg["scan_interval"] = float(data["scan_interval_seconds"])

    return cfg


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Disconnect idle Bluetooth gamepads after a period of inactivity."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_default_config_path(),
        help="Path to TOML config file (default: ~/.config/gamepad-sleep/config.toml).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Inactivity timeout in seconds (overrides config).",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=float,
        default=None,
        help="Inactivity timeout in minutes (overrides config).",
    )
    parser.add_argument(
        "--scan-interval",
        type=float,
        default=None,
        help="Seconds between hotplug scans and timeout checks (overrides config).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log idle timeouts without disconnecting Bluetooth devices.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cfg = load_config(args.config)
    timeout_seconds = cfg["timeout_seconds"]
    if args.timeout_minutes is not None:
        timeout_seconds = args.timeout_minutes * 60.0
    if args.timeout is not None:
        timeout_seconds = args.timeout

    scan_interval = (
        args.scan_interval if args.scan_interval is not None else cfg["scan_interval"]
    )
    if timeout_seconds <= 0 or scan_interval <= 0:
        LOG.error("timeout and scan_interval must be positive numbers")
        return 2

    daemon = GamepadTimeoutDaemon(
        timeout_seconds=timeout_seconds,
        scan_interval=scan_interval,
        dry_run=args.dry_run,
    )

    def _on_signal(_signum: int, _frame: object) -> None:
        daemon.stop()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    LOG.info(
        "Started gamepad-sleep (timeout=%.0fs, scan=%.0fs, dry_run=%s)",
        timeout_seconds,
        scan_interval,
        args.dry_run,
    )
    daemon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
