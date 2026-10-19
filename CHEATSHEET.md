# Cheat sheet

Quick stuff we keep forgetting. Run these on the Pi over SSH: `ssh admin@raspberrypi.local`

The code lives in `~/Not-A-Fluke` on the Pi.

## Everyday

| Do this | Command |
|---|---|
| Start the GUI | `notafluke` |
| Stop it (screen goes back to the terminal) | `notafluke stop` |
| See errors | `notafluke log` |
| Run the tests | `notafluke test` |
| Screen is blank / stuck | `notafluke stop` |
| Touch is off | `notafluke calibrate` |
| Start on boot or not | `notafluke autostart on` / `off` (just `notafluke autostart` shows which) |
| Reboot / shut down | `sudo reboot` / `sudo shutdown now` |

Don't just pull the power, shut it down first. That's how SD cards get corrupted.

## Change something and test it

**1. Start from the latest code**

```
cd ~/Not-A-Fluke
git status          # should say "nothing to commit". If not, someone left changes, ask first
git pull
```

**2. Make your change**, whichever way is easiest:

- edit on the Pi: `nano fluke/wifi.py`, or use VS Code's Remote-SSH to `admin@raspberrypi.local`
- or edit on your computer and copy the file over (run this on your computer, in the folder with the file):
  ```
  scp wifi.py admin@raspberrypi.local:~/Not-A-Fluke/fluke/
  ```
  Grab the newest copy from GitHub before you edit, so you don't overwrite someone else's work.

**3. Run the tests**

```
notafluke test
```

It has to end with `OK`. If something says `FAIL` or `ERROR`, it tells you which check broke.

**4. Restart the GUI with your change**

```
notafluke stop && notafluke
```

**5. Look at it**, on the Pi's screen or from your computer (see "See the Pi's screen on your computer" below). Plug a cable in to run a real scan.

**6. Something wrong?**

```
notafluke log       # the error is usually at the bottom
```

**7a. It works, save it to GitHub**

```
git add -A
git commit -m "what you changed"
git push
```

On our Pi, `pushtim "what you changed"` does all three and puts Tim's name on it.

**7b. It doesn't work, undo it**

```
git checkout fluke/wifi.py      # just that file
git checkout .                  # everything you changed
notafluke stop && notafluke
```

This puts back the last version that was saved to GitHub, so trying stuff can't break anything for good.

## See the Pi's screen on your computer

Live view of the small screen that you can click on like the touchscreen. No VM needed.

1. Get a free VNC viewer once: RealVNC Viewer or TigerVNC (Windows / Mac / Linux). On a Mac the built-in Screen Sharing works too.
2. On your computer, run this and leave it open:
   ```
   ssh -t -L 5901:localhost:5901 admin@raspberrypi.local notafluke view
   ```
3. In the VNC viewer connect to `localhost:5901`.

Ctrl-C in the ssh window stops sharing. The GUI keeps running on the Pi. The screen only gets shared through your SSH login, so nobody else on the network can see it.

## Try someone else's version without breaking yours

```
notafluke try                 # list the branches on GitHub
notafluke try test_branch     # run that branch
notafluke try clean           # delete the test copies
```

It runs a separate copy in `~/fluke-try`, so your folder doesn't change. Run `notafluke` to go back to your normal version.

Other safe ways to test:
- on a VM: `notafluke` opens a window there instead of the small screen
- `notafluke test` works anywhere, no sudo or cable needed

Never run `git` with `sudo`.

## Set up a new Pi

```
git clone https://github.com/chrisf2020/Not-A-Fluke.git ~/Not-A-Fluke
cd ~/Not-A-Fluke && ./notafluke install
```

That installs everything, adds the `notafluke` command, starts it on boot and makes the screen go black at shutdown.

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
- Don't want it starting on boot: `notafluke autostart off`
- Screen shows garbage after closing: `notafluke stop`
- Screen stays white after shutdown: run `sudo rpi-eeprom-config --edit`, change `POWER_OFF_ON_HALT=0` to `1`, save, reboot. Then the Pi really powers off when it shuts down. Change it back to `0` to undo.
