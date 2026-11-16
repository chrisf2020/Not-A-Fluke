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


# where the raspberry sits in logo.png (left, top, right, bottom)
BERRY_BOX = (326, 4, 476, 171)       # stops just above the letters


# the logo split in two: everything but the raspberry, and the raspberry on its own (so it can wiggle)
def logo_parts(logo_width):
    logo = Image.open(LOGO).convert("RGBA")
    berry = logo.crop(BERRY_BOX)
    pixels = berry.load()
    for y in range(berry.height):
        for x in range(berry.width):
            r, g, b, a = pixels[x, y]
            blue = b > r + 40 and b > g             # the blue ring behind it stays put
            # egg shape: wide at the leaves, narrower at the bottom so the A next to it stays put
            across = (x + BERRY_BOX[0] - 396) / (74 if y + BERRY_BOX[1] < 92 else 53)
            down = (y + BERRY_BOX[1] - 92) / 88
            # soft edges (and a soft bottom) so it blends into the still logo drawn underneath
            edge = min(1, max(0, (1 - (across * across + down * down) ** 0.5) / 0.15))
            bottom = min(1, max(0, (BERRY_BOX[3] - BERRY_BOX[1] - y) / 14))
            pixels[x, y] = (r, g, b, 0 if blue else int(a * edge * bottom))
    scale = logo_width / logo.width
    small = lambda picture: picture.resize((round(picture.width * scale), round(picture.height * scale)), Image.LANCZOS)
    return small(logo), small(berry), (round(BERRY_BOX[0] * scale), round(BERRY_BOX[1] * scale))


# draws the start screen once. angle = how far the raspberry is tipped (for the jingle)
def draw_screen(name, plugged, message="Tap the screen to start", angle=0):
    width, height = [int(n) for n in read_number("/sys/class/graphics/fb0/virtual_size").split(",")]
    bits = int(read_number("/sys/class/graphics/fb0/bits_per_pixel"))

    # standing up: draw it tall, then turn it to match the screen
    standing = os.path.exists(ORIENT_PATH) and read_number(ORIENT_PATH) == "right"
    if standing:
        width, height = height, width

    picture = Image.new("RGB", (width, height), "black")
    # as big as fits, with room for the text under it (the parts are made once, then reused)
    logo_width = min(int(width * 0.8), int(height * 0.6 * 512 / 341))
    if logo_width not in PARTS:
        PARTS[logo_width] = logo_parts(logo_width)
    logo, berry, berry_spot = PARTS[logo_width]
    longest = max(width, height)     # same size letters standing up or flat
    font = ImageFont.truetype(FONT, longest // 24)
    small = ImageFont.truetype(FONT_PLAIN, longest // 34)
    gap = height // 20
    text_height = font.size + gap + 2 * int(small.size * 1.5)
    top = (height - logo.height - gap - text_height) // 2
    left = (width - logo_width) // 2
    picture.paste(logo, (left, top), logo)
    # the raspberry jingles: a bit bigger and tipped, drawn over the still one so no gaps show
    if angle != 0:
        grow = 1.06
        big = berry.resize((round(berry.width * grow), round(berry.height * grow)), Image.LANCZOS)
        big = big.rotate(angle * 0.6, resample=Image.BICUBIC, center=(big.width / 2, big.height * 0.8))
        x = left + berry_spot[0] - (big.width - berry.width) // 2
        y = top + berry_spot[1] - (big.height - berry.height) // 2
        picture.paste(big, (x, y), big)

    draw = ImageDraw.Draw(picture)
    y = top + logo.height + gap
    draw.text((width // 2, y), message, font=font, fill="white", anchor="mt")

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
PARTS = {}


# a little sleigh-bell "ta-da" out of the screen's speaker (HDMI), made on the spot
def play_chime(path="/tmp/notafluke-chime.wav"):
    import math, wave, struct
    rate = 22050
    samples = []
    for start, note in ((0.0, 1319), (0.12, 1568), (0.24, 2093), (0.36, 2637)):   # E6 G6 C7 E7
        for n in range(int(rate * 0.9)):
            t = n / rate
            # a bell: the note plus a higher shimmer, fading out fast
            value = (math.sin(2 * math.pi * note * t) + 0.4 * math.sin(2 * math.pi * note * 2.76 * t)) * math.exp(-6 * t)
            k = int(start * rate) + n
            samples += [0.0] * (k + 1 - len(samples))
            samples[k] += value * 0.18
    with wave.open(path, "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(rate)
        sound.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, v)) * 32000)) for v in samples))
    # straight to the HDMI speaker, or the normal sound output if that's busy (right after boot)
    subprocess.Popen(f"aplay -q -D plughw:0,0 {path} || (sleep 1; aplay -q {path})", shell=True,
                     stderr=subprocess.DEVNULL)


def main():
    # --starting: tapped, the app is loading. The raspberry jingles until the app covers the screen
    if "--starting" in sys.argv:
        play_chime()
        status = (wifi_name(), cable_plugged_in())
        for frame in range(400):                  # about 20 seconds at most
            angle = [0, 9, 14, 9, 0, -9, -14, -9][frame % 8] * (0.4 if frame % 16 >= 8 else 1)
            draw_screen(*status, message="Starting...", angle=angle)
            time.sleep(0.03)
        return
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
