from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from bleak import BleakClient, BleakScanner

from src.protocol import ProtocolTranslator
from src.moonraker import MoonrakerClient

# Nordic UART Service UUIDs (lowercase for Bleak).
NUS_RX_CHAR_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # gateway writes here → nRF5340
NUS_TX_CHAR_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # nRF5340 notifies here → gateway

# ---------------------------------------------------------------------------
# Klipper macro defaults — edit here to change behaviour without CLI flags.
# ---------------------------------------------------------------------------
DEFAULT_LIGHT_ON   = "SET_LED LED=light WHITE=1.00 SYNC=0 TRANSMIT=1"
DEFAULT_LIGHT_OFF  = "SET_LED LED=light WHITE=0.00 SYNC=0 TRANSMIT=1"
DEFAULT_SHEET      = "SET_PRINT_SHEET NAME={sheet}"
DEFAULT_Z_OFFSET   = "SET_GCODE_OFFSET Z_ADJUST={value:.2f} MOVE=1"

# Control
DEFAULT_HOME_ALL   = "G28"
DEFAULT_HOME_X     = "G28 X"
DEFAULT_HOME_Y     = "G28 Y"
DEFAULT_HOME_Z     = "G28 Z"
DEFAULT_MOTORS_OFF = "M84"
DEFAULT_FAN        = "M106 S{value}"   # {value} = 0-255

# Temperature
DEFAULT_COOL_DOWN  = "TURN_OFF_HEATERS"

# Filament
DEFAULT_PREHEAT_PLA  = "PREHEAT_PLA"
DEFAULT_PREHEAT_PETG = "PREHEAT_PETG"
DEFAULT_LOAD_FILAMENT   = "LOAD_FILAMENT"
DEFAULT_UNLOAD_FILAMENT = "UNLOAD_FILAMENT"

# Calibration
DEFAULT_CALIB_Z           = "CALIBRATE_Z"
DEFAULT_CALIB_BED_MESH    = "M80"
DEFAULT_CALIB_FIRST_LAYER = "FIRST_LAYER_CALIBRATION"
DEFAULT_CALIB_PROBE       = "PROBE_CALIBRATE"

# MMU
DEFAULT_MMU_HOME           = "HOME_TRADRACK"
DEFAULT_MMU_RESUME         = "MMU_RESUME"
DEFAULT_MMU_LOCATE         = "LOCATE_SELECTOR lane={tool}"  # {tool} = 0-8
DEFAULT_MMU_SET_TOOL       = "T{tool}"                 # {tool} = 0-8

# Printing
DEFAULT_PRINT_PAUSE  = "PAUSE"
DEFAULT_FLOW         = "M221 S{value}"   # {value} = percent
DEFAULT_SPEED        = "M220 S{value}"   # {value} = percent

# Fake position (for testing without homing)
DEFAULT_FAKE_POSITION = "SET_KINEMATIC_POSITION X=150 Y=150 Z=10"
# ---------------------------------------------------------------------------


async def _ble_send_line(client: BleakClient, line: str) -> None:
    # Write one text line to the NUS RX characteristic in ≤20-byte chunks.
    if not client.is_connected:
        return
    data = (line + "\n").encode("utf-8")
    try:
        for offset in range(0, len(data), 20):
            await client.write_gatt_char(
                NUS_RX_CHAR_UUID, data[offset:offset + 20], response=False
            )
    except Exception as exc:
        logging.warning("BLE TX failed: %s", exc)


def _make_rx_handler(
    loop: asyncio.AbstractEventLoop,
    translator: ProtocolTranslator,
    moonraker: MoonrakerClient,
):
    # Return a Bleak notification callback that reassembles lines and dispatches commands.
    buf = [""]

    def _on_notify(_char, data: bytearray) -> None:
        buf[0] += data.decode("utf-8", errors="ignore")
        while "\n" in buf[0]:
            line, buf[0] = buf[0].split("\n", 1)
            _handle_line(line.strip())
        if buf[0]:
            _handle_line(buf[0].strip())
            buf[0] = ""

    def _handle_line(line: str) -> None:
        if not line:
            return
        try:
            gcodes = translator.translate(line)
        except ValueError as exc:
            logging.warning("Dropping unsupported command '%s': %s", line, exc)
            return
        for gcode in gcodes:
            logging.info("RX cmd -> Moonraker: %s", gcode)
            loop.create_task(moonraker.send_gcode(gcode))

    return _on_notify


async def run(args: argparse.Namespace) -> int:
    loop = asyncio.get_running_loop()

    translator = ProtocolTranslator(
        light_on_gcode=args.light_on_gcode,
        light_off_gcode=args.light_off_gcode,
        sheet_template=args.sheet_template,
        z_offset_template=args.z_offset_template,
    )

    logging.info("Scanning for '%s' (timeout %ds) ...", args.device_name, args.scan_timeout)
    device = await BleakScanner.find_device_by_name(
        args.device_name, timeout=args.scan_timeout
    )
    if device is None:
        logging.error("Device '%s' not found", args.device_name)
        return 1

    logging.info("Found %s (%s), connecting ...", device.name, device.address)
    disconnected = asyncio.Event()

    async with BleakClient(device, disconnected_callback=lambda _: disconnected.set()) as client:
        logging.info("Connected, GATT resolved")

        def on_state_update(msgs: list[str]) -> None:
            for msg in msgs:
                logging.debug("state -> BLE: %s", msg)
                loop.create_task(_ble_send_line(client, msg))

        moonraker = MoonrakerClient(url=args.moonraker_url, on_state=on_state_update)

        await client.start_notify(
            NUS_TX_CHAR_UUID,
            _make_rx_handler(loop, translator, moonraker),
        )
        logging.info("Subscribed to NUS TX notifications")

        moonraker_task = asyncio.create_task(moonraker.run())
        logging.info(
            "Gateway running — remote '%s' connected, Moonraker at %s",
            args.device_name, args.moonraker_url,
        )

        await disconnected.wait()
        logging.warning("BLE device disconnected")
        moonraker_task.cancel()

    return 0


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="BLE NUS central <-> Moonraker WebSocket gateway for RemoteTwo"
    )
    parser.add_argument(
        "--moonraker-url",
        default="ws://localhost:7125/websocket",
        help="Moonraker WebSocket URL (default: ws://localhost:7125/websocket)",
    )
    parser.add_argument("--device-name",     default="remote_two", help="BLE name of the remote control (default: remote_two)")
    parser.add_argument("--scan-timeout",    type=int, default=30, help="Seconds to scan for device before giving up")
    parser.add_argument("--light-on-gcode",  default=DEFAULT_LIGHT_ON,  help="G-code for l:on")
    parser.add_argument("--light-off-gcode", default=DEFAULT_LIGHT_OFF, help="G-code for l:off")
    parser.add_argument(
        "--sheet-template",
        default=DEFAULT_SHEET,
        help="Format string for s:<sheet>; supports {sheet}",
    )
    parser.add_argument(
        "--z-offset-template",
        default=DEFAULT_Z_OFFSET,
        help="Format string for offset:<delta>; supports {value}",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--reconnect-delay", type=int, default=5, help="Seconds to wait before reconnecting after disconnect (default: 5)")
    return parser


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    async def _main() -> int:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        def shutdown(*_) -> None:
            logging.info("Stopping gateway")
            stop.set()

        loop.add_signal_handler(signal.SIGINT,  shutdown)
        loop.add_signal_handler(signal.SIGTERM, shutdown)

        while not stop.is_set():
            try:
                await run(args)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logging.error("Unexpected error: %s", exc)

            if stop.is_set():
                break
            logging.info("Reconnecting in %ds ...", args.reconnect_delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=args.reconnect_delay)
            except asyncio.TimeoutError:
                pass

        return 0

    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
