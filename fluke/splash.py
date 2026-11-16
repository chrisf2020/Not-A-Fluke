# draws the logo and "Tap the screen to start" straight onto the 7 inch screen's console
# (no X running then). The notafluke launcher runs it after the GUI closes

import os
import sys
import time
import subprocess
import numpy
from PIL import Image, ImageDraw, ImageFont

FB = "/dev/fb0"
LOGO = os.path.join(os.path.dirname(__file__), "logo.png")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_PLAIN = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
GREEN = "#76FF03"
GREY = "#8A8A8A"
ORIENT_PATH = "/var/log/fluke-gui/orientation"


def read_number(path):
    with open(path) as f:
        return f.read().strip()


# the Wi-Fi network the Pi is on, or "" if it isn't on one
def wifi_name():
    try:
        text = subprocess.run(["iw", "dev", "wlan0", "link"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in text.splitlines():
        if line.strip().startswith("SSID:"):
            return line.strip()[5:].strip()
    return ""


def cable_plugged_in():
    try:
        return read_number("/sys/class/net/eth0/carrier") == "1"
    except OSError:
        return False


# draws the start screen once
def draw_screen(name, plugged):
    width, height = [int(n) for n in read_number("/sys/class/graphics/fb0/virtual_size").split(",")]
    bits = int(read_number("/sys/class/graphics/fb0/bits_per_pixel"))

    # standing up: draw it tall, then turn it to match the screen
    standing = os.path.exists(ORIENT_PATH) and read_number(ORIENT_PATH) == "right"
    if standing:
        width, height = height, width

    picture = Image.new("RGB", (width, height), "black")
    logo = Image.open(LOGO).convert("RGBA")
    # as big as fits, with room for the text under it
    logo_width = min(int(width * 0.8), int(height * 0.6 * logo.width / logo.height))
    logo = logo.resize((logo_width, int(logo.height * logo_width / logo.width)), Image.LANCZOS)
    longest = max(width, height)     # same size letters standing up or flat
    font = ImageFont.truetype(FONT, longest // 24)
    small = ImageFont.truetype(FONT_PLAIN, longest // 34)
    gap = height // 20
    text_height = font.size + gap + 2 * int(small.size * 1.5)
    top = (height - logo.height - gap - text_height) // 2
    picture.paste(logo, ((width - logo_width) // 2, top), logo)

    draw = ImageDraw.Draw(picture)
    y = top + logo.height + gap
    draw.text((width // 2, y), "Tap the screen to start", font=font, fill="white", anchor="mt")

    # what the Pi is connected to: green dot = connected, grey = not
    lines = [(f"Wi-Fi: {name}" if name else "Wi-Fi: not connected", name != ""),
             ("LAN: cable plugged in" if plugged else "LAN: no cable", plugged)]
    y = y + font.size + gap
    for text, on in lines:
        text_width = draw.textlength(text, font=small)
        dot = small.size // 2
        left = (width - text_width - dot - 12) // 2
        draw.ellipse((left, y + small.size // 2 - dot // 2, left + dot, y + small.size // 2 + dot // 2),
                     fill=GREEN if on else GREY)
        draw.text((left + dot + 12, y), text, font=small, fill="white" if on else GREY)
        y = y + int(small.size * 1.5)

    if standing:
        picture = picture.rotate(-90, expand=True)   # same way xrandr turns it

    # the screen wants 16 bit (RGB565) or 32 bit pixels
    if bits == 16:
        pixels = numpy.asarray(picture, dtype=numpy.uint16)
        rgb565 = ((pixels[..., 0] >> 3) << 11) | ((pixels[..., 1] >> 2) << 5) | (pixels[..., 2] >> 3)
        data = rgb565.astype("<u2").tobytes()
    else:
        data = picture.convert("RGBA").tobytes("raw", "BGRA")
    with open(FB, "wb") as screen:
        screen.write(data)


# --live keeps it up to date: redraws when the Wi-Fi or the cable changes (and every few
# seconds anyway, in case the console wrote over it)
def main():
    shown = None
    last_draw = 0
    while True:
        status = (wifi_name(), cable_plugged_in())
        if status != shown or time.time() - last_draw > 5:
            draw_screen(*status)
            shown = status
            last_draw = time.time()
        if "--live" not in sys.argv:
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
