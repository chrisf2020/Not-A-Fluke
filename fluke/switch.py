# switch info (name, port, VLAN, PoE) from CDP and LLDP.
# we ask lldpd instead of sniffing ourselves since Cisco only sends CDP every ~60s.

import subprocess

from .config import INTERFACE, logger


# keyvalue output is one fact per line, like lldp.eth0.chassis.name=SWITCH-01
def query_lldpd(interface=INTERFACE):
    try:
        result = subprocess.run(
            ["lldpctl", "-f", "keyvalue", interface],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout
    except FileNotFoundError:
        logger.error("lldpctl not found - is lldpd installed?")
        return ""
    except subprocess.TimeoutExpired:
        logger.warning("lldpctl timed out")
        return ""


# returns "" if lldpd hasn't heard that field
def get_lldp_value(kv_text, field_name, interface=INTERFACE):
    prefix = "lldp." + interface + "." + field_name + "="
    for line in kv_text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return ""


# the Pi can't measure PoE itself, so this is just what the switch says
def get_poe_text(kv_text, interface=INTERFACE):
    supported = get_lldp_value(kv_text, "port.power.supported", interface)
    power_class = get_lldp_value(kv_text, "port.power.class", interface)
    allocated = get_lldp_value(kv_text, "port.power.allocated", interface)
    med_power = get_lldp_value(kv_text, "lldp-med.poe.power", interface)

    if supported == "" and med_power == "":
        return "Not advertised"
    if supported == "no":
        return "No"

    text = "Yes"
    if power_class != "":
        text = text + ", " + power_class
    if allocated != "":
        text = text + ", " + allocated + " mW"
    elif med_power != "":
        text = text + ", " + med_power + " mW"
    return text


# returns the switch_* fields, or None if lldpd hasn't heard anything yet
def parse_switch_topology(kv_text, interface=INTERFACE):
    if kv_text.strip() == "":
        return None

    def value(field, fallback):
        found = get_lldp_value(kv_text, field, interface)
        return found if found != "" else fallback

    # switches put the port name in different fields, so try each
    port = value("port.ifname", "")
    if port == "":
        port = value("port.descr", "")
    if port == "":
        port = value("port.local", "Unknown")

    # voice VLAN: any line with "voice" and a vid
    voice = "None"
    for line in kv_text.splitlines():
        if "voice" in line.lower() and "vid=" in line:
            voice = line.split("=", 1)[1]
            break

    return {
        "switch_name": value("chassis.name", "Unknown"),
        "switch_port": port,
        "switch_vlan": value("vlan.vlan-id", "None / Untagged"),
        "switch_ip": value("chassis.mgmt-ip", "Not Advertised"),
        "switch_proto": value("via", "--"),
        "switch_voice": voice,
        "switch_poe": get_poe_text(kv_text, interface),
    }


# lldpd doesn't pass on CDP power info, so we sniff CDP ourselves for this
def cdp_poe_text(packet):
    from scapy.contrib.cdp import CDPMsgPowerAvailable
    if packet.haslayer(CDPMsgPowerAvailable):
        values = packet[CDPMsgPowerAvailable].power_available_list
        if len(values) > 0:
            watts = values[0] / 1000
            return f"Yes, {watts:.1f} W available (CDP)"
    return "No (switch offers no power)"


# runs the whole time the app is up. None if it can't start (not root, no port...)
def start_cdp_listener(on_poe, interface=INTERFACE):
    try:
        from scapy.all import AsyncSniffer
        sniffer = AsyncSniffer(
            iface=interface, filter="ether dst 01:00:0c:cc:cc:cc",
            prn=lambda packet: on_poe(cdp_poe_text(packet)), store=False
        )
        sniffer.start()
        return sniffer
    except Exception:
        logger.exception("could not start the CDP listener (PoE check)")
        return None


# listens for the frames a managed switch sends: STP (every 2s), CDP and LLDP. Skips the Pi's
# own frames (lldpd sends LLDP out of this port too). on_heard() is called on the first one.
# Routers and unmanaged switches (GL.iNet etc.) send none of these.
SWITCH_FRAMES = ("ether dst 01:80:c2:00:00:00 or ether dst 01:00:0c:cc:cc:cd"    # STP, Cisco PVST
                 " or ether dst 01:00:0c:cc:cc:cc or ether dst 01:80:c2:00:00:0e")  # CDP, LLDP


def start_switch_frame_listener(on_heard, interface=INTERFACE):
    try:
        with open(f"/sys/class/net/{interface}/address") as f:
            own_mac = f.read().strip()
        import threading
        from scapy.all import AsyncSniffer
        ready = threading.Event()
        sniffer = AsyncSniffer(
            iface=interface, filter=f"({SWITCH_FRAMES}) and not ether src {own_mac}",
            prn=lambda packet: on_heard(), store=False, started_callback=ready.set
        )
        sniffer.start()
        # stopping it before it's really listening throws, so wait until it is
        ready.wait(timeout=5)
        return sniffer
    except Exception:
        logger.exception("could not start the switch frame listener")
        return None


# stops a listener from start_switch_frame_listener (never throws)
def stop_listener(sniffer):
    if sniffer is None:
        return
    try:
        sniffer.stop()
    except Exception:
        logger.exception("could not stop the switch frame listener")
