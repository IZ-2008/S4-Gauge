"""Find the manufacturer (mode 22) DID that carries oil temperature on your ECU.

  scan   engine idling: lists every DID that answers -> candidates.json
  watch  cold start, idle while it warms: logs candidates vs coolant -> watch.csv
  use    save a result to config.json, e.g.:  python3 find_oil_did.py use engine 1234
"""
import csv
import json
import sys
import time
from pathlib import Path

from elm import ELM327

MODULES = {"engine": ("7E0", "7E8"), "cluster": ("714", "77E")}
RANGES = [(0xF400, 0xF4FF), (0x1000, 0x2FFF)]  # OBD mirror range + VAG measuring values


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


def watch(elm, minutes=15):
    # Oil temp is a 1- or 2-byte value; anything longer is something else.
    cands = [c for c in json.load(open("candidates.json")) if len(c["raw"]) <= 2]
    history = {c["did"] + c["module"]: [] for c in cands}
    with open("watch.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "coolant_c"] + [c["module"] + ":" + c["did"] for c in cands])
        t0 = time.time()
        while time.time() - t0 < minutes * 60:
            cool = elm.request("7E0", "7E8", "0105")
            cool_c = cool[0] - 40 if cool else None
            row = [round(time.time() - t0), cool_c]
            for c in cands:
                d = elm.request(c["tx"], c["rx"], "22" + c["did"])
                row.append(" ".join(f"{b:02X}" for b in d) if d else "")
                if d:
                    history[c["did"] + c["module"]].append(d)
            w.writerow(row)
            f.flush()
            print(f"t={row[0]:4}s coolant={cool_c}°C  - logged {len(cands)} candidates")
            time.sleep(2)
    # Rank: single-byte values whose A-40 started near ambient and rose steadily.
    ranked = []
    for c in cands:
        h = history[c["did"] + c["module"]]
        if len(h) < 5 or len(h[0]) != 1:
            continue
        first, last = h[0][0] - 40, h[-1][0] - 40
        rises = sum(b[0] >= a[0] for a, b in zip(h, h[1:])) / (len(h) - 1)
        if -20 < first < 50 and last - first > 5 and rises > 0.9:
            ranked.append((last - first, c, first, last))
    print("\nLikely oil-temp DIDs (formula A-40). Compare with the car's own oil-temp display:")
    for rise, c, first, last in sorted(ranked, key=lambda r: -r[0])[:10]:
        print(f"  {c['module']:8} tx={c['tx']} rx={c['rx']} 22{c['did']}: {first}°C -> {last}°C")


def use(module, did, formula="A-40"):
    tx, rx = MODULES[module]
    path = Path(__file__).parent / "config.json"
    cfg = json.loads(path.read_text())
    cfg["oil_temp"] = {"tx": tx, "rx": rx, "request": "22" + did.upper(), "formula": formula}
    path.write_text(json.dumps(cfg, indent=2))
    print(f"Saved: {module} DID {did.upper()}, formula {formula}. Restart: sudo systemctl restart s4gauge")


if __name__ == "__main__":
    if sys.argv[1] == "use":
        use(*sys.argv[2:])
        sys.exit()
    elm = ELM327()
    elm.init()
    {"scan": scan, "watch": watch}[sys.argv[1]](elm)
