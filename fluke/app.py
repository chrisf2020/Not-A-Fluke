# main window: top bar, tab bar (Cable / Wi-Fi / History), the cable test screens and the scan loop
# history and wifi screens come from history.py and wifi.py as mixins

import re
import time
import threading
import tkinter as tk
from tkinter import ttk

from .config import (VERSION, INTERFACE, WIFI_INTERFACE, CARRIER_PATH, SWITCH_DISCOVERY_TIMEOUT, SWITCH_QUICK_CHECK,
                     BG, CARD_BG, BAR_BG, ACCENT, TEXT, MUTED, GOOD, OK, BAD, logger)
from .history import HistoryScreens, save_scan_record, now_stamp, is_clock_synced, format_stamp
from .wifi import WifiScreens
from .graph import MetricsGraph
from .speed import get_ip, link_speed, read_number, run_ping_test, run_bandwidth_test
from .switch import query_lldpd, parse_switch_topology, start_cdp_listener, start_switch_frame_listener, stop_listener

# same size as the Pi's TFT so a VM window looks like the real screen
WINDOW_SIZE = "480x320"

# the steps shown in the checklist while a scan runs
SCAN_STEPS = ["Switch", "DNS", "Ping", "Speed"]

# which tab each screen belongs to
VIEW_TAB = {"scanner": "cable", "results": "cable", "cable_speed": "cable", "wifi": "wifi", "ap": "wifi",
            "wifi_speed": "wifi", "history": "history", "detail": "history",
            "share_cable": "cable", "share_history": "history"}


# blank scan record. "--" while waiting, then each test starts as "Failed" until it passes
def empty_scan_results(started=False):
    failed = "Failed" if started else "--"
    return {
        "started_at": now_stamp() if started else "--",
        "finished_at": "--",
        "clock_synced": is_clock_synced() if started else "unknown",
        "complete": False,
        "link_speed": "--",
        "switch_name": "Not Found" if started else "--",
        "switch_ip": "Not Advertised" if started else "--",
        "switch_port": "Not Found" if started else "--",
        "switch_vlan": "None / Untagged" if started else "--",
        "switch_voice": "None" if started else "--",
        "switch_proto": "--",
        "speed_down": failed,
        "speed_up": failed,
        "speed_ping": failed,
        "ping_latency": failed,
        "ping_jitter": failed,
        "ping_loss": failed,
        "dns_queries": 0,
        "dns_last_domain": "None" if started else "--",
        "dns_server": "None" if started else "--",
        "dns_time": failed,
        "switch_poe": "Unknown (no CDP/LLDP)" if started else "--",
        "speed_rating": "--"
    }


# "1000 Mbps full" -> "1G", "100 Mbps half" -> "100M half"
def short_link(text):
    speed = read_number(text)
    if speed is None:
        return ""
    short = f"{speed / 1000:g}G" if speed >= 1000 else f"{speed:g}M"
    return short + (" half" if "half" in text else "")


# QR code size on the Send to phone screen (pixels)
QR_SIZE = 210

# shown as the switch name when it's plugged into a router / unmanaged switch
NO_SWITCH = "No managed switch"


# the coloured row at the top of the results: [(text, colour), ...]
def scan_summary(results):
    link = results.get("link_speed", "--")
    speed = read_number(link)
    link_color = GOOD if speed is None or (speed >= 1000 and "half" not in link) else OK

    found = results.get("switch_name") not in ("Not Found", "--", None, NO_SWITCH)
    dns_ok = "ms" in str(results.get("dns_time"))

    down = read_number(results.get("speed_down"))
    if down is None:
        internet_color = BAD
    elif results.get("speed_rating") == "Poor":
        internet_color = OK
    else:
        internet_color = GOOD

    return [
        (("Link " + short_link(link)).strip(), link_color),
        ("Switch", GOOD if found else OK),      # no CDP/LLDP isn't always a problem
        ("DNS", GOOD if dns_ok else BAD),
        ("Internet", internet_color),
    ]


class FlukeApp(HistoryScreens, WifiScreens):
    # windowed = 480x320 window for VM/desktop, start_workers=False is for tests
    def __init__(self, root, windowed=False, start_workers=True):
        self.root = root
        self.root.title("Not-A-Fluke")
        if windowed:
            self.root.geometry(WINDOW_SIZE)
        else:
            self.root.attributes("-fullscreen", True)
        self.root.configure(bg=BG)
        self.root.bind("<Escape>", self.close_window)

        self.current_view = "scanner"
        self.cable_view = "scanner"     # scanner or results, whatever the test is on
        self.history_records = []
        self.history_page = 0
        self.exit_armed = False
        self.rescan_requested = False
        self.scan_results = empty_scan_results()
        self.speed_curves = {"down": [], "up": []}   # samples for the speed graph

        # if the finger moved enough it's a drag, so don't also press the button under it
        self.drag_start_y = 0
        self.drag_moved = False

        self.style = ttk.Style()
        self.style.theme_use('default')
        self.style.configure("Custom.Horizontal.TProgressbar", troughcolor=CARD_BG, background=ACCENT, thickness=10)

        # title on the left. exit, version and IP on the right
        top_bar = tk.Frame(root, bg=BAR_BG)
        top_bar.pack(side=tk.TOP, fill=tk.X)
        tk.Label(top_bar, text="NOT-A-FLUKE", font=("Helvetica", 11, "bold"), bg=BAR_BG, fg=ACCENT).pack(side=tk.LEFT, padx=6)
        self.exit_button = tk.Label(top_bar, text=" ✕ ", font=("DejaVu Sans", 12, "bold"), bg=BAR_BG, fg=BAD)
        self.exit_button.pack(side=tk.RIGHT, padx=(4, 0))
        self.exit_button.bind("<ButtonRelease-1>", lambda event: self.exit_pressed())
        tk.Label(top_bar, text="v" + VERSION, font=("Helvetica", 10), bg=BAR_BG, fg=MUTED).pack(side=tk.RIGHT, padx=4)
        # show the IP (cable first, then wifi) so you know where to SSH
        self.ip_lbl = tk.Label(top_bar, text="", font=("Helvetica", 10), bg=BAR_BG, fg="#BBBBBB")
        self.ip_lbl.pack(side=tk.RIGHT, padx=4)

        # tabs along the bottom, packed before the arrows so they span the full width
        tab_bar = tk.Frame(root, bg=BAR_BG)
        tab_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.tab_buttons = {}
        for name, text, command in (("cable", "Cable", self.open_cable), ("wifi", "Wi-Fi", self.open_wifi),
                                    ("history", "History", self.open_history)):
            button = tk.Button(tab_bar, text=text, command=command, font=("Helvetica", 13, "bold"),
                               bd=0, relief=tk.FLAT, pady=7, highlightthickness=0)
            button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=1, pady=(1, 0))
            self.tab_buttons[name] = button

        # up/down arrows on the right side
        arrow_strip = tk.Frame(root, bg=BG, width=40)
        arrow_strip.pack(side=tk.RIGHT, fill=tk.Y)
        arrow_strip.pack_propagate(False)
        self.make_arrow(arrow_strip, "▲", -1)
        self.make_arrow(arrow_strip, "▼", 1)

        self.container = tk.Frame(root, bg=BG)
        self.container.pack(expand=True, fill=tk.BOTH)

        # keyboard and mouse wheel scrolling for VM/desktop
        self.root.bind("<Up>", lambda event: self.scroll_by(-1))
        self.root.bind("<Down>", lambda event: self.scroll_by(1))
        self.root.bind("<Prior>", lambda event: self.scroll_by(-5))     # Page Up
        self.root.bind("<Next>", lambda event: self.scroll_by(5))       # Page Down
        self.root.bind("<MouseWheel>", self.scroll_with_mouse_wheel)
        self.root.bind("<Button-4>", lambda event: self.scroll_by(-1))
        self.root.bind("<Button-5>", lambda event: self.scroll_by(1))

        # drag a finger anywhere on a page to scroll it
        self.root.bind_all("<ButtonPress-1>", self.drag_start, add="+")
        self.root.bind_all("<B1-Motion>", self.drag_move, add="+")

        self.build_scanner_ui()
        self.build_results_ui()
        self.build_history_ui()
        self.build_detail_ui()
        self.build_wifi_ui()
        self.build_ap_ui()
        self.build_wifi_speed_ui()
        self.build_share_ui()
        self.frames = [self.scanner_frame, self.results_frame, self.cable_speed_frame, self.history_frame,
                       self.detail_frame, self.wifi_frame, self.ap_frame, self.wifi_speed_frame,
                       self.share_frame]
        self.scroll_canvases = {"results": self.scroll_canvas, "cable_speed": self.cable_speed_canvas,
                                "detail": self.detail_canvas,
                                "wifi": self.wifi_canvas, "ap": self.ap_canvas,
                                "wifi_speed": self.wifi_speed_canvas}
        self.show_scanner_ui()

        self.running = True
        self.sniffing = False
        # listen for CDP the whole time for PoE info (see switch.py)
        self.cdp_poe = ""
        if start_workers:
            start_cdp_listener(self.set_cdp_poe)
            self.worker_thread = threading.Thread(target=self.main_workflow, daemon=True)
            self.worker_thread.start()
            self.refresh_ip()

    # refresh the top bar IP every 5s in case the cable goes in later
    def refresh_ip(self):
        for interface in (INTERFACE, WIFI_INTERFACE):
            ip = get_ip(interface)
            if ip != "":
                self.ip_lbl.config(text=f"{interface} {ip}")
                break
        else:
            self.ip_lbl.config(text="no IP")
        self.root.after(5000, self.refresh_ip)

    # --- UI BUILDERS ---

    # big dark touch button, caller packs it
    def make_button(self, parent, text, command, size=14):
        return tk.Button(
            parent, text=text, command=command, font=("Helvetica", size, "bold"),
            bg=BAR_BG, fg=TEXT, activebackground="#444444", activeforeground=TEXT,
            padx=12, pady=6 if size >= 14 else 2
        )

    # title strip for the screens you open from a list, with a Back button
    def make_header(self, parent, text, back=None):
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill=tk.X, pady=(4, 0))
        if back is not None:
            self.make_button(bar, "‹ Back", back, size=11).pack(side=tk.LEFT, padx=6)
        tk.Label(bar, text=text, font=("Helvetica", 13, "bold"), bg=BG, fg=ACCENT).pack(side=tk.LEFT, padx=6)
        return bar

    # two or three buttons side by side, the active one highlighted (Wi-Fi: Networks | Speed)
    def make_switcher(self, parent, options, active):
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill=tk.X, padx=6, pady=(4, 2))
        for text, command in options:
            on = text == active
            tk.Button(bar, text=text, command=command, font=("Helvetica", 11, "bold"), relief=tk.FLAT, bd=0,
                      bg=ACCENT if on else BAR_BG, fg="#000000" if on else TEXT, pady=3,
                      activebackground=ACCENT, highlightthickness=0).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=1)
        return bar

    # big arrow button on the right strip, hold it to keep scrolling
    def make_arrow(self, parent, symbol, direction):
        arrow = tk.Label(parent, text=symbol, font=("DejaVu Sans", 16, "bold"), bg=BAR_BG, fg=ACCENT, relief=tk.RAISED, bd=1)
        arrow.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=2, pady=2)
        arrow.bind("<ButtonPress-1>", lambda event: self.arrow_pressed(arrow, direction))
        arrow.bind("<ButtonRelease-1>", lambda event: self.arrow_released(arrow))

    # scrollable box, returns (canvas, inner frame) to put widgets in
    def make_scroll_area(self, parent):
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0)
        canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        inner = tk.Frame(canvas, bg=BG)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfig(window, width=event.width))
        return canvas, inner

    # a card with a small title, rows go in with make_row
    def make_card(self, parent, title):
        card = tk.Frame(parent, bg=CARD_BG)
        card.pack(fill=tk.X, padx=8, pady=5)
        tk.Label(card, text=title, font=("Helvetica", 12, "bold"), bg=CARD_BG, fg=ACCENT, anchor="w").pack(fill=tk.X, padx=10, pady=(6, 2))
        return card

    # one "label   value" line in a card, returns the value label to fill in later
    def make_row(self, card, label, value="--"):
        row = tk.Frame(card, bg=CARD_BG)
        row.pack(fill=tk.X, padx=10, pady=1)
        tk.Label(row, text=label, width=11, anchor="w", font=("Helvetica", 11), bg=CARD_BG, fg=MUTED).pack(side=tk.LEFT)
        value_lbl = tk.Label(row, text=value, anchor="w", justify=tk.LEFT, wraplength=260,
                             font=("Helvetica", 11, "bold"), bg=CARD_BG, fg=TEXT)
        value_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        return value_lbl

    def build_scanner_ui(self):
        self.scanner_frame = tk.Frame(self.container, bg=BG)

        self.status_label = tk.Label(self.scanner_frame, text="WAITING FOR CABLE...", font=("Helvetica", 18, "bold"),
                                     bg=BAR_BG, fg=OK, pady=8)
        self.status_label.pack(fill=tk.X)

        # ✓ Switch  ● DNS  ○ Ping  ○ Speed
        steps = tk.Frame(self.scanner_frame, bg=BG)
        steps.pack(pady=(10, 0))
        self.step_labels = []
        for name in SCAN_STEPS:
            label = tk.Label(steps, text="○ " + name, font=("DejaVu Sans", 12, "bold"), bg=BG, fg=MUTED)
            label.pack(side=tk.LEFT, padx=8)
            self.step_labels.append(label)

        self.timer_label = tk.Label(self.scanner_frame, text="--:--", font=("Helvetica", 40, "bold"), bg=BG, fg=TEXT)
        self.timer_label.pack(pady=2)

        # loading bar for the speed test
        self.loading_frame = tk.Frame(self.scanner_frame, bg=BG)
        self.loading_text = tk.Label(self.loading_frame, text="", font=("Helvetica", 13), bg=BG, fg=ACCENT)
        self.loading_text.pack(pady=5)
        self.progress_bar = ttk.Progressbar(self.loading_frame, style="Custom.Horizontal.TProgressbar",
                                            mode="indeterminate", length=320)
        self.progress_bar.pack(pady=4)

        # what ping prints while the ping step runs, last few lines
        self.ping_lines = []
        # anchor center = the block of lines sits in the middle, justify left = lines still line up
        self.ping_log = tk.Label(self.scanner_frame, text="", font=("DejaVu Sans Mono", 12), bg=BG, fg=MUTED,
                                 justify=tk.LEFT, anchor="center")
        # fills the space under the step row, so the lines sit in the middle of it
        self.ping_log.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True, padx=10, pady=6)

    def build_results_ui(self):
        # Results | Speed, same idea as Networks | Speed on the Wi-Fi tab
        cable_views = [("Results", self.show_cable_results), ("Speed", self.show_cable_speed)]
        self.results_frame = tk.Frame(self.container, bg=BG)
        self.make_switcher(self.results_frame, cable_views, "Results")
        self.scroll_canvas, self.scroll_content = self.make_scroll_area(self.results_frame)

        # the speed graph for the cable test
        self.cable_speed_frame = tk.Frame(self.container, bg=BG)
        self.make_switcher(self.cable_speed_frame, cable_views, "Speed")
        self.cable_speed_canvas, speed_inner = self.make_scroll_area(self.cable_speed_frame)
        graph_card = self.make_card(speed_inner, "Cable speed")
        self.cable_graph = MetricsGraph(graph_card, INTERFACE)
        self.cable_graph.canvas.pack(fill=tk.X, padx=4, pady=4)

        # pass / fail at a glance
        summary = tk.Frame(self.scroll_content, bg=BG)
        summary.pack(fill=tk.X, padx=8, pady=(8, 2))
        self.summary_labels = []
        for k in range(4):
            label = tk.Label(summary, text="", font=("DejaVu Sans", 11, "bold"), bg=CARD_BG, fg=MUTED, pady=4)
            label.grid(row=0, column=k, sticky="ew", padx=2)
            summary.columnconfigure(k, weight=1)
            self.summary_labels.append(label)

        when = tk.Frame(self.scroll_content, bg=BG)
        when.pack(fill=tk.X, padx=8, pady=2)
        self.lbl_tested = tk.Label(when, text="Tested: --", font=("Helvetica", 10), bg=BG, fg=MUTED)
        self.lbl_tested.pack(side=tk.LEFT)
        self.rescan_button = self.make_button(when, "↻ Rescan", self.rescan_pressed, size=11)
        self.rescan_button.pack(side=tk.RIGHT)
        # send this scan to a phone as a PDF (share.py)
        self.make_button(when, "QR", lambda: self.open_share(self.scan_results, "results"),
                         size=11).pack(side=tk.RIGHT, padx=4)

        switch = self.make_card(self.scroll_content, "Switch")
        self.res_switch = self.make_row(switch, "Switch")
        self.res_port = self.make_row(switch, "Port")
        self.res_vlan = self.make_row(switch, "VLAN")
        self.res_voice = self.make_row(switch, "Voice VLAN")
        # what the switch says about power, the Pi can't measure it itself
        self.res_poe = self.make_row(switch, "PoE")
        self.res_link = self.make_row(switch, "Link")
        self.res_switch_ip = self.make_row(switch, "Switch IP")
        self.res_proto = self.make_row(switch, "Found via")
        tk.Frame(switch, bg=CARD_BG, height=6).pack()

        dns = self.make_card(self.scroll_content, "DNS")
        self.res_dns_server = self.make_row(dns, "Server")
        self.res_dns_time = self.make_row(dns, "Answer")
        self.res_dns_seen = self.make_row(dns, "Lookups")
        tk.Frame(dns, bg=CARD_BG, height=6).pack()

        # just the numbers here, the graph is on the Speed view
        speed = self.make_card(self.scroll_content, "Speed")
        self.res_down = self.make_row(speed, "Download")
        self.res_up = self.make_row(speed, "Upload")
        self.res_latency = self.make_row(speed, "Ping")
        self.res_jitter = self.make_row(speed, "Jitter")
        self.res_loss = self.make_row(speed, "Packet loss")
        self.res_rating = self.make_row(speed, "Rating")
        tk.Frame(speed, bg=CARD_BG, height=6).pack()

        tk.Label(self.scroll_content, text="Drag or use the arrows to scroll", font=("Helvetica", 9),
                 bg=BG, fg=MUTED, pady=8).pack(fill=tk.X)

    # --- EVENT HANDLERS ---

    def close_window(self, event):
        self.root.destroy()

    # tap ✕ once for "Sure?", again within 3s to quit
    # (the notafluke launcher puts the terminal back after)
    def exit_pressed(self):
        if self.exit_armed:
            logger.info("exit button pressed")
            self.root.destroy()
            return
        self.exit_armed = True
        self.exit_button.config(text=" Sure? ", bg=BAD, fg=TEXT)
        self.root.after(3000, self.disarm_exit)

    def disarm_exit(self):
        self.exit_armed = False
        self.exit_button.config(text=" ✕ ", bg=BAR_BG, fg=BAD)

    # --- SEND TO PHONE (QR code -> PDF or text, see share.py) ---

    def build_share_ui(self):
        self.share_frame = tk.Frame(self.container, bg=BG)
        bar = self.make_header(self.share_frame, "Send to phone", back=self.close_share)
        # PDF = link to a PDF (same network), Email = the scan as an email draft (no network needed)
        self.share_mode_buttons = {}
        for mode in ("Email", "PDF"):
            button = tk.Button(bar, text=mode, command=lambda m=mode: self.set_share_mode(m),
                               font=("Helvetica", 11, "bold"), relief=tk.FLAT, bd=0, padx=10, pady=2,
                               bg=BAR_BG, fg=TEXT, activebackground=ACCENT, highlightthickness=0)
            button.pack(side=tk.RIGHT, padx=1)
            self.share_mode_buttons[mode] = button
        body = tk.Frame(self.share_frame, bg=BG)
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        # QR on the left, what to do on the right
        self.qr_canvas = tk.Canvas(body, width=QR_SIZE, height=QR_SIZE, bg="white", highlightthickness=0)
        self.qr_canvas.pack(side=tk.LEFT, anchor="n")
        self.share_lbl = tk.Label(body, text="", font=("Helvetica", 11), bg=BG, fg=TEXT,
                                  justify=tk.LEFT, anchor="nw", wraplength=180)
        self.share_lbl.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))

    # record = the scan to send, came_from = "results" or "detail" (where Back goes)
    def open_share(self, record, came_from):
        self.share_came_from = came_from
        self.share_record = dict(record)
        view = "share_cable" if came_from == "results" else "share_history"
        self.show_frame(self.share_frame, view)
        self.set_share_mode("PDF")

    def set_share_mode(self, mode):
        self.share_mode = mode
        for name, button in self.share_mode_buttons.items():
            on = name == mode
            button.config(bg=ACCENT if on else BAR_BG, fg="#000000" if on else TEXT)
        self.qr_canvas.delete("all")
        if mode == "Email":
            self.show_email_share()
            return
        self.share_lbl.config(text="Making the PDF...", fg=TEXT)
        # the PDF takes a second, so make it off the screen thread
        threading.Thread(target=self.make_share, args=(self.share_record,), daemon=True).start()

    def make_share(self, record):
        from .share import share_record, qr_matrix
        try:
            url, interface = share_record(record)
            matrix = qr_matrix(url)
        except ImportError:
            logger.exception("QR package missing")
            self.root.after(0, self.share_failed, "The QR package is missing. Run  notafluke  once over SSH to install it.")
            return
        except Exception as err:
            logger.exception("could not share the scan as a PDF")
            # no network to share it on (no IP etc.): the text QR still works
            self.root.after(0, self.share_failed, f"No PDF: {err}.\n\nShowing the Email QR instead, it works without a network.", True)
            return
        self.root.after(0, self.show_share, url, interface, matrix)

    def show_share(self, url, interface, matrix):
        if self.share_mode != "PDF":    # switched to Text while the PDF was being made
            return
        self.draw_qr(matrix)
        network = "Wi-Fi" if interface == WIFI_INTERFACE else "the cable's network"
        self.share_lbl.config(fg=TEXT, text=(
            "Scan with your phone camera to open the PDF.\n\n"
            "If it says the connection isn't private: Show Details, then visit this website.\n\n"
            f"Phone must be on the Pi's network ({network}). Not on it? Tap Email."))

    def show_email_share(self):
        from .share import scan_email_link, qr_matrix
        try:
            # L = least error correction, so the squares stay as big as possible
            self.draw_qr(qr_matrix(scan_email_link(self.share_record), error_correction="L"))
        except ImportError:
            self.share_failed("The QR package is missing. Run  notafluke  once over SSH to install it.")
            return
        self.share_lbl.config(fg=TEXT, text=(
            "Scan with your phone camera. Your mail app opens a new email with the "
            "results typed in - no Wi-Fi needed to scan it.\n\n"
            "Send it to yourself, or save it as a draft."))

    # black squares on the white canvas, as big as fit
    def draw_qr(self, matrix):
        self.qr_canvas.delete("all")
        # cell size isn't rounded down, so the QR always fills the whole box (bigger squares)
        cell = QR_SIZE / len(matrix)
        offset = 0
        for row_number, row in enumerate(matrix):
            for column_number, black in enumerate(row):
                if black:
                    x = offset + column_number * cell
                    y = offset + row_number * cell
                    self.qr_canvas.create_rectangle(x, y, x + cell, y + cell, fill="black", outline="")

    def share_failed(self, message, use_text=False):
        if use_text and self.share_mode == "PDF":
            self.set_share_mode("Email")
            self.share_lbl.config(text=message + "\n\n" + self.share_lbl.cget("text"), fg=TEXT)
            return
        self.qr_canvas.delete("all")
        self.share_lbl.config(text=message, fg=BAD)

    def close_share(self):
        if self.share_came_from == "results":
            self.show_cable_results()
        else:
            self.show_frame(self.detail_frame, "detail")

    # start a new scan without unplugging the cable
    def rescan_pressed(self):
        self.rescan_requested = True
        self.rescan_button.config(text="Starting...")

    # scroll up (negative) or down (positive). History flips pages instead
    def scroll_by(self, steps):
        if self.current_view == "history":
            if steps < 0:
                self.history_newer()
            else:
                self.history_older()
            return
        canvas = self.scroll_canvases.get(self.current_view)
        if canvas is not None:
            canvas.yview_scroll(steps, "units")

    def scroll_with_mouse_wheel(self, event):
        # delta > 0 means wheel up
        self.scroll_by(-1 if event.delta > 0 else 1)

    # scroll once, then keep going while held
    def arrow_pressed(self, arrow, direction):
        arrow.config(bg="#444444", relief=tk.SUNKEN)
        self.arrow_direction = direction
        if self.current_view == "history":
            self.scroll_by(direction)      # history is paged, so no repeat
            return
        self.scroll_by(direction * 2)
        self.arrow_job = self.root.after(400, self.arrow_repeat)

    def arrow_repeat(self):
        self.scroll_by(self.arrow_direction * 2)
        self.arrow_job = self.root.after(100, self.arrow_repeat)

    def arrow_released(self, arrow):
        arrow.config(bg=BAR_BG, relief=tk.RAISED)
        if getattr(self, "arrow_job", None) is not None:
            self.root.after_cancel(self.arrow_job)
            self.arrow_job = None

    def drag_start(self, event):
        self.drag_start_y = event.y_root
        self.drag_moved = False

    def drag_move(self, event):
        canvas = self.scroll_canvases.get(self.current_view)
        if canvas is None:
            return
        # small wobbles on the resistive screen still count as a tap
        if not self.drag_moved:
            if abs(event.y_root - self.drag_start_y) > 10:
                self.drag_moved = True
                canvas.scan_mark(0, event.y_root)
            return
        canvas.scan_dragto(0, event.y_root, gain=1)

    # --- UI STATE MANAGERS ---

    # swap whatever screen is showing for `frame` and light up its tab
    def show_frame(self, frame, view):
        for other in self.frames:
            other.pack_forget()
        frame.pack(expand=True, fill=tk.BOTH)
        self.current_view = view
        for name, button in self.tab_buttons.items():
            on = name == VIEW_TAB[view]
            button.config(bg=ACCENT if on else BAR_BG, fg="#000000" if on else TEXT,
                          activebackground=ACCENT if on else "#444444")

    def current_tab(self):
        return VIEW_TAB[self.current_view]

    def show_cable_results(self):
        self.show_frame(self.results_frame, "results")

    def show_cable_speed(self):
        self.show_frame(self.cable_speed_frame, "cable_speed")
        self.cable_speed_canvas.yview_moveto(0)

    # Cable tab: the scanner or the results, whichever the test is on
    def open_cable(self):
        if self.cable_view == "results":
            self.show_frame(self.results_frame, "results")
        else:
            self.show_frame(self.scanner_frame, "scanner")

    # the test keeps running while you're on another tab, it just won't pull you back
    def show_scanner_ui(self):
        self.stop_loading_animation()
        self.cable_view = "scanner"
        if self.current_tab() == "cable":
            self.show_frame(self.scanner_frame, "scanner")

    def show_results_ui(self):
        self.stop_loading_animation()
        results = self.scan_results

        for label, (text, color) in zip(self.summary_labels, scan_summary(results)):
            label.config(text="● " + text, fg=color)

        tested_text = f"Tested {format_stamp(results['finished_at'])}"
        if results["clock_synced"] != "yes":
            tested_text += " (clock not synced)"
        self.lbl_tested.config(text=tested_text)
        self.rescan_button.config(text="↻ Rescan")

        self.res_switch.config(text=results["switch_name"])
        self.res_port.config(text=results["switch_port"])
        self.res_vlan.config(text=results["switch_vlan"])
        self.res_voice.config(text=results["switch_voice"])
        self.res_poe.config(text=results["switch_poe"])
        self.res_link.config(text=results["link_speed"])
        self.res_switch_ip.config(text=results["switch_ip"])
        self.res_proto.config(text=results["switch_proto"])
        self.res_dns_server.config(text=results["dns_server"])
        self.res_dns_time.config(text=results["dns_time"])
        self.res_dns_seen.config(text=f"{results['dns_queries']}  (last: {results['dns_last_domain']})")
        self.res_down.config(text=results["speed_down"])
        self.res_up.config(text=results["speed_up"])
        self.res_latency.config(text=results["ping_latency"])
        self.res_jitter.config(text=results["ping_jitter"])
        self.res_loss.config(text=results["ping_loss"])
        self.res_rating.config(text=results["speed_rating"])
        self.cable_graph.show(results, self.speed_curves)

        self.cable_view = "results"
        if self.current_tab() == "cable":
            self.show_frame(self.results_frame, "results")
            self.scroll_canvas.yview_moveto(0)

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
        self.timer_label.pack(pady=10, before=self.ping_log)

    # newest line goes at the bottom, older ones scroll off the top
    def add_ping_line(self, line):
        line = re.sub(r"^\d+ bytes from |, time \d+ms$", "", line)[:44]   # fit the small screen
        self.ping_lines = (self.ping_lines + [line])[-8:]   # 8 lines fill the space under the steps
        self.ping_log.config(text="\n".join(self.ping_lines))

    # step = index in SCAN_STEPS that's running now (-1 = waiting, 4 = all done)
    def update_scanner(self, status, status_color, step, timer_text):
        if step != 2:     # the ping output only shows during the ping step
            self.ping_lines = []
            self.ping_log.config(text="")
        # the timer is blank while pinging, so hide it and let the ping lines sit in the middle
        if step == 2:
            self.timer_label.pack_forget()
        elif not self.timer_label.winfo_manager():
            self.timer_label.pack(pady=2, before=self.ping_log)
        self.status_label.config(text=status, fg=status_color)
        for k, label in enumerate(self.step_labels):
            if k < step:
                label.config(text="✓ " + SCAN_STEPS[k], fg=GOOD)
            elif k == step:
                label.config(text="● " + SCAN_STEPS[k], fg=ACCENT)
            else:
                label.config(text="○ " + SCAN_STEPS[k], fg=MUTED)
        self.timer_label.config(text=timer_text)

    # stamp the finish time and log the scan. True when results are ready,
    # False if the cable got pulled mid-scan
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

    # --- THE SCAN ---

    def set_cdp_poe(self, text):
        self.cdp_poe = text

    def is_cable_connected(self):
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

    # check lldpd once a second (switch.py). it's always running so usually the
    # first check already has data. returns False on timeout or cable pulled.
    # if no switch frames at all (STP/CDP/LLDP) show up in SWITCH_QUICK_CHECK seconds, it's
    # plugged into a router or unmanaged switch, so stop early instead of waiting 35s
    def discover_switch_topology(self):
        self.heard_switch_frames = False
        listener = start_switch_frame_listener(self.set_heard_switch_frames)
        try:
            return self.wait_for_switch(listener is not None)
        finally:
            stop_listener(listener)

    def set_heard_switch_frames(self):
        self.heard_switch_frames = True

    def wait_for_switch(self, can_skip):
        seconds_left = SWITCH_DISCOVERY_TIMEOUT
        while seconds_left >= 0:
            if not self.is_cable_connected():
                return False

            waited = SWITCH_DISCOVERY_TIMEOUT - seconds_left
            if can_skip and waited >= SWITCH_QUICK_CHECK and not self.heard_switch_frames:
                logger.info("no STP/CDP/LLDP in %ds - not a managed switch, skipping the wait", waited)
                self.scan_results["switch_name"] = NO_SWITCH
                self.scan_results["switch_port"] = "--"
                return False

            minutes = seconds_left // 60
            seconds = seconds_left % 60
            self.root.after(0, self.timer_label.config, {'text': f"{minutes:02d}:{seconds:02d}"})

            topology = parse_switch_topology(query_lldpd())
            if topology is not None:
                self.scan_results.update(topology)
                return True

            time.sleep(1)
            seconds_left = seconds_left - 1

        return False

    def main_workflow(self):
        # import scapy here on the worker thread since it's slow, so the screen shows up right away
        from .dns import run_dns_test

        testing = "TESTING NETWORK..."
        while self.running:
            # 1. wait for a cable
            self.root.after(0, self.show_scanner_ui)
            if not self.is_cable_connected():
                self.root.after(0, self.update_scanner, "WAITING FOR CABLE...", OK, -1, "--:--")
                while not self.is_cable_connected() and self.running:
                    time.sleep(1)

            if not self.running:
                break

            self.scan_results = empty_scan_results(started=True)
            self.scan_results["link_speed"] = link_speed()
            self.cdp_poe = ""    # forget the last cable's PoE
            self.speed_curves = {"down": [], "up": []}
            logger.info("cable connected, scan started (clock synced: %s)", self.scan_results["clock_synced"])

            # 2. find the switch (up to SWITCH_DISCOVERY_TIMEOUT)
            start = f"{SWITCH_DISCOVERY_TIMEOUT // 60:02d}:{SWITCH_DISCOVERY_TIMEOUT % 60:02d}"
            self.root.after(0, self.update_scanner, testing, TEXT, 0, start)
            switch_found = self.discover_switch_topology()
            if not self.is_cable_connected():
                self.finish_scan(False)
                continue
            if not switch_found:
                logger.warning("no CDP/LLDP announcement heard within %d seconds", SWITCH_DISCOVERY_TIMEOUT)

            # PoE from LLDP (lldpd) or our own CDP listener
            if self.cdp_poe != "" and self.scan_results["switch_poe"] in ("Not advertised", "Unknown (no CDP/LLDP)"):
                self.scan_results["switch_poe"] = self.cdp_poe

            # 3. DNS lookup out the test port + 3s of sniffing
            self.root.after(0, self.update_scanner, testing, TEXT, 1, "00:03")
            self.sniffing = True
            timer_thread = threading.Thread(target=self.countdown_timer, args=(3,))
            timer_thread.start()

            try:
                run_dns_test(self.scan_results)
            except Exception:
                logger.exception("DNS test failed")
                self.scan_results["dns_time"] = "Failed"
            finally:
                self.sniffing = False
            timer_thread.join()
            if not self.is_cable_connected():
                self.finish_scan(False)
                continue

            # 3b. ~4s of pings for latency, jitter and loss
            self.root.after(0, self.update_scanner, testing, TEXT, 2, "")
            run_ping_test(self.scan_results, on_line=lambda line: self.root.after(0, self.add_ping_line, line))
            if not self.is_cable_connected():
                self.finish_scan(False)
                continue

            # 4. speed test with the loading bar
            self.root.after(0, self.update_scanner, testing, TEXT, 3, "")
            self.root.after(0, self.start_loading_animation, "Connecting to closest server...")
            self.speed_curves = run_bandwidth_test(
                self.scan_results, INTERFACE,
                on_status=lambda text: self.root.after(0, self.set_loading_message, text)
            )

            # 5. show results
            if not self.is_cable_connected():
                self.finish_scan(False)
                continue

            self.finish_scan(True)
            self.root.after(0, self.update_scanner, "DONE", GOOD, 4, "")
            self.root.after(0, self.show_results_ui)

            # wait for the cable to come out, or for Rescan
            self.rescan_requested = False
            while self.is_cable_connected() and self.running and not self.rescan_requested:
                time.sleep(0.5)
            logger.info("rescan pressed" if self.rescan_requested else "cable removed")
