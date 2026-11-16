# the Performance Metrics graph: speed curves, latency / jitter / loss, quality score
# shown after the cable test and on the Wi-Fi tab. also the Wi-Fi channel map (ChannelGraph)

import re
import tkinter as tk
import tkinter.font as tkfont

from . import config
from .config import THEME, CARD_BG, TEXT, MUTED, SOFT, LINE, GOOD, OK, BAD, CHANNEL_COLORS, px
from .speed import read_number, network_quality, QUALITY_RULES

DOWN_COLOR = "#FF9A2E"
UP_COLOR = "#B15CFF"
# channel map x axis per band: (lowest MHz, highest MHz, channel numbers to label)
BAND_AXES = {
    "2.4": (2397, 2497, list(range(1, 15))),
    "5": (5140, 5850, [36, 44, 52, 60, 100, 108, 116, 124, 132, 140, 149, 157, 165]),
}
SIGNAL_MIN = -100
SIGNAL_MAX = -30
RATING_COLORS = {"Great": "#4DA3FF", "Good": GOOD, "Average": OK, "Poor": BAD, "N/A": MUTED}


# 3 significant figures, e.g. 290 / 54.3 / 8.20, or N/A
def format_value(value):
    if value is None:
        return "N/A"
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


# mixes two #rrggbb colours, amount 0 is all color and 1 is all bg
def blend(color, bg, amount):
    c = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(c[k] + (b[k] - c[k]) * amount) for k in range(3))


# big number with a small unit after it. Tk canvas text can't mix sizes, so we measure
# both and line them up by hand. returns the y under the number
def draw_big_value(canvas, x, y, number, unit, size, center=False):
    big_spec = ("Helvetica", size, "bold")
    small_spec = ("Helvetica", max(9, size // 3), "bold")
    big = tkfont.Font(family="Helvetica", size=size, weight="bold")
    small = tkfont.Font(family="Helvetica", size=max(9, size // 3), weight="bold")
    gap = px(4) if unit else 0
    total = big.measure(number) + (gap + small.measure(unit) if unit else 0)
    left = x - total / 2 if center else x
    canvas.create_text(left, y, text=number, anchor="nw", fill=TEXT, font=big_spec)
    if unit:
        baseline = y + big.metrics("ascent")
        canvas.create_text(left + big.measure(number) + gap, baseline + small.metrics("descent"),
                           text=unit, anchor="sw", fill=SOFT, font=small_spec)
    return y + big.metrics("linespace")


# speed curve with a gradient fill and a dashed average line. canvas shapes can't fade so the
# gradient is a PhotoImage, and it goes in `images` or Tk drops it
def draw_speed_chart(canvas, images, x, y, width, height, values, color, average):
    if len(values) < 2:
        canvas.create_line(x, y + height - 1, x + width, y + height - 1, fill=LINE)
        if average is not None:                     # got a speed, just no curve
            canvas.create_text(x + width / 2, y + height / 2, text="no live speed curve recorded",
                               fill=MUTED, font=("Helvetica", 9))
        return

    # scale to the typical speed, not the max. the start of an upload fills the buffer and shows
    # a big fake spike that would flatten everything else. spikes just get clipped at the top
    typical = sorted(values)[int(0.9 * (len(values) - 1))]
    top = max(typical, average or 0) * 1.2
    if top <= 0:
        top = 1
    last = len(values) - 1
    columns = []                                   # curve height for each pixel column
    for px in range(width):
        pos = px * last / max(width - 1, 1)
        i = min(int(pos), last - 1)
        value = values[i] * (1 - (pos - i)) + values[i + 1] * (pos - i)
        columns.append(min(height - 1, max(0, height - 1 - int(value / top * (height - 2)))))

    # brightest at the top, fading into the card colour
    shades = [blend(color, CARD_BG, 0.35 + 0.63 * row / (height - 1)) for row in range(height)]
    rows = []
    for row in range(height):
        shade = shades[row]
        rows.append("{" + " ".join(shade if row >= columns[px] else CARD_BG for px in range(width)) + "}")
    image = tk.PhotoImage(width=width, height=height)
    image.put(" ".join(rows))
    images.append(image)
    canvas.create_image(x, y, image=image, anchor="nw")

    if average is not None and average > 0:
        average_y = y + height - 1 - int(average / top * (height - 2))
        canvas.create_line(x, average_y, x + width, average_y, fill=MUTED, dash=(4, 3))
        if average_y + 15 <= y + height:
            canvas.create_text(x + 3, average_y + 2, text="average", anchor="nw",
                               fill=MUTED, font=("Helvetica", 8))
        else:                                       # too close to the bottom, put the label above
            canvas.create_text(x + 3, average_y - 2, text="average", anchor="sw",
                               fill=MUTED, font=("Helvetica", 8))

    coords = []
    for px in range(0, width, 3):
        coords += [x + px, y + columns[px]]
    coords += [x + width - 1, y + columns[-1]]
    canvas.create_line(*coords, fill=color, width=2, smooth=True)


# Download or Upload block: label, big number, chart. returns the next y
def draw_speed_section(canvas, images, y, width, label, value, values, color):
    pad = px(12)
    small_width = int(width / config.SCALE)       # the width it would be on the small screen
    canvas.create_text(pad, y, text=label, anchor="nw", fill=color, font=("Helvetica", 11, "bold"))
    # the number goes right under the label (measured, so they never overlap)
    label_height = tkfont.Font(family="Helvetica", size=11, weight="bold").metrics("linespace")
    size = max(26, min(40, small_width // 13))
    bottom = draw_big_value(canvas, pad, y + label_height, format_value(value), "Mbps" if value is not None else "", size)
    chart_height = px(max(64, min(110, small_width // 6)))
    chart_top = bottom + px(2)
    draw_speed_chart(canvas, images, pad, chart_top, width - 2 * pad, chart_height, values, color, value)
    return chart_top + chart_height + px(12)


# stats = [(label, number, unit), ...]
def draw_stat_row(canvas, y, width, stats):
    column = width / len(stats)
    bottom = y
    for k, (label, number, unit) in enumerate(stats):
        center = column * (k + 0.5)
        canvas.create_text(center, y, text=label, anchor="n", fill=TEXT, font=("Helvetica", 10, "bold"))
        label_height = tkfont.Font(family="Helvetica", size=10, weight="bold").metrics("linespace")
        bottom = draw_big_value(canvas, center, y + label_height, number, unit, 22, center=True)
    return bottom + px(10)


# ratings come from network_quality()
def draw_quality_section(canvas, y, width, ratings):
    canvas.create_text(px(12), y, text="Network Quality Score", anchor="nw", fill=TEXT,
                       font=("Helvetica", 11, "bold"))
    y += px(24)
    column = width / len(ratings)
    shorts = {name: short for name, short, rules in QUALITY_RULES}
    for k, (name, rating) in enumerate(ratings.items()):
        center = column * (k + 0.5)
        text = shorts[name] if column < px(130) else name       # short names on narrow screens
        canvas.create_text(center, y, text=text, anchor="n", fill=SOFT, font=("Helvetica", 10))
        canvas.create_text(center, y + tkfont.Font(family="Helvetica", size=10).metrics("linespace"), text=rating, anchor="n", fill=RATING_COLORS[rating],
                           font=("Helvetica", 15, "bold"))
        if k > 0:
            canvas.create_line(column * k, y, column * k, y + px(42), fill=LINE)
    return y + px(52)


# the whole Performance Metrics card: speeds and curves, ping stats, quality score.
# failed readings show N/A
class MetricsGraph:
    def __init__(self, parent, interface):
        self.interface = interface
        self.results = {}
        self.curves = {"down": [], "up": []}
        self.images = []              # keep PhotoImages alive
        self.drawn_width = 0
        self.canvas = tk.Canvas(parent, height=px(400), bg=CARD_BG, highlightthickness=0)
        self.canvas.bind("<Configure>", self.on_resize)

    def show(self, results, curves):
        self.results = results
        self.curves = curves
        self.draw()

    def draw(self):
        canvas = self.canvas
        canvas.delete("all")
        self.images = []
        self.drawn_width = canvas.winfo_width()
        # hidden card says width 1, so guess 300 until on_resize redraws
        width = canvas.winfo_width() if canvas.winfo_width() > 1 else px(300)

        results = self.results
        down = read_number(results.get("speed_down"))
        up = read_number(results.get("speed_up"))
        latency = read_number(results.get("ping_latency"))
        jitter = read_number(results.get("ping_jitter"))
        loss = read_number(results.get("ping_loss"))

        y = px(6)
        y = draw_speed_section(canvas, self.images, y, width, "Download", down, self.curves["down"], DOWN_COLOR)
        y = draw_speed_section(canvas, self.images, y, width, "Upload", up, self.curves["up"], UP_COLOR)

        canvas.create_line(px(12), y - px(4), width - px(12), y - px(4), fill=LINE)
        y = draw_stat_row(canvas, y + px(6), width, [
            ("Latency", format_value(latency), "ms" if latency is not None else ""),
            ("Jitter", format_value(jitter), "ms" if jitter is not None else ""),
            ("Packet Loss", "N/A" if loss is None else f"{loss:g}", "%" if loss is not None else ""),
        ])

        canvas.create_line(px(12), y, width - px(12), y, fill=LINE)
        y = draw_quality_section(canvas, y + px(10), width, network_quality(down, up, latency, jitter, loss))

        # which speed test server it used
        server = results.get("speed_server")
        if server:
            canvas.create_text(px(12), y + px(4), text=f"Server: {server}", anchor="nw", fill=MUTED,
                               font=("Helvetica", 9))
            y = y + px(20)
        canvas.configure(height=y + px(10))

    # real width only shows up once the screen is visible, so redraw when it changes
    def on_resize(self, event):
        if event.width > 1 and event.width != self.drawn_width:
            self.draw()


# a small graph of past speed tests: download and upload lines, oldest on the left
def draw_history_chart(canvas, width, height, tests):
    canvas.delete("all")
    left, right, top, bottom = px(40), width - px(8), px(24), height - px(30)
    highest = max(max(down, up) for down, up in tests) * 1.15 or 1
    # key: which colour is which
    canvas.create_text(left, px(2), text="● Download", anchor="nw", fill=DOWN_COLOR, font=("Helvetica", 9, "bold"))
    canvas.create_text(left + px(90), px(2), text="● Upload", anchor="nw", fill=UP_COLOR, font=("Helvetica", 9, "bold"))
    # Mbps up the side, test number along the bottom
    for value in (0, highest / 2, highest):
        y = bottom - value / highest * (bottom - top)
        canvas.create_line(left, y, right, y, fill=LINE)
        canvas.create_text(left - px(4), y, text=f"{value:.0f}", anchor="e", fill=MUTED, font=("Helvetica", 8))
    canvas.create_text(px(2), px(2), text="Mbps", anchor="nw", fill=MUTED, font=("Helvetica", 8))
    canvas.create_text(left, bottom + px(3), text="1", anchor="n", fill=MUTED, font=("Helvetica", 8))
    canvas.create_text(right, bottom + px(3), text=str(len(tests)), anchor="n", fill=MUTED, font=("Helvetica", 8))
    canvas.create_text((left + right) / 2, bottom + px(14), text="Test (oldest to newest)", anchor="n",
                       fill=MUTED, font=("Helvetica", 8))
    step = (right - left) / max(len(tests) - 1, 1)
    for k, color in ((0, DOWN_COLOR), (1, UP_COLOR)):
        points = [(left + n * step, bottom - test[k] / highest * (bottom - top)) for n, test in enumerate(tests)]
        if len(points) > 1:
            canvas.create_line(*[v for point in points for v in point], fill=color, width=2)
        for x, y in points:
            canvas.create_oval(x - px(3), y - px(3), x + px(3), y + px(3), fill=color, outline="")


# --- WI-FI CHANNEL MAP ---

# the colour of the AP in this place on the chart (0 = strongest)
def ap_color(rank):
    return CHANNEL_COLORS[rank % len(CHANNEL_COLORS)]


# "80 MHz" -> 80, 20 if the scan didn't say
def width_mhz(text):
    match = re.match(r"\d+", text or "")
    return int(match.group()) if match else 20


# 6 -> 2437, 36 -> 5180 (14 is the odd one out on 2.4 GHz)
def channel_freq(channel, band):
    if band == "2.4":
        return 2484 if channel == 14 else 2407 + 5 * channel
    return 5000 + 5 * channel


# only the APs on this band ("2.4" or "5"), strongest first
def band_aps(aps, band):
    if band == "2.4":
        picked = [ap for ap in aps if 2400 <= ap["freq"] < 3000]
    else:
        picked = [ap for ap in aps if 5000 <= ap["freq"] < 5925]
    return sorted(picked, key=lambda ap: ap["signal"], reverse=True)


# dBm -> pixel row, clamped so a weird reading never draws outside the box
def signal_y(dbm, top, bottom):
    dbm = max(SIGNAL_MIN, min(SIGNAL_MAX, dbm))
    return bottom - (dbm - SIGNAL_MIN) / (SIGNAL_MAX - SIGNAL_MIN) * (bottom - top)


# flat [x, y, x, y, ...] for one arch: starts on the bottom line, peaks at (cx, top), ends on the bottom line
def arch_points(cx, top, half, bottom, steps=14):
    coords = []
    for k in range(-steps, steps + 1):
        t = k / steps
        coords += [cx + t * half, top + (bottom - top) * t * t]
    return coords


# squeezes every x in a flat coordinate list into left..right
def clip_x(coords, left, right):
    return [max(left, min(right, v)) if k % 2 == 0 else v for k, v in enumerate(coords)]


# long names get cut so the labels stay readable on the small screen
def short_name(ssid, limit=12):
    return ssid if len(ssid) <= limit else ssid[:limit - 1] + "…"


# Wi-Fi Analyzer style chart: one filled arch per AP, centred on its channel and as wide as
# its channel width, height is the signal strength. aps come from scan_wifi() in wifi.py.
# s = how much bigger to draw (1 = small screen), low / high = MHz shown (zoomed in), names
# longer than name_limit get cut. Each AP's shapes get the tag "ap<number>" so a tap can find it.
# returns the APs drawn, strongest first (the number in the tag is the place in this list)
def draw_channel_chart(canvas, width, height, aps, band, s=1, low=None, high=None, name_limit=12):
    left, right, top, bottom = 34 * s, width - 8 * s, 18 * s, height - 30 * s
    band_low, band_high, labelled = BAND_AXES[band]
    low = band_low if low is None else low
    high = band_high if high is None else high
    span = high - low

    def freq_x(freq):
        return left + (freq - low) / span * (right - left)

    #dBm grid and labels
    for dbm in range(SIGNAL_MIN + 10, SIGNAL_MAX + 1, 10):
        y = signal_y(dbm, top, bottom)
        canvas.create_line(left, y, right, y, fill=LINE)
        canvas.create_text(left - 4 * s, y, text=str(dbm), anchor="e", fill=MUTED, font=("Helvetica", 8))
    canvas.create_line(left, bottom, right, bottom, fill=LINE)
    # what the numbers are: signal strength up the side, channel along the bottom
    canvas.create_text(left - 4 * s, 2 * s, text="dBm", anchor="ne", fill=MUTED, font=("Helvetica", 8, "bold"))
    canvas.create_text((left + right) / 2, bottom + 16 * s, text="Channel", anchor="n", fill=MUTED,
                       font=("Helvetica", 8, "bold"))

    #channel numbers under the axis. zoomed in there's room for every channel
    if span < (band_high - band_low) / 1.5:
        step = 1 if band == "2.4" else 4
        labelled = range(1 if band == "2.4" else 32, 15 if band == "2.4" else 178, step)
    for channel in labelled:
        freq = channel_freq(channel, band)
        if freq < low or freq > high:
            continue
        x = freq_x(freq)
        canvas.create_line(x, bottom, x, bottom + 3 * s, fill=LINE)
        canvas.create_text(x, bottom + 4 * s, text=str(channel), anchor="n", fill=SOFT, font=("Helvetica", 8))

    in_band = band_aps(aps, band)
    if len(in_band) == 0:
        canvas.create_text((left + right) / 2, (top + bottom) / 2, text=f"No networks on {band} GHz",
                           fill=MUTED, font=("Helvetica", 11))
        return in_band

    #fills first (weakest underneath) so every outline stays visible on top of the overlaps
    shapes = []
    for rank, ap in enumerate(in_band):
        color = ap_color(rank)
        peak = signal_y(ap["signal"], top, bottom)
        half = width_mhz(ap["width"]) / 2 / span * (right - left)
        shapes.append((rank, ap, color, freq_x(ap.get("center", ap["freq"])), peak, half))
    for rank, ap, color, cx, peak, half in reversed(shapes):
        canvas.create_polygon(*clip_x(arch_points(cx, peak, half, bottom), left, right),
                              fill=blend(color, CARD_BG, 0.7), outline="", tags=("ap%d" % rank,))
    for rank, ap, color, cx, peak, half in reversed(shapes):
        canvas.create_line(*clip_x(arch_points(cx, peak, half, bottom), left, right), fill=color,
                           width=3 if ap["connected"] else 2, tags=("ap%d" % rank,))

    #names above the peaks, nudged up when two would sit on top of each other
    placed = []
    line_height = 14 * s
    for rank, ap, color, cx, peak, half in shapes:
        if cx < left or cx > right:              #zoomed in and this one is off to the side
            continue
        name = "hidden" if ap["ssid"] == "<hidden>" else short_name(ap["ssid"], name_limit)
        text_width = len(name) * 7.5 * s + 6 * s   #about 7.5 pixels a letter, plus a gap
        label_x = max(left + text_width / 2, min(right - text_width / 2, cx))
        label_y = peak - 2 * s
        moved = True
        while moved:
            moved = False
            for other_x, other_y, other_width in placed:
                if abs(other_x - label_x) < (text_width + other_width) / 2 and abs(other_y - label_y) < line_height:
                    label_y = other_y - line_height
                    moved = True
        if label_y < top + line_height:          #no room above, put it inside the arch
            label_y = peak + line_height
        placed.append((label_x, label_y, text_width))
        canvas.create_text(label_x, label_y, text=name, anchor="s", fill=color, tags=("ap%d" % rank,),
                           font=("Helvetica", 9, "bold"))
    return in_band


# the channel map canvas. show() gives it a scan and a band, it redraws whenever it is resized.
# tap an arch or a name -> on_tap(ap). zoom_in / zoom_out make it wider, drag sideways to move
class ChannelGraph:
    def __init__(self, parent, on_tap=None):
        self.aps = []
        self.band = "2.4"
        self.shown = []
        self.on_tap = on_tap
        self.zoom = 1
        self.middle = None                       #MHz in the middle of the chart when zoomed in
        self.press_x = 0
        self.dragged = False
        self.canvas = tk.Canvas(parent, bg=CARD_BG, highlightthickness=0)
        self.canvas.bind("<Configure>", lambda event: self.draw())
        self.canvas.bind("<ButtonPress-1>", self.pressed)
        self.canvas.bind("<B1-Motion>", self.moved)
        self.canvas.bind("<ButtonRelease-1>", self.released)

    def show(self, aps, band):
        if band != self.band:
            self.zoom = 1
            self.middle = None
        self.aps = aps
        self.band = band
        self.draw()

    def zoom_in(self):
        self.zoom = min(8, self.zoom * 2)
        self.draw()

    def zoom_out(self):
        self.zoom = max(1, self.zoom // 2)
        self.draw()

    # the MHz range shown, keeps the zoomed part inside the band
    def view(self):
        band_low, band_high = BAND_AXES[self.band][:2]
        span = (band_high - band_low) / self.zoom
        if self.middle is None:
            self.middle = (band_low + band_high) / 2
        self.middle = max(band_low + span / 2, min(band_high - span / 2, self.middle))
        return self.middle - span / 2, self.middle + span / 2

    def draw(self):
        self.canvas.delete("all")
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width < 100 or height < 80:              #hidden frames report a tiny size
            return
        low, high = self.view()
        self.shown = draw_channel_chart(self.canvas, width, height, self.aps, self.band, s=config.SCALE,
                                        low=low, high=high, name_limit=32)

    def pressed(self, event):
        self.press_x = event.x
        self.dragged = False

    # drag sideways to move along the band when zoomed in
    def moved(self, event):
        if self.zoom == 1 or abs(event.x - self.press_x) < px(10):
            return
        self.dragged = True
        low, high = self.view()
        self.middle -= (event.x - self.press_x) / self.canvas.winfo_width() * (high - low)
        self.press_x = event.x
        self.draw()

    # a tap (not a drag) on an AP opens its details
    def released(self, event):
        if self.dragged or self.on_tap is None:
            return
        for item in reversed(self.canvas.find_overlapping(event.x - 3, event.y - 3, event.x + 3, event.y + 3)):
            for tag in self.canvas.gettags(item):
                if tag.startswith("ap"):
                    rank = int(tag[2:])
                    self.on_tap(self.shown[rank], ap_color(rank))
                    return
