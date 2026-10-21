# Not-A-Fluke

A touchscreen network tester for the Raspberry Pi. Plug it into an Ethernet drop and it shows the switch, port, VLAN and PoE (from CDP/LLDP), checks DNS, measures latency, jitter and packet loss, and runs a speed test with a speed graph. The Wi-Fi tab scans nearby access points and runs the same speed test over Wi-Fi.

## Commands

| Command | What it does |
|---|---|
| `notafluke` | start the GUI (small screen on the Pi, 480x320 window on a VM) |
| `notafluke -f` | same, but in the foreground (Ctrl-C to stop) |
| `notafluke stop` | close it and give the small screen back to the terminal |
| `notafluke calibrate` | calibrate the touchscreen |
| `notafluke log` | show the GUI's output and errors |
| `notafluke test` | run the tests (no sudo, cable or screen needed) |
| `notafluke update` | pull the latest code for this branch and restart |
| `notafluke install` | install everything (first time on a new Pi or VM) |
| `notafluke autostart on/off` | start the GUI when the Pi boots, or don't |

Don't run the Python file with `sudo python3 ...`. Use `notafluke`, it sorts out sudo and the display. Running Python directly over SSH gives the `no display name and no $DISPLAY environment variable` error.

## Files

| File | What's in it |
|---|---|
| `notafluke` | the launcher |
| `fluke-calibrate` | touchscreen calibration |
| `fluke/main.py` | starts the app |
| `fluke/app.py` | main window, scan and results screens |
| `fluke/wifi.py` | Wi-Fi scanner, AP info, Wi-Fi speed test |
| `fluke/history.py` | scan log and History screen |
| `fluke/switch.py` | CDP/LLDP (via lldpd) and PoE |
| `fluke/dns.py` | DNS test |
| `fluke/speed.py` | speed test, ping stats, quality score |
| `fluke/graph.py` | the speed graph (Cable and Wi-Fi tabs) |
| `fluke/config.py` | version, ports, log paths, colours |
| `tests/` | tests (`notafluke test`) |

## Install

```bash
git clone https://github.com/chrisf2020/Not-A-Fluke.git
cd Not-A-Fluke
./notafluke install
```

You can also just run `notafluke` and it will install anything missing on the first run. Packages come from apt, not pip, since newer Pi OS and Debian block `sudo pip install`.

On the Pi, `install` also makes it start on boot. Hit Exit to get to the terminal, and `notafluke autostart off` if you don't want it on boot.

## Testing on a VM

- `notafluke` opens a 480x320 window (same size as the Pi screen) on the VM desktop.
- Run it from a terminal on the VM's desktop, not over plain SSH.
- It picks the first wired port it finds. To pick one yourself: `NOTAFLUKE_ETH=enp0s3 notafluke`
- Most VMs have no Wi-Fi adapter, so the Wi-Fi tab will just say so.

## Requirements

- Raspberry Pi OS or Debian 12 (Bookworm) / 13 (Trixie)
- `armhf`, `arm64` or `x86_64`
- 3.5" SPI touchscreen (`dtoverlay=piscreen` or `tft35a`), or any desktop on a VM
- An Ethernet port (`eth0` by default)
