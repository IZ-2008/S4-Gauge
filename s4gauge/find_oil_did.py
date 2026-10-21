"""Find the manufacturer (mode 22) DID that carries oil temperature on your ECU.

  check      ignition on, ~15 s: standard oil routes (PID 5C, DID F45C) and whether the
             engine / cluster accept manufacturer requests at all (tells you if a scan can work)
  scan       engine idling: lists every DID that answers -> candidates.json
             scan engine   = engine only (skip the cluster if check says it doesn't answer)
  watch      cold start, idle while it warms: logs candidates vs coolant -> watch.csv, then ranks
  rank       re-rank an existing watch.csv (no car needed):  python3 find_oil_did.py rank watch.csv
  read       live raw value of one DID, to note a warm reference:  read engine 1234
  calibrate  formula from two known points (raw hex, °C):  calibrate 0A1B 8 0C3C 95
  use        save a result to config.json:  use engine 1234            (1-byte, formula A-40)
                                            use engine 1234 "<formula>" (2-byte: from calibrate)
"""
import csv
import json
import sys
import time
from pathlib import Path

MODULES = {"engine": ("7E0", "7E8"), "cluster": ("714", "77E")}
RANGES = [(0xF400, 0xF4FF), (0x1000, 0x2FFF)]  # OBD mirror range + VAG measuring values
HEX = set("0123456789ABCDEF")
NRC = {0x11: "service not supported", 0x12: "sub-function not supported",
       0x13: "wrong message length", 0x22: "conditions not correct", 0x31: "DID not supported",
       0x33: "security access denied", 0x78: "response pending",
       0x7F: "service not supported in this session"}


def hexs(d):
    return " ".join(f"{b:02X}" for b in d)


def raw_int(d):
    return int.from_bytes(bytes(d), "big")


# ---------------------------------------------------------------------- check

def raw_reply(elm, tx, rx, req):
    """Send a request WITHOUT the single-reply shortcut and classify what came back.

    Unlike elm.request(), this understands multi-frame replies (a VIN is 17 characters,
    too long for one CAN frame), so 'no answer' really means no answer.
    Returns (kind, detail) with kind in: yes, refused, no answer, unclear.
    """
    elm.set_header(tx)
    lines = elm.cmd(req, wait=1.5)
    frames = [l.split() for l in lines]
    frames = [f for f in frames if len(f) >= 3 and f[0] == rx and all(set(t) <= HEX for t in f)]
    if not frames:
        return "no answer", " / ".join(lines) or "nothing"
    first = frames[0]
    pci = int(first[1], 16)
    if pci >> 4 == 1:  # first frame of a multi-frame reply: 1L LL, data follows
        length = ((pci & 0x0F) << 8) | int(first[2], 16)
        tokens = first[3:] + [t for f in frames[1:] for t in f[2:]]
    else:
        length, tokens = pci, first[2:]
    data = [int(t, 16) for t in tokens[:length]]
    service = int(req[:2], 16)
    if data and data[0] == service + 0x40:
        text = bytes(data[3:]).decode("ascii", "replace").strip()
        return "yes", text + ("" if len(data) == length else "  (reply cut short by adapter)")
    if len(data) >= 3 and data[0] == 0x7F:
        return "refused", f"7F {data[2]:02X} = {NRC.get(data[2], 'negative response')}"
    return "unclear", hexs(data)


def check(elm):
    """Quick answers: is oil temp already standard, and can a scan work at all?"""
    print("== Standard routes ==")
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
            print(f"{label:28} answered: raw {hexs(d)} -> A-40 = {d[0] - 40} °C")
        else:
            print(f"{label:28} no answer")
    temps = [elm.request("7E0", "7E8", p) for p in ("0105", "010F")]
    if temps[0]:
        print(f"(coolant now {temps[0][0] - 40} °C, intake {temps[1][0] - 40 if temps[1] else '?'} °C"
              " - after a cold soak, oil should read about the same)")

    print("\n== Do the modules accept manufacturer requests (UDS service 22)? ==")
    verdict = {}
    for module, did, label in (("engine", "F190", "VIN"), ("engine", "F187", "part number"),
                               ("cluster", "F190", "VIN")):
        tx, rx = MODULES[module]
        kind, detail = raw_reply(elm, tx, rx, "22" + did)
        print(f"{module:8} 22{did} ({label:11}) -> {kind}: {detail}")
        # Any reply to service 22 proves the module speaks it, except 'service not supported'.
        speaks = kind == "yes" or (kind == "refused" and not detail.startswith(("7F 11", "7F 7F")))
        verdict[module] = verdict.get(module, False) or speaks

    print("\n== What to do next ==")
    if not any(verdict.values()):
        print("Neither module accepts manufacturer requests here, so a scan would find nothing.")
        print("Stop and keep this output - the next step is a different route, not a 45-minute scan.")
    elif verdict["engine"] and not verdict["cluster"]:
        print("Engine yes, cluster no:  python3 find_oil_did.py scan engine   (skips ~35 min of cluster timeouts)")
    else:
        print("Run the full scan:  python3 find_oil_did.py scan")
    print("If PID 5C answered above, the gauge already shows it as oil temp. If only F45C answered:")
    print("  python3 find_oil_did.py use engine F45C")


# ---------------------------------------------------------------------- scan

def scan(elm, only=None):
    found = []
    modules = {only: MODULES[only]} if only else MODULES
    for name, (tx, rx) in modules.items():
        for lo, hi in RANGES:
            for did in range(lo, hi + 1):
                d = elm.request(tx, rx, f"22{did:04X}", wait=0.3)
                if d:
                    found.append({"module": name, "tx": tx, "rx": rx, "did": f"{did:04X}", "raw": d})
                    print(f"{name:8} {did:04X}  {hexs(d)}")
                if did % 256 == 0:
                    print(f"  ...{name} {did:04X}", file=sys.stderr)
    json.dump(found, open("candidates.json", "w"), indent=1)
    print(f"{len(found)} DIDs answered -> candidates.json")


# ---------------------------------------------------------------------- watch + rank

def watch(elm, minutes=15):
    minutes = float(minutes)
    if not Path("candidates.json").exists():
        sys.exit("No candidates.json here - run the scan first (from the same folder).")
    # Oil temp is a 1- or 2-byte value; anything longer is something else.
    cands = [c for c in json.load(open("candidates.json")) if 1 <= len(c["raw"]) <= 2]
    print(f"Watching {len(cands)} candidates for {minutes} min. Leave the engine idling, parked, outdoors.")
    with open("watch.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "coolant_c", "intake_c"] + [c["module"] + ":" + c["did"] for c in cands])
        t0 = time.time()
        while time.time() - t0 < minutes * 60:
            temps = []
            for pid in ("0105", "010F"):
                d = elm.request("7E0", "7E8", pid)
                temps.append(d[0] - 40 if d else "")
            row = [round(time.time() - t0)] + temps
            for c in cands:
                d = elm.request(c["tx"], c["rx"], "22" + c["did"])
                row.append(hexs(d) if d else "")
            w.writerow(row)
            f.flush()
            print(f"t={row[0]:4}s coolant={temps[0]}°C intake={temps[1]}°C - logged {len(cands)} candidates")
            time.sleep(2)
    rank("watch.csv")


def pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / (sxx * syy) ** 0.5 if sxx and syy else 0.0


def score_column(rows, col):
    """Judge one candidate column of watch.csv. Returns a result dict, or None if it can't be oil."""
    pts = []
    for r in rows:
        raw = r[col].split()
        if raw and all(set(t) <= HEX for t in raw):
            pts.append((float(r["t"]), [int(t, 16) for t in raw], r.get("coolant_c", "")))
    if len(pts) < 20:
        return None
    nbytes = len(pts[0][1])
    if nbytes > 2 or any(len(p[1]) != nbytes for p in pts):
        return None
    vals = [raw_int(p[1]) for p in pts]
    if vals[-1] <= vals[0]:
        return None  # oil only warms up at idle after a cold start
    steps = list(zip(vals, vals[1:]))
    steady = sum(b >= a for a, b in steps) / len(steps)
    if steady < 0.9:
        return None  # jumps up and down: not a slowly warming fluid
    pairs = [(v, float(p[2])) for v, p in zip(vals, pts) if p[2] not in ("", "None")]
    if len(pairs) < 10:
        return None
    r = pearson([v for v, _ in pairs], [c for _, c in pairs])
    if r < 0.8:
        return None  # does not follow the engine warming up

    notes = []
    if r > 0.995:
        notes.append("moves exactly like coolant - could be a coolant copy, check in testing")
    # A timer counts at a constant rate; a warming fluid slows down as it nears temperature.
    t0, v0 = pts[0][0], vals[0]
    rates = [(v - v0) / (p[0] - t0) for v, p in zip(vals, pts) if p[0] - t0 >= 60]
    if len(rates) >= 5 and max(rates) - min(rates) <= 0.03 * max(rates):
        notes.append("rises in a perfectly straight line - could be a timer/counter")
    res = {"col": col, "bytes": nbytes, "first": vals[0], "last": vals[-1], "r": r,
           "first_hex": hexs(pts[0][1]).replace(" ", ""), "last_hex": hexs(pts[-1][1]).replace(" ", ""),
           "first_cool": pts[0][2], "last_cool": pts[-1][2], "notes": notes}
    if nbytes == 1:
        if all(v - 40 == c for v, c in pairs):
            notes.append("A-40 is identical to coolant every sample - almost certainly a coolant copy")
        if not -20 < vals[0] - 40 < 50:
            notes.append("A-40 does not start near ambient - may use a different formula")
    return res


def rank(path="watch.csv", top=10):
    """Rank watch.csv columns: steady warm-up that follows coolant, best first."""
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"{path} has no data rows.")
    cols = [c for c in rows[0] if ":" in c]
    results = [r for r in (score_column(rows, c) for c in cols) if r]
    results.sort(key=lambda r: (len(r["notes"]) > 0, -r["r"]))
    print(f"\n{len(cols)} candidates in {path}, {len(results)} behave like a warming fluid.")
    print(f"Coolant went {rows[0].get('coolant_c', '?')} -> {rows[-1].get('coolant_c', '?')} °C, "
          f"intake started at {rows[0].get('intake_c', '?')} °C.")
    for nb, title in ((1, "1-byte candidates (shown with formula A-40)"),
                      (2, "2-byte candidates (scale unknown - use 'calibrate')")):
        print(f"\n{title}:")
        group = [r for r in results if r["bytes"] == nb][:top]
        for r in group:
            module, did = r["col"].split(":")
            if nb == 1:
                span = f"{r['first'] - 40}°C -> {r['last'] - 40}°C"
            else:
                span = f"raw {r['first_hex']} ({r['first']}) -> {r['last_hex']} ({r['last']})"
            print(f"  {module:8} 22{did}  {span:38} follows coolant r={r['r']:.3f}")
            for n in r["notes"]:
                print(f"      ! {n}")
        if not group:
            print("  (none)")
    print("\nBest bets are near the top with no '!' notes. Nothing here is verified yet:")
    print("for a 2-byte value, note its raw hex at a cold start (= outside temperature) and when warm")
    print("(= cluster/VCDS reading), then:  python3 find_oil_did.py calibrate RAW1 TEMP1 RAW2 TEMP2")


# ---------------------------------------------------------------------- read / calibrate / use

def formula(expr):
    code = compile(expr, "<formula>", "eval")
    return lambda d: eval(code, {"__builtins__": {}}, dict(zip("ABCDEFGH", d)))


def read(elm, module, did, expr=None, minutes=5):
    """Print one DID live next to coolant and intake, e.g. to note a warm reference reading."""
    tx, rx = MODULES[module]
    minutes = float(minutes)
    fn = formula(expr) if expr else None
    print(f"Reading {module} 22{did.upper()} every 2 s for {minutes} min (Ctrl-C to stop).")
    t0 = time.time()
    while time.time() - t0 < minutes * 60:
        d = elm.request(tx, rx, "22" + did.upper())
        cool = elm.request("7E0", "7E8", "0105")
        iat = elm.request("7E0", "7E8", "010F")
        shown = ("no answer" if not d else
                 f"raw {hexs(d).replace(' ', '')}" + (f" -> {fn(d):.1f} °C" if fn else
                                                      f" (A-40 = {d[0] - 40} °C)" if len(d) == 1 else ""))
        print(f"t={time.time() - t0:5.0f}s  {shown:32} coolant {cool[0] - 40 if cool else '?'} °C"
              f"  intake {iat[0] - 40 if iat else '?'} °C")
        time.sleep(2)


def calibrate(raw1, temp1, raw2, temp2):
    """Straight line through two (raw, °C) points -> a formula for config.json."""
    raw1, raw2 = raw1.replace(" ", "").upper(), raw2.replace(" ", "").upper()
    if len(raw1) != len(raw2) or len(raw1) not in (2, 4) or not set(raw1 + raw2) <= HEX:
        sys.exit("Give both raw values as hex of the same length, e.g. 0A1B and 0C3C (2-byte) or 5A and 8C.")
    r1, r2, t1, t2 = int(raw1, 16), int(raw2, 16), float(temp1), float(temp2)
    if r1 == r2 or abs(t2 - t1) < 30:
        sys.exit("Need two clearly different points, at least 30 °C apart (cold start and fully warm).")
    scale = (t2 - t1) / (r2 - r1)
    offset = t1 - scale * r1
    term = "A" if len(raw1) == 2 else "(A*256+B)"
    expr = f"{term}*{scale:.6g}{offset:+.6g}"
    print(f"Formula: {expr}")
    print(f"  check: raw {raw1} -> {formula(expr)(list(bytes.fromhex(raw1))):.1f} °C,"
          f" raw {raw2} -> {formula(expr)(list(bytes.fromhex(raw2))):.1f} °C")
    if scale < 0:
        print("  ! scale is negative (value goes DOWN as it warms) - unusual for a temperature, double-check")
    print("Save it with:  python3 find_oil_did.py use engine <DID> \"" + expr + "\"")
    print("Two points only fit a straight line - confirm at a third temperature before trusting it.")


def use(module, did, expr="A-40"):
    tx, rx = MODULES[module]
    formula(expr)([0] * 8)  # fail now, not in the gauge, if the formula is mistyped
    path = Path(__file__).parent / "config.json"
    cfg = json.loads(path.read_text())
    cfg["oil_temp"] = {"tx": tx, "rx": rx, "request": "22" + did.upper(), "formula": expr}
    path.write_text(json.dumps(cfg, indent=2))
    print(f"Saved: {module} DID {did.upper()}, formula {expr}. Restart: sudo systemctl restart s4gauge")


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "use":
        use(*args)
    elif cmd == "rank":
        rank(*args)
    elif cmd == "calibrate" and len(args) == 4:
        calibrate(*args)
    elif cmd in ("check", "scan", "watch", "read"):
        from elm import ELM327
        elm = ELM327()
        elm.init()
        {"check": check, "scan": scan, "watch": watch, "read": read}[cmd](elm, *args)
    else:
        sys.exit(__doc__)
