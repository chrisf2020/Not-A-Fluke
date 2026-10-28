# Tests for the parts that don't need a screen, a cable or root. Run them with:  notafluke test

import os
import tempfile
import unittest

from fluke import config, history, share, speed, switch, wifi

IW_SCAN = """BSS 18:60:41:57:46:56(on wlan0) -- associated
\tfreq: 2437.0
\tsignal: -48.00 dBm
\tSSID: HomeNet
\tRSN:\t * Version: 1
\t\t * Authentication suites: PSK SAE
\tHT capabilities:
\tVHT capabilities:
BSS aa:bb:cc:dd:ee:ff(on wlan0)
\tfreq: 5180
\tsignal: -71.00 dBm
\tSSID: 
\tBSS Load:
\t\t * station count: 3
\t\t * channel utilisation: 51/255
"""

PING = """64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=10.0 ms
64 bytes from 1.1.1.1: icmp_seq=2 ttl=57 time=14.0 ms
64 bytes from 1.1.1.1: icmp_seq=3 ttl=57 time=12.0 ms
--- 1.1.1.1 ping statistics ---
4 packets transmitted, 3 received, 25% packet loss, time 600ms
"""

LLDP = """lldp.eth0.via=CDPv2
lldp.eth0.chassis.name=SWITCH-01
lldp.eth0.chassis.mgmt-ip=10.0.0.2
lldp.eth0.port.ifname=GigabitEthernet1/0/24
lldp.eth0.vlan.vlan-id=20
lldp.eth0.lldp-med.policy.voice.vid=30
lldp.eth0.port.power.supported=yes
lldp.eth0.port.power.class=class 4
lldp.eth0.port.power.allocated=25500
"""


class WifiTests(unittest.TestCase):
    def test_channels(self):
        self.assertEqual([wifi.freq_to_channel(f) for f in (2412, 2437, 2484, 5180, 5805, 5975, 1)], [1, 6, 14, 36, 161, 5, 0])

    def test_parse_iw_scan(self):
        home, hidden = wifi.parse_iw_scan(IW_SCAN)
        self.assertEqual((home["ssid"], home["channel"], home["signal"]), ("HomeNet", 6, -48))
        self.assertEqual((home["security"], home["standard"], home["connected"]), ("WPA2/3", "Wi-Fi 5 (ac)", True))
        self.assertEqual((hidden["ssid"], hidden["channel"], hidden["clients"], hidden["busy"]), ("<hidden>", 36, "3", "20%"))

    def test_parse_iw_link(self):
        self.assertIsNone(wifi.parse_iw_link("Not connected."))
        link = wifi.parse_iw_link("Connected to 18:60:41:57:46:56 (on wlan0)\n\tSSID: HomeNet\n\tsignal: -52 dBm\n")
        self.assertEqual(link, {"ssid": "HomeNet", "signal": -52})

    def test_ap_detail(self):
        rows = dict(wifi.ap_detail_rows(wifi.parse_iw_scan(IW_SCAN)[0]))
        self.assertEqual(rows["Signal"], "Excellent (-48 dBm)")
        self.assertEqual(rows["Band"], "2.4 GHz, channel 6")
        self.assertEqual(rows["Security"], "Locked (WPA2/3)")

    def test_signal_words(self):
        self.assertEqual([wifi.signal_level(d) for d in (-45, -65, -75, -90)], [3, 2, 1, 0])
        self.assertEqual(wifi.signal_bars(-65), "▂▄▆ ")


class SpeedTests(unittest.TestCase):
    def test_parse_ping(self):
        ping = speed.parse_ping_output(PING)
        self.assertAlmostEqual(ping["latency"], 12.0)
        self.assertAlmostEqual(ping["jitter"], 3.0)
        self.assertEqual(ping["loss"], 25.0)
        self.assertIsNone(speed.parse_ping_output("100% packet loss"))

    def test_ping_shows_lines(self):
        lines = []
        ping = speed.ping_burst("lo", "127.0.0.1", count=3, on_line=lines.append)
        self.assertEqual(ping["loss"], 0.0)
        self.assertEqual(sum("icmp_seq=" in line for line in lines), 3)

    def test_read_number(self):
        self.assertEqual(speed.read_number("290.0 Mbps"), 290.0)
        self.assertIsNone(speed.read_number("Failed"))

    def test_quality(self):
        great = speed.network_quality(100, 50, 10, 2, 0)
        self.assertEqual(set(great.values()), {"Great"})
        self.assertEqual(speed.network_quality(None, None, None, None, None)["Video Streaming"], "N/A")
        self.assertEqual(speed.network_quality(2, 1, 300, 5, 0)["Online Gaming"], "Poor")

    def test_rates_from_totals(self):
        # 1,250,000 bytes per second = 10 Mbps, then the transfer stops
        points = [(t * 0.5, int(min(t, 6) * 625_000)) for t in range(10)]
        rates = speed.rates_from_totals(points)
        self.assertAlmostEqual(rates[2], 10.0)
        self.assertNotEqual(rates[-1], 0)

    def test_rating(self):
        self.assertEqual([speed.speed_rating(m) for m in (5, 20, 50, 200, 900)], ["Poor", "Fair", "Good", "Very Good", "Excellent"])


class SummaryTests(unittest.TestCase):
    def test_short_link(self):
        from fluke.app import short_link
        self.assertEqual([short_link(t) for t in ("1000 Mbps full", "100 Mbps half", "2500 Mbps full", "--")],
                         ["1G", "100M half", "2.5G", ""])

    def test_summary_colours(self):
        from fluke.app import scan_summary, empty_scan_results
        from fluke.config import GOOD, OK, BAD
        good = dict(empty_scan_results(), link_speed="1000 Mbps full", switch_name="SW1",
                    dns_time="12 ms", speed_down="90 Mbps", speed_rating="Good")
        self.assertEqual(scan_summary(good), [("Link 1G", GOOD), ("Switch", GOOD), ("DNS", GOOD), ("Internet", GOOD)])
        bad = dict(empty_scan_results(), link_speed="100 Mbps half", switch_name="Not Found",
                   dns_time="no reply", speed_down="Failed")
        self.assertEqual([c for t, c in scan_summary(bad)], [OK, OK, BAD, BAD])


class SwitchTests(unittest.TestCase):
    def test_topology(self):
        topo = switch.parse_switch_topology(LLDP, "eth0")
        self.assertEqual(topo["switch_name"], "SWITCH-01")
        self.assertEqual(topo["switch_port"], "GigabitEthernet1/0/24")
        self.assertEqual((topo["switch_vlan"], topo["switch_voice"], topo["switch_proto"]), ("20", "30", "CDPv2"))
        self.assertEqual(topo["switch_poe"], "Yes, class 4, 25500 mW")

    def test_nothing_heard(self):
        self.assertIsNone(switch.parse_switch_topology("", "eth0"))
        self.assertEqual(switch.parse_switch_topology("lldp.eth0.via=LLDP", "eth0")["switch_name"], "Unknown")


class HistoryTests(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "scans.jsonl")
            for n in range(config.MAX_SCAN_RECORDS + 5):
                self.assertTrue(history.save_scan_record({"n": n}, path))
            with open(path, "a") as f:
                f.write("not json\n")
            records = history.load_scan_history(path)
            self.assertEqual(len(records), config.MAX_SCAN_RECORDS)
            self.assertEqual(records[0]["n"], config.MAX_SCAN_RECORDS + 4)   # newest first

    def test_summary(self):
        record = {"complete": False, "started_at": "2026-09-30T14:05:12", "switch_name": "SWITCH-01",
                  "switch_port": "GigabitEthernet1/0/5", "speed_down": "93.4 Mbps"}
        self.assertEqual(history.format_record_summary(record), "!09-30 02:05PM SWITCH-01    Gi1/0/5  93.4M")


class ConfigTests(unittest.TestCase):
    def test_find_interface_on_a_vm(self):
        with tempfile.TemporaryDirectory() as net:
            for name in ("lo", "enp0s3", "wlp2s0"):
                os.makedirs(os.path.join(net, name))
            os.makedirs(os.path.join(net, "enp0s3", "device"))
            os.makedirs(os.path.join(net, "wlp2s0", "device"))
            os.makedirs(os.path.join(net, "wlp2s0", "wireless"))
            self.assertEqual(config.find_interface("NO_SUCH_VAR", "eth0", False, net), "enp0s3")
            self.assertEqual(config.find_interface("NO_SUCH_VAR", "wlan0", True, net), "wlp2s0")


@unittest.skipUnless(os.environ.get("DISPLAY"), "no screen (DISPLAY not set)")
class ScreenTests(unittest.TestCase):
    # builds every screen without starting the scan, and clicks through them
    def test_screens(self):
        import tkinter as tk
        from fluke.app import FlukeApp, empty_scan_results
        root = tk.Tk()
        try:
            app = FlukeApp(root, windowed=True, start_workers=False)
            app.scan_results = empty_scan_results(started=True)
            app.update_scanner("TESTING", "#FFFFFF", 2, "")
            for n in range(7):
                app.add_ping_line(f"64 bytes from 1.1.1.1: icmp_seq={n} ttl=57 time=11.{n} ms")
            self.assertEqual(len(app.ping_lines), 5)
            app.open_history()
            app.show_results_ui()                       # finishing on another tab doesn't pull you back
            self.assertEqual(app.current_view, "history")
            app.open_cable()
            self.assertEqual(app.current_view, "results")
            app.show_cable_speed()
            self.assertEqual(app.current_tab(), "cable")
            app.show_wifi_frame()
            app.show_wifi_results(wifi.parse_iw_scan(IW_SCAN))
            app.show_ap_info(wifi.parse_iw_scan(IW_SCAN)[0])
            app.show_frame(app.wifi_speed_frame, "wifi_speed")
            app.finish_wifi_speed_test({"speed_down": "50 Mbps"}, {"down": [1, 2, 3], "up": []})
            app.rescan_pressed()
            self.assertTrue(app.rescan_requested)
            root.update()
            self.assertEqual(app.current_view, "wifi_speed")
        finally:
            root.destroy()


class ShareTests(unittest.TestCase):
    RECORD = {"started_at": "2026-10-28T12:59:29", "switch_name": "SW-01", "speed_down": "93.4 Mbps"}

    def test_report_filename_has_no_colons(self):
        self.assertEqual(share.report_filename(self.RECORD), "scan-2026-10-28_12-59-29.pdf")

    def test_build_pdf_makes_a_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "report.pdf")
            share.build_pdf(self.RECORD, path)
            with open(path, "rb") as f:
                self.assertEqual(f.read(4), b"%PDF")

    def test_scan_text_fits_a_scannable_qr(self):
        text = share.scan_text(self.RECORD)
        self.assertIn("SW-01", text)
        self.assertIn("DOWN 93.4", text)
        self.assertLess(len(text), 250)

    def test_email_link_is_a_mailto_with_the_results(self):
        link = share.scan_email_link(self.RECORD)
        self.assertTrue(link.startswith("mailto:?body="))
        self.assertIn("SW-01", link)
        self.assertNotIn(" ", link)     # spaces have to be encoded or phones won't open it

    def test_qr_matrix_is_square(self):
        try:
            matrix = share.qr_matrix("http://192.168.8.159:8080/scan.pdf")
        except ImportError:
            self.skipTest("python3-qrcode not installed")
        self.assertGreater(len(matrix), 20)
        self.assertEqual(len(matrix), len(matrix[0]))


if __name__ == "__main__":
    unittest.main()
