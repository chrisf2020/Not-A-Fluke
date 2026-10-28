# send a scan to your phone, two ways:
#   PDF:  makes a PDF report, serves it from a tiny web server on the Pi, and the QR code is a
#         link to it. The phone has to be on the same network as the Pi.
#   Message: the QR code holds the scan itself as a text message draft (an sms: link). Works with
#         no network: the phone opens Messages with the results typed in, you pick who it goes to.
#         (Plain text just gets Googled by the iPhone camera, and a mailto: with no address
#         shows "Add to contacts", so it's sms.)

import os
import ssl
import subprocess
import threading
import urllib.parse
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
def share_record(record):
    ip, interface = phone_reachable_ip()
    if ip == "":
        raise RuntimeError("The Pi has no IP address, so a phone can't reach it")
    os.makedirs(REPORT_DIR, exist_ok=True)
    name = report_filename(record)
    build_pdf(record, os.path.join(REPORT_DIR, name))
    delete_old_reports()
    port = start_server()
    address = ip if port == 80 else f"{ip}:{port}"
    return f"http://{address}/{name}", interface


# the scan as short text for the Text QR (speeds in Mbps). Kept to about 200 characters so
# the QR squares stay big enough for a phone to read off the small screen
def scan_text(record):
    def get(key):
        value = str(record.get(key, "--"))
        # shorter words for the long values, so more fits
        for long, short in ((" / Untagged", ""), ("Not Advertised", "--"), ("Not advertised", "no"),
                            (" Mbps", ""), (" ms", "ms"), (" %", "%"), ("GigabitEthernet", "Gi")):
            value = value.replace(long, short)
        return value

    when = format_stamp(record.get("started_at"), short=True)
    lines = [
        f"NOT-A-FLUKE {when}",
        f"SW {get('switch_name')} {get('switch_proto')}",
        f"PORT {get('switch_port')} VLAN {get('switch_vlan')}",
        f"SW IP {get('switch_ip')} VOICE {get('switch_voice')}",
        f"POE {get('switch_poe')}  LINK {get('link_speed')}",
        f"DOWN {get('speed_down')} UP {get('speed_up')} {get('speed_rating')}",
        f"LAT {get('ping_latency')} JIT {get('ping_jitter')} LOSS {get('ping_loss')}",
        f"DNS {get('dns_server')} {get('dns_time')}",
    ]
    return "\n".join(lines)


# the scan as a text message draft link. "sms:?&body=" opens on both iPhone and Android
def scan_message_link(record):
    return "sms:?&body=" + urllib.parse.quote(scan_text(record))


# QR code as rows of True (black) / False (white) squares, quiet border included
def qr_matrix(text, error_correction="M"):
    import qrcode
    levels = {"L": qrcode.constants.ERROR_CORRECT_L, "M": qrcode.constants.ERROR_CORRECT_M}
    qr = qrcode.QRCode(border=2, error_correction=levels[error_correction])
    qr.add_data(text)
    qr.make(fit=True)
    return qr.get_matrix()
