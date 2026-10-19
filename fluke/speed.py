# speed test, ping burst and the quality score
# used by the cable test and the Wi-Fi tab's Speed Test button

import os
import re
import time
import threading
import contextlib
import subprocess

import speedtest

from .config import INTERFACE, logger

PING_TARGET = "1.1.1.1"     # pinged for latency / jitter / loss
PING_COUNT = 20             # 0.2 s apart, so about 4 s total

# one speed test at a time, since SpeedSampler swaps classes in the speedtest module
# and two tests at once would mix up their curves
speed_test_lock = threading.Lock()

# our own rule of thumb, not an official formula. worst reading wins.
# each rule is (reading, great, good, average, lower_is_better), worse than average is Poor
# and a missing reading makes it N/A
QUALITY_LEVELS = ["Poor", "Average", "Good", "Great"]
QUALITY_RULES = [
    ("Video Streaming", "Streaming", [("down", 25, 10, 3, False)]),
    ("Online Gaming", "Gaming", [("latency", 40, 80, 150, True), ("jitter", 15, 30, 60, True),
                                 ("loss", 0.5, 1, 2, True)]),
    ("Video Chatting", "Video Chat", [("down", 5, 3, 1.5, False), ("up", 3, 1.5, 0.8, False),
                                      ("latency", 100, 150, 250, True), ("jitter", 30, 50, 100, True),
                                      ("loss", 0.5, 1, 3, True)]),
]


# IPv4 address on a port, "" if none
def get_ip(interface=INTERFACE):
    try:
        result = subprocess.run(["ip", "-4", "-br", "addr", "show", interface],
                                capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    words = result.stdout.split()
    if len(words) >= 3:
        return words[2].split("/")[0]
    return ""


# which port traffic really leaves from for this source IP ("" if unknown). speedtest-cli
# can only pick a source address, and with the cable in it usually routes out the cable
def route_interface(source_ip, target=PING_TARGET):
    try:
        result = subprocess.run(["ip", "route", "get", target, "from", source_ip],
                                capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    match = re.search(r"\bdev (\S+)", result.stdout)
    return match.group(1) if match else ""


# link speed and duplex from the network card, e.g. "1000 Mbps full" ("--" if unknown)
def link_speed(interface=INTERFACE):
    try:
        with open(f"/sys/class/net/{interface}/speed") as f:
            speed = int(f.read().strip())
        with open(f"/sys/class/net/{interface}/duplex") as f:
            duplex = f.read().strip()
    except (OSError, ValueError):
        return "--"
    if speed <= 0:
        return "--"
    return f"{speed} Mbps {duplex}"


def speed_rating(mbps):
    if mbps < 10:
        return "Poor"
    if mbps < 25:
        return "Fair"
    if mbps < 100:
        return "Good"
    if mbps < 500:
        return "Very Good"
    return "Excellent"


# "290.0 Mbps" -> 290.0, None if there's no number
def read_number(text):
    match = re.search(r"-?\d+(?:\.\d+)?", str(text))
    if match is None:
        return None
    return float(match.group())


# jitter is the average gap between back-to-back reply times. None if nothing replied,
# which might just mean ICMP is blocked, so don't treat it as 100% loss
def parse_ping_output(text):
    times = [float(t) for t in re.findall(r"time[=<]\s*([\d.]+)\s*ms", text)]
    loss_match = re.search(r"([\d.]+)% packet loss", text)
    if len(times) == 0 or loss_match is None:
        return None
    jitter = 0.0
    if len(times) > 1:
        jitter = sum(abs(times[k] - times[k - 1]) for k in range(1, len(times))) / (len(times) - 1)
    return {"latency": sum(times) / len(times), "jitter": jitter, "loss": float(loss_match.group(1))}


# None if ping is missing or nothing replied
def ping_burst(interface=INTERFACE, target=PING_TARGET, count=PING_COUNT):
    try:
        result = subprocess.run(
            ["ping", "-n", "-I", interface, "-c", str(count), "-i", "0.2", "-W", "1", target],
            capture_output=True, text=True, timeout=count + 10,
            env=dict(os.environ, LC_ALL="C")      # English output so the regex works
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("ping burst could not run")
        return None
    return parse_ping_output(result.stdout)


# never raises, if ping fails the results just stay Failed (shows N/A)
def run_ping_test(results, interface=INTERFACE):
    try:
        ping = ping_burst(interface)
        if ping is None:
            logger.warning("no ping replies on %s - latency, jitter and loss left as N/A", interface)
            return
        results["ping_latency"] = f"{ping['latency']:.1f} ms"
        results["ping_jitter"] = f"{ping['jitter']:.1f} ms"
        results["ping_loss"] = f"{ping['loss']:g} %"
    except Exception:
        logger.exception("ping test failed")


# download + upload through one port, returns the speed curves (empty if it failed)
def run_bandwidth_test(results, interface=INTERFACE, on_status=lambda text: None):
    with speed_test_lock:
        try:
            # source_address forces the test through this port
            on_status("Connecting to closest server...")
            st = speedtest.Speedtest(source_address=get_ip(interface))
            st.get_best_server()

            sampler = SpeedSampler()

            on_status("Testing Download Speed...")
            with sampler.watch("down"):
                down_bps = st.download()

            on_status("Testing Upload Speed...")
            with sampler.watch("up"):
                up_bps = st.upload()

            results["speed_down"] = f"{down_bps / 1_000_000:.1f} Mbps"
            results["speed_up"] = f"{up_bps / 1_000_000:.1f} Mbps"
            results["speed_ping"] = f"{st.results.ping:.0f} ms"
            results["speed_rating"] = speed_rating(down_bps / 1_000_000)
            return sampler.curves
        except Exception:
            # screen still says Failed, but the log says why
            logger.exception("speedtest failed on %s", interface)
            return {"down": [], "up": []}


# any reading can be None, returns {"Video Streaming": "Great", ...}
def network_quality(down, up, latency, jitter, loss):
    readings = {"down": down, "up": up, "latency": latency, "jitter": jitter, "loss": loss}
    ratings = {}
    for name, short, rules in QUALITY_RULES:
        grades = []
        for reading, great, good, average, lower_is_better in rules:
            value = readings[reading]
            if value is None:
                grades = []
                break
            if lower_is_better:
                grade = 3 if value <= great else 2 if value <= good else 1 if value <= average else 0
            else:
                grade = 3 if value >= great else 2 if value >= good else 1 if value >= average else 0
            grades.append(grade)
        ratings[name] = QUALITY_LEVELS[min(grades)] if grades else "N/A"
    return ratings


# (seconds, bytes so far) points -> Mbps, each averaged over the last `window` seconds
def rates_from_totals(points, window=1.0, max_points=80):
    rates = []
    for i in range(1, len(points)):
        t_now, b_now = points[i]
        j = i - 1
        while j > 0 and t_now - points[j - 1][0] <= window:
            j -= 1
        t_then, b_then = points[j]
        if t_now - t_then > 0:
            rates.append(max(0.0, (b_now - b_then) * 8 / (t_now - t_then) / 1_000_000))
    while len(rates) > 0 and rates[-1] == 0:      # zeros at the end are just after the transfer finished
        rates.pop()
    if len(rates) > max_points:
        step = len(rates) / max_points
        rates = [rates[int(k * step)] for k in range(max_points)]
    return rates


# speedtest-cli only gives one number at the end, but its worker threads keep byte counts.
# so we swap in subclasses that register themselves and read the counts every 0.2 s.
# if the hook ever breaks the test still runs, just without a curve
class SpeedSampler:
    INTERVAL = 0.2

    def __init__(self):
        self.curves = {"down": [], "up": []}

    @contextlib.contextmanager
    def watch(self, kind):
        class_name = "HTTPDownloader" if kind == "down" else "HTTPUploader"
        real = getattr(speedtest, class_name, None)
        if real is None:
            logger.warning("speedtest has no %s - no live speed curve this time", class_name)
            yield
            return

        if kind == "down":
            bytes_so_far = lambda worker: sum(worker.result)
        else:
            bytes_so_far = lambda worker: sum(worker.request.data.total)

        workers = []

        class Tracked(real):
            def __init__(self, *args, **kwargs):
                real.__init__(self, *args, **kwargs)
                workers.append(self)

        stop = threading.Event()
        started = time.time()
        points = [(0.0, 0)]

        def poll():
            while True:
                finished = stop.wait(self.INTERVAL)
                try:
                    total = sum(bytes_so_far(worker) for worker in list(workers))
                    points.append((time.time() - started, total))
                except Exception:
                    pass
                if finished:
                    break

        setattr(speedtest, class_name, Tracked)
        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)
            setattr(speedtest, class_name, real)
            try:
                self.curves[kind] = rates_from_totals(points)
            except Exception:
                logger.exception("could not build the %s speed curve", kind)
