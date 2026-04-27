from __future__ import annotations

import argparse  # CLI arguments for runtime configuration.
import logging   # Runtime logs for BLE/serial traffic and lifecycle events.
import re        # Parse controller text protocol tokens.
import signal    # Graceful stop on Ctrl+C or system SIGTERM.
import sys       # Process exit.

from bluezero import adapter, peripheral  # BLE GATT server primitives.

from src.serial import PrinterSerial  # Thin serial transport to printer.

NUS_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"  # Nordic UART Service
NUS_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # App writes G-code here

# Global serial instance created in main(), used in BLE callback.
_SERIAL: PrinterSerial | None = None
# Accumulates BLE chunks until a full line arrives.
_RX_BUFFER = ""

_PROTOCOL_RE = re.compile(r"^([A-Z]{2}):\s*(-?\d+(?:\.\d+)?)$")


def _translate_to_gcode(raw_line: str) -> str | None:
	"""Translate controller text commands to printer G-code.

	Supported controller commands:
	- PX/PY/PZ/PE:<value> -> G1 X/Y/Z/E<value>
	- TE:<value>          -> M104 S<value>
	- TB:<value>          -> M140 S<value>
	"""
	line = raw_line.strip()
	if not line:
		return None

	match = _PROTOCOL_RE.match(line)
	if not match:
		# Keep compatibility with raw-G-code tools (for example nRF Connect tests).
		if line[0].upper() in ("G", "M"):
			return line
		logging.warning("Dropping unsupported controller command: %s", line)
		return None

	token, raw_value = match.groups()
	value = float(raw_value)

	if token == "PX":
		return f"G1 X{value:.1f}"
	if token == "PY":
		return f"G1 Y{value:.1f}"
	if token == "PZ":
		return f"G1 Z{value:.1f}"
	if token == "PE":
		return f"G1 E{value:.1f}"
	if token == "TE":
		return f"M104 S{value:.1f}"
	if token == "TB":
		return f"M140 S{value:.1f}"

	logging.warning("Dropping unknown protocol token: %s", token)
	return None


def _send(line: str) -> None:
	"""Translate incoming message and forward one G-code line to the printer."""
	gcode = _translate_to_gcode(line)
	if gcode and _SERIAL is not None:
		logging.info("TX -> printer: %s", gcode)
		_SERIAL.send_gcode(gcode)


def _on_rx_write(value, _options) -> None:
	"""Handle BLE writes on NUS RX and translate controller payloads."""
	global _RX_BUFFER

	_RX_BUFFER += bytes(value).decode("utf-8", errors="ignore")

	# Process all complete lines.
	while "\n" in _RX_BUFFER:
		line, _RX_BUFFER = _RX_BUFFER.split("\n", 1)
		_send(line)

	# nRF Connect sends writes without a trailing newline — forward them immediately.
	if _RX_BUFFER:
		_send(_RX_BUFFER)
		_RX_BUFFER = ""


def _build_parser() -> argparse.ArgumentParser:
	"""Define command-line options for the gateway process."""
	parser = argparse.ArgumentParser(
		description="Minimal BLE NUS -> Serial passthrough for raw G-code testing"
	)
	parser.add_argument("--serial-port", required=True, help="Printer serial port, e.g. /dev/ttyUSB0")
	parser.add_argument("--baudrate", type=int, default=115200, help="Serial baudrate")
	parser.add_argument("--adapter", default="hci0", help="Bluetooth adapter name")
	parser.add_argument("--name", default="RTwo-NUS-Bridge", help="BLE advertised device name")
	parser.add_argument("--debug", action="store_true", help="Enable debug logging")
	return parser


def main() -> int:
	"""Initialize BLE + serial bridge and keep serving until interrupted."""
	global _SERIAL

	args = _build_parser().parse_args()
	logging.basicConfig(
		level=logging.DEBUG if args.debug else logging.INFO,
		format="%(asctime)s %(levelname)s %(message)s",
	)

	# Discover available local BLE adapters (for example hci0).
	bt_adapter = adapter.Adapter.available()
	if not bt_adapter:
		logging.error("No Bluetooth adapter found")
		return 1

	adapter_address = None
	for ad in bt_adapter:
		if ad.name == args.adapter:
			adapter_address = ad.address
			break

	if adapter_address is None:
		logging.error("Adapter %s was not found", args.adapter)
		return 1

	# Open serial link to the printer.
	_SERIAL = PrinterSerial(port=args.serial_port, baudrate=args.baudrate)

	# Create BLE peripheral that advertises the configured local name.
	nus = peripheral.Peripheral(adapter_address, local_name=args.name)
	nus.add_service(srv_id=1, uuid=NUS_SERVICE_UUID, primary=True)

	nus.add_characteristic(
		srv_id=1,
		chr_id=1,
		uuid=NUS_RX_CHAR_UUID,
		value=[],
		notifying=False,
		flags=["write", "write-without-response"],
		read_callback=None,
		write_callback=_on_rx_write,
		notify_callback=None,
	)

	def _shutdown(*_args) -> None:
		"""Unpublish BLE service and close serial port on process stop."""
		logging.info("Stopping gateway")
		try:
			nus.unpublish()
		finally:
			if _SERIAL is not None:
				_SERIAL.close()
		sys.exit(0)

	signal.signal(signal.SIGINT, _shutdown)
	signal.signal(signal.SIGTERM, _shutdown)

	logging.info("Starting BLE NUS gateway as '%s'", args.name)
	logging.info("Forwarding controller protocol as G-code to %s @ %d", args.serial_port, args.baudrate)
	# This call starts the BLE event loop and blocks until shutdown.
	nus.publish()
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
