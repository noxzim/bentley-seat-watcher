#!/usr/bin/env python3
"""
Bentley course seat watcher.

Polls Bentley's public course listing (no login required) and pushes a phone
notification via ntfy.sh when a watched section flips from Closed to Open.

Data source: https://bentleyapps.azurewebsites.net/course-listing/index.php
The page states seat counts are "updated in real-time when the query is submitted."
"""

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LISTING_URL = "https://bentleyapps.azurewebsites.net/course-listing/index.php"
REGISTER_URL = "https://www.bentley.edu/mybentley"
UA = "bentley-seat-watcher/1.0 (personal course registration alert)"

# Python installed from python.org on macOS ships without the system CA bundle,
# so fall back to certifi's when the default store can't verify anything.
try:
    import certifi

    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "watchlist.json"
DEFAULT_STATE = HERE / "state.json"


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}] {msg}", flush=True)


# ---------------------------------------------------------------- scraping

def fetch_dept(term, dept, timeout=60):
    """POST the search form for one department and return the raw HTML."""
    fields = [("acad_period[]", term), ("dept[]", dept), ("submit", "Search")]
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        LISTING_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UA,
            "Accept": "text/html",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read().decode("utf-8", "replace")


COURSE_RE = re.compile(r"<div class='course'>(.*?)</div>", re.S)
STATUS_RE = re.compile(r"<div>Status:\s*(\w+)</div>")
SEATS_RE = re.compile(r"<div>Seats Available:\s*(-?\d+)</div>")
INSTR_RE = re.compile(r"<div>Instructor:\s*(.*?)</div>", re.S)
MEET_RE = re.compile(r"<div class='meeting-pattern'>(.*?)</div>", re.S)


def _text(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def parse_sections(html):
    """Return a list of dicts, one per section found in the results HTML."""
    # The page ships a commented-out duplicate "Seats Available" div. Strip
    # comments first or every seat count reads as the stale commented value.
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    out = []
    for chunk in html.split("<div class='course-grid'>")[1:]:
        m = COURSE_RE.search(chunk)
        if not m:
            continue
        heading = _text(m.group(1))
        code, _, title = heading.partition(" - ")
        status = STATUS_RE.search(chunk)
        seats = SEATS_RE.search(chunk)
        instr = INSTR_RE.search(chunk)
        meet = MEET_RE.search(chunk)
        out.append(
            {
                "code": re.sub(r"\s+", " ", code).strip(),
                "title": title.strip(),
                "status": status.group(1) if status else "Unknown",
                "seats": int(seats.group(1)) if seats else 0,
                "instructor": _text(instr.group(1)) if instr else "",
                "meets": _text(meet.group(1)) if meet else "",
            }
        )
    return out


# ---------------------------------------------------------------- matching

def dept_of(entry):
    """'CS 350-3' -> 'CS'. Handles ML* language codes too."""
    m = re.match(r"\s*([A-Za-z]+)", entry)
    return m.group(1).upper() if m else ""


def norm(code):
    s = re.sub(r"\s+", " ", code).strip().upper()
    # The listing prints "CS 305-11"; accept "CS305-11" typed without the space.
    return re.sub(r"^([A-Z]+)\s*(\d)", r"\1 \2", s)


def matches(watch_entry, section_code):
    """Exact section ('CS 350-3') or whole course ('CS 350' -> any section)."""
    w, s = norm(watch_entry), norm(section_code)
    if w == s:
        return True
    return "-" not in w and s.startswith(w + "-")


# ---------------------------------------------------------------- notifying

def notify(topic, title, message, click=REGISTER_URL, priority="urgent", tags="rotating_light", dry_run=False):
    if dry_run:
        log(f"DRY RUN would push -> {title}: {message}")
        return
    req = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(topic)}",
        data=message.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": tags,
            "Click": click,
            "User-Agent": UA,
        },
    )
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
        r.read()
    log(f"pushed -> {title}")


# ---------------------------------------------------------------- state

def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(path, state):
    Path(path).write_text(json.dumps(state, indent=2, sort_keys=True))


# ---------------------------------------------------------------- one pass

def check_once(cfg, state, topic, dry_run=False):
    """One poll of every watched department. Returns updated state."""
    watch = cfg["sections"]
    term = cfg["term"]
    cooldown = int(cfg.get("realert_after_minutes", 180)) * 60
    now = time.time()

    depts = sorted({dept_of(w) for w in watch if dept_of(w)})
    found = {}
    hit = set()
    failed = 0
    for i, dept in enumerate(depts):
        if i:
            time.sleep(2)  # be polite to the school's server
        try:
            sections = parse_sections(fetch_dept(term, dept))
        except Exception as e:  # noqa: BLE001 - never let one dept kill the loop
            log(f"WARN {dept}: fetch/parse failed: {e}")
            failed += 1
            continue
        log(f"{dept}: {len(sections)} sections returned")
        for sec in sections:
            for w in watch:
                if matches(w, sec["code"]):
                    found[sec["code"]] = sec
                    hit.add(w)

    for w in watch:
        if w not in hit:
            log(f"WARN '{w}' matched no section - check the code or the term")

    health = state.get("__health", {})
    strikes = int(health.get("strikes", 0))
    warned = bool(health.get("warned", False))
    blind = failed == len(depts) or not found
    if blind:
        strikes += 1
        log(f"WARN poll found nothing usable (strike {strikes})")
        if strikes >= 3 and not warned:
            notify(topic, "Seat watcher is BLIND",
                   "3 polls in a row returned nothing from Bentley's course "
                   "listing. The site may be down or changed. Check your "
                   "sections manually until this clears.",
                   priority="high", tags="warning", dry_run=dry_run)
            warned = True
    else:
        if warned:
            notify(topic, "Seat watcher recovered",
                   f"Back to reading Bentley's listing normally. "
                   f"Watching {len(found)} sections again.",
                   priority="default", tags="white_check_mark", dry_run=dry_run)
        strikes, warned = 0, False
    state["__health"] = {"strikes": strikes, "warned": warned, "last_poll": now}

    for code, sec in sorted(found.items()):
        prev = state.get(code, {})
        open_now = sec["status"].lower() == "open" and sec["seats"] > 0
        was_open = bool(prev.get("open"))
        last_alert = float(prev.get("last_alert", 0))
        prev_seats = int(prev.get("seats", 0))

        # Alert on: closed->open, first ever sighting while open, more seats
        # than last alert, or the cooldown lapsing while still open.
        reason = None
        if open_now and not was_open:
            reason = "opened"
        elif open_now and "open" not in prev:
            reason = "open"
        elif open_now and sec["seats"] > prev_seats:
            reason = "more seats"
        elif open_now and now - last_alert > cooldown:
            reason = "still open"

        if reason:
            seat_word = "seat" if sec["seats"] == 1 else "seats"
            notify(
                topic,
                f"{code} is OPEN",
                f"{sec['seats']} {seat_word} - {sec['title']}\n"
                f"{sec['meets']}\n{sec['instructor']}\nRegister in Workday now.",
                dry_run=dry_run,
            )
            last_alert = now
        else:
            log(f"{code}: {sec['status']} ({sec['seats']} seats)")

        state[code] = {
            "open": open_now,
            "seats": sec["seats"],
            "status": sec["status"],
            "last_alert": last_alert,
            "last_seen": now,
        }
    return state


# ---------------------------------------------------------------- entry

def main():
    p = argparse.ArgumentParser(description="Watch Bentley course sections for open seats.")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--state", default=str(DEFAULT_STATE))
    p.add_argument("--loop", action="store_true", help="keep polling instead of exiting")
    p.add_argument("--interval", type=int, default=None, help="minutes between polls (overrides config)")
    p.add_argument("--max-minutes", type=int, default=0, help="stop looping after N minutes (0 = forever)")
    p.add_argument("--dry-run", action="store_true", help="print notifications instead of sending")
    p.add_argument("--test-push", action="store_true", help="send one test notification and exit")
    args = p.parse_args()

    cfg = load_json(args.config, None)
    if cfg is None:
        sys.exit(f"No config at {args.config}")

    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    if not topic and not args.dry_run:
        sys.exit("Set NTFY_TOPIC env var or ntfy_topic in the config.")

    if args.test_push:
        n = len(cfg.get("sections", []))
        every = cfg.get("interval_minutes", 10)
        notify(topic, "Bentley watcher is live",
               f"Watching {n} sections, checking every {every} minutes.\n"
               f"Sent at the same urgent priority as a real seat alert, so if "
               f"this banners, real ones will too.",
               priority="urgent", tags="white_check_mark", dry_run=args.dry_run)
        return

    interval = (args.interval if args.interval is not None else int(cfg.get("interval_minutes", 10))) * 60
    deadline = time.time() + args.max_minutes * 60 if args.max_minutes else None

    while True:
        state = load_json(args.state, {})
        try:
            state = check_once(cfg, state, topic, dry_run=args.dry_run)
            save_state(args.state, state)
        except Exception as e:  # noqa: BLE001 - a bad poll must not end the watch
            log(f"ERROR poll failed: {e}")
        if not args.loop:
            return
        if deadline and time.time() + interval > deadline:
            log("max-minutes reached, exiting")
            return
        log(f"sleeping {interval // 60} min")
        time.sleep(interval)


if __name__ == "__main__":
    main()
