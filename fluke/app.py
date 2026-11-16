# main window: top bar, tab bar (Cable / Wi-Fi / History), the cable test screens and the scan loop
# history and wifi screens come from history.py and wifi.py as mixins

import os
import re
import sys
import time
import threading
import json
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from .config import (VERSION, INTERFACE, WIFI_INTERFACE, CARRIER_PATH, SWITCH_DISCOVERY_TIMEOUT, SWITCH_QUICK_CHECK,
                     THEME, THEME_PATH, LOG_DIR, BG, CARD_BG, BAR_BG, ACCENT, TEXT, MUTED, SOFT,
                     PRESSED, ON_ACCENT, GOOD, OK, BAD, px, logger)
from .history import HistoryScreens, save_scan_record, now_stamp, is_clock_synced, format_stamp
from .wifi import WifiScreens
from .rotate import hdmi_output, read_orientation, save_orientation, apply_orientation, screen_size, set_scale
from . import config, graph, wifi, history
from .graph import MetricsGraph
from .speed import get_ip, link_speed, read_number, run_ping_test, run_bandwidth_test
from .switch import query_lldpd, parse_switch_topology, start_cdp_listener, start_switch_frame_listener, stop_listener

# same size as the Pi's TFT so a VM window looks like the real screen
WINDOW_SIZE = "480x320"

# the steps shown in the checklist while a scan runs
SCAN_STEPS = ["Switch", "DNS", "Ping", "Speed"]

# which tab each screen belongs to
VIEW_TAB = {"scanner": "cable", "results": "cable", "cable_speed": "cable", "wifi": "wifi", "ap": "wifi",
            "wifi_speed": "wifi", "channels": "wifi", "history": "history", "detail": "history",
            "share_cable": "cable", "share_history": "history", "speed_history": "history"}


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


LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo.png")


# the Not-A-Fluke logo, `height` pixels tall (None if the picture can't be loaded)
def logo_image(height):
    try:
        from PIL import Image, ImageTk
        picture = Image.open(LOGO_PATH)
        width = round(picture.width * height / picture.height)
        return ImageTk.PhotoImage(picture.resize((width, height), Image.LANCZOS))
    except Exception:
        logger.exception("could not load the logo")
        return None

# the finished test is kept here for a moment when the app restarts itself (turn / theme),
# so it comes back up instead of testing again
STATE_PATH = "/tmp/notafluke-state.json"
# where the ✕ button said to go: "start" or "terminal" (read by the notafluke launcher)
EXIT_PATH = "/tmp/notafluke-exit"


def load_state():
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
        os.remove(STATE_PATH)
    except (OSError, ValueError):
        return None
    if time.time() - state.get("saved", 0) > 60:     # old, from some other time
        return None
    return state


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


# switch every module to the other theme's colours while the app runs (they all did
# "from .config import BG, ..." so each one gets the new values)
def use_theme(theme):
    colors = config.theme_colors(theme)
    for module in (config, sys.modules[__name__], graph, wifi, history):
        for name, value in colors.items():
            if hasattr(module, name):
                setattr(module, name, value)
    graph.RATING_COLORS = {"Great": "#4DA3FF", "Good": colors["GOOD"], "Average": colors["OK"],
                           "Poor": colors["BAD"], "N/A": colors["MUTED"]}
    wifi.SIGNAL_COLORS = [colors["BAD"], "#FFA500", colors["OK"], colors["GOOD"]]


class FlukeApp(HistoryScreens, WifiScreens):
    # windowed = 480x320 window for VM/desktop, start_workers=False is for tests
    def __init__(self, root, windowed=False, start_workers=True):
        self.root = root
        self.windowed = windowed
        self.root.title("Not-A-Fluke")
        if windowed:
            self.root.geometry(WINDOW_SIZE)
        else:
            self.root.attributes("-fullscreen", True)
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

        # what the scanner screen last showed, so it can be put back after a rebuild
        self.scanner_state = None
        self.loading_message = None
        self.restart_box = None
        self.hdmi = hdmi_output()
        self.style = ttk.Style()
        self.style.theme_use('default')

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
        self.root.bind_all("<ButtonRelease-1>", self.drag_end, add="+")
        self.finger_down = False

        self.build_ui()

        # back from a restart: same tab, and the finished test instead of a new one
        self.restored = load_state()
        if self.restored is not None:
            self.root.after(100, self.restore_screen, self.restored)

        self.running = True
        self.sniffing = False
        # listen for CDP the whole time for PoE info (see switch.py)
        self.cdp_poe = ""
        if start_workers:
            start_cdp_listener(self.set_cdp_poe)
            self.worker_thread = threading.Thread(target=self.main_workflow, daemon=True)
            self.worker_thread.start()
            self.refresh_ip()

    # every widget on the screen. Run again (after clearing the screen) when the theme or the
    # orientation changes, the test keeps going underneath
    def build_ui(self):
        root = self.root
        root.configure(bg=BG)
        self.style.configure("Custom.Horizontal.TProgressbar", troughcolor=CARD_BG, background=ACCENT, thickness=px(10))

        # standing up = taller than wide, the logo gets bigger then
        width, height = (480, 320) if self.windowed else screen_size(root)
        self.portrait = height > width

        # logo on the left, then the cable and Wi-Fi addresses, buttons on the right
        top_bar = tk.Frame(root, bg=BAR_BG)
        top_bar.pack(side=tk.TOP, fill=tk.X, ipady=px(2))
        self.top_logo = logo_image(px(54) if self.portrait else px(36))
        if self.top_logo is not None:
            logo = tk.Label(top_bar, image=self.top_logo, bg=BAR_BG)
            logo.pack(side=tk.LEFT, padx=px(4))
            # tap the logo to start the test over
            logo.bind("<ButtonRelease-1>", lambda event: self.ask_restart())
        else:
            tk.Label(top_bar, text="NOT-A-FLUKE", font=("Helvetica", 11, "bold"), bg=BAR_BG, fg=ACCENT).pack(side=tk.LEFT, padx=6)
        # ✕ turns into two choices in the same spot: back to the start screen, or the terminal
        exit_slot = tk.Frame(top_bar, bg=BAR_BG)
        exit_slot.pack(side=tk.RIGHT)
        self.exit_button = self.make_top_button(exit_slot, " ✕ ", BAD, self.exit_pressed)
        self.exit_choices = tk.Frame(exit_slot, bg=BAR_BG)
        for text, where in (("Terminal", "terminal"), ("Start screen", "start")):
            choice = tk.Label(self.exit_choices, text=text, font=("Helvetica", 11, "bold"), bg=BAD, fg="white",
                              padx=px(8), pady=px(4))
            choice.pack(side=tk.RIGHT, padx=px(3))
            choice.bind("<ButtonRelease-1>", lambda event, w=where: self.exit_to(w))
        self.exit_armed = False
        # sun / moon button flips light and dark mode
        self.make_top_button(top_bar, "☀" if THEME == "dark" else "☾", ACCENT, self.switch_theme)
        # turn button: flat or standing up, only on an HDMI screen
        if self.hdmi != "":
            self.make_top_button(top_bar, "⟳", ACCENT, self.switch_orientation)
        tk.Label(top_bar, text="v" + VERSION, font=("Helvetica", 9), bg=BAR_BG, fg=MUTED).pack(side=tk.RIGHT, padx=px(4))
        # both addresses, one per line, so you can see which one has an IP (and where to SSH)
        addresses = tk.Frame(top_bar, bg=BAR_BG)
        addresses.pack(side=tk.LEFT, padx=px(6))
        self.lan_lbl = tk.Label(addresses, text="", font=("Helvetica", 10, "bold"), bg=BAR_BG, fg=SOFT, anchor="w")
        self.lan_lbl.pack(fill=tk.X)
        self.wifi_ip_lbl = tk.Label(addresses, text="", font=("Helvetica", 10, "bold"), bg=BAR_BG, fg=SOFT, anchor="w")
        self.wifi_ip_lbl.pack(fill=tk.X)

        # tabs along the bottom, packed before the arrows so they span the full width
        tab_bar = tk.Frame(root, bg=BAR_BG)
        tab_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.tab_buttons = {}
        for name, text, command in (("cable", "LAN", self.open_cable), ("wifi", "Wi-Fi", self.open_wifi),
                                    ("history", "History", self.open_history)):
            button = self.on_tap(tk.Button(tab_bar, text=text, font=("Helvetica", 13, "bold"),
                                           bd=0, relief=tk.FLAT, pady=px(7), highlightthickness=0), command)
            button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=1, pady=(1, 0))
            self.tab_buttons[name] = button

        # up/down arrows on the right side
        arrow_strip = tk.Frame(root, bg=BG, width=px(40))
        arrow_strip.pack(side=tk.RIGHT, fill=tk.Y)
        arrow_strip.pack_propagate(False)
        self.make_arrow(arrow_strip, "▲", -1)
        self.make_arrow(arrow_strip, "▼", 1)

        self.container = tk.Frame(root, bg=BG)
        self.container.pack(expand=True, fill=tk.BOTH)

        self.build_scanner_ui()
        self.build_results_ui()
        self.build_history_ui()
        self.build_speed_history_ui()
        self.build_detail_ui()
        self.build_wifi_ui()
        self.build_ap_ui()
        self.build_wifi_speed_ui()
        self.build_wifi_channels_ui()
        self.build_share_ui()
        self.frames = [self.scanner_frame, self.results_frame, self.cable_speed_frame, self.history_frame,
                       self.detail_frame, self.wifi_frame, self.ap_frame, self.wifi_speed_frame,
                       self.channels_frame, self.share_frame, self.speed_history_frame]
        self.scroll_canvases = {"results": self.scroll_canvas, "cable_speed": self.cable_speed_canvas,
                                "detail": self.detail_canvas,
                                "wifi": self.wifi_canvas, "ap": self.ap_canvas,
                                "wifi_speed": self.wifi_speed_canvas,
                                "speed_history": self.speed_history_canvas}
        self.show_scanner_ui()

    # refresh the top bar addresses every 5s in case the cable goes in later
    def refresh_ip(self):
        if not self.lan_lbl.winfo_exists():      # the screen is being rebuilt right now
            self.root.after(500, self.refresh_ip)
            return
        lan_ip = get_ip(INTERFACE)
        if lan_ip != "":
            self.lan_lbl.config(text=f"LAN  {lan_ip}")
        elif self.is_cable_connected():
            self.lan_lbl.config(text="LAN  no IP (no DHCP)")
        else:
            self.lan_lbl.config(text="LAN  unplugged")
        wifi_ip = get_ip(WIFI_INTERFACE)
        self.wifi_ip_lbl.config(text=f"Wi-Fi  {wifi_ip}" if wifi_ip else "Wi-Fi  off")
        self.lan_has_ip = lan_ip != ""
        self.wifi_has_ip = wifi_ip != ""
        self.color_addresses()
        self.root.after(5000, self.refresh_ip)

    # the address for the tab you're on is lit up: green with an IP, red without one.
    # The other one (and both on History) stays grey
    def color_addresses(self):
        tab = self.current_tab()
        for label, has_ip, its_tab in ((self.lan_lbl, getattr(self, "lan_has_ip", False), "cable"),
                                       (self.wifi_ip_lbl, getattr(self, "wifi_has_ip", False), "wifi")):
            if tab == its_tab:
                label.config(fg=GOOD if has_ip else BAD)
            else:
                label.config(fg=MUTED)

    # --- UI BUILDERS ---

    # a button in the top bar (exit, light/dark, turn), big enough to hit with a finger
    def make_top_button(self, top_bar, text, color, command):
        button = tk.Label(top_bar, text=text, font=("DejaVu Sans", 15, "bold"), bg=BAR_BG, fg=color, padx=px(6))
        button.pack(side=tk.RIGHT, padx=px(2))
        # goes darker while pressed so you can see the tap landed
        button.bind("<ButtonPress-1>", lambda event: button.config(bg=PRESSED))
        button.bind("<ButtonRelease-1>", lambda event: (button.config(bg=BAR_BG), command()))
        return button

    # runs command when a finger lifts off the widget, even if it slid off a bit (a normal
    # button needs the lift inside it, which a finger often misses). Not after a scroll drag
    def on_tap(self, widget, command):
        widget.bind("<ButtonRelease-1>", lambda event: None if self.drag_moved else command())
        return widget

    # big dark touch button, caller packs it
    def make_button(self, parent, text, command, size=14):
        return self.on_tap(tk.Button(
            parent, text=text, font=("Helvetica", size, "bold"),
            bg=BAR_BG, fg=TEXT, activebackground=PRESSED, activeforeground=TEXT,
            padx=px(12), pady=px(6) if size >= 14 else px(4)
        ), command)

    # title strip for the screens you open from a list, with a Back button
    def make_header(self, parent, text, back=None):
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill=tk.X, pady=(px(4), 0))
        if back is not None:
            back_button = self.make_button(bar, "‹ Back", back, size=12)
            back_button.config(padx=px(16), pady=px(6))    # big, it's pressed a lot
            back_button.pack(side=tk.LEFT, padx=px(6))
        tk.Label(bar, text=text, font=("Helvetica", 13, "bold"), bg=BG, fg=ACCENT).pack(side=tk.LEFT, padx=px(6))
        return bar

    # two or three buttons side by side, the active one highlighted (Wi-Fi: Networks | Speed)
    def make_switcher(self, parent, options, active):
        bar = tk.Frame(parent, bg=BG)
        bar.pack(fill=tk.X, padx=px(6), pady=(px(4), px(2)))
        for text, command in options:
            on = text == active
            self.on_tap(tk.Button(bar, text=text, font=("Helvetica", 11, "bold"), relief=tk.FLAT, bd=0,
                                  bg=ACCENT if on else BAR_BG, fg=ON_ACCENT if on else TEXT, pady=px(6),
                                  activebackground=ACCENT, highlightthickness=0),
                        command).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=1)
        return bar

    # big arrow button on the right strip, hold it to keep scrolling
    def make_arrow(self, parent, symbol, direction):
        arrow = tk.Label(parent, text=symbol, font=("DejaVu Sans", 16, "bold"), bg=BAR_BG, fg=ACCENT, relief=tk.RAISED, bd=1)
        arrow.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=px(2), pady=px(2))
        arrow.bind("<ButtonPress-1>", lambda event: self.arrow_pressed(arrow, direction))
        arrow.bind("<ButtonRelease-1>", lambda event: self.arrow_released(arrow))

    # scrollable box, returns (canvas, inner frame) to put widgets in
    def make_scroll_area(self, parent):
        # yscrollincrement = a small step in pixels, so scrolling is smooth
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0, yscrollincrement=1)
        canvas.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        inner = tk.Frame(canvas, bg=BG)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfig(window, width=event.width))
        return canvas, inner

    # a card with a small title, rows go in with make_row
    def make_card(self, parent, title, color=ACCENT):
        card = tk.Frame(parent, bg=CARD_BG)
        card.pack(fill=tk.X, padx=px(8), pady=px(5))
        tk.Label(card, text=title, font=("Helvetica", 12, "bold"), bg=CARD_BG, fg=color, anchor="w").pack(fill=tk.X, padx=px(10), pady=(px(6), px(2)))
        return card

    # one "label   value" line in a card, returns the value label to fill in later
    def make_row(self, card, label, value="--"):
        row = tk.Frame(card, bg=CARD_BG)
        row.pack(fill=tk.X, padx=px(10), pady=0 if self.portrait else px(1))
        tk.Label(row, text=label, width=11, anchor="w", font=("Helvetica", 11), bg=CARD_BG, fg=MUTED).pack(side=tk.LEFT)
        value_lbl = tk.Label(row, text=value, anchor="w", justify=tk.LEFT, wraplength=px(260),
                             font=("Helvetica", 11, "bold"), bg=CARD_BG, fg=TEXT)
        value_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        return value_lbl

    def build_scanner_ui(self):
        self.scanner_frame = tk.Frame(self.container, bg=BG)

        self.status_label = tk.Label(self.scanner_frame, text="WAITING FOR CABLE...", font=("Helvetica", 18, "bold"),
                                     bg=BAR_BG, fg=OK, pady=px(8))
        self.status_label.pack(fill=tk.X)

        # ✓ Switch  ● DNS  ○ Ping  ○ Speed
        steps = tk.Frame(self.scanner_frame, bg=BG)
        steps.pack(pady=(px(10), 0))
        self.step_labels = []
        for name in SCAN_STEPS:
            label = tk.Label(steps, text="○ " + name, font=("DejaVu Sans", 12, "bold"), bg=BG, fg=MUTED)
            label.pack(side=tk.LEFT, padx=px(8))
            self.step_labels.append(label)

        # the logo in the middle while waiting for a cable
        self.big_logo = logo_image(px(260) if self.portrait else px(165))
        self.logo_label = tk.Label(self.scanner_frame, image=self.big_logo, bg=BG)

        self.timer_label = tk.Label(self.scanner_frame, text="--:--", font=("Helvetica", 40, "bold"), bg=BG, fg=TEXT)
        self.timer_label.pack(pady=px(2))

        # loading bar for the speed test
        self.loading_frame = tk.Frame(self.scanner_frame, bg=BG)
        self.loading_text = tk.Label(self.loading_frame, text="", font=("Helvetica", 13), bg=BG, fg=ACCENT)
        self.loading_text.pack(pady=px(5))
        self.progress_bar = ttk.Progressbar(self.loading_frame, style="Custom.Horizontal.TProgressbar",
                                            mode="indeterminate", length=px(320))
        self.progress_bar.pack(pady=px(4))

        # what ping prints while the ping step runs, last few lines
        self.ping_lines = []
        # anchor center = the block of lines sits in the middle, justify left = lines still line up
        self.ping_log = tk.Label(self.scanner_frame, text="", font=("DejaVu Sans Mono", 12), bg=BG, fg=MUTED,
                                 justify=tk.LEFT, anchor="center")
        # fills the space under the step row, so the lines sit in the middle of it
        self.ping_log.pack(side=tk.BOTTOM, fill=tk.BOTH, expand=True, padx=px(10), pady=px(6))

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
        graph_card = self.make_card(speed_inner, "LAN speed")
        self.cable_graph = MetricsGraph(graph_card, INTERFACE)
        self.cable_graph.canvas.pack(fill=tk.X, padx=px(4), pady=px(4))

        # pass / fail at a glance
        summary = tk.Frame(self.scroll_content, bg=BG)
        summary.pack(fill=tk.X, padx=px(8), pady=(px(8), px(2)))
        self.summary_labels = []
        for k in range(4):
            label = tk.Label(summary, text="", font=("DejaVu Sans", 11, "bold"), bg=CARD_BG, fg=MUTED, pady=px(4))
            label.grid(row=0, column=k, sticky="ew", padx=px(2))
            summary.columnconfigure(k, weight=1)
            self.summary_labels.append(label)

        when = tk.Frame(self.scroll_content, bg=BG)
        when.pack(fill=tk.X, padx=px(8), pady=px(2))
        # buttons are packed first so they keep their size, the text gets whatever is left
        self.rescan_button = self.make_button(when, "↻ Rescan", self.ask_restart, size=11)
        self.rescan_button.pack(side=tk.RIGHT)
        # send this scan to a phone as a PDF (share.py). Standing up it's at the bottom instead
        if not self.portrait:
            self.make_button(when, "QR", lambda: self.open_share(self.scan_results, "results"),
                             size=11).pack(side=tk.RIGHT, padx=px(4))
        self.lbl_tested = tk.Label(when, text="Tested: --", font=("Helvetica", 10), bg=BG, fg=MUTED)
        self.lbl_tested.pack(side=tk.LEFT)

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
        tk.Frame(switch, bg=CARD_BG, height=px(6)).pack()

        dns = self.make_card(self.scroll_content, "DNS")
        self.res_dns_server = self.make_row(dns, "Server")
        self.res_dns_time = self.make_row(dns, "Answer")
        self.res_dns_seen = self.make_row(dns, "Lookups")
        tk.Frame(dns, bg=CARD_BG, height=px(6)).pack()

        # just the numbers here, the graph is on the Speed view
        speed = self.make_card(self.scroll_content, "Speed")
        self.res_down = self.make_row(speed, "Download")
        self.res_up = self.make_row(speed, "Upload")
        self.res_latency = self.make_row(speed, "Ping")
        self.res_jitter = self.make_row(speed, "Jitter")
        self.res_loss = self.make_row(speed, "Packet loss")
        self.res_rating = self.make_row(speed, "Rating")
        self.res_server = self.make_row(speed, "Server")
        tk.Frame(speed, bg=CARD_BG, height=px(6)).pack()

        # standing up there's room for the QR right here
        self.results_qr = None
        if self.portrait:
            card = tk.Frame(self.scroll_content, bg=CARD_BG)
            card.pack(fill=tk.X, padx=px(8), pady=px(5))
            self.results_qr = tk.Canvas(card, width=px(150), height=px(150), bg="white", highlightthickness=0)
            self.results_qr.pack(pady=px(8))
            self.results_qr_lbl = tk.Label(card, text="", font=("Helvetica", 10), bg=CARD_BG, fg=MUTED)
            self.results_qr_lbl.pack()


    # --- EVENT HANDLERS ---

    def close_window(self, event):
        self.root.destroy()

    # tap ✕: it shows "Start screen" and "Terminal" for a few seconds, tap one to close the app
    def exit_pressed(self):
        self.exit_armed = True
        self.exit_button.pack_forget()
        self.exit_choices.pack(side=tk.RIGHT)
        self.root.after(4000, self.disarm_exit)

    # where = "start" (the tap-to-start screen) or "terminal" (the command line, nothing on top).
    # The notafluke launcher reads the file after the app closes
    def exit_to(self, where):
        logger.info("exit to %s", where)
        try:
            with open(EXIT_PATH, "w") as f:
                f.write(where)
        except OSError as err:
            logger.warning("couldn't save where to exit to: %s", err)
        self.root.destroy()

    # save the other theme and restart the app so every screen picks up the new colours
    # (a scan that was running starts over, finished ones are still in History)
    def switch_theme(self):
        new_theme = "dark" if THEME == "light" else "light"
        logger.info("theme switched to %s", new_theme)
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(THEME_PATH, "w") as f:
                f.write(new_theme)
        except OSError as err:
            logger.warning("couldn't save the theme: %s", err)
        os.environ["NOTAFLUKE_THEME"] = new_theme
        use_theme(new_theme)
        self.root.after(10, self.rebuild_ui)     # after this tap is done, its button goes away

    # everything about what's on the screen, to put it back after a rebuild or restart
    def screen_state(self):
        return {"saved": time.time(), "tab": self.current_tab(), "view": self.current_view,
                "wifi_aps": self.wifi_aps, "wifi_stamp": getattr(self, "wifi_scan_stamp", ""),
                "band": self.channel_band, "zoom": self.channel_graph.zoom,
                "middle": self.channel_graph.middle, "history_page": self.history_page,
                "detail": getattr(self, "detail_record", None),
                "wifi_speed": getattr(self, "wifi_speed_last", None)}

    # clear the screen and build it again (new colours or new shape), then put back exactly
    # what was showing. The test thread keeps running the whole time
    def rebuild_ui(self):
        state = self.screen_state()
        cable_view = self.cable_view
        ping_lines = list(self.ping_lines)
        loading = self.loading_message
        scanning, speed_running = self.wifi_scanning, self.wifi_speed_running
        for child in self.root.winfo_children():
            child.destroy()
        self.restart_box = None
        self.build_ui()

        if self.scanner_state is not None:
            self.update_scanner(*self.scanner_state)
        if loading is not None:
            self.start_loading_animation(loading)
        self.ping_lines = ping_lines
        self.ping_log.config(text="\n".join(ping_lines))
        if cable_view == "results":
            self.show_results_ui()
        self.restore_screen(state)
        if state["view"] == "cable_speed":
            self.show_cable_speed()
        self.wifi_scanning = scanning
        if scanning:
            self.wifi_scan_button.config(text="Scanning...")
            self.channels_scan_button.config(text="Scanning...")
        self.wifi_speed_running = speed_running
        if speed_running:
            self.wifi_speed_button.config(text="Testing...")

    # start the app again. keep_test = remember the finished test so it isn't run again
    def restart_app(self, keep_test):
        if keep_test:
            state = self.screen_state()
            if self.cable_view == "results":
                state["results"] = self.scan_results
                state["curves"] = self.speed_curves
            try:
                with open(STATE_PATH, "w") as f:
                    json.dump(state, f)
            except OSError as err:
                logger.warning("couldn't save the test before restarting: %s", err)
        self.running = False
        command = [sys.executable, "-m", "fluke"]
        if self.windowed:
            command.append("--windowed")
        os.execv(sys.executable, command)

    # back from a turn / theme restart: open the same screen with the same things on it
    # (the cable results come back from main_workflow)
    def restore_screen(self, state):
        view = state.get("view", "scanner")
        if state.get("wifi_aps"):
            self.wifi_scan_stamp = state.get("wifi_stamp", "")
            self.channel_band = state.get("band", "2.4")
            self.channels_band_button.config(text=f"{self.channel_band} GHz")
            self.show_wifi_results(state["wifi_aps"], state.get("wifi_stamp"))
            self.channel_graph.zoom = state.get("zoom", 1)
            self.channel_graph.middle = state.get("middle")
            self.channel_graph.draw()
        if state.get("wifi_speed"):
            self.wifi_speed_done = True
            self.finish_wifi_speed_test(*state["wifi_speed"])
        if state.get("wifi_speed") and self.wifi_speed_status_lbl.cget("text") == "":
            self.wifi_speed_status_lbl.config(text="Last Wi-Fi test", fg=MUTED)
        if view in ("wifi", "ap"):
            self.show_wifi_frame()
        elif view == "channels":
            self.show_frame(self.channels_frame, "channels")
        elif view == "wifi_speed":
            self.wifi_speed_done = True
            self.show_frame(self.wifi_speed_frame, "wifi_speed")
        elif view == "speed_history":
            self.open_speed_history()
        elif view in ("history", "detail", "share_history"):
            self.open_history()
            self.history_page = min(state.get("history_page", 0), max(0, (len(self.history_records) - 1) // self.history_page_size))
            self.render_history_page()
            if view != "history" and state.get("detail"):
                self.show_detail_ui(state["detail"])
        self.restored_view = view

    # the logo was tapped: ask before starting the test over
    def ask_restart(self):
        if getattr(self, "restart_box", None) is not None:
            return
        box = tk.Frame(self.root, bg=CARD_BG, highlightthickness=px(2), highlightbackground=ACCENT)
        tk.Label(box, text="Restart the test?", font=("Helvetica", 15, "bold"), bg=CARD_BG,
                 fg=TEXT).pack(padx=px(20), pady=(px(14), px(10)))
        buttons = tk.Frame(box, bg=CARD_BG)
        buttons.pack(pady=(0, px(14)))
        self.make_button(buttons, "Restart", self.restart_test).pack(side=tk.LEFT, padx=px(8))
        self.make_button(buttons, "Cancel", self.close_restart_box).pack(side=tk.LEFT, padx=px(8))
        box.place(relx=0.5, rely=0.5, anchor="center")
        self.restart_box = box

    def close_restart_box(self):
        if self.restart_box is not None:
            self.restart_box.destroy()
            self.restart_box = None

    def restart_test(self):
        self.close_restart_box()
        if self.cable_view == "results":
            self.rescan_pressed()             # results are up: same as Rescan
        else:
            self.restart_app(keep_test=False)  # mid-test: start everything over

    # flat <-> standing up, then restart the app so it fits the new shape
    def switch_orientation(self):
        new = "normal" if read_orientation() == "right" else "right"
        logger.info("orientation switched to %s", new)
        try:
            save_orientation(new)
        except OSError as err:
            logger.warning("couldn't save the orientation: %s", err)
        # turn the screen, then build it again in the new shape (the test keeps going)
        apply_orientation(self.hdmi)
        self.root.after(200, self.fit_to_screen)

    def fit_to_screen(self):
        width, height = screen_size(self.root)
        set_scale(self.root, width, height)
        self.root.attributes("-fullscreen", False)
        self.root.attributes("-fullscreen", True)
        self.rebuild_ui()

    def disarm_exit(self):
        if not self.exit_choices.winfo_exists():
            return
        self.exit_armed = False
        self.exit_choices.pack_forget()
        self.exit_button.pack(side=tk.RIGHT, padx=px(2))

    # --- SEND TO PHONE (QR code -> PDF, see share.py) ---

    def build_share_ui(self):
        self.share_frame = tk.Frame(self.container, bg=BG)
        self.make_header(self.share_frame, "Send to phone", back=self.close_share)
        self.share_body = tk.Frame(self.share_frame, bg=BG)
        self.share_body.pack(fill=tk.BOTH, expand=True, padx=px(8), pady=px(4))
        # QR on the left (on top when standing up), what to do next to it. Sized in show_share
        self.qr_size = px(210)
        self.qr_canvas = tk.Canvas(self.share_body, width=self.qr_size, height=self.qr_size, bg="white",
                                   highlightthickness=0)
        self.share_lbl = tk.Label(self.share_body, text="", font=("Helvetica", 11), bg=BG, fg=TEXT,
                                  justify=tk.LEFT, anchor="nw")
        self.place_share()

    # record = the scan to send, came_from = "results" or "detail" (where Back goes)
    def open_share(self, record, came_from):
        self.share_came_from = came_from
        view = "share_cable" if came_from == "results" else "share_history"
        self.qr_canvas.delete("all")
        self.share_lbl.config(text="Making the PDF...", fg=TEXT)
        self.show_frame(self.share_frame, view)
        # the PDF takes a second, so make it off the screen thread
        threading.Thread(target=self.make_share, args=(dict(record),), daemon=True).start()

    def make_share(self, record):
        from .share import share_record, qr_matrix
        try:
            aps, wifi_stamp = self.wifi_for_pdf()
            url, interface = share_record(record, aps, wifi_stamp)
            matrix = qr_matrix(url)
        except ImportError:
            logger.exception("QR package missing")
            self.root.after(0, self.share_failed, "The QR package is missing. Run  notafluke  once over SSH to install it.")
            return
        except Exception as err:
            logger.exception("could not share the scan as a PDF")
            self.root.after(0, self.share_failed, f"No PDF: {err}.")
            return
        self.root.after(0, self.show_share, interface, matrix)

    # the QR at the bottom of the results (standing up)
    def make_results_qr(self, record):
        from .share import share_record, qr_matrix
        try:
            aps, wifi_stamp = self.wifi_for_pdf()
            url, interface = share_record(record, aps, wifi_stamp)
            matrix = qr_matrix(url)
        except Exception as err:
            logger.exception("could not make the results QR")
            message = f"No QR: {err}"
            self.root.after(0, lambda: self.results_qr_lbl.config(text=message, fg=BAD))
            return
        self.root.after(0, self.show_results_qr, interface, matrix)

    def show_results_qr(self, interface, matrix):
        if self.results_qr is None or not self.results_qr.winfo_exists():
            return
        # as big as fits without having to scroll down to it
        self.root.update_idletasks()
        rest = self.scroll_content.winfo_reqheight() - self.results_qr.winfo_reqheight()
        space = self.scroll_canvas.winfo_height() - rest - px(8)
        size = max(px(110), min(self.scroll_canvas.winfo_width() - px(60), space))
        self.results_qr.config(width=size, height=size)
        self.results_qr_lbl.pack_forget()
        self.draw_qr(matrix, self.results_qr, size)

    # the Wi-Fi tab's last scan for the PDF's channel map, or a fresh scan if there isn't one yet
    def wifi_for_pdf(self):
        if len(self.wifi_aps) > 0:
            return self.wifi_aps, getattr(self, "wifi_scan_stamp", "")
        from .wifi import cached_wifi
        try:
            return cached_wifi(), time.strftime("%I:%M %p")
        except Exception:
            logger.exception("Wi-Fi scan for the PDF failed")
            return None, ""

    # QR as big as fits: next to the text on a wide screen, above it on a tall one
    def place_share(self):
        self.root.update_idletasks()
        width = max(self.share_body.winfo_width(), px(300))
        height = max(self.share_body.winfo_height(), px(200))
        self.qr_canvas.pack_forget()
        self.share_lbl.pack_forget()
        # just the QR, as big as fits, in the middle. The text only says "Making the PDF..." or an error
        self.qr_size = min(width, height) - px(10)
        self.qr_canvas.config(width=self.qr_size, height=self.qr_size)
        self.share_lbl.config(wraplength=width - px(8))
        self.share_lbl.pack(side=tk.BOTTOM)
        self.qr_canvas.pack(expand=True)

    def show_share(self, interface, matrix):
        self.place_share()
        self.draw_qr(matrix)
        self.share_lbl.config(text="")

    # black squares on the white canvas, as big as fit
    def draw_qr(self, matrix, canvas=None, size=None):
        canvas = canvas or self.qr_canvas
        size = size or self.qr_size
        canvas.delete("all")
        # cell size isn't rounded down, so the QR always fills the whole box (bigger squares)
        cell = size / len(matrix)
        offset = 0
        for row_number, row in enumerate(matrix):
            for column_number, black in enumerate(row):
                if black:
                    x = offset + column_number * cell
                    y = offset + row_number * cell
                    canvas.create_rectangle(x, y, x + cell, y + cell, fill="black", outline="")

    def share_failed(self, message):
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
            canvas.yview_scroll(steps * px(6), "units")      # a unit is 1 pixel

    def scroll_with_mouse_wheel(self, event):
        # delta > 0 means wheel up
        self.scroll_by(-5 if event.delta > 0 else 5)

    # scroll once, then keep going while held
    def arrow_pressed(self, arrow, direction):
        arrow.config(bg=PRESSED, relief=tk.SUNKEN)
        self.arrow_direction = direction
        if self.current_view == "history":
            self.scroll_by(direction)      # history is paged, so no repeat
            return
        self.scroll_by(direction * 4)
        self.arrow_job = self.root.after(300, self.arrow_repeat)

    def arrow_repeat(self):
        self.scroll_by(self.arrow_direction)
        self.arrow_job = self.root.after(15, self.arrow_repeat)

    def arrow_released(self, arrow):
        arrow.config(bg=BAR_BG, relief=tk.RAISED)
        if getattr(self, "arrow_job", None) is not None:
            self.root.after_cancel(self.arrow_job)
            self.arrow_job = None

    def drag_start(self, event):
        self.drag_start_y = event.y_root
        self.drag_last_y = event.y_root
        self.drag_moved = False
        self.finger_down = True

    # finger lifted: the next touch starts fresh
    def drag_end(self, event):
        self.finger_down = False

    def drag_move(self, event):
        canvas = self.scroll_canvases.get(self.current_view)
        if canvas is None:
            return
        # the touchscreen can send "moved" before "pressed" on a new touch. Start the drag from
        # here then, instead of from where the last swipe ended (that threw the page back)
        if not self.finger_down:
            self.drag_start(event)
            return
        # small wobbles still count as a tap, and so does anything on a page that can't scroll
        if not self.drag_moved:
            if canvas.yview() == (0.0, 1.0):
                return
            if abs(event.y_root - self.drag_start_y) > px(14):
                self.drag_moved = True
                self.drag_last_y = event.y_root
            return
        # move the page by however far the finger moved since last time. A huge jump in one go
        # is a new touch somewhere else, not a swipe, so it doesn't move the page
        moved = self.drag_last_y - event.y_root
        if abs(moved) < px(120):
            canvas.yview_scroll(moved, "units")
        self.drag_last_y = event.y_root

    # --- UI STATE MANAGERS ---

    # swap whatever screen is showing for `frame` and light up its tab
    def show_frame(self, frame, view):
        for other in self.frames:
            other.pack_forget()
        frame.pack(expand=True, fill=tk.BOTH)
        self.current_view = view
        self.color_addresses()
        for name, button in self.tab_buttons.items():
            on = name == VIEW_TAB[view]
            button.config(bg=ACCENT if on else BAR_BG, fg=ON_ACCENT if on else TEXT,
                          activebackground=ACCENT if on else PRESSED)

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

        tested_text = f"Tested {format_stamp(results['finished_at'], short=True)}"
        if results["clock_synced"] != "yes":
            tested_text += " (clock off)"
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
        self.res_server.config(text=results.get("speed_server", "--"))
        self.cable_graph.show(results, self.speed_curves)
        if self.results_qr is not None:
            self.results_qr.delete("all")
            self.results_qr_lbl.config(text="Making the PDF...", fg=MUTED)
            self.results_qr_lbl.pack()
            threading.Thread(target=self.make_results_qr, args=(dict(results),), daemon=True).start()

        self.cable_view = "results"
        if self.current_tab() == "cable":
            self.show_frame(self.results_frame, "results")
            self.scroll_canvas.yview_moveto(0)

    def start_loading_animation(self, message):
        self.loading_message = message
        self.timer_label.pack_forget()
        self.loading_text.config(text=message)
        self.loading_frame.pack(pady=px(15))
        self.progress_bar.start(10)

    def set_loading_message(self, message):
        self.loading_message = message
        self.loading_text.config(text=message)

    def stop_loading_animation(self):
        self.loading_message = None
        self.progress_bar.stop()
        self.loading_frame.pack_forget()

    # newest line goes at the bottom, older ones scroll off the top
    def add_ping_line(self, line):
        line = re.sub(r"^\d+ bytes from |, time \d+ms$", "", line).replace("icmp_seq=", "seq=").rstrip()
        # as many letters and lines as fit in the space
        font = tkfont.Font(font=self.ping_log.cget("font"))
        letters = max(30, (self.ping_log.winfo_width() - px(10)) // max(1, font.measure("0")))
        lines = max(4, self.ping_log.winfo_height() // max(1, font.metrics("linespace")))
        self.ping_lines = (self.ping_lines + [line[:letters]])[-lines:]
        self.ping_log.config(text="\n".join(self.ping_lines))

    # step = index in SCAN_STEPS that's running now (-1 = waiting, 4 = all done)
    def update_scanner(self, status, status_color, step, timer_text=""):
        # a new step starts with an empty list of info lines
        old_step = self.scanner_state[2] if self.scanner_state else None
        self.scanner_state = (status, status_color, step, timer_text)
        if step != old_step:
            self.ping_lines = []
            self.ping_log.config(text="")
        # what each step is doing shows as lines in the middle (like ping), no timer
        self.timer_label.pack_forget()
        # waiting for a cable: the logo
        if step == -1 and self.big_logo is not None:
            if not self.logo_label.winfo_manager():
                self.logo_label.pack(expand=True, before=self.ping_log)
        else:
            self.logo_label.pack_forget()
        self.set_steps(status, status_color, step)

    # the status line and the ✓ ● ○ steps
    def set_steps(self, status, status_color, step):
        self.status_label.config(text=status, fg=status_color)
        for k, label in enumerate(self.step_labels):
            if k < step:
                label.config(text="✓ " + SCAN_STEPS[k], fg=GOOD)
            elif k == step:
                label.config(text="● " + SCAN_STEPS[k], fg=ACCENT)
            else:
                label.config(text="○ " + SCAN_STEPS[k], fg=MUTED)

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

    # one line under the steps, from the test thread
    def info(self, line):
        self.root.after(0, self.add_ping_line, line)

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
            # the app just restarted itself (turn / theme) after a test: show that test again
            kept = self.restored.get("results") if self.restored else None
            self.restored = None
            if kept is not None and self.is_cable_connected():
                self.scan_results = kept
                self.speed_curves = kept.get("speed_curves", {"down": [], "up": []})
                self.root.after(0, self.update_scanner, "DONE", GOOD, 4, "")
                self.root.after(0, self.show_results_ui)
                if getattr(self, "restored_view", "") == "cable_speed":
                    self.root.after(50, self.show_cable_speed)
                self.wait_after_results()
                continue

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
            self.root.after(0, self.update_scanner, testing, TEXT, 0)
            self.info(f"Link up: {self.scan_results['link_speed']}")
            self.info("Listening for the switch (CDP / LLDP)...")
            switch_found = self.discover_switch_topology()
            if not self.is_cable_connected():
                self.finish_scan(False)
                continue
            # say what was found for a moment before moving on
            if switch_found:
                self.info(f"Found {self.scan_results['switch_name']}")
                self.info(f"Port {self.scan_results['switch_port']}   VLAN {self.scan_results['switch_vlan']}")
            else:
                logger.warning("no CDP/LLDP announcement heard within %d seconds", SWITCH_DISCOVERY_TIMEOUT)
                self.info("No managed switch found (router or plain switch)")
            time.sleep(2.5)

            # PoE from LLDP (lldpd) or our own CDP listener
            if self.cdp_poe != "" and self.scan_results["switch_poe"] in ("Not advertised", "Unknown (no CDP/LLDP)"):
                self.scan_results["switch_poe"] = self.cdp_poe

            # 3. DNS lookup out the test port + 3s of sniffing
            self.root.after(0, self.update_scanner, testing, TEXT, 1)
            try:
                run_dns_test(self.scan_results, on_line=self.info)
            except Exception:
                logger.exception("DNS test failed")
                self.scan_results["dns_time"] = "Failed"
            if not self.is_cable_connected():
                self.finish_scan(False)
                continue

            # 3b. ~4s of pings for latency, jitter and loss
            self.root.after(0, self.update_scanner, testing, TEXT, 2)
            run_ping_test(self.scan_results, on_line=self.info)
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
            # saved with the scan so the PDF can draw the speed graph, even from History
            self.scan_results["speed_curves"] = self.speed_curves

            # 5. show results
            if not self.is_cable_connected():
                self.finish_scan(False)
                continue

            self.finish_scan(True)
            self.root.after(0, self.update_scanner, "DONE", GOOD, 4, "")
            self.root.after(0, self.show_results_ui)
            # scan Wi-Fi now in the background, so the QR / PDF is ready right away when tapped
            self.root.after(0, self.start_wifi_scan)

            self.wait_after_results()

    # wait for the cable to come out, or for Rescan
    def wait_after_results(self):
        self.rescan_requested = False
        while self.is_cable_connected() and self.running and not self.rescan_requested:
            time.sleep(0.5)
        logger.info("rescan pressed" if self.rescan_requested else "cable removed")
        if not self.rescan_requested:
            # keep the results up (the top bar says the cable is out) until it goes back in
            while not self.is_cable_connected() and self.running:
                time.sleep(0.5)
