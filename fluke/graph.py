# the Performance Metrics graph: speed curves, latency / jitter / loss, quality score
# shown after the cable test and on the Wi-Fi tab

import tkinter as tk
import tkinter.font as tkfont

from .config import CARD_BG
from .speed import read_number, network_quality, QUALITY_RULES, PING_COUNT, PING_TARGET

DOWN_COLOR = "#FF9A2E"
UP_COLOR = "#B15CFF"
RATING_COLORS = {"Great": "#4DA3FF", "Good": "#76FF03", "Average": "#FFD700",
                 "Poor": "#FF5252", "N/A": "#888888"}


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
    gap = 4 if unit else 0
    total = big.measure(number) + (gap + small.measure(unit) if unit else 0)
    left = x - total / 2 if center else x
    canvas.create_text(left, y, text=number, anchor="nw", fill="#FFFFFF", font=big_spec)
    if unit:
        baseline = y + big.metrics("ascent")
        canvas.create_text(left + big.measure(number) + gap, baseline + small.metrics("descent"),
                           text=unit, anchor="sw", fill="#BBBBBB", font=small_spec)
    return y + big.metrics("linespace")


# speed curve with a gradient fill and a dashed average line. canvas shapes can't fade so the
# gradient is a PhotoImage, and it goes in `images` or Tk drops it
def draw_speed_chart(canvas, images, x, y, width, height, values, color, average):
    if len(values) < 2:
        canvas.create_line(x, y + height - 1, x + width, y + height - 1, fill="#333333")
        if average is not None:                     # got a speed, just no curve
            canvas.create_text(x + width / 2, y + height / 2, text="no live speed curve recorded",
                               fill="#666666", font=("Helvetica", 9))
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
        canvas.create_line(x, average_y, x + width, average_y, fill="#7A7A7A", dash=(4, 3))
        if average_y + 15 <= y + height:
            canvas.create_text(x + 3, average_y + 2, text="average", anchor="nw",
                               fill="#8A8A8A", font=("Helvetica", 8))
        else:                                       # too close to the bottom, put the label above
            canvas.create_text(x + 3, average_y - 2, text="average", anchor="sw",
                               fill="#8A8A8A", font=("Helvetica", 8))

    coords = []
    for px in range(0, width, 3):
        coords += [x + px, y + columns[px]]
    coords += [x + width - 1, y + columns[-1]]
    canvas.create_line(*coords, fill=color, width=2, smooth=True)


# Download or Upload block: label, big number, chart. returns the next y
def draw_speed_section(canvas, images, y, width, label, value, values, color):
    pad = 12
    canvas.create_text(pad, y, text=label, anchor="nw", fill=color, font=("Helvetica", 11, "bold"))
    size = max(26, min(40, width // 13))
    bottom = draw_big_value(canvas, pad, y + 16, format_value(value), "Mbps" if value is not None else "", size)
    chart_height = max(64, min(110, width // 6))
    chart_top = bottom + 2
    draw_speed_chart(canvas, images, pad, chart_top, width - 2 * pad, chart_height, values, color, value)
    return chart_top + chart_height + 12


# stats = [(label, number, unit), ...]
def draw_stat_row(canvas, y, width, stats):
    column = width / len(stats)
    bottom = y
    for k, (label, number, unit) in enumerate(stats):
        center = column * (k + 0.5)
        canvas.create_text(center, y, text=label, anchor="n", fill="#FFFFFF", font=("Helvetica", 10, "bold"))
        bottom = draw_big_value(canvas, center, y + 16, number, unit, 22, center=True)
    return bottom + 10


# ratings come from network_quality()
def draw_quality_section(canvas, y, width, ratings):
    canvas.create_text(12, y, text="Network Quality Score", anchor="nw", fill="#FFFFFF",
                       font=("Helvetica", 11, "bold"))
    y += 24
    column = width / len(ratings)
    shorts = {name: short for name, short, rules in QUALITY_RULES}
    for k, (name, rating) in enumerate(ratings.items()):
        center = column * (k + 0.5)
        text = shorts[name] if column < 130 else name       # short names on narrow screens
        canvas.create_text(center, y, text=text, anchor="n", fill="#CCCCCC", font=("Helvetica", 10))
        canvas.create_text(center, y + 18, text=rating, anchor="n", fill=RATING_COLORS[rating],
                           font=("Helvetica", 15, "bold"))
        if k > 0:
            canvas.create_line(column * k, y, column * k, y + 42, fill="#333333")
    return y + 52


# the whole Performance Metrics card: speeds and curves, ping stats, quality score.
# failed readings show N/A
class MetricsGraph:
    def __init__(self, parent, interface):
        self.interface = interface
        self.results = {}
        self.curves = {"down": [], "up": []}
        self.images = []              # keep PhotoImages alive
        self.drawn_width = 0
        self.canvas = tk.Canvas(parent, height=400, bg=CARD_BG, highlightthickness=0)
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
        width = canvas.winfo_width() if canvas.winfo_width() > 1 else 300

        results = self.results
        down = read_number(results.get("speed_down"))
        up = read_number(results.get("speed_up"))
        latency = read_number(results.get("ping_latency"))
        jitter = read_number(results.get("ping_jitter"))
        loss = read_number(results.get("ping_loss"))

        y = 6
        y = draw_speed_section(canvas, self.images, y, width, "Download", down, self.curves["down"], DOWN_COLOR)
        y = draw_speed_section(canvas, self.images, y, width, "Upload", up, self.curves["up"], UP_COLOR)

        canvas.create_line(12, y - 4, width - 12, y - 4, fill="#333333")
        y = draw_stat_row(canvas, y + 6, width, [
            ("Latency", format_value(latency), "ms" if latency is not None else ""),
            ("Jitter", format_value(jitter), "ms" if jitter is not None else ""),
            ("Packet Loss", "N/A" if loss is None else f"{loss:g}", "%" if loss is not None else ""),
        ])

        canvas.create_line(12, y, width - 12, y, fill="#333333")
        y = draw_quality_section(canvas, y + 10, width, network_quality(down, up, latency, jitter, loss))

        canvas.create_text(
            12, y, anchor="nw", width=width - 24, fill="#777777", font=("Helvetica", 8),
            text=f"Latency, jitter and loss: {PING_COUNT} pings to {PING_TARGET} out of {self.interface}. "
                 f"Speed curves are measured during the test."
        )
        canvas.configure(height=y + 34)

    # real width only shows up once the screen is visible, so redraw when it changes
    def on_resize(self, event):
        if event.width > 1 and event.width != self.drawn_width:
            self.draw()
