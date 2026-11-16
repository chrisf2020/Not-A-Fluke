# send a scan to your phone: makes a PDF report, serves it from a tiny web server on the Pi,
# and the QR code is a link to it. The phone has to be on the same network as the Pi.

import os
import ssl
import subprocess
import threading
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

from .config import VERSION, LOG_DIR, INTERFACE, WIFI_INTERFACE, logger
from .history import format_stamp
from .speed import get_ip

REPORT_DIR = os.path.join(LOG_DIR, "reports")
# port 80 so the link has no port in it. Phone browsers try https first: with no port that goes
# to 443, gets refused straight away and falls back to http. With :8080 in the link the https
# attempt hits this plain web server and the page just fails to load.
SHARE_PORT = 80
BACKUP_PORT = 8080      # used if something else already has port 80
# iPhones (and Chrome with "always use secure connections") only try https and don't fall back,
# so we answer on 443 too. There's no real certificate for a local IP, so the phone warns once:
# tap Show Details -> visit this website
SECURE_PORT = 443
CERT_FILE = os.path.join(LOG_DIR, "share-cert.pem")
KEY_FILE = os.path.join(LOG_DIR, "share-key.pem")
MAX_REPORTS = 20        # older PDFs get deleted so the SD card doesn't fill up

# letter size page at 150 dpi
PAGE_WIDTH = 1275
PAGE_HEIGHT = 1650
MARGIN = 90
FONT_DIR = "/usr/share/fonts/truetype/dejavu"

_server = None
_secure_server = None


# e.g. scan-2026-10-28_12-59-29-k3x9q2ab.pdf. The random bit at the end means nobody on the
# network can guess the link to someone else's report
def report_filename(record):
    import secrets
    stamp = str(record.get("started_at") or "unknown").replace("T", "_").replace(":", "-")
    return f"scan-{stamp}-{secrets.token_hex(4)}.pdf"


# colours on the white page
INK = "#111111"
GREY = "#666666"
RULE = "#CCCCCC"
BLUE = "#0077B6"
PDF_GOOD = "#2E8B12"
PDF_OK = "#B8860B"
PDF_BAD = "#D32F2F"
LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.png")


def pdf_fonts():
    from PIL import ImageFont
    bold = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
    normal = os.path.join(FONT_DIR, "DejaVuSans.ttf")
    return {
        "title": ImageFont.truetype(bold, 44),
        "heading": ImageFont.truetype(bold, 32),
        "small": ImageFont.truetype(normal, 24),
        "label": ImageFont.truetype(normal, 27),
        "value": ImageFont.truetype(bold, 27),
    }


# the logo, `height` pixels tall
def pdf_logo(height):
    from PIL import Image
    logo = Image.open(LOGO_PATH).convert("RGBA")
    return logo.resize((round(logo.width * height / logo.height), height), Image.LANCZOS)


# a white page with the logo and title. The first page gets a big logo in the middle,
# the others a small one on the left. returns (page, draw, y where the content starts)
def new_page(record, fonts, first=False):
    from PIL import Image, ImageDraw
    page = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), "white")
    draw = ImageDraw.Draw(page)
    subtitle = f"Scan {format_stamp(record.get('started_at'))}   |   Not-A-Fluke v{VERSION}"
    if first:
        logo = pdf_logo(200)
        page.paste(logo, ((PAGE_WIDTH - logo.width) // 2, MARGIN - 30), logo)
        y = MARGIN - 30 + logo.height + 20
        draw.text((PAGE_WIDTH // 2, y), "Network scan report", font=fonts["title"], fill=INK, anchor="mt")
        draw.text((PAGE_WIDTH // 2, y + 64), subtitle, font=fonts["small"], fill=GREY, anchor="mt")
        y = y + 115
    else:
        logo = pdf_logo(100)
        page.paste(logo, (MARGIN, MARGIN - 20), logo)
        text_x = MARGIN + logo.width + 30
        draw.text((text_x, MARGIN + 2), "Network scan report", font=fonts["heading"], fill=INK)
        draw.text((text_x, MARGIN + 48), subtitle, font=fonts["small"], fill=GREY)
        y = MARGIN + 100
    draw.line((MARGIN, y, PAGE_WIDTH - MARGIN, y), fill=RULE, width=2)
    return page, draw, y + 30


# Link / Switch / DNS / Internet as coloured boxes, like the top of the results on screen
def draw_summary(draw, fonts, y, record):
    from .speed import read_number
    link = str(record.get("link_speed", "--"))
    speed = read_number(link)
    found = record.get("switch_name") not in ("Not Found", "--", None, "No managed switch")
    down = read_number(record.get("speed_down"))
    boxes = [
        ("Link", PDF_GOOD if speed is None or (speed >= 1000 and "half" not in link) else PDF_OK),
        ("Switch", PDF_GOOD if found else PDF_OK),
        ("DNS", PDF_GOOD if "ms" in str(record.get("dns_time")) else PDF_BAD),
        ("Internet", PDF_BAD if down is None else PDF_OK if record.get("speed_rating") == "Poor" else PDF_GOOD),
    ]
    width = (PAGE_WIDTH - 2 * MARGIN - 3 * 20) / 4
    for k, (text, color) in enumerate(boxes):
        x = MARGIN + k * (width + 20)
        draw.rounded_rectangle((x, y, x + width, y + 64), radius=14, fill=color)
        draw.text((x + width / 2, y + 32), text, font=fonts["value"], fill="white", anchor="mm")
    return y + 100


# one section: blue heading, then "label   value" lines. returns the y under it
def draw_section(draw, fonts, y, title, rows):
    draw.text((MARGIN, y), title, font=fonts["heading"], fill=BLUE)
    y = y + 50
    for label, value in rows:
        draw.text((MARGIN + 10, y), label, font=fonts["label"], fill=GREY)
        draw.text((MARGIN + 320, y), str(value)[:48], font=fonts["value"], fill=INK)
        y = y + 36
    draw.line((MARGIN, y + 8, PAGE_WIDTH - MARGIN, y + 8), fill=RULE, width=1)
    return y + 30


# the speed curves with Mbps up the side, on a light background with room for every label
def draw_speed_chart(draw, fonts, x, y, width, height, record):
    from .graph import DOWN_COLOR, UP_COLOR
    curves = record.get("speed_curves") or {}
    down = curves.get("down") or []
    up = curves.get("up") or []
    draw.rounded_rectangle((x, y, x + width, y + height), radius=12, fill="#F6F6F6", outline=RULE)
    if len(down) < 2 and len(up) < 2:
        draw.text((x + width / 2, y + height / 2), "No speed graph saved for this scan",
                  font=fonts["small"], fill=GREY, anchor="mm")
        return

    left = x + 110
    right = x + width - 30
    top = y + 60
    bottom = y + height - 70
    highest = max(down + up) * 1.15

    draw.text((left, y + 20), "Mbps", font=fonts["small"], fill=GREY, anchor="mt")
    for k in range(5):
        line_y = bottom - (bottom - top) * k / 4
        draw.line((left, line_y, right, line_y), fill="#DDDDDD", width=1)
        draw.text((left - 16, line_y), str(round(highest * k / 4)), font=fonts["small"], fill=GREY, anchor="rm")

    draw_curve(draw, down, DOWN_COLOR, left, right, top, bottom, highest)
    draw_curve(draw, up, UP_COLOR, left, right, top, bottom, highest)

    key_y = bottom + 40
    draw.rectangle((left, key_y - 8, left + 30, key_y + 8), fill=DOWN_COLOR)
    draw.text((left + 44, key_y), "Download " + str(record.get("speed_down", "--")), font=fonts["small"],
              fill=INK, anchor="lm")
    middle = (left + right) / 2
    draw.rectangle((middle, key_y - 8, middle + 30, key_y + 8), fill=UP_COLOR)
    draw.text((middle + 44, key_y), "Upload " + str(record.get("speed_up", "--")), font=fonts["small"],
              fill=INK, anchor="lm")


# lets the Wi-Fi channel map code in graph.py (made for the screen) draw onto a PDF page.
# it only calls create_line, create_polygon and create_text, so that's all this has.
# x0, y0 = top left of the chart on the page, scale = how much bigger than on screen
class PdfCanvas:
    def __init__(self, draw, x0, y0, scale):
        self.draw = draw
        self.x0 = x0
        self.y0 = y0
        self.scale = scale

    # [x, y, x, y, ...] on screen -> [(x, y), (x, y), ...] on the page
    def points(self, coords):
        result = []
        for k in range(0, len(coords), 2):
            result.append((self.x0 + coords[k] * self.scale, self.y0 + coords[k + 1] * self.scale))
        return result

    def create_line(self, *coords, fill="black", width=1, **unused):
        self.draw.line(self.points(coords), fill=fill, width=round(width * self.scale))

    def create_polygon(self, *coords, fill="", **unused):
        self.draw.polygon(self.points(coords), fill=fill)

    def create_text(self, x, y, text="", anchor="center", fill="black", font=("Helvetica", 10), **unused):
        from PIL import ImageFont
        font_file = "DejaVuSans.ttf"
        if len(font) > 2 and font[2] == "bold":
            font_file = "DejaVuSans-Bold.ttf"
        size = round(font[1] * self.scale * 1.35)
        # Tk and PIL name text anchors differently
        pil_anchors = {"center": "mm", "n": "mt", "s": "mb", "e": "rm", "w": "lm", "ne": "rt", "nw": "lt"}
        self.draw.text(self.points((x, y))[0], text, fill=fill, anchor=pil_anchors[anchor],
                       font=ImageFont.truetype(os.path.join(FONT_DIR, font_file), size))


# one speed curve (a list of Mbps numbers) as a line across the chart
def draw_curve(draw, values, color, left, right, top, bottom, highest):
    if len(values) < 2:
        return
    step = (right - left) / (len(values) - 1)
    points = []
    for k in range(len(values)):
        points.append((left + k * step, bottom - values[k] / highest * (bottom - top)))
    draw.line(points, fill=color, width=5)


# one band of the Wi-Fi channel map, drawn by the same code as the Wi-Fi tab (graph.py)
def draw_wifi_chart(draw, x, y, width, height, aps, band):
    from .graph import draw_channel_chart
    from .config import CARD_BG
    draw.rectangle((x, y, x + width, y + height), fill=CARD_BG)
    scale = 2.0
    draw_channel_chart(PdfCanvas(draw, x, y, scale), width / scale, height / scale, aps, band, name_limit=20)


# page 1: big logo, summary boxes and every detail. page 2: speed graph and Wi-Fi channel map.
# aps = a Wi-Fi scan from scan_wifi() (None = leave the Wi-Fi graph out), wifi_stamp = when it ran
def build_pdf(record, path, aps=None, wifi_stamp=""):
    from .history import DETAIL_GROUPS
    fonts = pdf_fonts()
    pages = []
    page, draw, y = new_page(record, fonts, first=True)
    pages.append(page)
    y = draw_summary(draw, fonts, y, record)

    scan_rows = [("Started", format_stamp(record.get("started_at"))),
                 ("Finished", format_stamp(record.get("finished_at"))),
                 ("Complete", "Yes" if record.get("complete") else "No (cable pulled)")]
    sections = [("Scan", scan_rows)]
    for title, rows in DETAIL_GROUPS:
        values = [(label, record.get(key, "--")) for label, key in rows]
        if title == "DNS":
            values.append(("Lookups", f"{record.get('dns_queries', 0)} (last: {record.get('dns_last_domain', '--')})"))
        sections.append((title, values))
    for title, rows in sections:
        if y + 50 + 36 * len(rows) > PAGE_HEIGHT - MARGIN:
            page, draw, y = new_page(record, fonts)
            pages.append(page)
        y = draw_section(draw, fonts, y, title, rows)

    # graphs page
    page, draw, y = new_page(record, fonts)
    pages.append(page)
    width = PAGE_WIDTH - 2 * MARGIN
    draw.text((MARGIN, y), "Speed test", font=fonts["heading"], fill=BLUE)
    draw_speed_chart(draw, fonts, MARGIN, y + 50, width, 440, record)
    y = y + 520
    if aps is not None:
        title = "Wi-Fi channels"
        if wifi_stamp:
            title = title + f"  (scanned {wifi_stamp})"
        draw.text((MARGIN, y), title, font=fonts["heading"], fill=BLUE)
        y = y + 50
        for band in ("2.4", "5"):
            draw.text((MARGIN, y), f"{band} GHz", font=fonts["small"], fill=GREY)
            draw_wifi_chart(draw, MARGIN, y + 34, width, 330, aps, band)
            y = y + 384

    pages[0].save(path, "PDF", resolution=150, save_all=True, append_images=pages[1:])


# keep only the newest MAX_REPORTS PDFs
def delete_old_reports():
    reports = sorted(name for name in os.listdir(REPORT_DIR) if name.endswith(".pdf"))
    for name in reports[:-MAX_REPORTS]:
        os.remove(os.path.join(REPORT_DIR, name))


# serves the PDFs only (no folder listing), and keeps its requests out of the terminal
class ReportHandler(SimpleHTTPRequestHandler):
    def list_directory(self, path):
        self.send_error(404)
        return None

    def log_message(self, format, *args):
        logger.info("phone download: %s", format % args)


# starts the web server the first time it's needed, then leaves it running.
# returns the port it's on
def start_server():
    global _server
    if _server is not None:
        return _server.server_address[1]
    handler = partial(ReportHandler, directory=REPORT_DIR)
    try:
        _server = ThreadingHTTPServer(("0.0.0.0", SHARE_PORT), handler)
    except OSError:
        logger.warning("port %d is taken, using %d for the report server", SHARE_PORT, BACKUP_PORT)
        _server = ThreadingHTTPServer(("0.0.0.0", BACKUP_PORT), handler)
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    port = _server.server_address[1]
    logger.info("report server running on port %d", port)
    start_secure_server(handler)
    return port


# makes a self-signed certificate the first time (good for 10 years)
def make_certificate():
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                    "-subj", "/CN=Not-A-Fluke", "-keyout", KEY_FILE, "-out", CERT_FILE],
                   capture_output=True, check=True, timeout=60)


# the same files over https on port 443. If it can't start, plain http still works
def start_secure_server(handler):
    global _secure_server
    try:
        make_certificate()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERT_FILE, KEY_FILE)
        _secure_server = ThreadingHTTPServer(("0.0.0.0", SECURE_PORT), handler)
        _secure_server.socket = context.wrap_socket(_secure_server.socket, server_side=True)
        threading.Thread(target=_secure_server.serve_forever, daemon=True).start()
        logger.info("report server also on https port %d", SECURE_PORT)
    except Exception:
        logger.exception("could not start the https report server (http still works)")


# the Pi's address the phone can reach: its Wi-Fi first (phones are usually on Wi-Fi),
# otherwise the cable's. returns (ip, interface) or ("", "") if it has neither
def phone_reachable_ip():
    for interface in (WIFI_INTERFACE, INTERFACE):
        ip = get_ip(interface)
        if ip != "":
            return ip, interface
    return "", ""


# makes the PDF and starts serving it. returns (url, interface) or raises with a reason
def share_record(record, aps=None, wifi_stamp=""):
    ip, interface = phone_reachable_ip()
    if ip == "":
        raise RuntimeError("The Pi has no IP address, so a phone can't reach it")
    os.makedirs(REPORT_DIR, exist_ok=True)
    name = report_filename(record)
    build_pdf(record, os.path.join(REPORT_DIR, name), aps, wifi_stamp)
    delete_old_reports()
    port = start_server()
    address = ip if port == 80 else f"{ip}:{port}"
    return f"http://{address}/{name}", interface


# QR code as rows of True (black) / False (white) squares, quiet border included
def qr_matrix(text):
    import qrcode
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.get_matrix()
