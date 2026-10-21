"""Find the manufacturer (mode 22) DID that carries oil temperature on your ECU.

  check  ignition on: 10-second test of the standard oil-temp routes (PID 5C, DID F45C)
  scan   engine idling: lists every DID that answers -> candidates.json
  watch  cold start, idle while it warms: logs candidates vs coolant -> watch.csv
  use    save a result to config.json, e.g.:  python3 find_oil_did.py use engine 1234
         2-byte values need a formula, e.g.:   python3 find_oil_did.py use engine 1234 "(A*256+B)/10-40"
"""
import csv
import json
import sys
import time
from pathlib import Path

from elm import ELM327

MODULES = {"engine": ("7E0", "7E8"), "cluster": ("714", "77E")}
RANGES = [(0xF400, 0xF4FF), (0x1000, 0x2FFF)]  # OBD mirror range + VAG measuring values


def check(elm):
    """Quick answer to 'does the car already report oil temp in a standard way?'"""
    bits = elm.request("7E0", "7E8", "0140")
    if bits:
        mask = int.from_bytes(bytes(bits[:4]), "big")
        listed = bool(mask & (1 << (32 - (0x5C - 0x40))))
        print(f"PID 5C listed as supported by the engine ECU: {'YES' if listed else 'no'}")
    else:
        print("PID 5C listed as supported by the engine ECU: no (no reply to 0140)")
    for label, req in (("Mode 01 PID 5C", "015C"), ("UDS DID F45C (OBD mirror)", "22F45C")):
        d = elm.request("7E0", "7E8", req, wait=1.0)
        if d:
            print(f"{label:28} answered: raw {' '.join(f'{b:02X}' for b in d)} -> A-40 = {d[0] - 40} °C")
        else:
            print(f"{label:28} no answer")
    cool = elm.request("7E0", "7E8", "0105")
    if cool:
        print(f"(coolant right now: {cool[0] - 40} °C - after a cold soak, oil should read about the same)")
    print("\nIf PID 5C answered, the gauge already shows it as oil temp. If only F45C answered:")
    print("  python3 find_oil_did.py use engine F45C")
    print("If neither answered, run the scan.")


def scan(elm):
    found = []
    for name, (tx, rx) in MODULES.items():
        for lo, hi in RANGES:
            for did in range(lo, hi + 1):
                d = elm.request(tx, rx, f"22{did:04X}", wait=0.3)
                if d:
                    found.append({"module": name, "tx": tx, "rx": rx, "did": f"{did:04X}", "raw": d})
                    print(f"{name:8} {did:04X}  {' '.join(f'{b:02X}' for b in d)}")
                if did % 256 == 0:
                    print(f"  ...{name} {did:04X}", file=sys.stderr)
    json.dump(found, open("candidates.json", "w"), indent=1)
    print(f"{len(found)} DIDs answered -> candidates.json")


def raw_int(d):
    return int.from_bytes(bytes(d), "big")


def watch(elm, minutes=15):
    if not Path("candidates.json").exists():
        sys.exit("No candidates.json here - run the scan first (from the same folder).")
    # Oil temp is a 1- or 2-byte value; anything longer is something else.
    cands = [c for c in json.load(open("candidates.json")) if 1 <= len(c["raw"]) <= 2]
    print(f"Watching {len(cands)} candidates for {minutes} min. Leave the engine idling, parked, outdoors.")
    history = {c["did"] + c["module"]: [] for c in cands}
    with open("watch.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "coolant_c", "intake_c"] + [c["module"] + ":" + c["did"] for c in cands])
        t0 = time.time()
        while time.time() - t0 < minutes * 60:
            temps = []
            for pid in ("0105", "010F"):
                d = elm.request("7E0", "7E8", pid)
                temps.append(d[0] - 40 if d else None)
            row = [round(time.time() - t0)] + temps
            for c in cands:
                d = elm.request(c["tx"], c["rx"], "22" + c["did"])
                row.append(" ".join(f"{b:02X}" for b in d) if d else "")
                if d and len(d) == len(c["raw"]):
                    history[c["did"] + c["module"]].append(d)
            w.writerow(row)
            f.flush()
            print(f"t={row[0]:4}s coolant={temps[0]}°C intake={temps[1]}°C - logged {len(cands)} candidates")
            time.sleep(2)

    # Rank: values that rose steadily while the engine warmed (oil only ever warms at idle).
    ranked = []
    for c in cands:
        h = [raw_int(d) for d in history[c["did"] + c["module"]]]
        if len(h) < 20:
            continue
        rises = sum(b >= a for a, b in zip(h, h[1:])) / (len(h) - 1)
        if h[-1] > h[0] and rises > 0.9:
            ranked.append((c, h[0], h[-1]))
    one = [r for r in ranked if len(r[0]["raw"]) == 1 and -20 < r[1] - 40 < 50 and r[2] - r[1] > 5]
    two = [r for r in ranked if len(r[0]["raw"]) == 2 and r[2] - r[1] > 10]

    print("\n1-byte candidates, shown with formula A-40. Compare with the coolant/intake columns:")
    print("at the start of a cold start, real oil temp is close to both.")
    for c, a, b in sorted(one, key=lambda r: r[1] - r[2])[:10]:
        print(f"  {c['module']:8} 22{c['did']}: {a - 40}°C -> {b - 40}°C    use: {c['module']} {c['did']}")
    if not one:
        print("  (none)")
    print("\n2-byte candidates that rose steadily (scale unknown - raw numbers only):")
    for c, a, b in sorted(two, key=lambda r: r[1] - r[2])[:10]:
        print(f"  {c['module']:8} 22{c['did']}: raw {a} -> {b}")
    if not two:
        print("  (none)")
    print("\nNothing here is verified yet. Before trusting a candidate, check it against a second")
    print("reference (cold-soak = ambient, plus a VCDS/OBDeleven or cluster reading when warm).")


def use(module, did, formula="A-40"):
    tx, rx = MODULES[module]
    path = Path(__file__).parent / "config.json"
    cfg = json.loads(path.read_text())
    cfg["oil_temp"] = {"tx": tx, "rx": rx, "request": "22" + did.upper(), "formula": formula}
    path.write_text(json.dumps(cfg, indent=2))
    print(f"Saved: {module} DID {did.upper()}, formula {formula}. Restart: sudo systemctl restart s4gauge")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("check", "scan", "watch", "use"):
        sys.exit(__doc__)
    if sys.argv[1] == "use":
        use(*sys.argv[2:])
        sys.exit()
    elm = ELM327()
    elm.init()
    {"check": check, "scan": scan, "watch": watch}[sys.argv[1]](elm)
