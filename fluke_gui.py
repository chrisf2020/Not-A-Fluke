#!/usr/bin/env python3

# Libraries
import os
import sys
import time
import socket
import threading
import subprocess
import tkinter as tk
from tkinter import ttk
import speedtest
from scapy.all import sniff
from scapy.layers.dns import DNS, DNSQR

INTERFACE = "eth0"
CARRIER_PATH = f"/sys/class/net/{INTERFACE}/carrier"
# Wait a bit past Cisco's slower ~60s CDP interval so a fresh cable always gets one full cycle.
SWITCH_DISCOVERY_TIMEOUT = 65


class FlukeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PiScout Pro")
        self.root.attributes("-fullscreen", True)
        self.root.configure(bg="#121212")
        self.root.bind("<Escape>", self.close_window)

        # Progress bar visual styling
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

        # Build UI Views
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
            "poe_available": "None Detected",
            "poe_class": "--",
            "poe_power": "--",
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

        # Loading container for bandwidth testing
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

        # Header Frame holding Title and v0.2 Version Tag
        header_frame = tk.Frame(self.results_frame, bg="#00E5FF")
        header_frame.pack(fill=tk.X)

        header_title = tk.Label(
            header_frame, text="DIAGNOSTIC COMPLETE", font=("Helvetica", 18, "bold"),
            bg="#00E5FF", fg="#000000", pady=8
        )
        header_title.pack(side=tk.LEFT, padx=15)

        header_version = tk.Label(
            header_frame, text="v0.2", font=("Helvetica", 12, "bold"),
            bg="#00E5FF", fg="#333333", pady=8
        )
        header_version.pack(side=tk.LEFT)

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
        self.root.bind("<Prior>", self.scroll_page_up)
        self.root.bind("<Next>", self.scroll_page_down)
        self.root.bind("<MouseWheel>", self.scroll_with_mouse_wheel)
        self.root.bind("<Button-4>", self.scroll_up)
        self.root.bind("<Button-5>", self.scroll_down)

        # --- Switch Topology Card ---
        topo_frame = tk.Frame(self.scroll_content, bg="#1A1A1A", bd=2, relief=tk.RIDGE)
        topo_frame.pack(fill=tk.X, padx=15, pady=6)
        tk.Label(topo_frame, text="Switch Topology", font=("Helvetica", 14, "bold"), bg="#1A1A1A", fg="#FFFFFF").pack(pady=4)

        self.lbl_sw = tk.Label(topo_frame, text="SW:    --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_sw.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_ip = tk.Label(topo_frame, text="IP:    --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_ip.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_port = tk.Label(topo_frame, text="PORT:  --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_port.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_vlan = tk.Label(topo_frame, text="VLAN:  --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_vlan.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_voice = tk.Label(topo_frame, text="VOICE: --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_voice.pack(fill=tk.X, padx=15, pady=1)

        self.lbl_poe_topo = tk.Label(topo_frame, text="POE:   --", font=("Courier", 13, "bold"), bg="#1A1A1A", fg="#76FF03", anchor="w")
        self.lbl_poe_topo.pack(fill=tk.X, padx=15, pady=3)

        # --- Dedicated PoE Details Card ---
        poe_frame = tk.Frame(self.scroll_content, bg="#1A1A1A", bd=2, relief=tk.RIDGE)
        poe_frame.pack(fill=tk.X, padx=15, pady=6)
        tk.Label(poe_frame, text="PoE: Power over Ethernet", font=("Helvetica", 14, "bold"), bg="#1A1A1A", fg="#FFFFFF").pack(pady=4)

        self.res_poe_status = tk.Label(poe_frame, text="PoE: Not Detected", font=("Helvetica", 12, "bold"), bg="#1A1A1A", fg="#FFA500")
        self.res_poe_status.pack(pady=1)

        self.res_poe_details = tk.Label(poe_frame, text="Allocated: -- | Standard: --", font=("Courier", 12), bg="#1A1A1A", fg="#76FF03")
        self.res_poe_details.pack(pady=3)

        # --- Bandwidth Card ---
        speed_frame = tk.Frame(self.scroll_content, bg="#1A1A1A", bd=2, relief=tk.RIDGE)
        speed_frame.pack(fill=tk.X, padx=15, pady=6)
        tk.Label(speed_frame, text="Bandwidth", font=("Helvetica", 14, "bold"), bg="#1A1A1A", fg="#FFFFFF").pack(pady=4)

        self.res_speed_lbl = tk.Label(speed_frame, text="Down: -- Mbps | Up: -- Mbps | Ping: -- ms", font=("Helvetica", 12), bg="#1A1A1A", fg="#FFD700")
        self.res_speed_lbl.pack(pady=4)

        # --- DNS Activity Card ---
        dns_frame = tk.Frame(self.scroll_content, bg="#1A1A1A", bd=2, relief=tk.RIDGE)
        dns_frame.pack(fill=tk.X, padx=15, pady=6)
        tk.Label(dns_frame, text="DNS Activity", font=("Helvetica", 14, "bold"), bg="#1A1A1A", fg="#FFFFFF").pack(pady=4)

        self.res_dns_lbl = tk.Label(dns_frame, text="Queries captured: 0 | Last: --", font=("Helvetica", 12), bg="#1A1A1A", fg="#FF00FF")
        self.res_dns_lbl.pack(pady=4)

        # Footer
        footer = tk.Label(
            self.scroll_content, text="[Up/Down] Scroll  |  Unplug cable to reset  |  [Esc] Exit",
            font=("Helvetica", 10), bg="#121212", fg="#888888", pady=10
        )
        footer.pack(fill=tk.X)

    # --- EVENT HANDLERS ---

    def close_window(self, event):
        self.root.destroy()

    def update_scroll_region(self, event):
        self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))

    def resize_scroll_content(self, event):
        self.scroll_canvas.itemconfig(self.canvas_window, width=event.width)

    def scroll_up(self, event):
        self.scroll_canvas.yview_scroll(-1, "units")

    def scroll_down(self, event):
        self.scroll_canvas.yview_scroll(1, "units")

    def scroll_page_up(self, event):
        self.scroll_canvas.yview_scroll(-5, "units")

    def scroll_page_down(self, event):
        self.scroll_canvas.yview_scroll(5, "units")

    def scroll_with_mouse_wheel(self, event):
        if event.delta > 0:
            self.scroll_canvas.yview_scroll(-1, "units")
        else:
            self.scroll_canvas.yview_scroll(1, "units")

    # --- UI STATE MANAGERS ---

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

        # Render PoE in Topology Card and Dedicated Card
        if self.scan_results["poe_available"] == "Available":
            self.lbl_poe_topo.config(text=f"POE:   {self.scan_results['poe_power']} ({self.scan_results['poe_class']})")
            self.res_poe_status.config(text="PoE: Supported (Advertised)", fg="#00E5FF")
            self.res_poe_details.config(text=f"Power: {self.scan_results['poe_power']} | Standard: {self.scan_results['poe_class']}")
        else:
            self.lbl_poe_topo.config(text="POE:   None Advertised")
            self.res_poe_status.config(text="PoE: Not Advertised on Port", fg="#888888")
            self.res_poe_details.config(text="Standard: None / Not Supported")

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
            "poe_available": "None Detected",
            "poe_class": "--",
            "poe_power": "--",
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
        seconds_left = seconds
        while seconds_left >= 0:
            if not self.sniffing:
                break
            self.root.after(0, self.timer_label.config, {'text': f"00:{seconds_left:02d}"})
            time.sleep(1)
            seconds_left = seconds_left - 1

    # --- SWITCH TOPOLOGY & POE (CDP/LLDP via lldpd) ---

    def query_lldpd(self):
        try:
            result = subprocess.run(
                ["lldpctl", "-f", "keyvalue", INTERFACE],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    def get_lldp_value(self, kv_text, field_name):
        prefix = "lldp." + INTERFACE + "." + field_name + "="
        for line in kv_text.splitlines():
            if line.startswith(prefix):
                return line[len(prefix):]
        return ""

    def evaluate_poe(self, kv_text):
        """Extracts PoE Power TLVs from LLDP-MED (Extended Power) and CDP power TLVs."""
        power_str = ""
        # Check LLDP-MED / dot3 power allocations
        for line in kv_text.splitlines():
            lowered = line.lower()
            if "power" in lowered and ("allocated=" in lowered or "val=" in lowered or "budget=" in lowered):
                power_str = line.split("=", 1)[1].strip()
                break

        if not power_str:
            power_str = self.get_lldp_value(kv_text, "power.allocated")
        if not power_str:
            power_str = self.get_lldp_value(kv_text, "power.budget")

        if power_str:
            try:
                clean_num = "".join([c for c in power_str if c.isdigit() or c == '.'])
                val = float(clean_num)
                if "mw" in power_str.lower() or val > 150:
                    watts = val / 1000.0
                else:
                    watts = val
            except ValueError:
                watts = 0.0

            if watts > 0:
                self.scan_results["poe_available"] = "Available"
                self.scan_results["poe_power"] = f"{watts:.1f}W"

                # Classify 802.3 IEEE PoE Standards
                if watts <= 4.0:
                    self.scan_results["poe_class"] = "Class 1 (802.3af Type 1)"
                elif watts <= 7.0:
                    self.scan_results["poe_class"] = "Class 2 (802.3af Type 1)"
                elif watts <= 15.4:
                    self.scan_results["poe_class"] = "Class 0/3 (802.3af PoE)"
                elif watts <= 30.0:
                    self.scan_results["poe_class"] = "Class 4 (802.3at PoE+)"
                elif watts <= 60.0:
                    self.scan_results["poe_class"] = "Class 5/6 (802.3bt 4PPoE)"
                else:
                    self.scan_results["poe_class"] = "Class 7/8 (802.3bt Type 4)"
                return

        self.scan_results["poe_available"] = "None Detected"
        self.scan_results["poe_class"] = "None"
        self.scan_results["poe_power"] = "0.0W"

    def apply_switch_topology(self, kv_text):
        if kv_text.strip() == "":
            return False

        switch_name = self.get_lldp_value(kv_text, "chassis.name")
        if switch_name == "":
            switch_name = "Unknown"
        self.scan_results["switch_name"] = switch_name

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

        voice = "None"
        for line in kv_text.splitlines():
            if "voice" in line.lower() and "vid=" in line:
                voice = line.split("=", 1)[1]
                break
        self.scan_results["switch_voice"] = voice

        # Evaluate PoE TLVs alongside switch identification
        self.evaluate_poe(kv_text)
        return True

    def discover_switch_topology(self):
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

    def lookup_google(self):
        socket.gethostbyname("google.com")

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

            # 2. SWITCH TOPOLOGY & POE DISCOVERY
            start_minutes = SWITCH_DISCOVERY_TIMEOUT // 60
            start_seconds = SWITCH_DISCOVERY_TIMEOUT % 60
            self.root.after(
                0, self.update_scanner, "TESTING NETWORK...", "#FFFF00",
                "Mode: Switch & PoE Discovery", f"{start_minutes:02d}:{start_seconds:02d}"
            )
            self.discover_switch_topology()
            if not self.is_cable_connected():
                continue

            # 3. DNS MONITORING
            self.root.after(0, self.update_scanner, "TESTING NETWORK...", "#FFFF00", "Mode: DNS Sniffing", "00:03")
            self.sniffing = True
            timer_thread = threading.Thread(target=self.countdown_timer, args=(3,))
            timer_thread.start()

            threading.Thread(target=self.lookup_google, daemon=True).start()

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
