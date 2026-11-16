# screen orientation for the 7 inch HDMI screen: flat (normal) or standing up (right)
# the choice is saved in a file so it stays after a reboot

import os
import subprocess

from .config import LOG_DIR

ORIENT_PATH = os.path.join(LOG_DIR, "orientation")

# touch has to be turned the same way as the picture
TOUCH_MATRIX = {
    "normal": "1 0 0 0 1 0 0 0 1",
    "right": "0 1 0 -1 0 1 0 0 1",
}


# the HDMI output name (like HDMI-1), or "" on the small screen / a desktop window
def hdmi_output():
    try:
        text = subprocess.run(["xrandr", "--current"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in text.splitlines():
        if line.startswith("HDMI") and " connected" in line:
            return line.split()[0]
    return ""


def read_orientation():
    try:
        with open(ORIENT_PATH) as f:
            if f.read().strip() == "right":
                return "right"
    except OSError:
        pass
    return "normal"


def save_orientation(orientation):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(ORIENT_PATH, "w") as f:
        f.write(orientation)


# turn the picture and the touch to the saved orientation
def apply_orientation(output=None):
    if output is None:
        output = hdmi_output()
    if output == "":
        return
    orientation = read_orientation()
    subprocess.run(["xrandr", "--output", output, "--rotate", orientation])
    subprocess.run(["xinput", "set-prop", "pointer:TSTP MTouch", "Coordinate Transformation Matrix"]
                   + TOUCH_MATRIX[orientation].split(), stderr=subprocess.DEVNULL)


# the screen's size right now (width, height). xrandr knows straight away after a turn,
# Tk can still have the old size for a moment
def screen_size(root):
    try:
        text = subprocess.run(["xrandr", "--current"], capture_output=True, text=True, timeout=5).stdout
        words = text.split("current ")[1].split(",")[0].split(" x ")
        return int(words[0]), int(words[1])
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return root.winfo_screenwidth(), root.winfo_screenheight()


_points = None   # Tk's own pixels per point, before we change it


# the app is drawn for a 480x320 screen, so a bigger screen makes everything bigger:
# tk scaling makes every font bigger, config.SCALE (px()) every gap and box
def set_scale(root, width, height):
    global _points
    from . import config
    if _points is None:
        _points = root.winfo_fpixels("1i") / 72
    factor = min(width / 480, height / 320)
    if factor < 1.1:
        factor = 1.0
    if width > height and factor > 1.1:
        factor = factor * 0.8          # flat: everything a bit smaller, so more fits without scrolling
    root.tk.call("tk", "scaling", _points * factor)
    config.SCALE = factor
