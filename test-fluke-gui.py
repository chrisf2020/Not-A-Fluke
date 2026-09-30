#!/usr/bin/env python3
# fluke-gui — full-screen touchscreen version of the Pi Fluke tester (Tkinter).
# Same PiScout-style results as netcheck/fluke-screen, as one Python window instead of a
# shell script writing to the framebuffer. Run with:  sudo bin/fluke-gui.py
#
# THE BUG THIS FIXES: it used to sniff for CDP/LLDP packets itself with scapy, for a fixed
# 30 seconds. Cisco only sends a CDP announcement every ~60s (LLDP is ~30s), so that window
# regularly closed before anything arrived and "Switch Topology" showed Not Found even on a
# switch that was announcing the whole time. This version asks the `lldpd` background service
# instead (same as bin/netcheck's `lldpctl` call) — it listens continuously, so we usually get
# an answer immediately, and we wait a full announcement cycle instead of half of one.
#
# LOGGING: every scan is saved as one JSON line in /var/log/fluke-gui/scans.jsonl (newest 500
# kept), with start/finish timestamps and a "complete" flag (false if the cable was pulled
# mid-scan). Events and errors go to /var/log/fluke-gui/events.log (rotating). Past scans can
# be browsed with the History button on screen, or printed over SSH with:  bin/fluke-gui.py --history

import os
import sys
import json
import time
import socket
import logging
import threading
import subprocess
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from logging.handlers import RotatingFileHandler
import speedtest
from scapy.all import sniff
from scapy.layers.dns import DNS, DNSQR

INTERFACE = "eth0"
CARRIER_PATH = f"/sys/class/net/{INTERFACE}/carrier"
# Wait a bit past Cisco's slower ~60s CDP interval so a fresh cable always gets one full cycle.
SWITCH_DISCOVERY_TIMEOUT = 65

LOG_DIR = "/var/log/fluke-gui"
SCAN_LOG_PATH = os.path.join(LOG_DIR, "scans.jsonl")
EVENT_LOG_PATH = os.path.join(LOG_DIR, "events.log")
MAX_SCAN_RECORDS = 500
HISTORY_PAGE_SIZE = 6

logger = logging.getLogger("fluke-gui")


# --- LOGGING + HISTORY HELPERS (no Tkinter needed, so --history works over SSH) ---

#--------------------------------------------------------------------------
# setup_logging
# points the "fluke-gui" logger at a rotating file in LOG_DIR
# input: none
# output: none (prints a warning to stderr if the log folder can't be made)
#--------------------------------------------------------------------------
def setup_logging():
    logger.setLevel(logging.INFO)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = RotatingFileHandler(EVENT_LOG_PATH, maxBytes=500_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
    except OSError as err:
        print(f"[!] Could not open log folder {LOG_DIR}: {err}", file=sys.stderr)


#--------------------------------------------------------------------------
# now_stamp
# current local time as text, e.g. 2026-09-30T14:05:12
# input: none
# output: string
#--------------------------------------------------------------------------
def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


#--------------------------------------------------------------------------
# is_clock_synced
# asks systemd whether the Pi's clock has been set by NTP (the Pi has no
# battery clock, so on a network with no internet the time can be wrong)
# input: none
# output: "yes", "no", or "unknown"
#--------------------------------------------------------------------------
def is_clock_synced():
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=5
        )
    except FileNotFoundError:
        return "unknown"
    except subprocess.TimeoutExpired:
        return "unknown"

    value = result.stdout.strip()
    if value == "yes" or value == "no":
        return value
    return "unknown"


#--------------------------------------------------------------------------
# save_scan_record
# appends one scan to the jsonl log, then trims the file to the newest
# MAX_SCAN_RECORDS lines so the SD card never fills up
# input: record (dict of scan results)
# output: True if saved, False on a file error
#--------------------------------------------------------------------------
def save_scan_record(record):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)

        lines = []
        if os.path.exists(SCAN_LOG_PATH):
            with open(SCAN_LOG_PATH, "r") as f:
                for line in f.read().splitlines():
                    if line.strip() != "":
                        lines.append(line)

        lines.append(json.dumps(record))
        lines = lines[-MAX_SCAN_RECORDS:]  #keep newest only

        #write to a temp file first so a power cut can't leave half a log
        temp_path = SCAN_LOG_PATH + ".tmp"
        with open(temp_path, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(temp_path, SCAN_LOG_PATH)
        return True
    except OSError as err:
        logger.error("could not save scan record: %s", err)
        return False


#--------------------------------------------------------------------------
# load_scan_history
# reads every saved scan, skipping any line that isn't valid json
# input: none
# output: list of dicts, newest first
#--------------------------------------------------------------------------
def load_scan_history():
    records = []
    if not os.path.exists(SCAN_LOG_PATH):
        return records

    try:
        with open(SCAN_LOG_PATH, "r") as f:
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


#--------------------------------------------------------------------------
# format_stamp
# turns 2026-09-30T14:05:12 into 2026-09-30 14:05:12 for display
# input: stamp (string, may be "--" or missing)
# output: string
#--------------------------------------------------------------------------
def format_stamp(stamp):
    if not stamp:
        return "--"
    return stamp.replace("T", " ")


#--------------------------------------------------------------------------
# format_record_summary
# one short line per scan for the history list
# input: record (dict)
# output: string like "  09-30 14:05  SWITCH-01      Gi1/0/5    93.4 Mbps"
#         (starts with "! " instead of two spaces if the scan was cut short)
#--------------------------------------------------------------------------
def format_record_summary(record):
    marker = "  " if record.get("complete") else "! "
    when = format_stamp(record.get("started_at"))[5:16]
    name = str(record.get("switch_name", "--"))[:14]
    port = str(record.get("switch_port", "--"))[:9]
    down = str(record.get("speed_down", "--"))
    return f"{marker}{when}  {name:<14}  {port:<9}  {down}"


#--------------------------------------------------------------------------
# format_record_detail
# full multi-line text for one scan, used by the detail screen
# input: record (dict)
# output: string
#--------------------------------------------------------------------------
def format_record_detail(record):
    lines = [
        f"Started:   {format_stamp(record.get('started_at'))}",
        f"Finished:  {format_stamp(record.get('finished_at'))}",
        f"Clock sync: {record.get('clock_synced', 'unknown')}",
        f"Complete:  {'yes' if record.get('complete') else 'NO (cable pulled)'}",
        "",
        f"SW:    {record.get('switch_name', '--')} ({record.get('switch_proto', '--')})",
        f"IP:    {record.get('switch_ip', '--')}",
        f"PORT:  {record.get('switch_port', '--')}",
        f"VLAN:  {record.get('switch_vlan', '--')}",
        f"VOICE: {record.get('switch_voice', '--')}",
        "",
        f"Down: {record.get('speed_down', '--')}",
        f"Up:   {record.get('speed_up', '--')}",
        f"Ping: {record.get('speed_ping', '--')}",
        "",
        f"DNS queries: {record.get('dns_queries', 0)}  Last: {record.get('dns_last_domain', '--')}",
    ]
    return "\n".join(lines)


#--------------------------------------------------------------------------
# print_history
# prints the scan log as a table, oldest first so the newest is at the bottom
# input: none
# output: none (prints to the terminal)
#--------------------------------------------------------------------------
def print_history():
    records = load_scan_history()
    if len(records) == 0:
        print("No scans logged yet.")
        return

    print(f"{'STARTED':<20} {'SWITCH':<22} {'PORT':<14} {'DOWN':<13} {'UP':<13} {'DONE'}")
    for record in reversed(records):
        done = "yes" if record.get("complete") else "no"
        print(
            f"{format_stamp(record.get('started_at')):<20} "
            f"{str(record.get('switch_name', '--'))[:21]:<22} "
            f"{str(record.get('switch_port', '--'))[:13]:<14} "
            f"{str(record.get('speed_down', '--')):<13} "
            f"{str(record.get('speed_up', '--')):<13} "
            f"{done}"
        )


class FlukeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PiScout Pro")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="#121212")
        self.root.bind("<Escape>", self.close_window)

        # Which screen is showing, and where the History "Back" button should return to
        self.current_view = "scanner"
        self.return_view = "scanner"
        self.history_records = []
        self.history_page = 0

        # Progress bar visual styling
        self.style = ttk.Style()
        self.style.theme_use('default')
        self.style.configure(
            "Custom.Horizontal.TProgressbar",
            troughcolor='#1A1A1A',
            background='#00E5FF',
            thickness=14
        )

        # Root Container
        self.container = tk.Frame(root, bg="#121212")
        self.container.pack(expand=True, fill=tk.BOTH)

        # Build UI Views
        self.build_scanner_ui()
        self.build_results_ui()
        self.build_history_ui()
        self.build_detail_ui()
        self.show_scanner_ui()

        # Shared Diagnostic Storage
        self.scan_results = {
            "started_at": "--",
            "finished_at": "--",
            "clock_synced": "unknown",
            "complete": False,
            "switch_name": "--",
            "switch_ip": "--",
            "switch_port": "--",
            "switch_vlan": "--",
            "switch_voice": "--",
            "switch_proto": "--",
            "speed_down": "--",
            "speed_up": "--",
            "speed_ping": "--",
            "dns_queries": 0,
            "dns_last_domain": "--"
        }

        self.running = True
        self.sniffing = False
        self.worker_thread = threading.Thread(target=self.main_workflow, daemon=True)
        self.worker_thread.start()

    # --- UI BUILDERS ---

    #--------------------------------------------------------------------------
    # make_button
    # builds a big touch-friendly button in the dark theme (caller packs it)
    # input: parent (widget), text (label), command (function to call on tap)
    # output: the tk.Button
    #--------------------------------------------------------------------------
    def make_button(self, parent, text, command):
        return tk.Button(
            parent, text=text, command=command, font=("Helvetica", 14, "bold"),
            bg="#2B2B2B", fg="#FFFFFF", activebackground="#444444", activeforeground="#FFFFFF",
            padx=30, pady=8
        )

    def build_scanner_ui(self):
        self.scanner_frame = tk.Frame(self.container, bg="#121212")

        self.status_label = tk.Label(
            self.scanner_frame, text="WAITING FOR CABLE...", font=("Helvetica", 20, "bold"),
            bg="#2B2B2B", fg="#FFA500", pady=15
        )
        self.status_label.pack(fill=tk.X)

        self.mode_label = tk.Label(
            self.scanner_frame, text="Ready", font=("Helvetica", 16, "italic"),
            bg="#121212", fg="#888888"
        )
        self.mode_label.pack(pady=20)

        self.timer_label = tk.Label(
            self.scanner_frame, text="--:--", font=("Helvetica", 54, "bold"),
            bg="#121212", fg="#FFFFFF"
        )
        self.timer_label.pack(pady=10)

        # Loading container for bandwidth testing
        self.loading_frame = tk.Frame(self.scanner_frame, bg="#121212")
        self.loading_text = tk.Label(
            self.loading_frame, text="", font=("Helvetica", 14),
            bg="#121212", fg="#00E5FF"
        )
        self.loading_text.pack(pady=5)
        self.progress_bar = ttk.Progressbar(
            self.loading_frame, style="Custom.Horizontal.TProgressbar",
            mode="indeterminate", length=400
        )
        self.progress_bar.pack(pady=10)

        self.footer = tk.Label(
            self.scanner_frame, text="Press 'Esc' to exit", font=("Helvetica", 10),
            bg="#121212", fg="#444444", pady=5
        )
        self.footer.pack(side=tk.BOTTOM)

        # Packed after the footer with side=BOTTOM, so it sits just above it
        self.make_button(self.scanner_frame, "History", self.open_history).pack(side=tk.BOTTOM, pady=10)

    def build_results_ui(self):
        self.results_frame = tk.Frame(self.container, bg="#121212")

        header = tk.Label(
            self.results_frame, text="DIAGNOSTIC COMPLETE", font=("Helvetica", 18, "bold"),
            bg="#00E5FF", fg="#000000", pady=8
        )
        header.pack(fill=tk.X)

        # Button bar is packed before the canvas so it stays visible while the cards scroll
        history_bar = tk.Frame(self.results_frame, bg="#121212")
        history_bar.pack(side=tk.BOTTOM, fill=tk.X, pady=6)
        self.make_button(history_bar, "History", self.open_history).pack()

        # Canvas & Scrollbar Frame to support Arrow Key Scrolling
        self.scroll_canvas = tk.Canvas(self.results_frame, bg="#121212", highlightthickness=0)
        self.scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.scroll_content = tk.Frame(self.scroll_canvas, bg="#121212")
        self.canvas_window = self.scroll_canvas.create_window((0, 0), window=self.scroll_content, anchor="nw")

        self.scroll_content.bind("<Configure>", self.update_scroll_region)
        self.scroll_canvas.bind("<Configure>", self.resize_scroll_content)

        # Keyboard and Mousewheel bindings for scrolling
        self.root.bind("<Up>", self.scroll_up)
        self.root.bind("<Down>", self.scroll_down)
        self.root.bind("<Prior>", self.scroll_page_up)     # Page Up
        self.root.bind("<Next>", self.scroll_page_down)    # Page Down
        self.root.bind("<MouseWheel>", self.scroll_with_mouse_wheel)
        self.root.bind("<Button-4>", self.scroll_up)
        self.root.bind("<Button-5>", self.scroll_down)

        # --- Timestamp line ---
        self.lbl_tested = tk.Label(
            self.scroll_content, text="Tested: --", font=("Helvetica", 12),
            bg="#121212", fg="#888888"
        )
        self.lbl_tested.pack(pady=(8, 0))

        # --- Switch Topology Card ---
        topo_frame = tk.Frame(self.scroll_content, bg="#1A1A1A", bd=2, relief=tk.RIDGE)
        topo_frame.pack(fill=tk.X, padx=15, pady=8)
        tk.Label(topo_frame, text="Switch Topology", font=("Helvetica", 14, "bold"), bg="#1A1A1A", fg="#FFFFFF").pack(pady=4)

        self.lbl_sw = tk.Label(topo_frame, text="SW: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_sw.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_ip = tk.Label(topo_frame, text="IP: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_ip.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_port = tk.Label(topo_frame, text="PORT: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_port.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_vlan = tk.Label(topo_frame, text="VLAN: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_vlan.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_voice = tk.Label(topo_frame, text="VOICE: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_voice.pack(fill=tk.X, padx=15, pady=3)

        # --- Bandwidth Card ---
        speed_frame = tk.Frame(self.scroll_content, bg="#1A1A1A", bd=2, relief=tk.RIDGE)
        speed_frame.pack(fill=tk.X, padx=15, pady=8)
        tk.Label(speed_frame, text="Bandwidth", font=("Helvetica", 14, "bold"), bg="#1A1A1A", fg="#FFFFFF").pack(pady=4)

        self.res_speed_lbl = tk.Label(speed_frame, text="Down: -- Mbps | Up: -- Mbps | Ping: -- ms", font=("Helvetica", 12), bg="#1A1A1A", fg="#FFD700")
        self.res_speed_lbl.pack(pady=4)

        # --- DNS Activity Card ---
        dns_frame = tk.Frame(self.scroll_content, bg="#1A1A1A", bd=2, relief=tk.RIDGE)
        dns_frame.pack(fill=tk.X, padx=15, pady=8)
        tk.Label(dns_frame, text="DNS Activity", font=("Helvetica", 14, "bold"), bg="#1A1A1A", fg="#FFFFFF").pack(pady=4)

        self.res_dns_lbl = tk.Label(dns_frame, text="Queries captured: 0 | Last: --", font=("Helvetica", 12), bg="#1A1A1A", fg="#FF00FF")
        self.res_dns_lbl.pack(pady=4)

        # Footer
        footer = tk.Label(
            self.scroll_content, text="[Up/Down] Scroll  |  Unplug cable to reset  |  [Esc] Exit",
            font=("Helvetica", 10), bg="#121212", fg="#888888", pady=10
        )
        footer.pack(fill=tk.X)

    #--------------------------------------------------------------------------
    # build_history_ui
    # builds the paged list of past scans (6 per page, one big button each)
    # input: none
    # output: none (creates self.history_frame, self.history_rows, self.history_page_lbl)
    #--------------------------------------------------------------------------
    def build_history_ui(self):
        self.history_frame = tk.Frame(self.container, bg="#121212")

        header = tk.Label(
            self.history_frame, text="SCAN HISTORY", font=("Helvetica", 18, "bold"),
            bg="#00E5FF", fg="#000000", pady=8
        )
        header.pack(fill=tk.X)

        #bottom items are packed first so the list gets whatever space is left
        self.make_button(self.history_frame, "Back", self.close_history).pack(side=tk.BOTTOM, pady=8)

        nav = tk.Frame(self.history_frame, bg="#121212")
        nav.pack(side=tk.BOTTOM, fill=tk.X, padx=15)
        self.make_button(nav, "< Newer", self.history_newer).pack(side=tk.LEFT)
        self.make_button(nav, "Older >", self.history_older).pack(side=tk.RIGHT)
        self.history_page_lbl = tk.Label(nav, text="", font=("Helvetica", 12), bg="#121212", fg="#888888")
        self.history_page_lbl.pack(expand=True)

        self.history_rows = tk.Frame(self.history_frame, bg="#121212")
        self.history_rows.pack(fill=tk.BOTH, expand=True, padx=15, pady=8)

    #--------------------------------------------------------------------------
    # build_detail_ui
    # builds the full-detail screen shown when a history row is tapped
    # input: none
    # output: none (creates self.detail_frame and self.detail_lbl)
    #--------------------------------------------------------------------------
    def build_detail_ui(self):
        self.detail_frame = tk.Frame(self.container, bg="#121212")

        header = tk.Label(
            self.detail_frame, text="SCAN DETAIL", font=("Helvetica", 18, "bold"),
            bg="#00E5FF", fg="#000000", pady=8
        )
        header.pack(fill=tk.X)

        self.make_button(self.detail_frame, "Back", self.back_to_history).pack(side=tk.BOTTOM, pady=8)

        self.detail_lbl = tk.Label(
            self.detail_frame, text="", font=("Courier", 13, "bold"),
            bg="#121212", fg="#76FF03", justify=tk.LEFT, anchor="nw"
        )
        self.detail_lbl.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

    # --- EVENT HANDLERS ---
    # Tkinter calls these automatically when the matching key/event happens. The "event"
    # argument is required by Tkinter even when we don't need to look at it ourselves.

    def close_window(self, event):
        self.root.destroy()

    def update_scroll_region(self, event):
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def resize_scroll_content(self, event):
        self.scroll_canvas.itemconfig(self.canvas_window, width=event.width)

    def scroll_up(self, event):
        self.scroll_canvas.yview_scroll(-1, "units")

    def scroll_down(self, event):
        self.scroll_canvas.yview_scroll(1, "units")

    def scroll_page_up(self, event):
        self.scroll_canvas.yview_scroll(-5, "units")

    def scroll_page_down(self, event):
        self.scroll_canvas.yview_scroll(5, "units")

    def scroll_with_mouse_wheel(self, event):
        # event.delta is positive when scrolling up and negative when scrolling down.
        if event.delta > 0:
            self.scroll_canvas.yview_scroll(-1, "units")
        else:
            self.scroll_canvas.yview_scroll(1, "units")

    # --- UI STATE MANAGERS ---

    #--------------------------------------------------------------------------
    # hide_all_frames
    # takes every screen off the display so one can be packed in its place
    # input: none
    # output: none
    #--------------------------------------------------------------------------
    def hide_all_frames(self):
        self.scanner_frame.pack_forget()
        self.results_frame.pack_forget()
        self.history_frame.pack_forget()
        self.detail_frame.pack_forget()

    def show_scanner_ui(self):
        self.stop_loading_animation()
        self.hide_all_frames()
        self.scanner_frame.pack(expand=True, fill=tk.BOTH)
        self.current_view = "scanner"

    def show_results_ui(self):
        self.stop_loading_animation()

        tested_text = f"Tested: {format_stamp(self.scan_results['finished_at'])}"
        if self.scan_results["clock_synced"] != "yes":
            tested_text += "  (clock not verified)"
        self.lbl_tested.config(text=tested_text)

        self.lbl_sw.config(text=f"SW:    {self.scan_results['switch_name']} ({self.scan_results['switch_proto']})")
        self.lbl_ip.config(text=f"IP:    {self.scan_results['switch_ip']}")
        self.lbl_port.config(text=f"PORT:  {self.scan_results['switch_port']}")
        self.lbl_vlan.config(text=f"VLAN:  {self.scan_results['switch_vlan']}")
        self.lbl_voice.config(text=f"VOICE: {self.scan_results['switch_voice']}")

        sp_text = f"Down: {self.scan_results['speed_down']} | Up: {self.scan_results['speed_up']} | Ping: {self.scan_results['speed_ping']}"
        dn_text = f"Queries: {self.scan_results['dns_queries']} | Domain: {self.scan_results['dns_last_domain']}"
        self.res_speed_lbl.config(text=sp_text)
        self.res_dns_lbl.config(text=dn_text)

        self.hide_all_frames()
        self.results_frame.pack(expand=True, fill=tk.BOTH)
        self.scroll_canvas.yview_moveto(0)
        self.current_view = "results"

    #--------------------------------------------------------------------------
    # open_history
    # loads the saved scans fresh from disk and shows page 1 of the list
    # input: none
    # output: none
    #--------------------------------------------------------------------------
    def open_history(self):
        if self.current_view == "scanner" or self.current_view == "results":
            self.return_view = self.current_view

        self.history_records = load_scan_history()
        self.history_page = 0
        self.render_history_page()
        self.show_history_frame()

    #--------------------------------------------------------------------------
    # show_history_frame
    # swaps the display to the history list without reloading anything
    # input: none
    # output: none
    #--------------------------------------------------------------------------
    def show_history_frame(self):
        self.hide_all_frames()
        self.history_frame.pack(expand=True, fill=tk.BOTH)
        self.current_view = "history"

    #--------------------------------------------------------------------------
    # close_history
    # goes back to whichever screen History was opened from. Packs the frame
    # directly instead of calling show_scanner_ui so a running speed test
    # animation isn't stopped
    # input: none
    # output: none
    #--------------------------------------------------------------------------
    def close_history(self):
        self.hide_all_frames()
        if self.return_view == "results":
            self.results_frame.pack(expand=True, fill=tk.BOTH)
        else:
            self.scanner_frame.pack(expand=True, fill=tk.BOTH)
        self.current_view = self.return_view

    #--------------------------------------------------------------------------
    # render_history_page
    # redraws the row buttons for the current page
    # input: none
    # output: none
    #--------------------------------------------------------------------------
    def render_history_page(self):
        for child in self.history_rows.winfo_children():
            child.destroy()

        total = len(self.history_records)
        if total == 0:
            tk.Label(
                self.history_rows, text="No scans logged yet", font=("Helvetica", 14),
                bg="#121212", fg="#888888"
            ).pack(pady=40)
            self.history_page_lbl.config(text="0 / 0")
            return

        pages = (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
        start = self.history_page * HISTORY_PAGE_SIZE
        for record in self.history_records[start:start + HISTORY_PAGE_SIZE]:
            tk.Button(
                self.history_rows, text=format_record_summary(record), font=("Courier", 12),
                anchor="w", bg="#1A1A1A", fg="#FFFFFF", activebackground="#2B2B2B",
                activeforeground="#FFFFFF", relief=tk.RIDGE, pady=8,
                command=lambda r=record: self.show_detail_ui(r)
            ).pack(fill=tk.X, pady=2)

        self.history_page_lbl.config(text=f"Page {self.history_page + 1} / {pages}   (! = cut short)")

    def history_newer(self):
        if self.history_page > 0:
            self.history_page = self.history_page - 1
            self.render_history_page()

    def history_older(self):
        if (self.history_page + 1) * HISTORY_PAGE_SIZE < len(self.history_records):
            self.history_page = self.history_page + 1
            self.render_history_page()

    #--------------------------------------------------------------------------
    # show_detail_ui
    # shows every saved field of one scan
    # input: record (dict from the scan log)
    # output: none
    #--------------------------------------------------------------------------
    def show_detail_ui(self, record):
        self.detail_lbl.config(text=format_record_detail(record))
        self.hide_all_frames()
        self.detail_frame.pack(expand=True, fill=tk.BOTH)
        self.current_view = "detail"

    def back_to_history(self):
        self.show_history_frame()

    def start_loading_animation(self, message):
        self.timer_label.pack_forget()
        self.loading_text.config(text=message)
        self.loading_frame.pack(pady=15)
        self.progress_bar.start(10)

    def set_loading_message(self, message):
        self.loading_text.config(text=message)

    def stop_loading_animation(self):
        self.progress_bar.stop()
        self.loading_frame.pack_forget()
        self.timer_label.pack(pady=10)

    def update_scanner(self, status, status_color, mode, timer_text):
        self.status_label.config(text=status, fg=status_color)
        self.mode_label.config(text=mode)
        self.timer_label.config(text=timer_text)

    def reset_data(self):
        self.scan_results = {
            "started_at": now_stamp(),
            "finished_at": "--",
            "clock_synced": is_clock_synced(),
            "complete": False,
            "switch_name": "Not Found",
            "switch_ip": "Not Advertised",
            "switch_port": "Not Found",
            "switch_vlan": "None / Untagged",
            "switch_voice": "None",
            "switch_proto": "--",
            "speed_down": "Failed",
            "speed_up": "Failed",
            "speed_ping": "Failed",
            "dns_queries": 0,
            "dns_last_domain": "None"
        }

    #--------------------------------------------------------------------------
    # finish_scan
    # stamps the finish time and saves the scan to the log. Called once per
    # scan: with True when the results page is ready, with False when the
    # cable was pulled part way through
    # input: complete (True if every step ran, False if cut short)
    # output: none
    #--------------------------------------------------------------------------
    def finish_scan(self, complete):
        self.scan_results["finished_at"] = now_stamp()
        self.scan_results["complete"] = complete
        save_scan_record(dict(self.scan_results))

        if complete:
            logger.info("scan complete: switch=%s port=%s down=%s",
                        self.scan_results["switch_name"], self.scan_results["switch_port"],
                        self.scan_results["speed_down"])
        else:
            logger.warning("scan cut short: cable removed before results")

    # --- NETWORK HELPERS ---

    def is_cable_connected(self):
        if not os.path.exists(CARRIER_PATH):
            return False
        try:
            with open(CARRIER_PATH, "r") as f:
                return f.read().strip() == "1"
        except OSError:
            return False

    def countdown_timer(self, seconds):
        seconds_left = seconds
        while seconds_left >= 0:
            if not self.sniffing:
                break
            self.root.after(0, self.timer_label.config, {'text': f"00:{seconds_left:02d}"})
            time.sleep(1)
            seconds_left = seconds_left - 1

    # --- SWITCH TOPOLOGY (CDP/LLDP via lldpd) ---

    def query_lldpd(self):
        # Ask the lldpd service (installed/started by install.sh) what it's heard on this port.
        # "-f keyvalue" prints one line per fact, like:  lldp.eth0.chassis.name=SWITCH-01
        try:
            result = subprocess.run(
                ["lldpctl", "-f", "keyvalue", INTERFACE],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout
        except FileNotFoundError:
            logger.error("lldpctl not found - is lldpd installed?")
            return ""
        except subprocess.TimeoutExpired:
            logger.warning("lldpctl timed out")
            return ""

    def get_lldp_value(self, kv_text, field_name):
        # kv_text is many lines of "lldp.eth0.<field_name>=<value>". Find the one line that
        # starts with our field name and return the value after the "=". If there's no such
        # line, lldpd hasn't heard that fact, so return an empty string.
        prefix = "lldp." + INTERFACE + "." + field_name + "="
        for line in kv_text.splitlines():
            if line.startswith(prefix):
                return line[len(prefix):]
        return ""

    def apply_switch_topology(self, kv_text):
        if kv_text.strip() == "":
            return False

        switch_name = self.get_lldp_value(kv_text, "chassis.name")
        if switch_name == "":
            switch_name = "Unknown"
        self.scan_results["switch_name"] = switch_name

        # Different switches put the port name in different fields; try each in turn.
        port = self.get_lldp_value(kv_text, "port.ifname")
        if port == "":
            port = self.get_lldp_value(kv_text, "port.descr")
        if port == "":
            port = self.get_lldp_value(kv_text, "port.local")
        if port == "":
            port = "Unknown"
        self.scan_results["switch_port"] = port

        vlan = self.get_lldp_value(kv_text, "vlan.vlan-id")
        if vlan == "":
            vlan = "None / Untagged"
        self.scan_results["switch_vlan"] = vlan

        switch_ip = self.get_lldp_value(kv_text, "chassis.mgmt-ip")
        if switch_ip == "":
            switch_ip = "Not Advertised"
        self.scan_results["switch_ip"] = switch_ip

        protocol = self.get_lldp_value(kv_text, "via")
        if protocol == "":
            protocol = "--"
        self.scan_results["switch_proto"] = protocol

        # Voice VLAN (LLDP-MED / CDP appliance TLV): any line mentioning "voice" with a VLAN id.
        voice = "None"
        for line in kv_text.splitlines():
            if "voice" in line.lower() and "vid=" in line:
                voice = line.split("=", 1)[1]
                break
        self.scan_results["switch_voice"] = voice

        return True

    def discover_switch_topology(self):
        # Poll lldpd once a second instead of sniffing packets ourselves — lldpd already runs
        # continuously in the background, so this usually returns data on the very first check.
        seconds_left = SWITCH_DISCOVERY_TIMEOUT
        while seconds_left >= 0:
            if not self.is_cable_connected():
                return False

            minutes = seconds_left // 60
            seconds = seconds_left % 60
            self.root.after(0, self.timer_label.config, {'text': f"{minutes:02d}:{seconds:02d}"})

            kv_text = self.query_lldpd()
            if self.apply_switch_topology(kv_text):
                return True

            time.sleep(1)
            seconds_left = seconds_left - 1

        return False

    # --- PACKET HANDLERS ---

    def lookup_google(self):
        # Just triggers a real DNS lookup so there's something for parse_dns_packet to catch.
        socket.gethostbyname("google.com")

    def parse_dns_packet(self, packet):
        if packet.haslayer(DNS) and packet.haslayer(DNSQR):
            qname = packet[DNSQR].qname.decode(errors="replace").rstrip(".")
            self.scan_results["dns_queries"] += 1
            self.scan_results["dns_last_domain"] = qname[:25]
            return True
        return False

    # --- MAIN FLOW ---

    def main_workflow(self):
        while self.running:
            # 1. STANDBY
            self.root.after(0, self.show_scanner_ui)
            if not self.is_cable_connected():
                self.root.after(0, self.update_scanner, "WAITING FOR CABLE...", "#FFA500", "Standby", "--:--")
                while not self.is_cable_connected() and self.running:
                    time.sleep(1)

            if not self.running:
                break

            self.reset_data()
            logger.info("cable connected, scan started (clock synced: %s)", self.scan_results["clock_synced"])

            # 2. SWITCH TOPOLOGY DISCOVERY (up to SWITCH_DISCOVERY_TIMEOUT seconds)
            start_minutes = SWITCH_DISCOVERY_TIMEOUT // 60
            start_seconds = SWITCH_DISCOVERY_TIMEOUT % 60
            self.root.after(
                0, self.update_scanner, "TESTING NETWORK...", "#FFFF00",
                "Mode: Switch Discovery (CDP/LLDP)", f"{start_minutes:02d}:{start_seconds:02d}"
            )
            switch_found = self.discover_switch_topology()
            if not self.is_cable_connected():
                self.finish_scan(False)
                continue
            if not switch_found:
                logger.warning("no CDP/LLDP announcement heard within %d seconds", SWITCH_DISCOVERY_TIMEOUT)

            # 3. DNS MONITORING (3s active query)
            self.root.after(0, self.update_scanner, "TESTING NETWORK...", "#FFFF00", "Mode: DNS Sniffing", "00:03")
            self.sniffing = True
            timer_thread = threading.Thread(target=self.countdown_timer, args=(3,))
            timer_thread.start()

            threading.Thread(target=self.lookup_google, daemon=True).start()

            sniff(
                iface=INTERFACE,
                filter="port 53",
                stop_filter=self.parse_dns_packet,
                timeout=3,
                store=0
            )

            self.sniffing = False
            timer_thread.join()
            if not self.is_cable_connected():
                self.finish_scan(False)
                continue

            # 4. BANDWIDTH TEST: Animated Loading Screen
            self.root.after(0, self.update_scanner, "TESTING NETWORK...", "#FFFF00", "Mode: Bandwidth Speed Test", "")
            self.root.after(0, self.start_loading_animation, "Connecting to closest server...")

            try:
                st = speedtest.Speedtest()
                st.get_best_server()

                self.root.after(0, self.set_loading_message, "Testing Download Speed...")
                down_bps = st.download()

                self.root.after(0, self.set_loading_message, "Testing Upload Speed...")
                up_bps = st.upload()

                self.scan_results["speed_down"] = f"{down_bps / 1_000_000:.1f} Mbps"
                self.scan_results["speed_up"] = f"{up_bps / 1_000_000:.1f} Mbps"
                self.scan_results["speed_ping"] = f"{st.results.ping:.0f} ms"
            except Exception:
                #the results page still says Failed, but now the log says why
                logger.exception("speedtest failed")

            # 5. DIAGNOSTIC RESULTS PAGE
            if not self.is_cable_connected():
                self.finish_scan(False)
                continue

            self.finish_scan(True)
            self.root.after(0, self.show_results_ui)

            while self.is_cable_connected() and self.running:
                time.sleep(1)
            logger.info("cable removed")


if __name__ == "__main__":
    # --history only reads the log, so it works without sudo and without a screen
    if "--history" in sys.argv:
        print_history()
        sys.exit(0)

    if os.geteuid() != 0:
        print("[!] Error: Run with sudo for raw packet sniffing access.")
        sys.exit(1)

    setup_logging()
    logger.info("fluke-gui started")

    root = tk.Tk()
    app = FlukeApp(root)
    root.mainloop()
