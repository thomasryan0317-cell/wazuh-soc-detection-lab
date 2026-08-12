# vps_ip_updater.py — Auto-updating firewall access for my Wazuh dashboard

## What it does

`vps_ip_updater.py` keeps my Wazuh dashboard reachable when my home IP changes.
It runs on my Mac, detects when my public IP has rotated, and automatically
updates the firewall rule on my VPS — so I never get locked out of my own
monitoring.

## The problem

My home ISP hands me a dynamic IP that can rotate roughly every seven days. My
Wazuh dashboard on the VPS is locked down with UFW to accept only my home IP on
port 443 — which is good for security, but it means the moment my IP changes I'm
locked out of my own dashboard and have to SSH into the VPS and update the rule by
hand. I wrote this so I don't have to: wherever I'm working from, the script keeps
the firewall pointed at my current IP, so I keep access without ever leaving the
dashboard open to anyone else.

## How it works

There are two parts: the script, and the schedule that runs it.

The script does one thing. When it runs, it looks up my Mac's current public IP —
the Mac being the machine my SSH key lives on — and compares it to the last IP it
recorded. If nothing changed, it stops. If my IP rotated, it SSHes into the VPS,
adds a firewall rule allowing my new IP through to the dashboard, then deletes the
rule for the old one, so only my current address is ever allowed in. The script has
no sense of time on its own — run it, and it checks once.

The scheduling is separate. I set up a cron job on the Mac to run the script once an
hour, so the check happens automatically without me thinking about it. Keeping those
two concerns separate — a simple single-purpose script, scheduled externally — means
each piece does one job and I can test them independently.

## Design decisions

### Least privilege

With this being an expansion on my first project, I wanted to take one of the
foundational principles of security I learned studying for Security+ — least
privilege — and actually implement it. The script is designed to allow only the
minimal access the system needs.

On the firewall, only one IP is whitelisted at a time: whichever is my current IP.
That access goes with me — whether I'm at home, at work, or at a friend's place, the
script runs and updates automatically, and the old rule from wherever I was last is
deleted at the same time. So my dashboard is never reachable from anywhere except the
one place I'm actually connecting from.

On the VPS side, I applied the same principle to the script's own reach. Working from
the assumption that the script could someday be compromised or corrupted, I didn't
give it full system access. It's allowed to run only the one command it actually
needs — `ufw` — through sudo without a password, instead of blanket `sudo`. That's a
scoped sudoers rule limited to the `ufw` binary: so even in a worst case, the most a
compromised script could do is change firewall rules — it can't touch the rest of the
server. And the automation authenticates with the SSH key that lives on my Mac.

### Add before delete

The order the script makes its changes is deliberate, and it serves a couple of
purposes. Part of it is cleanliness — making sure IPs don't just stack up from every
coffee shop or friend's house I've connected from — and part of it is keeping to least
privilege, with only one active IP at a time. But the bigger reason is resilience: the
script adds the new IP *before* it deletes the old one. That way, if something happens
partway through — an internet drop, some error — and the old rule doesn't get removed,
I'm briefly allowed in from two IPs instead of zero. I'm gaining access on the new one
before I ever lose it on the old one. So the worst case is a leftover rule, not a
lockout.

**A limitation I found in my own code.** Being honest about the current behavior: if
the *add* succeeds but the *delete* fails, that stale rule doesn't get cleaned up on the
next hourly run. Because my IP hasn't changed again, the script sees "no change" and does
nothing — so the leftover rule sits there until the next time my IP actually rotates.
It's low-risk (it's an allow for an address I'm not connecting from, and least privilege
still mostly holds), but it isn't the self-healing behavior someone might assume from
"add before delete."

**Planned fix (v2).** A better version would, on every run, prune any rule on the
dashboard port that isn't my current IP — not just the single old one it remembers. That
would make the firewall self-correct every hour: any stale or unexpected allow rule gets
removed on the next pass, and the "one IP at a time" guarantee holds even if a previous
delete failed. That's my next improvement to this script.

I also want to fix the dry-run logging I mention in Testing below: the success messages
should only print when the script actually made a change, not when it's only reporting
what it would do. Small fix, but the log should never claim something happened that
didn't.

### Validate the IP — and don't trust a single source

This one comes straight out of my first project. When I was hardening the OS to only
allow the dashboard from a specific IP, one of the first problems I ran into was trying
to feed an IPv6 address into the firewall — UFW needed IPv4, and the rule kept getting
rejected until I forced IPv4 with `curl -4`. I didn't want to repeat that mistake here,
so I built the lesson into the script: it makes sure the value it grabs is a valid IPv4
address and verifies it before it ever uses it. That does two things — it stops an
accidental IPv6 reply from breaking the firewall rule, and it means I'm never passing an
unchecked value from an outside service straight into a command that runs on my server.

I also recognized that pulling my IP from any single source could hand me a wrong or bad
result if that service was down or misbehaving. So the script checks more than one
lookup source rather than relying on just one — if the first doesn't give a clean IPv4
answer, it falls back to another.

### The script runs on the machine with the SSH key

The automation has to run from the device my SSH key lives on — my Mac — for two
reasons. First, that Mac is the only machine that knows its own current home IP, which
is the whole thing the script needs to detect. Second, reaching the VPS to change the
rule requires the SSH key, and that key is on the Mac. So the script has to run there:
it's the one device that can both see the IP that changed and authenticate to the server
to fix it. This works because SSH on the VPS stays open with key-only authentication —
so even in the moment right after my IP rotates and the dashboard is locked to the old
address, the Mac can still reach the server over SSH and update the firewall. The one
door I keep open (SSH, protected by the key) is what lets me automatically fix the other
door (the dashboard).

## Testing

The script was written in VS Code, but I did all the testing from the terminal so I
could see exactly what it did.

I started with a version that just printed my current public IP, to confirm the lookup
worked. Once I'd verified that, I took the print out and tested the real behavior: I
planted a fake old IP in the script's state file, then ran it. The script detected the
mismatch, added a firewall rule for my actual current IP, and deleted the fake one. I
confirmed it server-side with `sudo ufw status` — only my real IP was left allowed on
the dashboard port, and the fake one was gone. That proved the full lifecycle: detect
the change, add the new rule, remove the old one.

I ran it first in dry-run mode, so it would report what it *would* do without touching
the VPS. That run surfaced a bug in my own logging — worth showing honestly:

```
$ python3 ~/Downloads/vps_ip_updater.py
2026-08-11T23:28:23  IP change detected: none on record -> <MY_HOME_IP>
2026-08-11T23:28:23  [DRY RUN] would run on VPS: sudo ufw allow from <MY_HOME_IP> to any port 443 proto tcp
2026-08-11T23:28:23  Added allow rule for <MY_HOME_IP> on port 443
2026-08-11T23:28:23  Firewall updated and new IP recorded.
```

The `[DRY RUN]` line is correct — in that mode the script makes no changes to the VPS at
all. But the two lines after it ("Added allow rule" / "Firewall updated") still print as
if it did, because those log messages aren't dry-run-aware. Nothing actually changed on
the server, but the log reads like it did. That's a cosmetic logging bug, not a
functional one, and fixing it is on my improvement list below.

To prove the *real* behavior, I switched dry-run off and ran a live swap test: I planted
a fake old IP in the script's state file, ran the script, and let it do the real add and
delete. Server-side, I confirmed only my current IP survived on the dashboard port:

```
$ ssh reapr@<VPS_PUBLIC_IP> "sudo ufw status" | grep 443
443/tcp            ALLOW      <MY_HOME_IP>
```

**Then I tested the scheduling.** I didn't want to wait a full hour for the first run, so
I temporarily set the cron job to run every minute, waited, and pulled the log. The
timestamps confirmed it was firing on schedule and running cleanly each time — a run
every minute, each one checking the IP and finding no change:

```
$ tail -9 ~/.vps-ip-updater/updater.log
2026-08-12T08:55:00  No change (<MY_HOME_IP>). Nothing to do.
2026-08-12T08:56:00  No change (<MY_HOME_IP>). Nothing to do.
2026-08-12T08:57:01  No change (<MY_HOME_IP>). Nothing to do.
2026-08-12T08:58:00  No change (<MY_HOME_IP>). Nothing to do.
2026-08-12T08:59:01  No change (<MY_HOME_IP>). Nothing to do.
2026-08-12T09:00:00  No change (<MY_HOME_IP>). Nothing to do.
2026-08-12T09:01:01  No change (<MY_HOME_IP>). Nothing to do.
2026-08-12T09:02:01  No change (<MY_HOME_IP>). Nothing to do.
2026-08-12T09:03:01  No change (<MY_HOME_IP>). Nothing to do.
```

Once I'd confirmed cron was firing reliably, I set the schedule back to once an hour:

```
$ crontab -l
0 * * * * /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 /Users/reapersix/scripts/vps_ip_updater.py
```

## What I learned

This was my first real experience actually writing and implementing a script. I've
known what scripts are and what they do — I use them at work fairly often — but building
one from scratch and setting it up to run something like this was new to me, and it was
a genuinely fun learning experience.

I'm still learning Python syntax and coding, so I won't claim I learned everything here.
But the biggest thing I took away was seeing what automation actually looks like when you
set it up end to end: writing the script on my own machine, and then using cron to have
that script run automatically on a schedule I set. Getting the two pieces to work
together — a script that does one job, and a scheduler that runs it on its own — was the
part that really clicked for me.

More than the specific tool, what I think this taught me is a way of thinking about
systems. I could always go back into my VPS hosting panel and fix a firewall rule by
hand. But building the automation so I never lose access in the first place — so nothing
depends on me remembering to do a manual step — is the better approach. Designing a
system so the failure can't happen beats relying on someone to catch it after it does.
That's the mindset I want to carry into setting up dashboards and security systems going
forward.
