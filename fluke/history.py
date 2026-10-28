# scan history: saves each scan to /var/log/fluke-gui/scans.jsonl (newest 500 kept)
# plus the History / Scan Detail screens

import os
import sys
import json
import logging
import subprocess
import tkinter as tk
from datetime import datetime
from logging.handlers import RotatingFileHandler

from .config import (LOG_DIR, SCAN_LOG_PATH, EVENT_LOG_PATH, MAX_SCAN_RECORDS, HISTORY_PAGE_SIZE,
                     BG, CARD_BG, BAR_BG, ACCENT, TEXT, MUTED, logger)


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


# --- HISTORY SCREENS (mixed into FlukeApp, see app.py) ---

class HistoryScreens:

    # paged list of past scans, 6 per page
    def build_history_ui(self):
        self.history_frame = tk.Frame(self.container, bg=BG)

        #pack the bottom row first so the list gets the leftover space
        nav = tk.Frame(self.history_frame, bg=BG)
        nav.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=4)
        self.make_button(nav, "‹ Newer", self.history_newer, size=11).pack(side=tk.LEFT)
        self.make_button(nav, "Older ›", self.history_older, size=11).pack(side=tk.RIGHT)
        self.history_page_lbl = tk.Label(nav, text="", font=("Helvetica", 10), bg=BG, fg=MUTED)
        self.history_page_lbl.pack(expand=True)

        self.history_rows = tk.Frame(self.history_frame, bg=BG)
        self.history_rows.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

    def build_detail_ui(self):
        self.detail_frame = tk.Frame(self.container, bg=BG)
        bar = self.make_header(self.detail_frame, "Scan detail", back=self.show_history_frame)
        # send this scan to a phone as a PDF (share.py)
        self.make_button(bar, "QR", lambda: self.open_share(self.detail_record, "detail"),
                         size=11).pack(side=tk.RIGHT, padx=6)

        # scrollable since a full scan doesn't fit on screen
        self.detail_canvas, detail_inner = self.make_scroll_area(self.detail_frame)
        self.detail_lbl = tk.Label(
            detail_inner, text="", font=("Courier", 12, "bold"),
            bg=BG, fg=TEXT, justify=tk.LEFT, anchor="nw"
        )
        self.detail_lbl.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

    # reload from disk and go to page 1
    def open_history(self):
        self.history_records = load_scan_history()
        self.history_page = 0
        self.render_history_page()
        self.show_history_frame()

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
            ).pack(pady=40)
            self.history_page_lbl.config(text="0 / 0")
            return

        pages = (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
        start = self.history_page * HISTORY_PAGE_SIZE
        for record in self.history_records[start:start + HISTORY_PAGE_SIZE]:
            tk.Button(
                self.history_rows, text=format_record_summary(record), font=("Courier", 11),
                anchor="w", bg=CARD_BG, fg=TEXT, activebackground=BAR_BG,
                activeforeground=TEXT, relief=tk.FLAT, pady=4,
                command=lambda r=record: self.show_detail_ui(r)
            ).pack(fill=tk.X, pady=2)

        self.history_page_lbl.config(text=f"Page {self.history_page + 1} / {pages}  (! = cut short)")

    def history_newer(self):
        if self.history_page > 0:
            self.history_page = self.history_page - 1
            self.render_history_page()

    def history_older(self):
        if (self.history_page + 1) * HISTORY_PAGE_SIZE < len(self.history_records):
            self.history_page = self.history_page + 1
            self.render_history_page()

    def show_detail_ui(self, record):
        self.detail_record = record
        self.detail_lbl.config(text=format_record_detail(record))
        self.show_frame(self.detail_frame, "detail")
        self.detail_canvas.yview_moveto(0)
