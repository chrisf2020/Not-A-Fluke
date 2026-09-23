#!/usr/bin/env python3
import os
import sys
import time
import socket
import threading
import tkinter as tk
from tkinter import ttk
import speedtest
from scapy.all import sniff, load_contrib
from scapy.contrib.cdp import (
    CDPMsgDeviceID,
    CDPMsgPortID,
    CDPMsgNativeVLAN,
    CDPMsgApplianceID,
    CDPAddrRecordIPv4
)
from scapy.contrib.lldp import (
    LLDPDUChassisID,
    LLDPDUPortID,
    LLDPDUGenericOrganisationSpecific,
    LLDPDUManagementAddress
)
from scapy.layers.dns import DNS, DNSQR

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

        # Style configuration for loading progress bar
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

        # Build UI views
        self.build_scanner_ui()
        self.build_results_ui()
        self.show_scanner_ui()

        # Shared Diagnostic Storage
        self.scan_results = {
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

        # Loading container for speed testing
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

    def build_results_ui(self):
        self.results_frame = tk.Frame(self.container, bg="#121212")

        header = tk.Label(
            self.results_frame, text="DIAGNOSTIC COMPLETE", font=("Helvetica", 18, "bold"),
            bg="#00E5FF", fg="#000000", pady=8
        )
        header.pack(fill=tk.X)

        # Canvas & Scrollbar Frame to support Arrow Key Scrolling
        self.scroll_canvas = tk.Canvas(self.results_frame, bg="#121212", highlightthickness=0)
        self.scroll_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.scroll_content = tk.Frame(self.scroll_canvas, bg="#121212")
        self.canvas_window = self.scroll_canvas.create_window((0, 0), window=self.scroll_content, anchor="nw")

        self.scroll_content.bind("<Configure>", lambda e: self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all")))
        self.scroll_canvas.bind("<Configure>", lambda e: self.scroll_canvas.itemconfig(self.canvas_window, width=e.width))

        # Keyboard and Mousewheel bindings for scrolling
        self.root.bind("<Up>", lambda e: self.scroll_canvas.yview_scroll(-1, "units"))
        self.root.bind("<Down>", lambda e: self.scroll_canvas.yview_scroll(1, "units"))
        self.root.bind("<Prior>", lambda e: self.scroll_canvas.yview_scroll(-5, "units"))  # Page Up
        self.root.bind("<Next>", lambda e: self.scroll_canvas.yview_scroll(5, "units"))   # Page Down
        self.root.bind("<MouseWheel>", lambda e: self.scroll_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))
        self.root.bind("<Button-4>", lambda e: self.scroll_canvas.yview_scroll(-1, "units"))
        self.root.bind("<Button-5>", lambda e: self.scroll_canvas.yview_scroll(1, "units"))

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

    # --- UI MANAGERS ---

    def show_scanner_ui(self):
        self.stop_loading_animation()
        self.results_frame.pack_forget()
        self.scanner_frame.pack(expand=True, fill=tk.BOTH)

    def show_results_ui(self):
        self.stop_loading_animation()
        self.lbl_sw.config(text=f"SW:    {self.scan_results['switch_name']} ({self.scan_results['switch_proto']})")
        self.lbl_ip.config(text=f"IP:    {self.scan_results['switch_ip']}")
        self.lbl_port.config(text=f"PORT:  {self.scan_results['switch_port']}")
        self.lbl_vlan.config(text=f"VLAN:  {self.scan_results['switch_vlan']}")
        self.lbl_voice.config(text=f"VOICE: {self.scan_results['switch_voice']}")

        sp_text = f"Down: {self.scan_results['speed_down']} | Up: {self.scan_results['speed_up']} | Ping: {self.scan_results['speed_ping']}"
        dn_text = f"Queries: {self.scan_results['dns_queries']} | Domain: {self.scan_results['dns_last_domain']}"
        self.res_speed_lbl.config(text=sp_text)
        self.res_dns_lbl.config(text=dn_text)

        self.scanner_frame.pack_forget()
        self.results_frame.pack(expand=True, fill=tk.BOTH)
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
        self.timer_label.pack(pady=10)

    def update_scanner(self, status, status_color, mode, timer_text):
        self.status_label.config(text=status, fg=status_color)
        self.mode_label.config(text=mode)
        self.timer_label.config(text=timer_text)

    def reset_data(self):
        self.scan_results = {
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
        # 1. LLDP Frame Parsing
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

            # Parse Management IP Address TLV
            mgmt = packet.getlayer(LLDPDUManagementAddress)
            if mgmt and hasattr(mgmt, 'management_address'):
                addr_bytes = mgmt.management_address
                if len(addr_bytes) == 4:
                    self.scan_results["switch_ip"] = socket.inet_ntoa(addr_bytes)
                else:
                    self.scan_results["switch_ip"] = str(addr_bytes)

            # Parse IEEE 802.1 / TIA Org-Specific TLVs for Data/Voice VLAN
            layer = packet.getlayer(LLDPDUGenericOrganisationSpecific)
            while layer:
                # 802.1 Port VLAN ID (OUI: 00-80-c2, Subtype: 1)
                if layer.org_code == 0x0080c2 and layer.subtype == 1:
                    if len(layer.data) >= 2:
                        vlan_id = int.from_bytes(layer.data[:2], byteorder="big")
                        self.scan_results["switch_vlan"] = str(vlan_id)

                # LLDP-MED Network Policy for Voice (OUI: 00-12-bb, Subtype: 2)
                elif layer.org_code == 0x0012bb and layer.subtype == 2:
                    if len(layer.data) >= 4:
                        policy = int.from_bytes(layer.data[:4], byteorder="big")
                        vlan_id = (policy >> 9) & 0x0FFF
                        self.scan_results["switch_voice"] = str(vlan_id)

                layer = layer.payload.getlayer(LLDPDUGenericOrganisationSpecific)

            return True

        # 2. CDP Frame Parsing
        elif packet.haslayer("CDP"):
            device = packet.getlayer(CDPMsgDeviceID)
            port = packet.getlayer(CDPMsgPortID)
            vlan = packet.getlayer(CDPMsgNativeVLAN)
            voice = packet.getlayer(CDPMsgApplianceID)
            ip_layer = packet.getlayer(CDPAddrRecordIPv4)

            dev_val = device.val.decode(errors="replace") if isinstance(device.val, bytes) else device.val
            port_val = port.iface.decode(errors="replace") if isinstance(port.iface, bytes) else port.iface
            
            self.scan_results["switch_name"] = str(dev_val)
            self.scan_results["switch_port"] = str(port_val)
            self.scan_results["switch_proto"] = "CDP"

            if ip_layer and hasattr(ip_layer, 'addr'):
                self.scan_results["switch_ip"] = str(ip_layer.addr)

            if vlan:
                self.scan_results["switch_vlan"] = str(vlan.vlan)

            if voice:
                self.scan_results["switch_voice"] = str(voice.vlan)

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

            # 2. SWITCH TOPOLOGY DISCOVERY (30s)
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

            # 3. DNS MONITORING (3s)
            self.root.after(0, self.update_scanner, "TESTING NETWORK...", "#FFFF00", "Mode: DNS Sniffing", "00:03")
            self.sniffing = True
            timer_thread = threading.Thread(target=self.countdown_timer, args=(3,))
            timer_thread.start()

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
                pass

            # 5. DIAGNOSTIC RESULTS PAGE
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
