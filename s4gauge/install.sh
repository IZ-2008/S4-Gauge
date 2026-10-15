#!/bin/bash
# One-time installer for the S4 gauge.
# Usage:  bash ~/s4gauge/install.sh YOUR-HOTSPOT-PASSWORD
set -e
PASS="$1"
if [ ${#PASS} -lt 8 ]; then
  echo "Please add a hotspot password of 8 or more characters, e.g.:"
  echo "  bash ~/s4gauge/install.sh mysecret123"
  exit 1
fi
DIR="$HOME/s4gauge"

echo "== 1/4 Installing software (takes a few minutes) =="
sudo apt-get update
sudo apt-get install -y python3-aiohttp python3-serial dnsmasq-base
sudo usermod -aG dialout "$USER"   # lets the gauge open the USB adapter

echo "== 2/4 Making the gauge start automatically =="
sudo tee /etc/systemd/system/s4gauge.service > /dev/null <<UNIT
[Unit]
Description=S4 OBD gauge
After=network.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/gauge.py
Restart=always
RestartSec=3
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable s4gauge

echo "== 3/4 Creating the S4-Gauge Wi-Fi hotspot (turns on at next restart) =="
sudo nmcli connection delete gauge-ap > /dev/null 2>&1 || true
sudo nmcli connection add type wifi ifname wlan0 con-name gauge-ap autoconnect yes \
  ssid S4-Gauge mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS" \
  connection.autoconnect-priority 100 > /dev/null

echo "== 4/4 Done =="
echo "Restart with:  sudo reboot"
echo "Then join the Wi-Fi 'S4-Gauge' (password: $PASS) and open http://10.42.0.1"
