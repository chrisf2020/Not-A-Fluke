# entry point. run it through the notafluke launcher (handles sudo and the screen)

import os
import sys
import argparse

from .config import INTERFACE, WIFI_INTERFACE, logger
from .history import setup_logging


def main():
    parser = argparse.ArgumentParser(prog="fluke", description="Not-A-Fluke touchscreen network tester")
    parser.add_argument("--windowed", action="store_true", help="480x320 window instead of fullscreen (VM / desktop)")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("[!] Needs root for raw packet sniffing. Start it with:  notafluke", file=sys.stderr)
        sys.exit(1)

    # import tkinter late so a missing screen gives a clear message
    # instead of the long "no $DISPLAY" traceback
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as err:
        print(f"[!] No screen to draw on ({err}).", file=sys.stderr)
        print("    Start it with:  notafluke   - it opens the small screen on the Pi, or a window", file=sys.stderr)
        print("    when run from a VM / desktop terminal (not plain SSH).", file=sys.stderr)
        sys.exit(1)

    setup_logging()
    logger.info("fluke-gui started (test port %s, wifi %s)", INTERFACE, WIFI_INTERFACE)

    from .app import FlukeApp
    FlukeApp(root, windowed=args.windowed)
    root.mainloop()


if __name__ == "__main__":
    main()
