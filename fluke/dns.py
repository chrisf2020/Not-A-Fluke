# DNS test: one real lookup out the test port, plus a few secs of sniffing
# to count other DNS traffic.

import time
import random
import socket
import threading
import subprocess

from scapy.all import AsyncSniffer
from scapy.layers.dns import DNS, DNSQR

from .config import INTERFACE, logger


# DNS servers the network gave the test port, from nmcli
def get_dns_servers(interface=INTERFACE):
    try:
        result = subprocess.run(["nmcli", "-t", "-g", "IP4.DNS", "dev", "show", interface],
                                capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("could not ask nmcli for DNS servers")
        return []
    servers = []
    for part in result.stdout.replace("|", "\n").split("\n"):
        part = part.strip()
        if part != "":
            servers.append(part)
    return servers


# forced out the test port since gethostbyname could go out wifi instead
def dns_lookup(server, name, interface=INTERFACE):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode())
        sock.settimeout(2)
        question = DNS(id=random.randint(1, 65535), rd=1, qd=DNSQR(qname=name))
        start = time.time()
        sock.sendto(bytes(question), (server, 53))
        data, sender = sock.recvfrom(4096)
        milliseconds = round((time.time() - start) * 1000)
        reply = DNS(data)
        if reply.rcode == 0 and reply.ancount > 0:
            return True, str(milliseconds) + " ms"
        return False, "bad reply (code " + str(reply.rcode) + ")"
    except socket.timeout:
        return False, "no reply"
    except OSError as err:
        return False, str(err)
    finally:
        sock.close()


# the name the DNS test looks up
TEST_DOMAIN = "google.com"


# only count questions (qr == 0), the answers are the same lookups. Skips the Pi's own background
# lookups (updates, apps running on it) - only our test lookup and lookups
# from other devices on the port count
def count_dns_question(results, packet, own_ip=""):
    if packet.haslayer(DNS) and packet.haslayer(DNSQR) and packet[DNS].qr == 0:
        qname = packet[DNSQR].qname.decode(errors="replace").rstrip(".")
        from_pi = packet.haslayer("IP") and packet["IP"].src == own_ip
        if from_pi and qname != TEST_DOMAIN:
            return
        results["dns_queries"] += 1
        results["dns_last_domain"] = qname[:25]


# start the sniffer, do one lookup, then keep sniffing 3s. fills the dns_* fields
# on_line(text) gets a line for the screen as each part happens
def run_dns_test(results, interface=INTERFACE, on_line=lambda text: None):
    sniffer = None
    sniffer_ok = False
    from .speed import get_ip
    own_ip = get_ip(interface)
    try:
        sniffer_ready = threading.Event()
        sniffer = AsyncSniffer(
            iface=interface, filter="port 53", prn=lambda packet: count_dns_question(results, packet, own_ip),
            store=False, started_callback=sniffer_ready.set
        )
        sniffer.start()
        sniffer_ok = sniffer_ready.wait(timeout=5)
    except Exception:
        logger.exception("DNS sniffer could not start")

    # sniffer didn't start (no libpcap or bad port name), carry on instead of hanging
    if not sniffer_ok:
        logger.warning("DNS sniffer is not listening - doing the active lookup only")

    try:
        # no IP on the cable = the network gave it nothing (no DHCP), so there's nothing to ask
        if own_ip == "":
            results["dns_server"] = "--"
            results["dns_time"] = "No IP on the cable (no DHCP)"
            on_line("No IP address on the cable (no DHCP) - can't ask DNS")
            time.sleep(2)
            return
        on_line(f"Pi's address: {own_ip}")
        servers = get_dns_servers(interface)
        if len(servers) == 0:
            servers = ["1.1.1.1"]   # none from the network, use a public one
            on_line("The network gave no DNS server, using 1.1.1.1")
        else:
            on_line("DNS from the network: " + ", ".join(servers))
        server = servers[0]

        on_line(f"Asking {server} for {TEST_DOMAIN}...")
        worked, answer = dns_lookup(server, TEST_DOMAIN, interface)
        results["dns_server"] = server
        results["dns_time"] = answer
        on_line(f"Answered in {answer}" if worked else f"No good answer: {answer}")

        if sniffer_ok:
            on_line("Listening for other DNS lookups on the port...")
            time.sleep(3)           # catch other DNS traffic
            on_line(f"Lookups seen: {results['dns_queries']}")
        else:
            # no sniffer, so just count our own lookup
            results["dns_queries"] += 1
            results["dns_last_domain"] = TEST_DOMAIN
    finally:
        # stop() can re-raise whatever killed the sniffer
        if sniffer is not None:
            try:
                sniffer.stop()
            except Exception:
                logger.warning("DNS sniffer stop() raised (sniffer was not running)")
