# Moonraker WebSocket client.
#
# Connects to Moonraker (ws://host:7125/websocket), sends G-code via JSON-RPC,
# and pushes printer state updates back via a callback as state:… strings
# matching the firmware NUS protocol defined in BLE_commands.c.
#
# Runs as a coroutine in the caller's asyncio event loop — no background thread.
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

logger = logging.getLogger(__name__)

# Printer objects we want live updates for.
_SUBSCRIBED_OBJECTS = {
    "extruder":    ["temperature", "target"],
    "heater_bed":  ["temperature", "target"],
    "fan":         ["speed"],
    "gcode_move":  ["speed_factor"],
    "print_stats": ["state", "print_duration", "filename"],
    "toolhead":    ["position", "homed_axes"],
}


class MoonrakerClient:
    # Async Moonraker WebSocket client with automatic reconnect.
    #
    # url      – full WebSocket URL, e.g. ws://localhost:7125/websocket.
    # on_state – sync callback called with a list of state:… protocol strings
    #            to forward to the remote control over BLE NUS TX.

    def __init__(
        self,
        url: str = "ws://localhost:7125/websocket",
        on_state: Callable[[list[str]], None] | None = None,
    ) -> None:
        self._url = url
        self._on_state = on_state
        self._ws = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        # Long-running coroutine — call with asyncio.create_task(). Reconnects on error.
        self._running = True
        while self._running:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Moonraker WS error: %s — retrying in 5 s", exc)
                await asyncio.sleep(5)

    async def send_gcode(self, script: str) -> bool:
        # Send a single G-code line and wait up to 5 s for Moonraker's ACK.
        if self._ws is None:
            logger.warning("Moonraker not connected, dropping: %s", script)
            return False
        req_id = self._alloc_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(json.dumps({
                "jsonrpc": "2.0",
                "method":  "printer.gcode.script",
                "params":  {"script": script},
                "id":      req_id,
            }))
            return await asyncio.wait_for(asyncio.shield(fut), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Moonraker G-code ack timed out: %s", script)
            self._pending.pop(req_id, None)
            return False

    async def _session(self) -> None:
        import websockets  # lazy import so missing dep gives a clear error

        logger.info("Connecting to Moonraker at %s", self._url)
        async with websockets.connect(self._url) as ws:
            self._ws = ws
            logger.info("Moonraker connected")
            await self._subscribe()
            async for raw in ws:
                await self._handle_message(raw)
        self._ws = None
        logger.info("Moonraker disconnected")

    async def _subscribe(self) -> None:
        req_id = self._alloc_id()
        await self._ws.send(json.dumps({
            "jsonrpc": "2.0",
            "method":  "printer.objects.subscribe",
            "params":  {"objects": _SUBSCRIBED_OBJECTS},
            "id":      req_id,
        }))
        logger.debug("Subscribed to printer objects (id=%d)", req_id)

    async def _send_gcode_coro(self, script: str) -> bool:
        if self._ws is None:
            return False
        req_id = self._alloc_id()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._ws.send(json.dumps({
                "jsonrpc": "2.0",
                "method":  "printer.gcode.script",
                "params":  {"script": script},
                "id":      req_id,
            }))
            return await asyncio.wait_for(asyncio.shield(fut), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Moonraker G-code ack timed out: %s", script)
            self._pending.pop(req_id, None)
            return False

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # JSON-RPC response (reply to our requests)
        if "id" in data:
            req_id = data["id"]
            fut = self._pending.pop(req_id, None)
            if fut is not None and not fut.done():
                if "error" in data:
                    logger.warning("Moonraker error for id=%d: %s", req_id, data["error"])
                    fut.set_result(False)
                else:
                    fut.set_result(True)
            return

        # Push notification from the subscription
        if data.get("method") == "notify_status_update":
            params = data.get("params", [{}])
            status = params[0] if params else {}
            self._process_status(status)

    def _process_status(self, status: dict) -> None:
        # Convert Moonraker status dict to firmware state:… protocol strings.
        msgs: list[str] = []

        # Temperatures
        extruder   = status.get("extruder",   {})
        heater_bed = status.get("heater_bed", {})
        temp_parts: list[str] = []
        te = extruder.get("temperature")
        tb = heater_bed.get("temperature")
        if te is not None:
            temp_parts.append(f"e:{te:.1f}")
        if tb is not None:
            temp_parts.append(f"b:{tb:.1f}")
        if temp_parts:
            msgs.append("state:temp:" + ":".join(temp_parts))

        # Toolhead position and homed axes
        toolhead = status.get("toolhead", {})
        if "position" in toolhead:
            pos = toolhead["position"]  # [x, y, z, e]
            if len(pos) >= 4:
                msgs.append(
                    f"state:pos:x:{pos[0]:.2f}:y:{pos[1]:.2f}"
                    f":z:{pos[2]:.2f}:e:{pos[3]:.2f}"
                )
        if "homed_axes" in toolhead:
            homed = toolhead["homed_axes"]  # string like "xyz"
            msgs.append(
                f"state:homed"
                f":x:{'1' if 'x' in homed else '0'}"
                f":y:{'1' if 'y' in homed else '0'}"
                f":z:{'1' if 'z' in homed else '0'}"
            )

        # Print state and can_move
        print_stats = status.get("print_stats", {})
        if "state" in print_stats:
            pstate = print_stats["state"]
            printing = pstate == "printing"
            can_move = pstate in ("standby", "complete", "error", "cancelled")
            msgs.append(f"state:printing:{'1' if printing else '0'}")
            msgs.append(f"state:can_move:{'1' if can_move else '0'}")

        if msgs and self._on_state:
            self._on_state(msgs)

    def _alloc_id(self) -> int:
        req_id = self._next_id
        self._next_id += 1
        return req_id
