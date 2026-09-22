#!/usr/bin/env python3
import os
import sys
import time
import threading
import tkinter as tk
from scapy.all import sniff, load_contrib
from scapy.contrib.cdp import CDPMsgDeviceID, CDPMsgPortID
from scapy.contrib.lldp import LLDPDUChassisID, LLDPDUPortID

# Preload protocol layers
load_contrib("cdp")
load_contrib("lldp")

INTERFACE = "eth0"
CARRIER_PATH = f"/sys/class/net/{INTERFACE}/carrier"

class FlukeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PiScout Portable")
        
        # Configure full-screen for 7-inch display
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="#121212")

        # Allow pressing Escape to close the application
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        # Header / Status Banner
        self.status_label = tk.Label(
            root, text="WAITING FOR CABLE...", font=("Helvetica", 20, "bold"),
            bg="#2B2B2B", fg="#FFA500", pady=15
        )
        self.status_label.pack(fill=tk.X)

        # Main Info Display Area
        self.info_frame = tk.Frame(root, bg="#121212", pady=20)
        self.info_frame.pack(expand=True, fill=tk.BOTH)

        # Protocol Indicator
        self.proto_label = tk.Label(
            self.info_frame, text="Protocol: --", font=("Helvetica", 16),
            bg="#121212", fg="#888888"
        )
        self.proto_label.pack(pady=5)

        # Switch Name / Device ID
        self.device_title = tk.Label(
            self.info_frame, text="Switch Name / ID", font=("Helvetica", 14),
            bg="#121212", fg="#666666"
        )
        self.device_title.pack()
        self.device_label = tk.Label(
            self.info_frame, text="--", font=("Helvetica", 24, "bold"),
            bg="#121212", fg="#00E5FF", wraplength=700
        )
        self.device_label.pack(pady=10)

        # Switch Port
        self.port_title = tk.Label(
            self.info_frame, text="Switch Port", font=("Helvetica", 14),
            bg="#121212", fg="#666666"
        )
        self.port_title.pack()
        self.port_label = tk.Label(
            self.info_frame, text="--", font=("Helvetica", 24, "bold"),
            bg="#121212", fg="#76FF03"
        )
        self.port_label.pack(pady=10)

        # Footer Hint
        self.footer = tk.Label(
            root, text="Press 'Esc' on keyboard to exit", font=("Helvetica", 10),
            bg="#121212", fg="#444444", pady=5
        )
        self.footer.pack(side=tk.BOTTOM)

        # Start the background network-sniffing thread
        self.running = True
        self.worker_thread = threading.Thread(target=self.network_loop, daemon=True)
        self.worker_thread.start()

    def update_ui(self, status, status_color, proto="--", device="--", port="--"):
        self.status_label.config(text=status, fg=status_color)
        self.proto_label.config(text=f"Protocol: {proto}")
        self.device_label.config(text=device)
        self.port_label.config(text=port)

    def is_cable_connected(self):
        if not os.path.exists(CARRIER_PATH):
            return False
        try:
            with open(CARRIER_PATH, "r") as f:
                return f.read().strip() == "1"
        except OSError:
            return False

    def parse_packet(self, packet):
        found = False

        if packet.haslayer("LLDPDU"):
            chassis = packet.getlayer(LLDPDUChassisID)
            port = packet.getlayer(LLDPDUPortID)

            cid = getattr(chassis, 'macaddr', None) or getattr(chassis, 'id', 'Unknown')
            pid = getattr(port, 'portid', 'Unknown')
            if isinstance(pid, bytes):
                pid = pid.decode(errors="replace")

            self.root.after(0, self.update_ui, "SWITCH DETECTED", "#76FF03", "LLDP", str(cid), str(pid))
            found = True

        elif packet.haslayer("CDP"):
            device = packet.getlayer(CDPMsgDeviceID)
            port = packet.getlayer(CDPMsgPortID)

            dev_val = device.val.decode(errors="replace") if isinstance(device.val, bytes) else device.val
            port_val = port.iface.decode(errors="replace") if isinstance(port.iface, bytes) else port.iface

            self.root.after(0, self.update_ui, "SWITCH DETECTED", "#00E5FF", "CDP", str(dev_val), str(port_val))
            found = True

        return found

    def network_loop(self):
        while self.running:
            # 1. State: Cable Unplugged
            if not self.is_cable_connected():
                self.root.after(0, self.update_ui, "WAITING FOR CABLE...", "#FFA500")
                while not self.is_cable_connected() and self.running:
                    time.sleep(1)

            if not self.running:
                break

            # 2. State: Cable Plugged In, Sniffing
            self.root.after(0, self.update_ui, "CABLE CONNECTED - LISTENING...", "#FFFF00")
            
            sniff(
                iface=INTERFACE,
                filter="ether proto 0x88cc or ether dst 01:00:0c:cc:cc:cc",
                stop_filter=self.parse_packet,
                store=0,
                timeout=90  # Reset if switch doesn't broadcast within 90 seconds
            )

            # 3. State: Hold results until physical disconnection
            while self.is_cable_connected() and self.running:
                time.sleep(1)

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("[!] Error: You must run this script with sudo (raw socket access).")
        sys.exit(1)

    root = tk.Tk()
    app = FlukeApp(root)
    root.mainloop()
