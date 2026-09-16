#!/usr/bin/env python3

import subprocess
import speedtest

INTERFACE = "Ethernet" 

def check_interface(interface):
    try:
        result = subprocess.run(["ipconfig"], capture_output=True, text=True)
        return interface.lower() in result.stdout.lower()
    except Exception:
        input("Press Enter to exit...")
        return False

def run_speedtest():
    st = speedtest.Speedtest()
    st.get_best_server()
    download = st.download() / 1_000_000
    upload = st.upload() / 1_000_000
    ping = st.results.ping
    return download, upload, ping

def main():
    print("Simple SpeedTest Diagnostic")

    print("\nChecking network interface...")
    if check_interface(INTERFACE):
        print(f"Interface '{INTERFACE}' detected")
    else:
        input("Press Enter to exit...")
        print(f"Interface '{INTERFACE}' NOT found")
        return

    print("\nRunning speed test...")
    download, upload, ping = run_speedtest()

    print(f"Download: {download:.2f} Mbps")
    print(f"Upload:   {upload:.2f} Mbps")
    print(f"Ping:     {ping:.2f} ms")

    print("\nDone")

if __name__ == "__main__":
    main()
    input("Press Enter to exit...")

