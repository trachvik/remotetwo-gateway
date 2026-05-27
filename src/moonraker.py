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
        self._on_state = on_state        # callback to forward state strings to the BLE side
        self._ws = None                  # active WebSocket connection (None when disconnected)
        self._next_id = 1                # counter for JSON-RPC request IDs
        self._pending: dict[int, asyncio.Future] = {}  # id -> Future waiting for ACK
        self._running = False
        # Cached printer state — kept between partial updates so we always have
        # a complete picture to send to the remote control.
        self._temp_cache: dict[str, float] = {}  # 'e' and 'b' temperatures
        self._fan_pct: float | None = None        # fan speed 0–100 %
        self._printing: bool = False              # True while a print job is running
        self._can_move: bool = True               # allow movement (False while printing)
        self._homed: str | None = None            # Klipper homed_axes string, e.g. 'xyz'

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        # Main loop: connect to Moonraker, handle messages, reconnect on error.
        # Call this with asyncio.create_task() — it runs until cancelled.
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

    async def send_gcode_nowait(self, script: str) -> None:
        # Fire-and-forget: send G-code to Moonraker without waiting for ACK.
        # Use for rapid real-time commands (jogging) where latency matters more
        # than delivery confirmation.
        if self._ws is None:
            logger.debug("Moonraker not connected, dropping nowait: %s", script)
            return
        try:
            await self._ws.send(json.dumps({
                "jsonrpc": "2.0",
                "method":  "printer.gcode.script",
                "params":  {"script": script},
                "id":      self._alloc_id(),
            }))
        except Exception as exc:
            logger.debug("Moonraker nowait send error: %s", exc)

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

        # Explicit initial query — ensures we have the current state even if
        # the subscription response omits unchanged fields.
        req_id = self._alloc_id()
        await self._ws.send(json.dumps({
            "jsonrpc": "2.0",
            "method":  "printer.objects.query",
            "params":  {"objects": _SUBSCRIBED_OBJECTS},
            "id":      req_id,
        }))
        logger.debug("Initial state query (id=%d)", req_id)

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
            # Process initial full state from subscription response
            result = data.get("result", {})
            if isinstance(result, dict) and "status" in result:
                self._process_status(result["status"])
            return

        # Push notification from the subscription
        if data.get("method") == "notify_status_update":
            params = data.get("params", [{}])
            status = params[0] if params else {}
            self._process_status(status)

    def _process_status(self, status: dict) -> None:
        # Called for every status update from Moonraker (both the initial full
        # snapshot and incremental notifications). Converts the relevant fields
        # into 'state:...' protocol strings and forwards them to the BLE side.
        #
        # Not every update contains every field — Moonraker only sends fields
        # that changed. We use instance variables as a cache so we can always
        # produce a complete state message from partial data.
        msgs: list[str] = []

        # Temperatures — merge into cache so partial updates don't lose previous values
        te = status.get("extruder",   {}).get("temperature")
        tb = status.get("heater_bed", {}).get("temperature")
        if te is not None:
            self._temp_cache["e"] = te
            logger.info("temp extruder=%.1f", te)
        if tb is not None:
            self._temp_cache["b"] = tb
            logger.info("temp bed=%.1f", tb)
        else:
            logger.debug("heater_bed not in this update (cache b=%.1f)",
                         self._temp_cache.get("b", 0.0))
        # Send extruder and bed as SEPARATE messages to stay within 20-byte BLE MTU.
        # A combined message like 'state:temp:e:100.0:b:100.0' is 28 bytes and would
        # be split across two ATT packets, causing the second value to be lost.
        if "e" in self._temp_cache:
            msgs.append(f"state:temp:e:{self._temp_cache['e']:.1f}")
        if "b" in self._temp_cache:
            msgs.append(f"state:temp:b:{self._temp_cache['b']:.1f}")

        # Fan speed (Moonraker: 0.0–1.0 → firmware: 0–100 %)
        fan = status.get("fan", {})
        if "speed" in fan:
            self._fan_pct = fan["speed"] * 100.0
            logger.info("fan speed=%.1f%%", self._fan_pct)
        if self._fan_pct is not None:
            msgs.append(f"state:fan:{self._fan_pct:.1f}")

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
            self._homed = toolhead["homed_axes"]
            logger.debug("homed_axes=%r", self._homed)
        if self._homed is not None:
            homed = self._homed
            # Build per-axis flags: '1' if the axis appears in the homed string, else '0'.
            x = '1' if 'x' in homed else '0'
            y = '1' if 'y' in homed else '0'
            z = '1' if 'z' in homed else '0'
            msgs.append(f"state:homed:x:{x}:y:{y}:z:{z}")

        # Print state
        print_stats = status.get("print_stats", {})
        if "state" in print_stats:
            pstate = print_stats["state"]
            self._printing = (pstate == "printing")
            logger.info("print_stats.state=%r → printing=%s", pstate, self._printing)
            msgs.append(f"state:printing:{'1' if self._printing else '0'}")

        # can_move: allow movement whenever not actively printing.
        # Klipper itself enforces homing requirements and will error if axes
        # are not homed — no need to duplicate that logic here.
        new_can_move = not self._printing
        if new_can_move != self._can_move:
            logger.info("can_move changed: %s → %s (printing=%s)",
                        self._can_move, new_can_move, self._printing)
            self._can_move = new_can_move
        msgs.append(f"state:can_move:{'1' if self._can_move else '0'}")

        if msgs and self._on_state:
            self._on_state(msgs)

    def _alloc_id(self) -> int:
        req_id = self._next_id
        self._next_id += 1
        return req_id
