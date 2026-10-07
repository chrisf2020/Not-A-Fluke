#!/usr/bin/env python3
# fluke-gui — full-screen touchscreen network tester for the small screen (Tkinter).
# Start it with:  notafluke      (or directly: sudo python3 fluke-gui.py)
#
# THE BUG THIS FIXES: it used to sniff for CDP/LLDP packets itself with scapy, for a fixed
# 30 seconds. Cisco only sends a CDP announcement every ~60s (LLDP is ~30s), so that window
# regularly closed before anything arrived and "Switch Topology" showed Not Found even on a
# switch that was announcing the whole time. This version asks the `lldpd` background service
# instead — it listens continuously, so we usually get
# an answer immediately, and we wait a full announcement cycle instead of half of one.
#
# LOGGING: every scan is saved as one JSON line in /var/log/fluke-gui/scans.jsonl (newest 500
# kept), with start/finish timestamps and a "complete" flag (false if the cable was pulled
# mid-scan). Events and errors go to /var/log/fluke-gui/events.log (rotating). Past scans can
# be browsed with the History button on screen, or printed over SSH with:  python3 fluke-gui.py --history
#
# v0.5 ADDED: version number (top right), up/down arrow buttons, a working DNS test,
# a PoE line (from what the switch announces), a Wi-Fi scanner (nearby APs + AP info),
# a bandwidth rating, and an Exit button that hands the small screen back to the terminal (CLI).

import os
import re
import sys
import json
import math
import time
import random
import socket
import logging
import threading
import subprocess
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from logging.handlers import RotatingFileHandler
import speedtest
from scapy.all import AsyncSniffer
from scapy.layers.dns import DNS, DNSQR
from scapy.contrib.cdp import CDPMsgPowerAvailable

VERSION = "0.5"

INTERFACE = "eth0"
WIFI_INTERFACE = "wlan0"    # the Pi's own wifi, used by the Wi-Fi scanner
CARRIER_PATH = f"/sys/class/net/{INTERFACE}/carrier"
# Give up after 35s: covers LLDP's ~30s cycle (lldpd listens all the time, so a switch that
# already announced shows up instantly). Cisco CDP is every ~60s, so a brand-new link may need a replug.
SWITCH_DISCOVERY_TIMEOUT = 35

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
# turns 2026-09-30T14:05:12 into 2026-09-30 02:05:12 PM for display (12-hour clock)
# input: stamp (string, may be "--" or missing), short (True = "09-30 02:05PM")
# output: string
#--------------------------------------------------------------------------
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


#--------------------------------------------------------------------------
# short_port_name
# GigabitEthernet1/0/24 -> Gi1/0/24 (like PiScout shows it, fits the small screen)
#--------------------------------------------------------------------------
def short_port_name(port):
    port = port.replace("TenGigabitEthernet", "Te")
    port = port.replace("TwoGigabitEthernet", "Tw")
    port = port.replace("GigabitEthernet", "Gi")
    port = port.replace("FastEthernet", "Fa")
    return port


#--------------------------------------------------------------------------
# format_record_summary
# one short line per scan for the history list
# input: record (dict)
# output: string like " 09-30 02:05PM SWITCH-01    Gi1/0/5  93.4M"
#         (starts with "!" instead of a space if the scan was cut short)
#--------------------------------------------------------------------------
def format_record_summary(record):
    marker = " " if record.get("complete") else "!"
    when = format_stamp(record.get("started_at"), short=True)
    name = str(record.get("switch_name", "--"))[:12]
    port = short_port_name(str(record.get("switch_port", "--")))[:8]
    down = str(record.get("speed_down", "--")).replace(" Mbps", "M")
    return f"{marker}{when} {name:<12} {port:<8} {down}"


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
        f"SW:    {record.get('switch_name', '--')}",
        f"VIA:   {record.get('switch_proto', '--')}",
        f"IP:    {record.get('switch_ip', '--')}",
        f"PORT:  {record.get('switch_port', '--')}",
        f"VLAN:  {record.get('switch_vlan', '--')}",
        f"VOICE: {record.get('switch_voice', '--')}",
        "",
        f"Down: {record.get('speed_down', '--')}",
        f"Up:   {record.get('speed_up', '--')}",
        f"Ping: {record.get('speed_ping', '--')}",
        f"Rating: {record.get('speed_rating', '--')}",
        "",
        f"DNS server: {record.get('dns_server', '--')}  {record.get('dns_time', '--')}",
        f"DNS queries: {record.get('dns_queries', 0)}  Last: {record.get('dns_last_domain', '--')}",
        "",
        f"PoE: {record.get('switch_poe', '--')}",
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

    print(f"{'STARTED':<23} {'SWITCH':<22} {'PORT':<14} {'DOWN':<13} {'UP':<13} {'DONE'}")
    for record in reversed(records):
        done = "yes" if record.get("complete") else "no"
        print(
            f"{format_stamp(record.get('started_at')):<23} "
            f"{str(record.get('switch_name', '--'))[:21]:<22} "
            f"{str(record.get('switch_port', '--'))[:13]:<14} "
            f"{str(record.get('speed_down', '--')):<13} "
            f"{str(record.get('speed_up', '--')):<13} "
            f"{done}"
        )


# --- NETWORK HELPERS (v0.5) ---

#--------------------------------------------------------------------------
# get_eth_ip
# the IPv4 address on the test port, e.g. "192.168.1.50" ("" if none)
#--------------------------------------------------------------------------
def get_eth_ip():
    result = subprocess.run(["ip", "-4", "-br", "addr", "show", INTERFACE], capture_output=True, text=True)
    words = result.stdout.split()
    if len(words) >= 3:
        return words[2].split("/")[0]
    return ""


#--------------------------------------------------------------------------
# get_dns_servers
# the DNS servers the network handed out on the test port (from NetworkManager)
# output: list like ["192.168.1.1"]
#--------------------------------------------------------------------------
def get_dns_servers():
    result = subprocess.run(["nmcli", "-t", "-g", "IP4.DNS", "dev", "show", INTERFACE], capture_output=True, text=True)
    servers = []
    for part in result.stdout.replace("|", "\n").split("\n"):
        part = part.strip()
        if part != "":
            servers.append(part)
    return servers


#--------------------------------------------------------------------------
# dns_lookup
# sends ONE real DNS question to a DNS server, forced out of the test port,
# and times the answer. (The old version used socket.gethostbyname, which
# could go out the wifi instead, and it fired before the sniffer had started,
# so the DNS card often showed 0 queries.)
# output: (worked True/False, text like "17 ms" or "no reply")
#--------------------------------------------------------------------------
def dns_lookup(server, name):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, INTERFACE.encode())
        sock.settimeout(2)
        question = DNS(id=random.randint(1, 65535), rd=1, qd=DNSQR(qname=name))
        start = time.time()
        sock.sendto(bytes(question), (server, 53))
        data, sender = sock.recvfrom(4096)
        milliseconds = round((time.time() - start) * 1000)
        reply = DNS(data)
        if reply.rcode == 0 and reply.ancount > 0:
            return True, str(milliseconds) + " ms"
        return False, "bad reply (code " + str(reply.rcode) + ")"
    except socket.timeout:
        return False, "no reply"
    except OSError as err:
        return False, str(err)
    finally:
        sock.close()


# --- WI-FI SCANNER HELPERS (v0.5) ---

#--------------------------------------------------------------------------
# freq_to_channel
# 2437 -> 6, 5805 -> 161, 5975 -> 5 (6 GHz)
#--------------------------------------------------------------------------
def freq_to_channel(freq):
    if freq == 2484:
        return 14
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if 5000 <= freq <= 5900:
        return (freq - 5000) // 5
    if 5925 <= freq <= 7125:
        return (freq - 5950) // 5
    return 0


#--------------------------------------------------------------------------
# parse_iw_scan
# turns the text from "iw dev wlan0 scan" into a list of access points (dicts)
#--------------------------------------------------------------------------
def parse_iw_scan(text):
    access_points = []
    ap = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        # Each access point starts with a line like:  BSS 18:60:41:57:46:56(on wlan0) -- associated
        if raw_line.startswith("BSS "):
            ap = {
                "bssid": raw_line[4:21], "ssid": "<hidden>", "freq": 0, "channel": 0,
                "signal": -100, "security": "OPEN", "auth": "", "width": "20 MHz",
                "standard": "a/b/g", "clients": "--", "busy": "--",
                "connected": "associated" in raw_line,
            }
            access_points.append(ap)
            continue

        if ap is None:
            continue

        if line.startswith("freq:"):
            ap["freq"] = int(float(line.split(":")[1]))
            ap["channel"] = freq_to_channel(ap["freq"])
        elif line.startswith("signal:"):
            ap["signal"] = int(float(line.split(":")[1].split()[0]))
        elif line.startswith("SSID:"):
            name = line[5:].strip()
            if name != "" and name.replace("\\x00", "") != "":
                ap["ssid"] = name
        elif line.startswith("capability:") and "Privacy" in line:
            ap["security"] = "WEP"
        elif line.startswith("WPA:"):
            ap["security"] = "WPA"
        elif line.startswith("RSN:"):
            ap["security"] = "WPA2"
        elif "Authentication suites:" in line:
            ap["auth"] = ap["auth"] + line
        elif "station count:" in line:
            ap["clients"] = line.split(":")[1].strip()
        elif "channel utilisation:" in line:
            # given as "6/255" -> turn it into a percent
            parts = line.split(":")[1].strip().split("/")
            ap["busy"] = str(round(int(parts[0]) * 100 / int(parts[1]))) + "%"
        elif "channel width:" in line and "MHz" in line and "STA" not in line:
            width = re.search(r"\((.*MHz)\)", line)
            if width:
                ap["width"] = width.group(1)
        elif line.startswith("HT capabilities") and ap["standard"] == "a/b/g":
            ap["standard"] = "Wi-Fi 4 (n)"
        elif line.startswith("VHT capabilities"):
            ap["standard"] = "Wi-Fi 5 (ac)"
        elif line.startswith("HE capabilities"):
            ap["standard"] = "Wi-Fi 6 (ax)"
        elif line.startswith("EHT capabilities"):
            ap["standard"] = "Wi-Fi 7 (be)"

    # SAE = WPA3. Many APs offer both WPA2 (PSK) and WPA3 (SAE).
    for ap in access_points:
        if "SAE" in ap["auth"] and "PSK" in ap["auth"]:
            ap["security"] = "WPA2/3"
        elif "SAE" in ap["auth"]:
            ap["security"] = "WPA3"
        elif "802.1X" in ap["auth"]:
            ap["security"] = "WPA2-ENT"

    return access_points


#--------------------------------------------------------------------------
# scan_wifi
# runs a real Wi-Fi scan (takes ~4 seconds). If the radio is busy it tries
# again, then falls back to the last results the radio remembers.
# output: list of access points, strongest first
#--------------------------------------------------------------------------
def scan_wifi():
    output = ""
    for attempt in range(3):
        result = subprocess.run(["iw", "dev", WIFI_INTERFACE, "scan"], capture_output=True, text=True, timeout=20)
        output = result.stdout
        if "BSS " in output:
            break
        time.sleep(1)

    if "BSS " not in output:
        logger.warning("wifi scan failed, using cached results")
        result = subprocess.run(["iw", "dev", WIFI_INTERFACE, "scan", "dump"], capture_output=True, text=True, timeout=10)
        output = result.stdout

    access_points = parse_iw_scan(output)
    access_points.sort(key=lambda ap: ap["signal"], reverse=True)
    return access_points


#--------------------------------------------------------------------------
# signal_color
# green = strong, yellow = ok, orange = weak, red = very weak
#--------------------------------------------------------------------------
def signal_color(dbm):
    if dbm >= -60:
        return "#76FF03"
    if dbm >= -70:
        return "#FFD700"
    if dbm >= -80:
        return "#FFA500"
    return "#FF5252"


#--------------------------------------------------------------------------
# estimate_distance
# ROUGH distance to an AP from its signal. Assumes the AP transmits at 20 dBm
# and indoor walls/people (loss factor 3.3). Can easily be half or double the
# real distance - good for "which AP is closer" and for walking towards one.
#--------------------------------------------------------------------------
def estimate_distance(dbm, freq):
    if freq <= 0:
        return "--"
    loss_at_one_metre = 20 * math.log10(freq) - 27.55
    metres = 10 ** ((20 - dbm - loss_at_one_metre) / 33)
    if metres < 10:
        return "~" + str(round(metres, 1)) + " m"
    return "~" + str(round(metres)) + " m"


#--------------------------------------------------------------------------
# format_ap_detail
# full text for the AP info screen
#--------------------------------------------------------------------------
def format_ap_detail(ap):
    if ap["freq"] < 3000:
        band = "2.4 GHz"
    elif ap["freq"] < 5925:
        band = "5 GHz"
    else:
        band = "6 GHz"

    quality = 2 * (ap["signal"] + 100)   # -50 dBm or better = 100%, -100 dBm = 0%
    if quality > 100:
        quality = 100
    if quality < 0:
        quality = 0

    lines = [
        f"SSID:     {ap['ssid']}",
        f"BSSID:    {ap['bssid']}",
        f"Signal:   {ap['signal']} dBm ({quality}%)",
        f"Distance: {estimate_distance(ap['signal'], ap['freq'])} (rough)",
        f"Channel:  {ap['channel']} ({band})",
        f"Freq:     {ap['freq']} MHz",
        f"Width:    {ap['width']}",
        f"Security: {ap['security']}",
        f"Standard: {ap['standard']}",
        f"Clients:  {ap['clients']}",
        f"Ch. busy: {ap['busy']}",
    ]
    if ap["connected"]:
        lines.append("")
        lines.append("(the Pi's own wifi is on this AP)")
    return "\n".join(lines)


#--------------------------------------------------------------------------
# speed_rating
# a plain-English rating for a download speed
# input: mbps (number)
# output: "Poor", "Fair", "Good", "Very Good" or "Excellent"
#--------------------------------------------------------------------------
def speed_rating(mbps):
    if mbps < 10:
        return "Poor"
    if mbps < 25:
        return "Fair"
    if mbps < 100:
        return "Good"
    if mbps < 500:
        return "Very Good"
    return "Excellent"


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
        self.wifi_scanning = False
        self.exit_armed = False

        # Finger drag scrolling: did the finger move far enough to count as a drag?
        # (if so, the button under the finger shouldn't also get pressed)
        self.drag_start_y = 0
        self.drag_moved = False

        # Progress bar visual styling
        self.style = ttk.Style()
        self.style.theme_use('default')
        self.style.configure(
            "Custom.Horizontal.TProgressbar",
            troughcolor='#1A1A1A',
            background='#00E5FF',
            thickness=14
        )

        # Top bar: title on the left, version number top right
        top_bar = tk.Frame(root, bg="#2B2B2B")
        top_bar.pack(side=tk.TOP, fill=tk.X)
        tk.Label(top_bar, text="NOT-A-FLUKE", font=("Helvetica", 11, "bold"), bg="#2B2B2B", fg="#00E5FF").pack(side=tk.LEFT, padx=6)
        tk.Label(top_bar, text="v" + VERSION, font=("Helvetica", 11, "bold"), bg="#2B2B2B", fg="#888888").pack(side=tk.RIGHT, padx=6)

        # Up / down arrow buttons down the right-hand side (scroll whatever page is showing)
        arrow_strip = tk.Frame(root, bg="#121212", width=54)
        arrow_strip.pack(side=tk.RIGHT, fill=tk.Y)
        arrow_strip.pack_propagate(False)
        self.make_arrow(arrow_strip, "▲", -1)
        self.make_arrow(arrow_strip, "▼", 1)

        # Root Container
        self.container = tk.Frame(root, bg="#121212")
        self.container.pack(expand=True, fill=tk.BOTH)

        # Build UI Views
        self.build_scanner_ui()
        self.build_results_ui()
        self.build_history_ui()
        self.build_detail_ui()
        self.build_wifi_ui()
        self.build_ap_ui()
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
            "dns_last_domain": "--",
            "dns_server": "--",
            "dns_time": "--",
            "switch_poe": "--",
            "speed_rating": "--"
        }

        self.running = True
        self.sniffing = False
        # PoE: listen for Cisco CDP packets the whole time. lldpd reads CDP for the switch
        # name/port, but it doesn't pass on the "Power Available" part, so we read that here.
        self.cdp_poe = ""
        try:
            self.cdp_sniffer = AsyncSniffer(
                iface=INTERFACE, filter="ether dst 01:00:0c:cc:cc:cc",
                prn=self.on_cdp_packet, store=False
            )
            self.cdp_sniffer.start()
        except Exception:
            logger.exception("could not start the CDP listener (PoE check)")

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
            padx=12, pady=6
        )

    #--------------------------------------------------------------------------
    # make_arrow
    # one big arrow button on the right-hand strip. Holding it down keeps scrolling.
    # input: parent, symbol ("▲" or "▼"), direction (-1 = up, 1 = down)
    #--------------------------------------------------------------------------
    def make_arrow(self, parent, symbol, direction):
        arrow = tk.Label(
            parent, text=symbol, font=("DejaVu Sans", 22, "bold"),
            bg="#2B2B2B", fg="#00E5FF", relief=tk.RAISED, bd=2
        )
        arrow.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=2, pady=2)
        arrow.bind("<ButtonPress-1>", lambda event: self.arrow_pressed(arrow, direction))
        arrow.bind("<ButtonRelease-1>", lambda event: self.arrow_released(arrow))

    #--------------------------------------------------------------------------
    # make_bottom_bar
    # the History / Wi-Fi / Exit buttons along the bottom of the main screens
    #--------------------------------------------------------------------------
    def make_bottom_bar(self, parent):
        bar = tk.Frame(parent, bg="#121212")
        bar.pack(side=tk.BOTTOM, fill=tk.X, pady=4)
        self.make_button(bar, "History", self.open_history).pack(side=tk.LEFT, expand=True)
        self.make_button(bar, "Wi-Fi", self.open_wifi).pack(side=tk.LEFT, expand=True)
        exit_button = self.make_button(bar, "Exit", self.exit_pressed)
        exit_button.config(fg="#FF5252")
        exit_button.pack(side=tk.LEFT, expand=True)
        return exit_button

    #--------------------------------------------------------------------------
    # make_scroll_area
    # a scrollable box: put widgets inside the returned frame, the canvas shows
    # the part that fits (scroll it with the arrows or a finger drag)
    # output: (canvas, inner frame)
    #--------------------------------------------------------------------------
    def make_scroll_area(self, parent):
        canvas = tk.Canvas(parent, bg="#121212", highlightthickness=0)
        canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        inner = tk.Frame(canvas, bg="#121212")
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfig(window, width=event.width))
        return canvas, inner

    def build_scanner_ui(self):
        self.scanner_frame = tk.Frame(self.container, bg="#121212")

        self.status_label = tk.Label(
            self.scanner_frame, text="WAITING FOR CABLE...", font=("Helvetica", 20, "bold"),
            bg="#2B2B2B", fg="#FFA500", pady=8
        )
        self.status_label.pack(fill=tk.X)

        self.mode_label = tk.Label(
            self.scanner_frame, text="Ready", font=("Helvetica", 14, "italic"),
            bg="#121212", fg="#888888"
        )
        self.mode_label.pack(pady=4)

        self.timer_label = tk.Label(
            self.scanner_frame, text="--:--", font=("Helvetica", 40, "bold"),
            bg="#121212", fg="#FFFFFF"
        )
        self.timer_label.pack(pady=2)

        # Loading container for bandwidth testing
        self.loading_frame = tk.Frame(self.scanner_frame, bg="#121212")
        self.loading_text = tk.Label(
            self.loading_frame, text="", font=("Helvetica", 14),
            bg="#121212", fg="#00E5FF"
        )
        self.loading_text.pack(pady=5)
        self.progress_bar = ttk.Progressbar(
            self.loading_frame, style="Custom.Horizontal.TProgressbar",
            mode="indeterminate", length=360
        )
        self.progress_bar.pack(pady=4)

        self.scanner_exit_button = self.make_bottom_bar(self.scanner_frame)

    def build_results_ui(self):
        self.results_frame = tk.Frame(self.container, bg="#121212")

        header = tk.Label(
            self.results_frame, text="DIAGNOSTIC COMPLETE", font=("Helvetica", 16, "bold"),
            bg="#00E5FF", fg="#000000", pady=3
        )
        header.pack(fill=tk.X)

        # Button bar is packed before the canvas so it stays visible while the cards scroll
        self.results_exit_button = self.make_bottom_bar(self.results_frame)

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

        # Touchscreen: drag a finger up/down anywhere on the results to scroll them
        self.root.bind_all("<ButtonPress-1>", self.drag_start, add="+")
        self.root.bind_all("<B1-Motion>", self.drag_move, add="+")

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

        self.lbl_sw = tk.Label(topo_frame, text="SW: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w", justify=tk.LEFT, wraplength=360)
        self.lbl_sw.pack(fill=tk.X, padx=15, pady=1)

        # CDP or LLDP - on its own line so a long switch name doesn't push it off the screen
        self.lbl_via = tk.Label(topo_frame, text="VIA: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_via.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_ip = tk.Label(topo_frame, text="IP: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_ip.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_port = tk.Label(topo_frame, text="PORT: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_port.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_vlan = tk.Label(topo_frame, text="VLAN: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_vlan.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_voice = tk.Label(topo_frame, text="VOICE: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_voice.pack(fill=tk.X, padx=15, pady=1)

        # PoE: what the switch says about power on this port (the Pi can't measure voltage itself)
        self.lbl_poe = tk.Label(topo_frame, text="POE: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_poe.pack(fill=tk.X, padx=15, pady=3)

        # --- Bandwidth Card ---
        speed_frame = tk.Frame(self.scroll_content, bg="#1A1A1A", bd=2, relief=tk.RIDGE)
        speed_frame.pack(fill=tk.X, padx=15, pady=8)
        tk.Label(speed_frame, text="Bandwidth", font=("Helvetica", 14, "bold"), bg="#1A1A1A", fg="#FFFFFF").pack(pady=4)

        self.res_speed_lbl = tk.Label(speed_frame, text="Down: -- Mbps | Up: -- Mbps | Ping: -- ms", font=("Helvetica", 12), bg="#1A1A1A", fg="#FFD700")
        self.res_speed_lbl.pack(pady=(4, 0))

        self.res_rating_lbl = tk.Label(speed_frame, text="Rating: --", font=("Helvetica", 12, "bold"), bg="#1A1A1A", fg="#FFD700")
        self.res_rating_lbl.pack(pady=(0, 4))

        # --- DNS Activity Card ---
        dns_frame = tk.Frame(self.scroll_content, bg="#1A1A1A", bd=2, relief=tk.RIDGE)
        dns_frame.pack(fill=tk.X, padx=15, pady=8)
        tk.Label(dns_frame, text="DNS Activity", font=("Helvetica", 14, "bold"), bg="#1A1A1A", fg="#FFFFFF").pack(pady=4)

        self.res_dns_server_lbl = tk.Label(dns_frame, text="Server: --", font=("Helvetica", 12), bg="#1A1A1A", fg="#FF00FF")
        self.res_dns_server_lbl.pack(pady=(0, 2))

        self.res_dns_lbl = tk.Label(dns_frame, text="Queries captured: 0 | Last: --", font=("Helvetica", 12), bg="#1A1A1A", fg="#FF00FF")
        self.res_dns_lbl.pack(pady=(0, 4))

        # Footer
        footer = tk.Label(
            self.scroll_content, text="Arrows / drag to scroll  |  Unplug cable to reset",
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
            self.history_frame, text="SCAN HISTORY", font=("Helvetica", 16, "bold"),
            bg="#00E5FF", fg="#000000", pady=3
        )
        header.pack(fill=tk.X)

        #bottom items are packed first so the list gets whatever space is left
        #(Newer / Back / Older share one row to fit the small screen; the arrows also flip pages)
        nav = tk.Frame(self.history_frame, bg="#121212")
        nav.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=4)
        self.make_button(nav, "< Newer", self.history_newer).pack(side=tk.LEFT)
        self.make_button(nav, "Older >", self.history_older).pack(side=tk.RIGHT)
        self.make_button(nav, "Back", self.close_history).pack(expand=True)

        self.history_page_lbl = tk.Label(self.history_frame, text="", font=("Helvetica", 10), bg="#121212", fg="#888888")
        self.history_page_lbl.pack(side=tk.BOTTOM)

        self.history_rows = tk.Frame(self.history_frame, bg="#121212")
        self.history_rows.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

    #--------------------------------------------------------------------------
    # build_detail_ui
    # builds the full-detail screen shown when a history row is tapped
    # input: none
    # output: none (creates self.detail_frame and self.detail_lbl)
    #--------------------------------------------------------------------------
    def build_detail_ui(self):
        self.detail_frame = tk.Frame(self.container, bg="#121212")

        header = tk.Label(
            self.detail_frame, text="SCAN DETAIL", font=("Helvetica", 16, "bold"),
            bg="#00E5FF", fg="#000000", pady=3
        )
        header.pack(fill=tk.X)

        self.make_button(self.detail_frame, "Back", self.back_to_history).pack(side=tk.BOTTOM, pady=4)

        # Scrollable, because a full scan detail is taller than the small screen
        self.detail_canvas, detail_inner = self.make_scroll_area(self.detail_frame)
        self.detail_lbl = tk.Label(
            detail_inner, text="", font=("Courier", 12, "bold"),
            bg="#121212", fg="#76FF03", justify=tk.LEFT, anchor="nw"
        )
        self.detail_lbl.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

    #--------------------------------------------------------------------------
    # build_wifi_ui
    # the Wi-Fi scanner list: one button per nearby access point, strongest first
    #--------------------------------------------------------------------------
    def build_wifi_ui(self):
        self.wifi_frame = tk.Frame(self.container, bg="#121212")

        header = tk.Label(
            self.wifi_frame, text="WI-FI SCANNER", font=("Helvetica", 16, "bold"),
            bg="#00E5FF", fg="#000000", pady=3
        )
        header.pack(fill=tk.X)

        bar = tk.Frame(self.wifi_frame, bg="#121212")
        bar.pack(side=tk.BOTTOM, fill=tk.X, pady=4)
        self.wifi_scan_button = self.make_button(bar, "Scan", self.start_wifi_scan)
        self.wifi_scan_button.pack(side=tk.LEFT, expand=True)
        self.make_button(bar, "Back", self.close_wifi).pack(side=tk.LEFT, expand=True)

        self.wifi_status_lbl = tk.Label(
            self.wifi_frame, text="", font=("Helvetica", 10), bg="#121212", fg="#888888"
        )
        self.wifi_status_lbl.pack(fill=tk.X)

        self.wifi_canvas, self.wifi_rows = self.make_scroll_area(self.wifi_frame)

    #--------------------------------------------------------------------------
    # build_ap_ui
    # the info screen for one access point (shown when a row is tapped)
    #--------------------------------------------------------------------------
    def build_ap_ui(self):
        self.ap_frame = tk.Frame(self.container, bg="#121212")

        header = tk.Label(
            self.ap_frame, text="ACCESS POINT", font=("Helvetica", 16, "bold"),
            bg="#00E5FF", fg="#000000", pady=3
        )
        header.pack(fill=tk.X)

        self.make_button(self.ap_frame, "Back", self.show_wifi_frame).pack(side=tk.BOTTOM, pady=4)

        self.ap_canvas, ap_inner = self.make_scroll_area(self.ap_frame)
        self.ap_lbl = tk.Label(
            ap_inner, text="", font=("Courier", 12, "bold"),
            bg="#121212", fg="#00E5FF", justify=tk.LEFT, anchor="nw"
        )
        self.ap_lbl.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

    # --- EVENT HANDLERS ---
    # Tkinter calls these automatically when the matching key/event happens. The "event"
    # argument is required by Tkinter even when we don't need to look at it ourselves.

    def close_window(self, event):
        self.root.destroy()

    #--------------------------------------------------------------------------
    # exit_pressed
    # Exit button: first tap asks "Sure?", a second tap within 3 seconds closes
    # the app (the notafluke launcher then puts the terminal back on the screen)
    #--------------------------------------------------------------------------
    def exit_pressed(self):
        if self.exit_armed:
            logger.info("exit button pressed")
            self.root.destroy()
            return
        self.exit_armed = True
        self.scanner_exit_button.config(text="Sure?", bg="#FF5252", fg="#FFFFFF")
        self.results_exit_button.config(text="Sure?", bg="#FF5252", fg="#FFFFFF")
        self.root.after(3000, self.disarm_exit)

    def disarm_exit(self):
        self.exit_armed = False
        self.scanner_exit_button.config(text="Exit", bg="#2B2B2B", fg="#FF5252")
        self.results_exit_button.config(text="Exit", bg="#2B2B2B", fg="#FF5252")

    def update_scroll_region(self, event):
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def resize_scroll_content(self, event):
        self.scroll_canvas.itemconfig(self.canvas_window, width=event.width)

    #--------------------------------------------------------------------------
    # current_canvas
    # which scrollable box is on screen right now (None if the page doesn't scroll)
    #--------------------------------------------------------------------------
    def current_canvas(self):
        if self.current_view == "results":
            return self.scroll_canvas
        if self.current_view == "detail":
            return self.detail_canvas
        if self.current_view == "wifi":
            return self.wifi_canvas
        if self.current_view == "ap":
            return self.ap_canvas
        return None

    #--------------------------------------------------------------------------
    # scroll_by
    # moves the page up (negative) or down (positive). On the History page
    # it flips pages instead, since that list is shown a page at a time.
    #--------------------------------------------------------------------------
    def scroll_by(self, steps):
        if self.current_view == "history":
            if steps < 0:
                self.history_newer()
            else:
                self.history_older()
            return
        canvas = self.current_canvas()
        if canvas is not None:
            canvas.yview_scroll(steps, "units")

    def scroll_up(self, event):
        self.scroll_by(-1)

    def scroll_down(self, event):
        self.scroll_by(1)

    #--------------------------------------------------------------------------
    # arrow_pressed / arrow_released
    # the on-screen arrows: scroll once, then keep going while held down
    #--------------------------------------------------------------------------
    def arrow_pressed(self, arrow, direction):
        arrow.config(bg="#444444", relief=tk.SUNKEN)
        self.arrow_direction = direction
        if self.current_view == "history":
            self.scroll_by(direction)      # one page per tap, no repeat
            return
        self.scroll_by(direction * 2)
        self.arrow_job = self.root.after(400, self.arrow_repeat)

    def arrow_repeat(self):
        self.scroll_by(self.arrow_direction * 2)
        self.arrow_job = self.root.after(100, self.arrow_repeat)

    def arrow_released(self, arrow):
        arrow.config(bg="#2B2B2B", relief=tk.RAISED)
        if getattr(self, "arrow_job", None) is not None:
            self.root.after_cancel(self.arrow_job)
            self.arrow_job = None

    def drag_start(self, event):
        self.drag_start_y = event.y_root
        self.drag_moved = False

    def drag_move(self, event):
        canvas = self.current_canvas()
        if canvas is None:
            return
        # small wobbles on the resistive screen are still a tap, not a drag
        if not self.drag_moved:
            if abs(event.y_root - self.drag_start_y) > 10:
                self.drag_moved = True
                canvas.scan_mark(0, event.y_root)
            return
        canvas.scan_dragto(0, event.y_root, gain=1)

    def scroll_page_up(self, event):
        self.scroll_by(-5)

    def scroll_page_down(self, event):
        self.scroll_by(5)

    def scroll_with_mouse_wheel(self, event):
        # event.delta is positive when scrolling up and negative when scrolling down.
        if event.delta > 0:
            self.scroll_by(-1)
        else:
            self.scroll_by(1)

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
        self.wifi_frame.pack_forget()
        self.ap_frame.pack_forget()

    #--------------------------------------------------------------------------
    # is_browsing
    # True while History or Wi-Fi is open. The test keeps running in the
    # background, but it won't yank you off those pages - Back takes you to it.
    #--------------------------------------------------------------------------
    def is_browsing(self):
        return self.current_view in ("history", "detail", "wifi", "ap")

    def show_scanner_ui(self):
        self.stop_loading_animation()
        if self.is_browsing():
            self.return_view = "scanner"
            return
        self.hide_all_frames()
        self.scanner_frame.pack(expand=True, fill=tk.BOTH)
        self.current_view = "scanner"

    def show_results_ui(self):
        self.stop_loading_animation()

        tested_text = f"Tested: {format_stamp(self.scan_results['finished_at'])}"
        if self.scan_results["clock_synced"] != "yes":
            tested_text += "  (clock not verified)"
        self.lbl_tested.config(text=tested_text)

        self.lbl_sw.config(text=f"SW:    {self.scan_results['switch_name']}")
        self.lbl_via.config(text=f"VIA:   {self.scan_results['switch_proto']}")
        self.lbl_ip.config(text=f"IP:    {self.scan_results['switch_ip']}")
        self.lbl_port.config(text=f"PORT:  {self.scan_results['switch_port']}")
        self.lbl_vlan.config(text=f"VLAN:  {self.scan_results['switch_vlan']}")
        self.lbl_voice.config(text=f"VOICE: {self.scan_results['switch_voice']}")

        self.lbl_poe.config(text=f"POE:   {self.scan_results['switch_poe']}")

        sp_text = f"Down: {self.scan_results['speed_down']} | Up: {self.scan_results['speed_up']} | Ping: {self.scan_results['speed_ping']}"
        dn_text = f"Queries: {self.scan_results['dns_queries']} | Domain: {self.scan_results['dns_last_domain']}"
        self.res_speed_lbl.config(text=sp_text)
        self.res_rating_lbl.config(text=f"Rating: {self.scan_results['speed_rating']}")
        self.res_dns_lbl.config(text=dn_text)
        self.res_dns_server_lbl.config(text=f"Server: {self.scan_results['dns_server']} | Answer: {self.scan_results['dns_time']}")

        if self.is_browsing():
            self.return_view = "results"
            return
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
        self.go_back_to_main()

    #--------------------------------------------------------------------------
    # go_back_to_main
    # back to the scanner or results screen, whichever the test is on
    #--------------------------------------------------------------------------
    def go_back_to_main(self):
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
                self.history_rows, text=format_record_summary(record), font=("Courier", 11),
                anchor="w", bg="#1A1A1A", fg="#FFFFFF", activebackground="#2B2B2B",
                activeforeground="#FFFFFF", relief=tk.RIDGE, pady=3,
                command=lambda r=record: self.show_detail_ui(r)
            ).pack(fill=tk.X, pady=1)

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
        self.detail_canvas.yview_moveto(0)
        self.current_view = "detail"

    def back_to_history(self):
        self.show_history_frame()

    # --- WI-FI SCANNER SCREENS ---

    #--------------------------------------------------------------------------
    # open_wifi
    # shows the Wi-Fi list and starts a fresh scan
    #--------------------------------------------------------------------------
    def open_wifi(self):
        if self.current_view == "scanner" or self.current_view == "results":
            self.return_view = self.current_view
        self.show_wifi_frame()
        self.start_wifi_scan()

    def show_wifi_frame(self):
        self.hide_all_frames()
        self.wifi_frame.pack(expand=True, fill=tk.BOTH)
        self.current_view = "wifi"

    def close_wifi(self):
        self.go_back_to_main()

    #--------------------------------------------------------------------------
    # start_wifi_scan
    # scanning takes ~4 seconds, so it runs on its own thread (screen stays responsive)
    #--------------------------------------------------------------------------
    def start_wifi_scan(self):
        if self.wifi_scanning:
            return
        self.wifi_scanning = True
        self.wifi_scan_button.config(text="Scanning...", fg="#FFFF00")
        self.wifi_status_lbl.config(text="Scanning for nearby access points...")
        threading.Thread(target=self.wifi_scan_thread, daemon=True).start()

    def wifi_scan_thread(self):
        try:
            access_points = scan_wifi()
        except Exception:
            logger.exception("wifi scan failed")
            access_points = []
        self.root.after(0, self.show_wifi_results, access_points)

    #--------------------------------------------------------------------------
    # show_wifi_results
    # one row per access point: signal (coloured), name, channel, security
    #--------------------------------------------------------------------------
    def show_wifi_results(self, access_points):
        self.wifi_scanning = False
        self.wifi_scan_button.config(text="Scan", fg="#FFFFFF")

        for child in self.wifi_rows.winfo_children():
            child.destroy()
        self.wifi_canvas.yview_moveto(0)

        stamp = datetime.now().strftime("%I:%M:%S %p")
        self.wifi_status_lbl.config(text=f"{len(access_points)} access points  |  scanned {stamp}  |  tap one for info")

        if len(access_points) == 0:
            tk.Label(self.wifi_rows, text="No access points found", font=("Helvetica", 14),
                     bg="#121212", fg="#888888").pack(pady=30)
            return

        for ap in access_points:
            name = ap["ssid"]
            if ap["connected"]:
                name = "*" + name     # * = the Pi's own wifi
            text = f"{ap['signal']:>4} {name[:16]:<16} ch{ap['channel']:<4}{ap['security']}"
            tk.Button(
                self.wifi_rows, text=text, font=("Courier", 12, "bold"), anchor="w",
                bg="#1A1A1A", fg=signal_color(ap["signal"]), activebackground="#2B2B2B",
                activeforeground="#FFFFFF", relief=tk.RIDGE, pady=3,
                command=lambda a=ap: self.show_ap_info(a)
            ).pack(fill=tk.X, padx=6, pady=1)

    #--------------------------------------------------------------------------
    # show_ap_info
    # full info for one access point. Ignored if the finger was dragging the
    # list (otherwise scrolling would open whatever row you started on)
    #--------------------------------------------------------------------------
    def show_ap_info(self, ap):
        if self.drag_moved:
            return
        self.ap_lbl.config(text=format_ap_detail(ap))
        self.hide_all_frames()
        self.ap_frame.pack(expand=True, fill=tk.BOTH)
        self.ap_canvas.yview_moveto(0)
        self.current_view = "ap"

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
            "dns_last_domain": "None",
            "dns_server": "None",
            "dns_time": "Failed",
            "switch_poe": "Unknown (no CDP/LLDP)",
            "speed_rating": "--"
        }
        self.cdp_poe = ""    # forget the last cable's PoE info

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

        self.scan_results["switch_poe"] = self.get_poe_text(kv_text)

        return True

    #--------------------------------------------------------------------------
    # get_poe_text
    # what the switch announced about PoE on this port (802.3at power info in
    # LLDP, or LLDP-MED). The Pi itself can't measure PoE voltage - that needs
    # a PoE HAT or a PoE tester - so this is the switch's word for it.
    # output: text like "Yes, class 4, 25500 mW" or "Not advertised"
    #--------------------------------------------------------------------------
    def get_poe_text(self, kv_text):
        supported = self.get_lldp_value(kv_text, "port.power.supported")
        power_class = self.get_lldp_value(kv_text, "port.power.class")
        allocated = self.get_lldp_value(kv_text, "port.power.allocated")
        med_power = self.get_lldp_value(kv_text, "lldp-med.poe.power")

        if supported == "" and med_power == "":
            return "Not advertised"
        if supported == "no":
            return "No"

        text = "Yes"
        if power_class != "":
            text = text + ", " + power_class
        if allocated != "":
            text = text + ", " + allocated + " mW"
        elif med_power != "":
            text = text + ", " + med_power + " mW"
        return text

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

    #--------------------------------------------------------------------------
    # test_dns
    # 1) start the packet sniffer and WAIT until it is really listening
    # 2) send a real DNS lookup out of the test port to the network's DNS server
    # 3) keep sniffing for the rest of the 3 seconds to catch any other DNS traffic
    #--------------------------------------------------------------------------
    def test_dns(self):
        sniffer_ready = threading.Event()
        sniffer = AsyncSniffer(
            iface=INTERFACE, filter="port 53", prn=self.parse_dns_packet,
            store=False, started_callback=sniffer_ready.set
        )
        sniffer.start()
        sniffer_ready.wait(timeout=5)

        servers = get_dns_servers()
        if len(servers) == 0:
            servers = ["1.1.1.1"]   # the network gave us none, try a public one
        server = servers[0]

        worked, answer = dns_lookup(server, "google.com")
        self.scan_results["dns_server"] = server
        self.scan_results["dns_time"] = answer

        time.sleep(3)
        sniffer.stop()

    #--------------------------------------------------------------------------
    # on_cdp_packet
    # called for every Cisco CDP announcement (about once a minute). A PoE port
    # includes "Power Available" in milliwatts; a non-PoE port leaves it out.
    #--------------------------------------------------------------------------
    def on_cdp_packet(self, packet):
        if packet.haslayer(CDPMsgPowerAvailable):
            values = packet[CDPMsgPowerAvailable].power_available_list
            if len(values) > 0:
                watts = values[0] / 1000
                self.cdp_poe = f"Yes, {watts:.1f} W available (CDP)"
                return
        self.cdp_poe = "No (switch offers no power)"

    def parse_dns_packet(self, packet):
        # count the questions (qr == 0); answers coming back are the same lookups
        if packet.haslayer(DNS) and packet.haslayer(DNSQR) and packet[DNS].qr == 0:
            qname = packet[DNSQR].qname.decode(errors="replace").rstrip(".")
            self.scan_results["dns_queries"] += 1
            self.scan_results["dns_last_domain"] = qname[:25]

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

            # PoE: what the switch announced (LLDP via lldpd, or CDP via our own listener)
            if self.cdp_poe != "" and self.scan_results["switch_poe"] in ("Not advertised", "Unknown (no CDP/LLDP)"):
                self.scan_results["switch_poe"] = self.cdp_poe

            # 3. DNS TEST (real lookup out of eth0 + 3s of sniffing)
            self.root.after(0, self.update_scanner, "TESTING NETWORK...", "#FFFF00", "Mode: DNS Test", "00:03")
            self.sniffing = True
            timer_thread = threading.Thread(target=self.countdown_timer, args=(3,))
            timer_thread.start()

            self.test_dns()

            self.sniffing = False
            timer_thread.join()
            if not self.is_cable_connected():
                self.finish_scan(False)
                continue

            # 4. BANDWIDTH TEST: Animated Loading Screen
            self.root.after(0, self.update_scanner, "TESTING NETWORK...", "#FFFF00", "Mode: Bandwidth Speed Test", "")
            self.root.after(0, self.start_loading_animation, "Connecting to closest server...")

            try:
                # source_address = run the test through the cable, not the Pi's wifi
                st = speedtest.Speedtest(source_address=get_eth_ip())
                st.get_best_server()

                self.root.after(0, self.set_loading_message, "Testing Download Speed...")
                down_bps = st.download()

                self.root.after(0, self.set_loading_message, "Testing Upload Speed...")
                up_bps = st.upload()

                self.scan_results["speed_down"] = f"{down_bps / 1_000_000:.1f} Mbps"
                self.scan_results["speed_up"] = f"{up_bps / 1_000_000:.1f} Mbps"
                self.scan_results["speed_ping"] = f"{st.results.ping:.0f} ms"
                self.scan_results["speed_rating"] = speed_rating(down_bps / 1_000_000)
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
