from __future__ import annotations

# pyserial library for UART/USB serial communication.
import serial


class PrinterSerial:
	"""Thin wrapper around pyserial for sending G-code lines to the printer."""

	def __init__(self, port: str, baudrate: int = 115200) -> None:
		"""Open serial connection to printer."""
		self._serial = serial.Serial(port=port, baudrate=baudrate)

	def send_gcode(self, line: str) -> None:
		"""Send one G-code line. Appends newline if missing, ignores empty input."""
		msg = line.strip()
		if not msg:
			return
		self._serial.write((msg + "\n").encode())
		self._serial.flush()

	def close(self) -> None:
		"""Close serial port if open."""
		if self._serial.is_open:
			self._serial.close()
