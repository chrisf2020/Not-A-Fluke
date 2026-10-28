# shared settings: version, network ports, log paths, colours

import os
import logging

VERSION = "0.7"


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

LOG_DIR = "/var/log/fluke-gui"
SCAN_LOG_PATH = os.path.join(LOG_DIR, "scans.jsonl")
EVENT_LOG_PATH = os.path.join(LOG_DIR, "events.log")
MAX_SCAN_RECORDS = 500
HISTORY_PAGE_SIZE = 6

# dark theme colours
BG = "#121212"
CARD_BG = "#1A1A1A"
BAR_BG = "#2B2B2B"
ACCENT = "#00E5FF"
TEXT = "#FFFFFF"
MUTED = "#8A8A8A"
# status colours, only used for good / so-so / bad
GOOD = "#76FF03"
OK = "#FFD700"
BAD = "#FF5252"

logger = logging.getLogger("fluke-gui")
