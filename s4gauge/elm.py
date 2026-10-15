"""Minimal ELM327 / STN11xx driver for CAN cars (ISO 15765-4, 11-bit, 500 kbps)."""
import glob
import time

import serial

HEX = set("0123456789ABCDEF")


def find_port():
    ports = (sorted(glob.glob("/dev/serial/by-id/*")) + sorted(glob.glob("/dev/ttyUSB*"))
             + sorted(glob.glob("/dev/ttyACM*")))
    if not ports:
        raise RuntimeError("No USB serial adapter found - is the OBD cable plugged in?")
    return ports[0]


class ELM327:
    def __init__(self, port=None, bauds=(115200, 38400, 500000, 9600)):
        self.port = port or find_port()
        self.header = None
        for baud in bauds:
            self.ser = serial.Serial(self.port, baud, timeout=0.05)
            reply = " ".join(self.cmd("ATI", wait=1.5)).upper()
            if any(tag in reply for tag in ("ELM", "STN", "VLINK")):
                self.baud = baud
                break
            self.ser.close()
        else:
            raise RuntimeError(f"No ELM327 answer on {self.port}")

    def cmd(self, text, wait=1.0):
        """Send one command, return the reply lines (prompt and echo stripped)."""
        self.ser.reset_input_buffer()
        self.ser.write((text + "\r").encode())
        buf, deadline = b"", time.monotonic() + wait
        while time.monotonic() < deadline:
            buf += self.ser.read(self.ser.in_waiting or 1)
            if buf.endswith(b">"):
                break
        lines = buf.decode(errors="ignore").replace(">", "").replace("\r", "\n").split("\n")
        return [l.strip() for l in lines if l.strip() and l.strip() != text]

    def init(self):
        self.cmd("ATZ", wait=3)          # reset
        for c in ("ATE0", "ATL0", "ATS1", "ATH1", "ATSP6", "ATCAF1", "ATAT2", "ATST32"):
            self.cmd(c)
        # ATSP6 = CAN 11-bit 500k (all B8/B8.5 Audis). ATAT2 = aggressive adaptive timing.
        return self.cmd("0100", wait=5)  # wakes the bus; returns the first PID bitmap

    def set_header(self, tx):
        if tx != self.header:
            self.cmd(f"ATSH{tx}")
            self.header = tx

    def request(self, tx, rx, req, wait=0.5):
        """Send a hex request (e.g. '010C' or '22xxxx') to CAN id tx, return data bytes
        after the service+PID echo, or None. Single-frame replies only (<= 7 bytes)."""
        self.set_header(tx)
        service = int(req[:2], 16)
        echo_len = len(req) // 2  # bytes of positive-response echo: service + PID/DID
        for line in self.cmd(req + "1", wait=wait):  # trailing '1' = return after 1 reply
            tok = line.split()
            if len(tok) < 3 or tok[0] != rx or not all(set(t) <= HEX for t in tok):
                continue
            data = [int(t, 16) for t in tok[2:]]  # tok[1] is the ISO-TP length byte
            if data[0] == service + 0x40:
                return data[echo_len:]
            if data[0] == 0x7F:
                return None  # negative response: DID/PID not supported
        return None

    def close(self):
        self.ser.close()
