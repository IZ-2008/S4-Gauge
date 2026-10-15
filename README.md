# S4 Cockpit — Raspberry Pi OBD2 gauge

A Raspberry Pi 4 reads live engine data from a 2013 Audi S4 (B8.5, 3.0 TFSI) through a USB OBD2 adapter, creates its own Wi-Fi network, and serves a cockpit-style dashboard to an iPhone in Safari. The main goal is oil temperature, which the car only reports through a manufacturer-specific code, so the project includes a script that finds it.

![Dashboard during a spirited drive](docs/cockpit-spirited.png)

Demo video: [`docs/s4-cockpit-demo.mp4`](docs/s4-cockpit-demo.mp4) (cold start → city → highway → spirited → overheat warning, simulated data).

## What it shows

- Oil temperature as the main gauge: blue when cold, white in range, amber when high, red when critical, with peak and a trend line
- Tachometer and speedometer
- Boost, throttle, pedal and engine load
- Coolant, intake air, ambient and catalyst temperatures; timing, MAF, manifold pressure; fuel level and fuel trims; battery voltage
- °F/mph or °C/km/h

## Hardware

- Raspberry Pi 4B (any RAM size)
- USB OBD2 adapter that speaks ELM327/STN commands and supports `ATSH` (tested design: Vgate vLinker FS USB; OBDLink SX also fine)
- 12 V socket adapter with a USB-C port rated 5 V / 3 A

## Install

1. Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager: hostname `s4gauge`, user `pi`, your home Wi-Fi, SSH enabled.
2. Copy the `s4gauge` folder to `/home/pi/s4gauge` on the Pi.
3. Run the installer with a password for the gauge's Wi-Fi (8+ characters), then reboot:

   ```bash
   bash ~/s4gauge/install.sh YOUR-HOTSPOT-PASSWORD
   sudo reboot
   ```

4. On the phone, join the Wi-Fi **S4-Gauge** and open `http://10.42.0.1`.

Try the dashboard without the car: `python3 ~/s4gauge/gauge.py --demo --http 8080`, or open `static/index.html?demo` in any browser.

## Finding the oil-temperature code

With the engine idling and the gauge stopped (`sudo systemctl stop s4gauge`):

```bash
cd ~/s4gauge
python3 find_oil_did.py scan     # engine idling, 10–45 min
python3 find_oil_did.py watch    # next cold start, 15 min
python3 find_oil_did.py use engine XXXX   # save the code it found
sudo systemctl start s4gauge
```

The finder only reads (UDS service 22); it never writes to the car. Use it parked, not while driving.

## Files

| File | Purpose |
| --- | --- |
| `s4gauge/elm.py` | Minimal ELM327/STN driver for CAN (11-bit, 500 kbps) |
| `s4gauge/gauge.py` | Polls supported PIDs and the oil-temp code, serves the page and a WebSocket |
| `s4gauge/static/index.html` | The dashboard (single self-contained file) |
| `s4gauge/find_oil_did.py` | Finds and saves the oil-temperature code |
| `s4gauge/config.json` | Oil-temp request and warning limits (°C) |
| `s4gauge/install.sh` | One-time setup: packages, autostart service, Wi-Fi hotspot |

## Status

Tested against a simulated ELM327 adapter and ECU, and in the browser at iPhone sizes. Not yet run on a real car. Hobby display: trust the car's own warnings over it.
