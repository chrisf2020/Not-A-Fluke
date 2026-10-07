# Not-A-Fluke

Not-A-Fluke is a standalone, touchscreen-friendly network diagnostic tool for Raspberry Pi and Debian-based Linux devices. Once connected to an Ethernet drop, it detects physical carrier state, extracts switch topology via CDP/LLDP, tests active DNS lookup handling, and runs an internet bandwidth benchmark.

---

# How to Run

| Command | What it does |
|---|---|
| `notafluke` | start the GUI on the small screen |
| `notafluke stop` | close it and put the terminal back on the small screen |
| `notafluke term` | put the terminal back on the small screen (if it's blank) |
| `notafluke calibrate` | line up the touchscreen |
| `notafluke log` | show the GUI's output / errors |

Files: `fluke-gui.py` (the GUI), `notafluke` (the launcher), `fluke-calibrate` (touchscreen calibration).

---

## Hardware & System Requirements

- **Operating System:** Raspberry Pi OS (Debian 11 Bullseye or Debian 12 Bookworm recommended)
- **Architecture:** `armhf`, `arm64`, or `x86_64`
- **Display:** Any display supported by X11 / Wayland (defaults to fullscreen resolution)
- **Network Interface:** Physical RJ-45 port (defaults to `eth0`)

---

## 1. System Package Dependencies

Not-A-Fluke requires several OS-level packages, the Tk GUI toolkit bindings, packet capture headers, and the `lldpd` daemon to capture switch advertisements.

Run the following command to install all system dependencies:

```bash
sudo apt update
sudo apt install -y \
    python3 \
    python3-pip \
    python3-tk \
    libpcap-dev \
    scapy \
sudo pip install speedtest-cli

    lldpd
