# waits for one tap on the touchscreen, then exits. The notafluke launcher runs this after the
# GUI closes, so tapping the terminal's "button" starts the GUI again (no keyboard needed)

import struct
import glob

# a touch event: time (2 numbers), type, code, value
EVENT = struct.Struct("llHHi")
EV_KEY = 1
BTN_TOUCH = 330


# the touchscreen's /dev/input/eventN, or "" if there isn't one
def find_touchscreen():
    for path in glob.glob("/sys/class/input/event*/device/name"):
        with open(path) as f:
            if "Touchscreen" in f.read():
                return "/dev/input/" + path.split("/")[4]
    return ""


def main():
    device = find_touchscreen()
    if device == "":
        raise SystemExit(1)
    with open(device, "rb") as touch:
        while True:
            data = touch.read(EVENT.size)
            seconds, micros, kind, code, value = EVENT.unpack(data)
            # finger lifted off the screen = one tap
            if kind == EV_KEY and code == BTN_TOUCH and value == 0:
                return


if __name__ == "__main__":
    main()
