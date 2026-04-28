from __future__ import annotations

from dataclasses import dataclass

# Wire protocol used by the RemoteTwo firmware (BLE NUS):
#
#   Framed command:  cmd:<id>:<payload>
#     id      – 16-bit sequence number used for ACK matching; stripped here.
#     payload – colon-separated command string with arguments, e.g. mv:x:10.5


def _format_number(value: float, decimals: int = 3) -> str:
	# Stringify a float without trailing zeros (e.g. 1.500 -> '1.5', 2.000 -> '2').
	text = f"{value:.{decimals}f}"
	text = text.rstrip("0").rstrip(".")
	if text == "-0":
		return "0"
	return text


@dataclass
class ProtocolTranslator:
	# Translate RemoteTwo controller commands into one or more printer G-code lines.

	# G-code / Klipper macro executed when the remote sends l:on / l:off.
	light_on_gcode: str = "LIGHT_ON"
	light_off_gcode: str = "LIGHT_OFF"
	# Python format strings; {sheet} and {value} are substituted at translation time.
	sheet_template: str = "SET_PRINT_SHEET NAME={sheet}"
	z_offset_template: str = "SET_GCODE_OFFSET Z_ADJUST={value:.2f} MOVE=1"

	def translate(self, raw_line: str) -> list[str]:
		# Parse one NUS line and return the G-code lines to send to Moonraker.
		line = raw_line.strip()
		if not line:
			return []

		payload = self._extract_payload(line)
		if payload is None:
			return self._translate_passthrough(line)

		return self._translate_payload(payload)

	@staticmethod
	def _extract_payload(line: str) -> str | None:
		# Strip the cmd:<id>: frame and return the payload, or return the line unchanged
		# if it is not framed (raw G-code passthrough path).
		if not line.startswith("cmd:"):
			return line

		# Expected format: cmd:<id>:<payload>
		parts = line.split(":", 2)
		if len(parts) != 3 or not parts[1]:
			raise ValueError(f"Invalid framed command: {line}")
		return parts[2].strip()

	@staticmethod
	def _translate_passthrough(line: str) -> list[str]:
		# Allow raw G-code and Klipper macros to pass through without modification.
		upper = line.upper()
		if upper.startswith(("G", "M", "T")) or line.startswith(("SET_", "LIGHT_")):
			return [line]
		raise ValueError(f"Unsupported command: {line}")

	def _translate_payload(self, payload: str) -> list[str]:
		# Dispatch a decoded payload string to the appropriate translator method.
		parts = payload.split(":")
		if not parts:
			return []

		command = parts[0]
		if command == "mv" and len(parts) == 3:
			return self._translate_move(parts[1], parts[2])
		if command == "tp" and len(parts) == 3:
			return self._translate_temperature(parts[1], parts[2])
		if command == "t" and len(parts) == 2:
			return self._translate_tool(parts[1])
		if command == "l" and len(parts) == 2:
			return self._translate_light(parts[1])
		if command == "offset" and len(parts) == 2:
			return self._translate_offset(parts[1])
		if command == "s" and len(parts) == 2:
			return self._translate_sheet(parts[1])

		raise ValueError(f"Unsupported payload: {payload}")

	@staticmethod
	def _translate_move(axis_name: str, raw_value: str) -> list[str]:
		# Convert mv:<axis>:<mm> to a relative-mode G1 move sequence.
		# XYZ axes: G91 (relative) → G1 <Axis><mm> → G90 (back to absolute).
		# E axis:   M83 (relative extruder) → G1 E<mm>  (no mode restore needed).
		axis = axis_name.lower()
		if axis not in {"x", "y", "z", "e"}:
			raise ValueError(f"Unsupported move axis: {axis_name}")

		value = float(raw_value)
		formatted_value = _format_number(value)
		if axis == "e":
			return ["M83", f"G1 E{formatted_value}"]
		return ["G91", f"G1 {axis.upper()}{formatted_value}", "G90"]

	@staticmethod
	def _translate_temperature(target: str, raw_value: str) -> list[str]:
		# Convert tp:<target>:<°C> to M104 (extruder) or M140 (bed) — non-blocking.
		value = _format_number(float(raw_value), decimals=1)
		if target == "e":
			return [f"M104 S{value}"]
		if target == "b":
			return [f"M140 S{value}"]
		raise ValueError(f"Unsupported temperature target: {target}")

	@staticmethod
	def _translate_tool(raw_tool: str) -> list[str]:
		# Convert t:<n> to Tn tool-change command.
		tool = int(raw_tool)
		if tool < 0:
			raise ValueError(f"Invalid tool index: {raw_tool}")
		return [f"T{tool}"]

	def _translate_light(self, state: str) -> list[str]:
		# Convert l:on / l:off to the configured light G-code macro.
		if state == "on":
			return [self.light_on_gcode]
		if state == "off":
			return [self.light_off_gcode]
		raise ValueError(f"Unsupported light state: {state}")

	def _translate_offset(self, raw_value: str) -> list[str]:
		# Convert offset:<delta_mm> to the configured Z-offset Klipper macro.
		value = float(raw_value)
		return [self.z_offset_template.format(value=value)]

	def _translate_sheet(self, sheet_name: str) -> list[str]:
		# Convert s:<name> to the configured print-sheet selection macro.
		return [self.sheet_template.format(sheet=sheet_name)]