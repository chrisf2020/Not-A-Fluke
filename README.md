<p align="center"><img src="fluke/logo.png" alt="Not-A-Fluke" width="360"></p>

A touchscreen network tester for the Raspberry Pi. It does the same job as a Fluke network tester at a much, much lower price.

## Commands

| Command | What it does |
|---|---|
| `notafluke` | start the GUI (touchscreen on the Pi, 480x320 window on a VM) |
| `notafluke -f` | same, but in the foreground (Ctrl-C to stop) |
| `notafluke stop` | close it and give the screen back to the terminal |
| `notafluke calibrate` | calibrate the touchscreen |
| `notafluke log` | show the GUI's output and errors |
| `notafluke test` | run the tests (no sudo, cable or screen needed) |
| `notafluke update` | pull the latest code for this branch and restart |
| `notafluke install` | install everything (first time on a new Pi or VM) |
| `notafluke autostart on/off` | start the GUI when the Pi boots, or don't |

## Install

```bash
git clone https://github.com/chrisf2020/Not-A-Fluke.git
cd Not-A-Fluke
./notafluke install
```

You can also just run `notafluke` and it will install anything missing on the first run. Packages come from apt, not pip, since newer Pi OS and Debian block `sudo pip install`.

On the Pi, `install` also makes it start on boot (on the start screen, tap to begin). Tap ✕ to go back to the start screen or to the terminal, and `notafluke autostart off` if you don't want it on boot.

## Testing on a VM

- `notafluke` opens a 480x320 window (the size of the 3.5" screen) on the VM desktop.
- Run it from a terminal on the VM's desktop, not over plain SSH.
- It picks the first wired port it finds. To pick one yourself: `NOTAFLUKE_ETH=enp0s3 notafluke`
- Most VMs have no Wi-Fi adapter, so the Wi-Fi tab will just say so.

## Requirements

- Raspberry Pi OS or Debian 12 / 13
- A 7" HDMI touchscreen, or a 3.5" SPI touchscreen, or a VM desktop
- An Ethernet port
