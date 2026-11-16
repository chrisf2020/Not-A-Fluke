# scan history: saves each scan to /var/log/fluke-gui/scans.jsonl (newest 5 pages = 30 kept)
# plus the History / Scan Detail screens

import os
import sys
import json
import logging
import subprocess
import tkinter as tk
from datetime import datetime
from logging.handlers import RotatingFileHandler

from .config import (LOG_DIR, SCAN_LOG_PATH, WIFI_LOG_PATH, EVENT_LOG_PATH, MAX_SCAN_RECORDS, HISTORY_PAGE_SIZE,
                     BG, CARD_BG, BAR_BG, ACCENT, TEXT, MUTED, GOOD, OK, BAD, logger, px)


# --- LOGGING + HISTORY HELPERS ---

# logs go to a rotating file in LOG_DIR
def setup_logging():
    logger.setLevel(logging.INFO)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = RotatingFileHandler(EVENT_LOG_PATH, maxBytes=500_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    except OSError as err:
        print(f"[!] Could not open log folder {LOG_DIR}: {err}", file=sys.stderr)


# e.g. 2026-09-30T14:05:12
def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


# Pi has no battery clock, so without internet the time can be off
# returns "yes", "no" or "unknown"
def is_clock_synced():
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"

    value = result.stdout.strip()
    if value == "yes" or value == "no":
        return value
    return "unknown"


# trims to the newest MAX_SCAN_RECORDS so the SD card doesn't fill up
# returns False if the file write fails
def save_scan_record(record, path=SCAN_LOG_PATH):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)

        lines = []
        if os.path.exists(path):
            with open(path, "r") as f:
                for line in f.read().splitlines():
                    if line.strip() != "":
                        lines.append(line)

        lines.append(json.dumps(record))
        lines = lines[-MAX_SCAN_RECORDS:]  #keep newest only

        #temp file first so a power cut can't leave half a log
        temp_path = path + ".tmp"
        with open(temp_path, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(temp_path, path)
        return True
    except OSError as err:
        logger.error("could not save scan record: %s", err)
        return False


# skips any broken lines, returns newest first
def load_scan_history(path=SCAN_LOG_PATH):
    records = []
    if not os.path.exists(path):
        return records

    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line == "":
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("skipping corrupt line in scan log")
    except OSError as err:
        logger.error("could not read scan log: %s", err)

    records.reverse()  #newest first
    return records


# 2026-09-30T14:05:12 -> 2026-09-30 02:05:12 PM (short=True gives 09-30 02:05PM)
def format_stamp(stamp, short=False):
    if not stamp or stamp == "--":
        return "--"
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp.replace("T", " ")
    if short:
        return when.strftime("%m-%d %I:%M%p")
    return when.strftime("%Y-%m-%d %I:%M:%S %p")


# GigabitEthernet1/0/24 -> Gi1/0/24 so it fits the small screen
def short_port_name(port):
    port = port.replace("TenGigabitEthernet", "Te")
    port = port.replace("TwoGigabitEthernet", "Tw")
    port = port.replace("GigabitEthernet", "Gi")
    port = port.replace("FastEthernet", "Fa")
    return port


# one line for the history list, starts with "!" if the scan got cut short
def format_record_summary(record):
    marker = " " if record.get("complete") else "!"
    when = format_stamp(record.get("started_at"), short=True)
    name = str(record.get("switch_name", "--"))[:12]
    port = short_port_name(str(record.get("switch_port", "--")))[:8]
    down = str(record.get("speed_down", "--")).replace(" Mbps", "M")
    return f"{marker}{when} {name:<12} {port:<8} {down}"


def format_record_detail(record):
    lines = [
        f"Started:   {format_stamp(record.get('started_at'))}",
        f"Finished:  {format_stamp(record.get('finished_at'))}",
        f"Clock sync: {record.get('clock_synced', 'unknown')}",
        f"Complete:  {'yes' if record.get('complete') else 'NO (cable pulled)'}",
        "",
        f"SW:    {record.get('switch_name', '--')}",
        f"VIA:   {record.get('switch_proto', '--')}",
        f"IP:    {record.get('switch_ip', '--')}",
        f"PORT:  {record.get('switch_port', '--')}",
        f"VLAN:  {record.get('switch_vlan', '--')}",
        f"VOICE: {record.get('switch_voice', '--')}",
        f"LINK:  {record.get('link_speed', '--')}",
        "",
        f"Down: {record.get('speed_down', '--')}",
        f"Up:   {record.get('speed_up', '--')}",
        f"Ping: {record.get('speed_ping', '--')}",
        f"Rating: {record.get('speed_rating', '--')}",
        f"Latency: {record.get('ping_latency', '--')}  Jitter: {record.get('ping_jitter', '--')}",
        f"Packet loss: {record.get('ping_loss', '--')}",
        "",
        f"DNS server: {record.get('dns_server', '--')}  {record.get('dns_time', '--')}",
        f"DNS queries: {record.get('dns_queries', 0)}  Last: {record.get('dns_last_domain', '--')}",
        "",
        f"PoE: {record.get('switch_poe', '--')}",
    ]
    return "\n".join(lines)


# the sections of a scan's details: (title, [(label, key in the record), ...]). Used by the
# Scan detail screen and the PDF
DETAIL_GROUPS = [
    ("Switch", [("Switch", "switch_name"), ("Port", "switch_port"), ("VLAN", "switch_vlan"),
                ("Voice VLAN", "switch_voice"), ("PoE", "switch_poe"), ("Link", "link_speed"),
                ("Switch IP", "switch_ip"), ("Found via", "switch_proto")]),
    ("DNS", [("Server", "dns_server"), ("Answer", "dns_time")]),
    ("Speed", [("Download", "speed_down"), ("Upload", "speed_up"), ("Ping", "ping_latency"),
               ("Jitter", "ping_jitter"), ("Packet loss", "ping_loss"), ("Rating", "speed_rating"),
               ("Server", "speed_server")]),
]


# speed tests grouped by where they ran: each switch (LAN) and each Wi-Fi network.
# returns [(title, [(download, upload), ...] oldest first), ...]. Failed tests are left out
def speed_groups(lan_records, wifi_records):
    from .speed import read_number
    groups = {}
    for kind, records, key in (("LAN", lan_records, "switch_name"), ("Wi-Fi", wifi_records, "ssid")):
        for record in reversed(records):            # the logs load newest first
            down = read_number(record.get("speed_down"))
            if down is not None:
                title = f"{kind}  {record.get(key, '--')}"
                groups.setdefault(title, []).append((down, read_number(record.get("speed_up")) or 0))
    return list(groups.items())


# green / yellow / red for a scan: red if it failed or got cut short, otherwise by the speed
def record_color(record):
    rating = record.get("speed_rating", "--")
    if not record.get("complete") or not str(record.get("speed_down", ""))[:1].isdigit():
        return BAD
    if rating == "Poor":
        return BAD
    if rating == "Fair":
        return OK
    return GOOD


# --- HISTORY SCREENS (mixed into FlukeApp, see app.py) ---

class HistoryScreens:

    # paged list of past scans, as many per page as fit on the screen
    def build_history_ui(self):
        self.history_frame = tk.Frame(self.container, bg=BG)
        self.history_page_size = HISTORY_PAGE_SIZE
        self.make_switcher(self.history_frame, self.history_tabs(), "Scans")

        #pack the bottom row first so the list gets the leftover space
        nav = tk.Frame(self.history_frame, bg=BG)
        nav.pack(side=tk.BOTTOM, fill=tk.X, padx=px(8), pady=px(4))
        self.make_button(nav, "‹ Newer", self.history_newer, size=11).pack(side=tk.LEFT)
        self.make_button(nav, "Older ›", self.history_older, size=11).pack(side=tk.RIGHT)
        self.history_page_lbl = tk.Label(nav, text="", font=("Helvetica", 10), bg=BG, fg=MUTED)
        self.history_page_lbl.pack(expand=True)

        self.history_rows = tk.Frame(self.history_frame, bg=BG)
        self.history_rows.pack(fill=tk.BOTH, expand=True, padx=px(8), pady=px(4))

    # Scans | Speed history along the top of the History tab
    def history_tabs(self):
        return [("Scans", self.open_history), ("Speed history", self.open_speed_history)]

    # one card per switch / Wi-Fi network with a graph of every speed test run on it
    def build_speed_history_ui(self):
        self.speed_history_frame = tk.Frame(self.container, bg=BG)
        self.make_switcher(self.speed_history_frame, self.history_tabs(), "Speed history")
        self.speed_history_canvas, self.speed_history_inner = self.make_scroll_area(self.speed_history_frame)

    def open_speed_history(self):
        from .graph import draw_history_chart
        for child in self.speed_history_inner.winfo_children():
            child.destroy()
        groups = speed_groups(load_scan_history(), load_scan_history(WIFI_LOG_PATH))
        if len(groups) == 0:
            tk.Label(self.speed_history_inner, text="No speed tests yet", font=("Helvetica", 14),
                     bg=BG, fg=MUTED).pack(pady=px(40))
        for title, tests in groups:
            card = self.make_card(self.speed_history_inner, title)
            down, up = tests[-1]
            tk.Label(card, text=f"{len(tests)} tests   last: {down:g} down / {up:g} up Mbps", font=("Helvetica", 10),
                     bg=CARD_BG, fg=MUTED, anchor="w").pack(fill=tk.X, padx=px(10))
            chart = tk.Canvas(card, height=px(140), bg=CARD_BG, highlightthickness=0)
            chart.pack(fill=tk.X, padx=px(6), pady=px(6))
            chart.bind("<Configure>", lambda event, c=chart, t=tests: draw_history_chart(c, event.width, event.height, t))
        self.show_frame(self.speed_history_frame, "speed_history")
        self.speed_history_canvas.yview_moveto(0)

    # same cards as the cable results, filled in when a scan is tapped
    def build_detail_ui(self):
        self.detail_frame = tk.Frame(self.container, bg=BG)
        bar = self.make_header(self.detail_frame, "Scan detail", back=self.show_history_frame)
        # send this scan to a phone as a PDF (share.py)
        self.make_button(bar, "QR", lambda: self.open_share(self.detail_record, "detail"),
                         size=11).pack(side=tk.RIGHT, padx=px(6))
        self.detail_canvas, self.detail_inner = self.make_scroll_area(self.detail_frame)

    # reload from disk and go to page 1
    def open_history(self):
        self.history_records = load_scan_history()[:MAX_SCAN_RECORDS]
        self.history_page = 0
        self.show_history_frame()
        # how many rows fit depends on the screen (about px(47) each)
        self.root.update_idletasks()
        height = self.history_rows.winfo_height()
        if height > 1:
            self.history_page_size = max(3, height // px(47))
        self.render_history_page()

    def show_history_frame(self):
        self.show_frame(self.history_frame, "history")

    def render_history_page(self):
        for child in self.history_rows.winfo_children():
            child.destroy()

        total = len(self.history_records)
        if total == 0:
            tk.Label(
                self.history_rows, text="No scans logged yet", font=("Helvetica", 14),
                bg=BG, fg=MUTED
            ).pack(pady=px(40))
            self.history_page_lbl.config(text="")
            return

        size = self.history_page_size
        pages = (total + size - 1) // size
        start = self.history_page * size
        for record in self.history_records[start:start + size]:
            self.make_history_row(record)

        self.history_page_lbl.config(text=f"Page {self.history_page + 1} of {pages}")

    # one scan: coloured stripe (green good, yellow so-so, red failed or slow), switch and port,
    # when, download speed in the same colour
    def make_history_row(self, record):
        color = record_color(record)
        row = tk.Frame(self.history_rows, bg=CARD_BG)
        row.pack(fill=tk.X, pady=px(2))
        stripe = tk.Frame(row, bg=color, width=px(6))
        stripe.pack(side=tk.LEFT, fill=tk.Y)

        down = str(record.get("speed_down", "--")).replace(" Mbps", "")
        speed = tk.Label(row, text=down, font=("Helvetica", 14, "bold"), bg=CARD_BG, fg=color)
        unit = tk.Label(row, text="Mbps" if down[:1].isdigit() else "", font=("Helvetica", 9), bg=CARD_BG, fg=MUTED)
        unit.pack(side=tk.RIGHT, padx=(0, px(10)))
        speed.pack(side=tk.RIGHT, padx=(px(6), px(3)))

        middle = tk.Frame(row, bg=CARD_BG)
        middle.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=px(8), pady=px(3))
        name = str(record.get("switch_name", "--"))
        port = short_port_name(str(record.get("switch_port", "--")))
        if port not in ("--", "Not Found"):
            name = f"{name}  {port}"
        when = format_stamp(record.get("started_at"), short=True)
        if not record.get("complete"):
            when += "   cut short"
        top = tk.Label(middle, text=name, font=("Helvetica", 12, "bold"), bg=CARD_BG, fg=TEXT, anchor="w")
        bottom = tk.Label(middle, text=when, font=("Helvetica", 10), bg=CARD_BG, fg=MUTED, anchor="w")
        top.pack(fill=tk.X)
        bottom.pack(fill=tk.X)

        # tap anywhere on the row for the details (not when it was a drag)
        for widget in (row, stripe, speed, unit, middle, top, bottom):
            widget.bind("<ButtonRelease-1>", lambda event, r=record: self.open_detail(r))

    def open_detail(self, record):
        if not self.drag_moved:
            self.show_detail_ui(record)

    def history_newer(self):
        if self.history_page > 0:
            self.history_page = self.history_page - 1
            self.render_history_page()

    def history_older(self):
        if (self.history_page + 1) * self.history_page_size < len(self.history_records):
            self.history_page = self.history_page + 1
            self.render_history_page()

    def show_detail_ui(self, record):
        self.detail_record = record
        for child in self.detail_inner.winfo_children():
            child.destroy()

        # the same Link / Switch / DNS / Internet boxes as the results, tap one for its log
        from .app import scan_summary
        summary = tk.Frame(self.detail_inner, bg=BG)
        summary.pack(fill=tk.X, padx=px(8), pady=(px(6), 0))
        for label, (text, color) in zip(self.make_summary_row(summary, lambda: record, "detail"), scan_summary(record)):
            label.config(text="● " + text, fg=color)

        scan = self.make_card(self.detail_inner, "Scan")
        self.make_row(scan, "Started", format_stamp(record.get("started_at")))
        self.make_row(scan, "Finished", format_stamp(record.get("finished_at")))
        self.make_row(scan, "Complete", "Yes" if record.get("complete") else "No (cable pulled)")
        self.make_row(scan, "Clock synced", record.get("clock_synced", "unknown"))

        for title, rows in DETAIL_GROUPS:
            card = self.make_card(self.detail_inner, title)
            for label, key in rows:
                self.make_row(card, label, str(record.get(key, "--")))
            if title == "DNS":
                self.make_row(card, "Lookups", f"{record.get('dns_queries', 0)}  (last: {record.get('dns_last_domain', '--')})")
            tk.Frame(card, bg=CARD_BG, height=px(6)).pack()

        self.show_frame(self.detail_frame, "detail")
        self.detail_canvas.yview_moveto(0)
