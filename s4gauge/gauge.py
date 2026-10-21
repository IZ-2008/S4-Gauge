"""S4 gauge: polls the ECU over OBD2 and streams readings to phones over a WebSocket."""
import argparse
import asyncio
import json
import math
import random
import threading
import time
from pathlib import Path

from aiohttp import web

HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "config.json").read_text())
ENGINE_TX, ENGINE_RX = "7E0", "7E8"

# key: (mode 01 PID, label, unit, decoder over data bytes, poll interval in s)
PIDS = {
    "rpm":       ("0C", "RPM",              "rpm", lambda d: (d[0] * 256 + d[1]) / 4, 0.0),
    "speed":     ("0D", "Speed",            "km/h", lambda d: d[0], 0.0),
    "map":       ("0B", "Manifold pressure", "kPa", lambda d: d[0], 0.0),
    "throttle":  ("11", "Throttle",         "%", lambda d: d[0] * 100 / 255, 0.0),
    "pedal":     ("49", "Accel pedal",      "%", lambda d: d[0] * 100 / 255, 0.0),
    "load":      ("04", "Engine load",      "%", lambda d: d[0] * 100 / 255, 0.5),
    "timing":    ("0E", "Timing advance",   "°", lambda d: d[0] / 2 - 64, 0.5),
    "maf":       ("10", "MAF",              "g/s", lambda d: (d[0] * 256 + d[1]) / 100, 0.5),
    "coolant":   ("05", "Coolant",          "°C", lambda d: d[0] - 40, 2.0),
    "iat":       ("0F", "Intake air",       "°C", lambda d: d[0] - 40, 2.0),
    "oil_std":   ("5C", "Oil temp (std)",   "°C", lambda d: d[0] - 40, 2.0),
    "stft1":     ("06", "STFT bank 1",      "%", lambda d: d[0] * 100 / 128 - 100, 1.0),
    "ltft1":     ("07", "LTFT bank 1",      "%", lambda d: d[0] * 100 / 128 - 100, 5.0),
    "stft2":     ("08", "STFT bank 2",      "%", lambda d: d[0] * 100 / 128 - 100, 1.0),
    "ltft2":     ("09", "LTFT bank 2",      "%", lambda d: d[0] * 100 / 128 - 100, 5.0),
    "volts":     ("42", "Module voltage",   "V", lambda d: (d[0] * 256 + d[1]) / 1000, 2.0),
    "baro":      ("33", "Barometric",       "kPa", lambda d: d[0], 10.0),
    "ambient":   ("46", "Ambient air",      "°C", lambda d: d[0] - 40, 10.0),
    "fuel":      ("2F", "Fuel level",       "%", lambda d: d[0] * 100 / 255, 10.0),
    "cat_b1":    ("3C", "Cat temp B1S1",    "°C", lambda d: (d[0] * 256 + d[1]) / 10 - 40, 5.0),
    "cat_b2":    ("3D", "Cat temp B2S1",    "°C", lambda d: (d[0] * 256 + d[1]) / 10 - 40, 5.0),
    "runtime":   ("1F", "Run time",         "s", lambda d: d[0] * 256 + d[1], 5.0),
}

state = {"values": {}, "meta": {}, "status": "starting", "ts": 0}

# Oil readings older than this are removed, so the phone shows "No data" instead of a
# frozen number that looks live. Values outside the range are never shown at all.
OIL_KEYS = ("oil", "oil_std")
OIL_STALE_S = 5
OIL_RANGE_C = (-40, 170)


def drop_oil():
    for key in OIL_KEYS:
        state["values"].pop(key, None)


def formula(expr):
    """Turn a config formula like 'A-40' or '(A*256+B)/10' into a decoder."""
    code = compile(expr, "<formula>", "eval")
    names = "ABCDEFGH"
    return lambda d: eval(code, {"__builtins__": {}}, dict(zip(names, d)))


def supported_pids(elm):
    """Ask the ECU which mode 01 PIDs it answers (bitmaps at 00, 20, 40, 60)."""
    ok = set()
    for base in (0x00, 0x20, 0x40, 0x60):
        bits = elm.request(ENGINE_TX, ENGINE_RX, f"01{base:02X}")
        if not bits:
            break
        mask = int.from_bytes(bytes(bits[:4]), "big")
        for i in range(32):
            if mask & (1 << (31 - i)):
                ok.add(f"{base + i + 1:02X}")
        if f"{base + 0x20:02X}" not in ok:
            break
    return ok


def poll_loop(port):
    from elm import ELM327
    elm = None
    while True:
        try:
            state["status"] = "connecting to adapter"
            if elm:
                elm.close()
            elm = ELM327(port)
            elm.init()
            ok = supported_pids(elm)
            if not ok:
                raise RuntimeError("ECU not answering - is the ignition on?")
            active = {k: v for k, v in PIDS.items() if v[0] in ok}
            oil = CONFIG.get("oil_temp")
            oil_fn = formula(oil["formula"]) if oil and oil.get("request") else None
            state["meta"] = {k: {"label": v[1], "unit": v[2]} for k, v in active.items()}
            if oil_fn:
                state["meta"]["oil"] = {"label": "Oil temp", "unit": "°C"}
            state["status"] = f"live ({len(state['meta'])} channels)"
            last = {}
            good_at = {}  # key -> monotonic time of its last good reading
            warned = False
            while True:
                now = time.monotonic()
                for key, (pid, _, _, fn, every) in active.items():
                    if now - last.get(key, 0) >= every:
                        d = elm.request(ENGINE_TX, ENGINE_RX, "01" + pid)
                        if d:
                            value = round(fn(d), 2)
                            # An impossible oil temperature counts as no reading.
                            if key not in OIL_KEYS or OIL_RANGE_C[0] <= value <= OIL_RANGE_C[1]:
                                state["values"][key] = value
                                good_at[key] = time.monotonic()
                            state["ts"] = time.monotonic()
                        last[key] = now
                if oil_fn and now - last.get("oil", 0) >= 1.0:
                    d = elm.request(oil["tx"], oil["rx"], oil["request"])
                    if d:
                        try:
                            value = round(oil_fn(d), 1)
                        except (IndexError, ZeroDivisionError):
                            value = None  # reply shorter than the formula expects
                        if value is not None and OIL_RANGE_C[0] <= value <= OIL_RANGE_C[1]:
                            state["values"]["oil"] = value
                            good_at["oil"] = time.monotonic()
                        elif not warned:
                            print(f"oil reading {value} from raw {d} ignored (outside {OIL_RANGE_C} °C)"
                                  " - check the formula in config.json", flush=True)
                            warned = True
                    last["oil"] = now
                for key in OIL_KEYS:
                    if key in state["values"] and time.monotonic() - good_at.get(key, 0) > OIL_STALE_S:
                        del state["values"][key]
                v = state["values"]
                if "map" in v and "baro" in v:
                    v["boost"] = round((v["map"] - v["baro"]) * 0.145038, 1)  # psi
                    state["meta"].setdefault("boost", {"label": "Boost", "unit": "psi"})
                if time.monotonic() - state["ts"] > 10:
                    raise RuntimeError("no data for 10 s - ignition off?")
        except Exception as exc:  # adapter unplugged, ignition off, etc.
            drop_oil()  # never leave an old oil number on screen while reconnecting
            state["status"] = f"error: {exc} - retrying"
            time.sleep(3)


def demo_loop():
    """Fake data so the phone page can be tested without the car."""
    state["meta"] = {k: {"label": v[1], "unit": v[2]} for k, v in PIDS.items()}
    state["meta"]["oil"] = {"label": "Oil temp", "unit": "°C"}
    state["meta"]["boost"] = {"label": "Boost", "unit": "psi"}
    state["status"] = "demo"
    t0 = time.time()
    while True:
        t = time.time() - t0
        rpm = 2500 + 2000 * math.sin(t / 3)
        state["values"] = {k: round(random.uniform(0, 100), 1) for k in PIDS}
        state["values"].update(rpm=round(rpm), oil=round(min(115, 20 + t * 2), 1),
                               coolant=round(min(95, 20 + t * 3), 1), boost=round(rpm / 400 - 3, 1))
        state["ts"] = time.monotonic()
        time.sleep(0.1)


async def ws_handler(request):
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    try:
        while not ws.closed:
            await ws.send_json({
                "status": state["status"],
                "values": state["values"].copy(),  # snapshot: the poll thread keeps writing
                "meta": state["meta"].copy(),
                "age": round(time.monotonic() - state["ts"], 1),  # seconds since last ECU reply
                "oil_limits": CONFIG["oil_limits_c"],
            })
            await asyncio.sleep(0.1)  # 10 updates per second
    except ConnectionResetError:
        pass
    return ws


async def index(request):
    return web.FileResponse(HERE / "static" / "index.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="fake data, no adapter needed")
    ap.add_argument("--port", help="serial device (default: auto-detect)")
    ap.add_argument("--http", type=int, default=80)
    args = ap.parse_args()
    target = demo_loop if args.demo else lambda: poll_loop(args.port)
    threading.Thread(target=target, daemon=True).start()
    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/ws", ws_handler),
                    web.static("/static", HERE / "static")])
    web.run_app(app, port=args.http)


if __name__ == "__main__":
    main()
