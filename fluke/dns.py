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


# only count questions (qr == 0), the answers are the same lookups
def count_dns_question(results, packet):
    if packet.haslayer(DNS) and packet.haslayer(DNSQR) and packet[DNS].qr == 0:
        qname = packet[DNSQR].qname.decode(errors="replace").rstrip(".")
        results["dns_queries"] += 1
        results["dns_last_domain"] = qname[:25]


# start the sniffer, do one lookup, then keep sniffing 3s. fills the dns_* fields
def run_dns_test(results, interface=INTERFACE):
    sniffer = None
    sniffer_ok = False
    try:
        sniffer_ready = threading.Event()
        sniffer = AsyncSniffer(
            iface=interface, filter="port 53", prn=lambda packet: count_dns_question(results, packet),
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
        servers = get_dns_servers(interface)
        if len(servers) == 0:
            servers = ["1.1.1.1"]   # none from the network, use a public one
        server = servers[0]

        worked, answer = dns_lookup(server, "google.com", interface)
        results["dns_server"] = server
        results["dns_time"] = answer

        if sniffer_ok:
            time.sleep(3)           # catch other DNS traffic
        else:
            # no sniffer, so just count our own lookup
            results["dns_queries"] += 1
            results["dns_last_domain"] = "google.com"
    finally:
        # stop() can re-raise whatever killed the sniffer
        if sniffer is not None:
            try:
                sniffer.stop()
            except Exception:
                logger.warning("DNS sniffer stop() raised (sniffer was not running)")
