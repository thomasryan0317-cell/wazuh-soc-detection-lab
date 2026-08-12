#!/usr/bin/env python3
"""
vps_ip_updater.py — keep the VPS dashboard firewall rule pointed at my current home IP.

My home ISP hands me a dynamic IP that rotates roughly every week. The Wazuh
dashboard on my VPS is locked down with UFW to my home IP on port 443, so when the
IP changes I lose access until I fix the rule by hand. This script automates that:

    1. Look up my current public IPv4.
    2. Compare it to the last IP I recorded.
    3. If it changed, SSH to the VPS and:
         - add an allow rule for the NEW IP on the dashboard port
         - delete the allow rule for the OLD IP
    4. Record the new IP and log what happened.

Why this can run unattended: SSH (port 22) stays open to the internet with key-only
auth, so this script can always reach the VPS even right after the IP changes. That
open SSH door is what makes the auto-fix possible — the firewall change only touches
the dashboard port (443), never SSH.
"""

import ipaddress
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG — fill these in with your own values
# ---------------------------------------------------------------------------
VPS_HOST = "YOUR_VPS_IP_OR_HOSTNAME"    # the server running Wazuh
VPS_USER = "reapr"                       # the non-root sudo user on the VPS
SSH_KEY_PATH = str(Path.home() / ".ssh" / "id_ed25519")   # key used for SSH
DASHBOARD_PORT = 443                     # the port locked to my home IP
STATE_FILE = Path.home() / ".vps-ip-updater" / "last_ip.txt"
LOG_FILE = Path.home() / ".vps-ip-updater" / "updater.log"
DRY_RUN = False   # True = print the firewall commands instead of running them

# Services that echo back your public IPv4. These are IPv4-only endpoints on
# purpose — that's the IPv4-vs-IPv6 lesson from the SIEM build baked into the tool.
IP_LOOKUP_URLS = [
    "https://api4.ipify.org",
    "https://ipv4.icanhazip.com",
]


def log(message):
    """Append a timestamped line to the log file and print it."""
    line = f"{datetime.now().isoformat(timespec='seconds')}  {message}"
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)


def get_current_ip():
    """Return my current public IPv4, or None if every lookup fails.

    Each answer is validated as a real IPv4 address before it's trusted. That
    does two jobs: it catches an accidental IPv6 reply (the exact bug that broke
    my first firewall rule), and it makes sure nothing unexpected from a lookup
    service ever gets passed into a shell command further down.
    """
    for url in IP_LOOKUP_URLS:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                candidate = resp.read().decode().strip()
            addr = ipaddress.ip_address(candidate)   # raises if it's not an IP
            if addr.version != 4:
                log(f"Skipping non-IPv4 answer from {url}: {candidate}")
                continue
            return str(addr)
        except Exception as e:
            log(f"Lookup failed via {url}: {e}")
    return None


def read_last_ip():
    """Return the last IP we recorded, or None on the first run."""
    if STATE_FILE.exists():
        return STATE_FILE.read_text().strip() or None
    return None


def write_last_ip(ip):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(ip + "\n")


def run_remote_ufw(args):
    """Run a single `sudo ufw ...` command on the VPS over SSH.

    Uses key-based SSH (no password) plus a scoped NOPASSWD sudoers rule on the
    VPS that allows ONLY ufw to run without a password — not blanket sudo.
    Returns the completed process so the caller can check the result.
    """
    remote_cmd = "sudo ufw " + " ".join(args)
    if DRY_RUN:
        log(f"[DRY RUN] would run on VPS: {remote_cmd}")
        return None
    ssh = ["ssh", "-i", SSH_KEY_PATH, f"{VPS_USER}@{VPS_HOST}", remote_cmd]
    return subprocess.run(ssh, capture_output=True, text=True)


def update_firewall(old_ip, new_ip):
    """Allow new_ip on the dashboard port, then remove old_ip's rule."""
    # Add the NEW rule first, so there's never a window where I'm locked out.
    add = run_remote_ufw(
        ["allow", "from", new_ip, "to", "any",
         "port", str(DASHBOARD_PORT), "proto", "tcp"]
    )
    if add and add.returncode != 0:
        log(f"Failed to add rule for {new_ip}: {add.stderr.strip()}")
        return False
    log(f"Added allow rule for {new_ip} on port {DASHBOARD_PORT}")

    # Then remove the stale rule — only if we know what the old one was.
    if old_ip and old_ip != new_ip:
        delete = run_remote_ufw(
            ["delete", "allow", "from", old_ip, "to", "any",
             "port", str(DASHBOARD_PORT), "proto", "tcp"]
        )
        if delete and delete.returncode == 0:
            log(f"Deleted old allow rule for {old_ip}")
        elif delete:
            # Non-fatal: the old rule may already be gone.
            log(f"Could not delete old rule for {old_ip} "
                f"(may already be gone): {delete.stderr.strip()}")
    return True


def main():
    current_ip = get_current_ip()
    if not current_ip:
        log("Could not determine current public IP — aborting this run.")
        sys.exit(1)

    last_ip = read_last_ip()

    if current_ip == last_ip:
        log(f"No change ({current_ip}). Nothing to do.")
        return

    log(f"IP change detected: {last_ip or 'none on record'} -> {current_ip}")
    if update_firewall(last_ip, current_ip):
        write_last_ip(current_ip)
        log("Firewall updated and new IP recorded.")
    else:
        # Leave the recorded IP unchanged so the next run retries the fix.
        log("Firewall update failed — recorded IP left unchanged for retry.")
        sys.exit(1)


if __name__ == "__main__":
    main()
