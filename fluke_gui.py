#!/usr/bin/env python3
import os
import sys
import time
import socket
import threading
import tkinter as tk
import speedtest
from scapy.all import sniff, load_contrib
from scapy.contrib.cdp import CDPMsgDeviceID, CDPMsgPortID
from scapy.contrib.lldp import LLDPDUChassisID, LLDPDUPortID
from scapy.layers.dns import DNS, DNSQR
from scapy.layers.inet import IP, UDP, TCP

load_contrib("cdp")
load_contrib("lldp")

INTERFACE = "eth0"
CARRIER_PATH = f"/sys/class/net/{INTERFACE}/carrier"

class FlukeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PiScout Pro")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="#121212")
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        # Main Container
        self.container = tk.Frame(root, bg="#121212")
        self.container.pack(expand=True, fill=tk.BOTH)

        # Build UI States
        self.build_scanner_ui()
        self.build_results_ui()
        self.show_scanner_ui()

        # Shared Data Storage
        self.scan_results = {
            "switch_name": "--",
            "switch_port": "--",
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
        self.mode_label.pack(pady=30)

        self.timer_label = tk.Label(
            self.scanner_frame, text="--:--", font=("Helvetica", 60, "bold"),
            bg="#121212", fg="#FFFFFF"
        )
        self.timer_label.pack(pady=20)

        self.footer = tk.Label(
            self.scanner_frame, text="Press 'Esc' to exit", font=("Helvetica", 10),
            bg="#121212", fg="#444444", pady=5
        )
        self.footer.pack(side=tk.BOTTOM)

    def build_results_ui(self):
        self.results_frame = tk.Frame(self.container, bg="#121212")
        
        header = tk.Label(
            self.results_frame, text="DIAGNOSTIC COMPLETE", font=("Helvetica", 20, "bold"),
            bg="#00E5FF", fg="#000000", pady=10
        )
        header.pack(fill=tk.X)

        # Switch Section
        switch_frame = tk.Frame(self.results_frame, bg="#1A1A1A", bd=2, relief=tk.RIDGE)
        switch_frame.pack(fill=tk.X, padx=20, pady=10)
        tk.Label(switch_frame, text="Switch Topology", font=("Helvetica", 16, "bold"), bg="#1A1A1A", fg="#FFFFFF").pack(pady=5)
        
        self.res_switch_lbl = tk.Label(switch_frame, text="Name: --", font=("Helvetica", 14), bg="#1A1A1A", fg="#76FF03")
        self.res_switch_lbl.pack()
        self.res_port_lbl = tk.Label(switch_frame, text="Port: --", font=("Helvetica", 14), bg="#1A1A1A", fg="#76FF03")
        self.res_port_lbl.pack(pady=5)

        # SpeedTest Section
        speed_frame = tk.Frame(self.results_frame, bg="#1A1A1A", bd=2, relief=tk.RIDGE)
        speed_frame.pack(fill=tk.X, padx=20, pady=10)
        tk.Label(speed_frame, text="Bandwidth", font=("Helvetica", 16, "bold"), bg="#1A1A1A", fg="#FFFFFF").pack(pady=5)
        
        self.res_speed_lbl = tk.Label(speed_frame, text="Down: -- Mbps | Up: -- Mbps | Ping: -- ms", font=("Helvetica", 14), bg="#1A1A1A", fg="#FFD700")
        self.res_speed_lbl.pack(pady=5)

        # DNS Section
        dns_frame = tk.Frame(self.results_frame, bg="#1A1A1A", bd=2, relief=tk.RIDGE)
        dns_frame.pack(fill=tk.X, padx=20, pady=10)
        tk.Label(dns_frame, text="DNS Activity", font=("Helvetica", 16, "bold"), bg="#1A1A1A", fg="#FFFFFF").pack(pady=5)
        
        self.res_dns_lbl = tk.Label(dns_frame, text="Queries captured: 0 | Last: --", font=("Helvetica", 14), bg="#1A1A1A", fg="#FF00FF")
        self.res_dns_lbl.pack(pady=5)

        self.res_footer = tk.Label(
            self.results_frame, text="Unplug cable to reset scanner | Press 'Esc' to exit", font=("Helvetica", 10),
            bg="#121212", fg="#888888", pady=5
        )
        self.res_footer.pack(side=tk.BOTTOM, pady=10)

    # --- STATE MANAGERS ---

    def show_scanner_ui(self):
        self.results_frame.pack_forget()
        self.scanner_frame.pack(expand=True, fill=tk.BOTH)

    def show_results_ui(self):
        sw_text = f"Name/ID: {self.scan_results['switch_name']} ({self.scan_results['switch_proto']})"
        pt_text = f"Port: {self.scan_results['switch_port']}"
        sp_text = f"Down: {self.scan_results['speed_down']} | Up: {self.scan_results['speed_up']} | Ping: {self.scan_results['speed_ping']}"
        dn_text = f"Queries: {self.scan_results['dns_queries']} | Domain: {self.scan_results['dns_last_domain']}"

        self.res_switch_lbl.config(text=sw_text)
        self.res_port_lbl.config(text=pt_text)
        self.res_speed_lbl.config(text=sp_text)
        self.res_dns_lbl.config(text=dn_text)

        self.scanner_frame.pack_forget()
        self.results_frame.pack(expand=True, fill=tk.BOTH)

    def update_scanner(self, status, status_color, mode, timer_text):
        self.status_label.config(text=status, fg=status_color)
        self.mode_label.config(text=mode)
        self.timer_label.config(text=timer_text)

    def reset_data(self):
        self.scan_results = {
            "switch_name": "Not Found",
            "switch_port": "Not Found",
            "switch_proto": "--",
            "speed_down": "Failed",
            "speed_up": "Failed",
            "speed_ping": "Failed",
            "dns_queries": 0,
            "dns_last_domain": "None"
        }

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
        for i in range(seconds, -1, -1):
            if not self.sniffing:
                break
            self.root.after(0, self.timer_label.config, {'text': f"00:{i:02d}"})
            time.sleep(1)

    # --- PACKET HANDLERS ---

    def parse_cdp_lldp(self, packet):
        if packet.haslayer("LLDPDU"):
            chassis = packet.getlayer(LLDPDUChassisID)
            port = packet.getlayer(LLDPDUPortID)
            cid = getattr(chassis, 'macaddr', None) or getattr(chassis, 'id', 'Unknown')
            pid = getattr(port, 'portid', 'Unknown')
            if isinstance(pid, bytes):
                pid = pid.decode(errors="replace")
            
            self.scan_results["switch_name"] = str(cid)
            self.scan_results["switch_port"] = str(pid)
            self.scan_results["switch_proto"] = "LLDP"
            return True

        elif packet.haslayer("CDP"):
            device = packet.getlayer(CDPMsgDeviceID)
            port = packet.getlayer(CDPMsgPortID)
            dev_val = device.val.decode(errors="replace") if isinstance(device.val, bytes) else device.val
            port_val = port.iface.decode(errors="replace") if isinstance(port.iface, bytes) else port.iface
            
            self.scan_results["switch_name"] = str(dev_val)
            self.scan_results["switch_port"] = str(port_val)
            self.scan_results["switch_proto"] = "CDP"
            return True
        return False

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

            # 2. SWITCH DISCOVERY: 30-second window
            self.root.after(0, self.update_scanner, "TESTING NETWORK...", "#FFFF00", "Mode: Switch Discovery (CDP/LLDP)", "00:30")
            self.sniffing = True
            timer_thread = threading.Thread(target=self.countdown_timer, args=(30,))
            timer_thread.start()

            sniff(
                iface=INTERFACE,
                filter="ether proto 0x88cc or ether dst 01:00:0c:cc:cc:cc",
                stop_filter=self.parse_cdp_lldp,
                timeout=30,
                store=0
            )
            
            self.sniffing = False
            timer_thread.join()
            if not self.is_cable_connected():
                continue

            # 3. DNS MONITORING: Active trigger with 3-second capture
            self.root.after(0, self.update_scanner, "TESTING NETWORK...", "#FFFF00", "Mode: DNS Sniffing", "00:03")
            self.sniffing = True
            timer_thread = threading.Thread(target=self.countdown_timer, args=(3,))
            timer_thread.start()

            # Generate query in background so Scapy intercepts it on eth0
            threading.Thread(target=lambda: socket.gethostbyname("google.com"), daemon=True).start()

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
                continue

            # 4. BANDWIDTH TEST
            self.root.after(0, self.update_scanner, "TESTING NETWORK...", "#FFFF00", "Mode: Bandwidth Speed Test", "--:--")
            try:
                st = speedtest.Speedtest()
                st.get_best_server()
                self.scan_results["speed_down"] = f"{st.download() / 1_000_000:.1f} Mbps"
                self.scan_results["speed_up"] = f"{st.upload() / 1_000_000:.1f} Mbps"
                self.scan_results["speed_ping"] = f"{st.results.ping:.0f} ms"
            except Exception:
                pass

            # 5. DIAGNOSTIC PAGE
            self.root.after(0, self.show_results_ui)

            while self.is_cable_connected() and self.running:
                time.sleep(1)

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("[!] Error: Run with sudo for raw packet sniffing access.")
        sys.exit(1)

    root = tk.Tk()
    app = FlukeApp(root)
    root.mainloop()
