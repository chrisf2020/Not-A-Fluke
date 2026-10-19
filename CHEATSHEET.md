# Cheat sheet

Quick stuff we keep forgetting. Run these over SSH: `ssh admin@raspberrypi.local`

The code lives in `~/pi-fluke` on a fresh setup. On our current Pi it's still `~/Not-A-Fluke`, so swap that in below.

## Everyday

| Do this | Command |
|---|---|
| Start the GUI | `notafluke` |
| Stop it (screen goes back to the terminal) | `notafluke stop` |
| See errors | `notafluke log` |
| Run the tests | `notafluke test` |
| Screen is blank / stuck | `notafluke stop` |
| Touch is off | `notafluke calibrate` |
| Don't start on boot | `sudo notafluke autostart off` (`on` to undo) |
| Reboot / shut down | `sudo reboot` / `sudo shutdown now` |

Don't just pull the power, shut it down first. That's how SD cards get corrupted.

## Put a new file on the Pi

Say Claude gave you a new `wifi.py`. From your computer, in the folder with the file:

```
scp wifi.py admin@raspberrypi.local:~/pi-fluke/fluke/
```

Then on the Pi:

```
notafluke test        # should end with OK
notafluke stop && notafluke
```

Didn't work? Put the old one back:

```
cd ~/pi-fluke && git checkout fluke/wifi.py
```

Tip: give Claude the current file from GitHub first, so it doesn't edit an old copy.

## Get the newest code from GitHub

```
cd ~/pi-fluke && git pull && notafluke stop && notafluke
```

## Try another version without breaking yours

```
notafluke try                 # list the branches
notafluke try test_branch     # run that branch
notafluke try clean           # delete the test copies
```

It runs a separate copy in `~/fluke-try`, so your folder doesn't change. When you're done, `notafluke` runs your normal version again.

Other safe ways to test:
- on a VM: `notafluke` opens a window there instead of the small screen
- `notafluke test` works anywhere, no sudo or cable needed

## Save your changes to GitHub

```
cd ~/pi-fluke
git add -A
git commit -m "what you changed"
git push
```

Pushing from the Pi needs a GitHub key. Ours is set up as `github-tim`, so the repo should be cloned as `git@github-tim:timsmithh23/pi-fluke.git`.

Never run `git` with `sudo`.

## Set up a new Pi

```
git clone https://github.com/timsmithh23/pi-fluke.git ~/pi-fluke
cd ~/pi-fluke && ./notafluke install
```

That installs everything, adds the `notafluke` command and starts it on boot.

## Where stuff is

| What | Where |
|---|---|
| The code | `fluke/` (see the README for what each file does) |
| Scan log | `/var/log/fluke-gui/scans.jsonl` |
| Errors | `/var/log/fluke-gui/events.log`, or `notafluke log` |
| Version number | `fluke/config.py` |

## If something goes wrong

- GUI is stuck: `notafluke stop`, then `notafluke`
- Can't find the Pi: `ssh admin@raspberrypi.local` works whatever the IP is, and the IP is in the top bar of the GUI
- It won't stop restarting on boot: `sudo notafluke autostart off`
- Screen shows garbage after closing: `notafluke stop`
- Screen stays white after shutdown: run `sudo rpi-eeprom-config --edit`, change `POWER_OFF_ON_HALT=0` to `1`, save, reboot. Then the Pi really powers off when it shuts down. Change it back to `0` to undo.
