# shared settings: version, network ports, log paths, colours

import os
import logging

VERSION = "1.0"


# VMs name ports enp0s3 etc instead of eth0/wlan0, so fall back to the first real one.
# NOTAFLUKE_ETH / NOTAFLUKE_WIFI override it
def find_interface(env_name, preferred, wireless, net_dir="/sys/class/net"):
    override = os.environ.get(env_name, "").strip()
    if override != "":
        return override
    if os.path.exists(os.path.join(net_dir, preferred)):
        return preferred
    try:
        names = sorted(os.listdir(net_dir))
    except OSError:
        return preferred
    for name in names:
        path = os.path.join(net_dir, name)
        is_wireless = os.path.exists(os.path.join(path, "wireless"))
        is_hardware = os.path.exists(os.path.join(path, "device"))  # skips lo, docker0, bridges
        if is_hardware and is_wireless == wireless:
            return name
    return preferred


INTERFACE = find_interface("NOTAFLUKE_ETH", "eth0", wireless=False)        # test port
WIFI_INTERFACE = find_interface("NOTAFLUKE_WIFI", "wlan0", wireless=True)  # Pi's own wifi
CARRIER_PATH = f"/sys/class/net/{INTERFACE}/carrier"

# 35s covers LLDP's 30s cycle. CDP is every ~60s so a new link might need a replug
SWITCH_DISCOVERY_TIMEOUT = 35
# managed switches send STP every 2s (and CDP/LLDP). If none of that shows up in this many
# seconds it's a router / unmanaged switch (like a GL.iNet), so don't wait the full 35s
SWITCH_QUICK_CHECK = 6

# how much bigger than the 3.5" screen (480x320) everything is drawn, main.py sets it
SCALE = 1.0


# a size in pixels made for the small screen -> the same size on this screen
def px(size):
    return int(round(size * SCALE))


LOG_DIR = "/var/log/fluke-gui"
SCAN_LOG_PATH = os.path.join(LOG_DIR, "scans.jsonl")
WIFI_LOG_PATH = os.path.join(LOG_DIR, "wifi_tests.jsonl")      # Wi-Fi speed tests, for Speed history
EVENT_LOG_PATH = os.path.join(LOG_DIR, "events.log")
HISTORY_PAGE_SIZE = 6
HISTORY_PAGES = 5
# History keeps 5 pages of scans, older ones get deleted
MAX_SCAN_RECORDS = HISTORY_PAGE_SIZE * HISTORY_PAGES

# light or dark. The sun/moon button at the top saves it in this file, so it stays after a reboot.
# NOTAFLUKE_THEME=light (or dark) overrides it
THEME_PATH = os.path.join(LOG_DIR, "theme")


def read_theme():
    theme = os.environ.get("NOTAFLUKE_THEME", "").strip()
    if theme == "":
        try:
            with open(THEME_PATH) as f:
                theme = f.read().strip()
        except OSError:
            theme = "dark"
    if theme != "light":
        theme = "dark"
    return theme


THEME = read_theme()

# every colour for one theme, by name. The app can switch theme while it runs (see app.py)
def theme_colors(theme):
    if theme == "light":
        return {
            "THEME": "light",
            "BG": "#F2F2F2",
            "CARD_BG": "#FFFFFF",
            "BAR_BG": "#DCDCDC",
            "ACCENT": "#0077B6",
            "TEXT": "#111111",
            "MUTED": "#666666",
            "SOFT": "#333333",        # second-level text, like the IP and units
            "LINE": "#D0D0D0",        # graph grid lines
            "PRESSED": "#BBBBBB",     # button while it's held down
            "ON_ACCENT": "#FFFFFF",   # text on a highlighted button
            "GOOD": "#2E8B12",
            "OK": "#B8860B",
            "BAD": "#D32F2F",
            # one colour per access point on the channel map, darker ones so they show on white
            "CHANNEL_COLORS": ["#E65100", "#0077B6", "#D32F2F", "#7B1FA2", "#2E7D32", "#C2185B", "#00838F", "#5D4037"],
        }
    return {
        "THEME": "dark",
        "BG": "#121212",
        "CARD_BG": "#1A1A1A",
        "BAR_BG": "#2B2B2B",
        "ACCENT": "#00E5FF",
        "TEXT": "#FFFFFF",
        "MUTED": "#8A8A8A",
        "SOFT": "#BBBBBB",
        "LINE": "#333333",
        "PRESSED": "#444444",
        "ON_ACCENT": "#000000",
        # status colours, only used for good / so-so / bad
        "GOOD": "#76FF03",
        "OK": "#FFD700",
        "BAD": "#FF5252",
        "CHANNEL_COLORS": ["#FFEB3B", "#00E5FF", "#FF5252", "#B15CFF", "#76FF03", "#FF9A2E", "#4DA3FF", "#FF80AB"],
    }


globals().update(theme_colors(THEME))   # BG, TEXT, GOOD ... for the saved theme

logger = logging.getLogger("fluke-gui")
