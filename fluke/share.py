# send a scan to your phone: makes a PDF report, serves it from a tiny web server on the Pi,
# and gives a QR code that points at it. Scan the QR with the phone camera to open the PDF.
# The phone has to be on the same network as the Pi (same Wi-Fi, or the network the cable is on).

import os
import threading
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

from .config import VERSION, LOG_DIR, INTERFACE, WIFI_INTERFACE, logger
from .history import format_record_detail, format_stamp
from .speed import get_ip

REPORT_DIR = os.path.join(LOG_DIR, "reports")
SHARE_PORT = 8080
MAX_REPORTS = 20        # older PDFs get deleted so the SD card doesn't fill up

# letter size page at 150 dpi
PAGE_WIDTH = 1275
PAGE_HEIGHT = 1650
MARGIN = 90
FONT_DIR = "/usr/share/fonts/truetype/dejavu"

_server = None


# e.g. scan-2026-10-28_12-59-29.pdf
def report_filename(record):
    stamp = str(record.get("started_at") or "unknown").replace("T", "_").replace(":", "-")
    return f"scan-{stamp}.pdf"


# draws the report text onto white pages and saves them as one PDF
def build_pdf(record, path):
    from PIL import Image, ImageDraw, ImageFont
    title_font = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf"), 44)
    small_font = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSans.ttf"), 24)
    body_font = ImageFont.truetype(os.path.join(FONT_DIR, "DejaVuSansMono.ttf"), 28)
    line_height = 40

    lines = format_record_detail(record).split("\n")
    pages = []
    page = None
    y = 0
    for line in lines:
        if page is None or y + line_height > PAGE_HEIGHT - MARGIN:
            page = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), "white")
            draw = ImageDraw.Draw(page)
            draw.text((MARGIN, MARGIN), "Not-A-Fluke scan report", font=title_font, fill="black")
            subtitle = f"Scan {format_stamp(record.get('started_at'))}   |   Not-A-Fluke v{VERSION}"
            draw.text((MARGIN, MARGIN + 60), subtitle, font=small_font, fill="#555555")
            draw.line((MARGIN, MARGIN + 105, PAGE_WIDTH - MARGIN, MARGIN + 105), fill="#999999", width=2)
            y = MARGIN + 130
            pages.append(page)
        draw.text((MARGIN, y), line, font=body_font, fill="black")
        y = y + line_height

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


# starts the web server the first time it's needed, then leaves it running
def start_server():
    global _server
    if _server is not None:
        return
    handler = partial(ReportHandler, directory=REPORT_DIR)
    _server = ThreadingHTTPServer(("0.0.0.0", SHARE_PORT), handler)
    threading.Thread(target=_server.serve_forever, daemon=True).start()
    logger.info("report server running on port %d", SHARE_PORT)


# the Pi's address the phone can reach: its Wi-Fi first (phones are usually on Wi-Fi),
# otherwise the cable's. returns (ip, interface) or ("", "") if it has neither
def phone_reachable_ip():
    for interface in (WIFI_INTERFACE, INTERFACE):
        ip = get_ip(interface)
        if ip != "":
            return ip, interface
    return "", ""


# makes the PDF and starts serving it. returns (url, interface) or raises with a reason
def share_record(record):
    ip, interface = phone_reachable_ip()
    if ip == "":
        raise RuntimeError("The Pi has no IP address, so a phone can't reach it")
    os.makedirs(REPORT_DIR, exist_ok=True)
    name = report_filename(record)
    build_pdf(record, os.path.join(REPORT_DIR, name))
    delete_old_reports()
    start_server()
    return f"http://{ip}:{SHARE_PORT}/{name}", interface


# QR code as rows of True (black) / False (white) squares, quiet border included
def qr_matrix(text):
    import qrcode
    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.get_matrix()
