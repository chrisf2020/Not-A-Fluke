# send a scan to your phone: makes a PDF report, serves it from a tiny web server on the Pi,
# and the QR code is a link to it. The phone has to be on the same network as the Pi.

import os
import ssl
import subprocess
import threading
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

from .config import VERSION, LOG_DIR, INTERFACE, WIFI_INTERFACE, logger
from .history import format_record_detail, format_stamp
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


# e.g. scan-2026-10-28_12-59-29.pdf
def report_filename(record):
    stamp = str(record.get("started_at") or "unknown").replace("T", "_").replace(":", "-")
    return f"scan-{stamp}.pdf"


def pdf_fonts():
    from PIL import ImageFont
    return {
        "title": ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"), 44),
        "heading": ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"), 32),
        "small": ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans.ttf"), 24),
        "body": ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSansMono.ttf"), 28),
    }


# a white page with the report title on it. returns (page, draw, y where the content starts)
def new_page(record, fonts):
    from PIL import Image, ImageDraw
    page = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), "white")
    draw = ImageDraw.Draw(page)
    draw.text((MARGIN, MARGIN), "Not-A-Fluke scan report", font=fonts["title"], fill="black")
    subtitle = f"Scan {format_stamp(record.get('started_at'))}   |   Not-A-Fluke v{VERSION}"
    draw.text((MARGIN, MARGIN + 60), subtitle, font=fonts["small"], fill="#555555")
    draw.line((MARGIN, MARGIN + 105, PAGE_WIDTH - MARGIN, MARGIN + 105), fill="#999999", width=2)
    return page, draw, MARGIN + 130


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
        pil_anchors = {"center": "mm", "n": "mt", "s": "mb", "e": "rm", "w": "lm"}
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


# the download and upload speed curves in a dark box, like the speed graph on screen
def draw_speed_chart(draw, fonts, x, y, width, height, record):
    from .graph import DOWN_COLOR, UP_COLOR
    from .config import CARD_BG, LINE, MUTED
    curves = record.get("speed_curves") or {}
    down = curves.get("down") or []
    up = curves.get("up") or []
    draw.rectangle((x, y, x + width, y + height), fill=CARD_BG)
    if len(down) < 2 and len(up) < 2:
        draw.text((x + width / 2, y + height / 2), "No speed graph saved for this scan",
                  font=fonts["small"], fill="#888888", anchor="mm")
        return

    left = x + 110
    right = x + width - 30
    top = y + 30
    bottom = y + height - 60
    highest = max(down + up) * 1.15

    # Mbps lines and numbers down the left
    for k in range(5):
        line_y = bottom - (bottom - top) * k / 4
        draw.line((left, line_y, right, line_y), fill=LINE, width=1)
        draw.text((left - 12, line_y), str(round(highest * k / 4)), font=fonts["small"], fill=MUTED, anchor="rm")
    draw.text((x + 20, top - 10), "Mbps", font=fonts["small"], fill=MUTED, anchor="lm")

    draw_curve(draw, down, DOWN_COLOR, left, right, top, bottom, highest)
    draw_curve(draw, up, UP_COLOR, left, right, top, bottom, highest)

    draw.text((left, bottom + 32), "Download " + str(record.get("speed_down", "--")),
              font=fonts["small"], fill=DOWN_COLOR, anchor="lm")
    draw.text((left + width / 2.4, bottom + 32), "Upload " + str(record.get("speed_up", "--")),
              font=fonts["small"], fill=UP_COLOR, anchor="lm")


# one band of the Wi-Fi channel map, drawn by the same code as the Wi-Fi tab (graph.py)
def draw_wifi_chart(draw, x, y, width, height, aps, band):
    from .graph import draw_channel_chart
    from .config import CARD_BG
    draw.rectangle((x, y, x + width, y + height), fill=CARD_BG)
    scale = 2.5
    draw_channel_chart(PdfCanvas(draw, x, y, scale), width / scale, height / scale, aps, band)


# report text first, then a page with the speed graph and the Wi-Fi channel map.
# aps = a Wi-Fi scan from scan_wifi() (None = leave the Wi-Fi graph out), wifi_stamp = when it ran
def build_pdf(record, path, aps=None, wifi_stamp=""):
    fonts = pdf_fonts()
    line_height = 40
    pages = []
    page, draw, y = new_page(record, fonts)
    pages.append(page)
    for line in format_record_detail(record).split("\n"):
        if y + line_height > PAGE_HEIGHT - MARGIN:
            page, draw, y = new_page(record, fonts)
            pages.append(page)
        draw.text((MARGIN, y), line, font=fonts["body"], fill="black")
        y = y + line_height

    # graphs page
    page, draw, y = new_page(record, fonts)
    pages.append(page)
    width = PAGE_WIDTH - 2 * MARGIN
    draw.text((MARGIN, y), "Speed test", font=fonts["heading"], fill="black")
    draw_speed_chart(draw, fonts, MARGIN, y + 50, width, 420, record)
    y = y + 500
    if aps is not None:
        title = "Wi-Fi channels (signal in dBm)"
        if wifi_stamp:
            title = title + f", scanned {wifi_stamp}"
        draw.text((MARGIN, y), title, font=fonts["heading"], fill="black")
        y = y + 50
        for band in ("2.4", "5"):
            draw.text((MARGIN, y), f"{band} GHz", font=fonts["small"], fill="#555555")
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
