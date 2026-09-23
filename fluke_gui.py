#!/usr/bin/env python3
import os
import sys
import time
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

        # Header Banner
        self.status_label = tk.Label(
            root, text="INITIALIZING...", font=("Helvetica", 18, "bold"),
            bg="#2B2B2B", fg="#FFA500", pady=12
        )
        self.status_label.pack(fill=tk.X)

        # Main Info Display Area
        self.info_frame = tk.Frame(root, bg="#121212", pady=10)
        self.info_frame.pack(expand=True, fill=tk.BOTH)

        # Mode Indicator
        self.mode_label = tk.Label(
            self.info_frame, text="Mode: Switch Discovery", font=("Helvetica", 14, "italic"),
            bg="#121212", fg="#888888"
        )
        self.mode_label.pack(pady=2)

        # Data Field 1
        self.field1_title = tk.Label(self.info_frame, text="Switch Name / IP", font=("Helvetica", 12), bg="#121212", fg="#555555")
        self.field1_title.pack()
        self.field1_label = tk.Label(self.info_frame, text="--", font=("Helvetica", 22, "bold"), bg="#121212", fg="#00E5FF", wraplength=700)
        self.field1_label.pack(pady=5)

        # Data Field 2
        self.field2_title = tk.Label(self.info_frame, text="Switch Port", font=("Helvetica", 12), bg="#121212", fg="#555555")
        self.field2_title.pack()
        self.field2_label = tk.Label(self.info_frame, text="--", font=("Helvetica", 22, "bold"), bg="#121212", fg="#76FF03")
        self.field2_label.pack(pady=5)

        # Extra Metrics Panel (SpeedTest / DNS)
        self.extra_label = tk.Label(
            self.info_frame, text="", font=("Helvetica", 13),
            bg="#121212", fg="#FFD700", justify=tk.CENTER
        )
        self.extra_label.pack(pady=10)

        # Footer Hint
        self.footer = tk.Label(
            root, text="Press 'Esc' to exit", font=("Helvetica", 10),
            bg="#121212", fg="#444444", pady=5
        )
        self.footer.pack(side=tk.BOTTOM)

        self.running = True
        self.worker_thread = threading.Thread(target=self.main_workflow, daemon=True)
        self.worker_thread.start()

    def update_ui(self, status, status_color, mode, f1_title, f1_val, f2_title, f2_val, extra=""):
        self.status_label.config(text=status, fg=status_color)
        self.mode_label.config(text=f"Mode: {mode}")
        self.field1_title.config(text=f1_title)
        self.field1_label.config(text=f1_val)
        self.field2_title.config(text=f2_title)
        self.field2_label.config(text=f2_val)
        self.extra_label.config(text=extra)

    def is_cable_connected(self):
        if not os.path.exists(CARRIER_PATH):
            return False
        try:
            with open(CARRIER_PATH, "r") as f:
                return f.read().strip() == "1"
        except OSError:
            return False

    def run_speedtest_metrics(self):
        self.root.after(0, self.update_ui, "RUNNING SPEED TEST...", "#FFD700", "Bandwidth Test", "Status", "Contacting servers...", "Metrics", "Measuring ping, download & upload...")
        try:
            st = speedtest.Speedtest()
            st.get_best_server()
            down = st.download() / 1_000_000  # Convert to Mbps
            up = st.upload() / 1_000_000      # Convert to Mbps
            ping = st.results.ping
            
            summary = f"Download: {down:.2f} Mbps  |  Upload: {up:.2f} Mbps  |  Ping: {ping:.1f} ms"
            return summary
        except Exception as e:
            return f"Speed test failed: {str(e)[:30]}"

    def parse_cdp_lldp(self, packet):
        found = False
        if packet.haslayer("LLDPDU"):
            chassis = packet.getlayer(LLDPDUChassisID)
            port = packet.getlayer(LLDPDUPortID)
            cid = getattr(chassis, 'macaddr', None) or getattr(chassis, 'id', 'Unknown')
            pid = getattr(port, 'portid', 'Unknown')
            if isinstance(pid, bytes): pid = pid.decode(errors="replace")
            
            self.root.after(0, self.update_ui, "SWITCH DETECTED (LLDP)", "#76FF03", "Switch Discovery", "Switch MAC / ID", str(cid), "Switch Port", str(pid))
            found = True

        elif packet.haslayer("CDP"):
            device = packet.getlayer(CDPMsgDeviceID)
            port = packet.getlayer(CDPMsgPortID)
            dev_val = device.val.decode(errors="replace") if isinstance(device.val, bytes) else device.val
            port_val = port.iface.decode(errors="replace") if isinstance(port.iface, bytes) else port.iface
            
            self.root.after(0, self.update_ui, "SWITCH DETECTED (CDP)", "#00E5FF", "Switch Discovery", "Switch Name", str(dev_val), "Switch Port", str(port_val))
            found = True
        return found

    def parse_dns_packet(self, packet):
        if packet.haslayer(DNS) and packet.haslayer(DNSQR):
            qname = packet[DNSQR].qname.decode(errors="replace")
            src_ip = packet[IP].src if packet.haslayer(IP) else "Unknown"
            proto = "TCP" if packet.haslayer(TCP) else ("UDP" if packet.haslayer(UDP) else "IP")
            
            extra_info = f"Captured DNS Query: {qname}\nSource IP: {src_ip} ({proto})"
            self.root.after(0, self.update_ui, "DNS TRAFFIC INSPECTED", "#FF00FF", "DNS Sniffer", "Query Domain", qname, "Transport Protocol", proto, extra_info)
            return True
        return False

    def main_workflow(self):
        while self.running:
            # 1. Wait for link
            if not self.is_cable_connected():
                self.root.after(0, self.update_ui, "WAITING FOR CABLE...", "#FFA500", "Standby", "Status", "Unplugged", "Port", "Disconnected")
                while not self.is_cable_connected() and self.running:
                    time.sleep(1)

            if not self.running: break

            # 2. Step One: Listen for Switch CDP/LLDP (max 30 seconds)
            self.root.after(0, self.update_ui, "LISTENING FOR SWITCH INFO...", "#FFFF00", "Switch Discovery", "Status", "Sniffing CDP/LLDP...", "Port", "Listening")
            sniff(iface=INTERFACE, filter="ether proto 0x88cc or ether dst 01:00:0c:cc:cc:cc", stop_filter=self.parse_cdp_lldp, timeout=30, store=0)

            if not self.is_cable_connected(): continue

            # 3. Step Two: Run Bandwidth Speed Test
            speed_results = self.run_speedtest_metrics()
            # Update screen to show speed metrics briefly
            self.field1_title.config(text="Bandwidth Status")
            self.field1_label.config(text="Speed Test Complete")
            self.field2_title.config(text="Results")
            self.field2_label.config(text="See Below")
            self.extra_label.config(text=speed_results)
            time.sleep(5)

            # 4. Step Three: Monitor live DNS queries passing through
            self.root.after(0, self.update_ui, "SNIFFING DNS QUERIES...", "#00FFFF", "DNS Monitor", "Status", "Listening to port 53...", "Action", "Waiting for query")
            sniff(iface=INTERFACE, filter="port 53", stop_filter=self.parse_dns_packet, timeout=60, store=0)

            # 5. Hold until cable unplugged
            while self.is_cable_connected() and self.running:
                time.sleep(1)

if __name__ == "__main__":
    if os.geteuid() != 0:
        print("[!] Error: Run with sudo for raw packet sniffing access.")
        sys.exit(1)

    root = tk.Tk()
    app = FlukeApp(root)
    root.mainloop()
