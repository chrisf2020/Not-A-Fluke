# waits for one tap on the touchscreen, then exits. The notafluke launcher runs this after the
# GUI closes, so tapping the terminal's "button" starts the GUI again (no keyboard needed)

import select
import struct
import glob

# a touch event: time (2 numbers), type, code, value
EVENT = struct.Struct("llHHi")
EV_KEY = 1
BTN_TOUCH = 330
BTN_LEFT = 272       # the 7 inch panel sometimes starts up as a mouse, then a tap is a click


# the touchscreens' /dev/input/eventN (the small screen, or the 7 inch one, which has two)
def find_touchscreens():
    devices = []
    for path in glob.glob("/sys/class/input/event*/device/name"):
        with open(path) as f:
            name = f.read()
        if "Touchscreen" in name or "MTouch" in name:
            devices.append("/dev/input/" + path.split("/")[4])
    return devices


def main():
    devices = find_touchscreens()
    if devices == []:
        raise SystemExit(1)
    files = [open(device, "rb") for device in devices]
    while True:
        ready, _, _ = select.select(files, [], [])
        for touch in ready:
            data = touch.read(EVENT.size)
            seconds, micros, kind, code, value = EVENT.unpack(data)
            # finger lifted off the screen = one tap
            if kind == EV_KEY and code in (BTN_TOUCH, BTN_LEFT) and value == 0:
                return


if __name__ == "__main__":
    main()
