"""
TimeNudge - timetable notifier using ntfy.sh

Run once a day at 9 PM IST. It:
  1. Checks holidays.json. If tomorrow is a holiday, it sends a holiday
     notice and stops (no class messages).
  2. Otherwise sends tomorrow's class list immediately.
  3. Schedules one reminder per class, REMINDER_MINUTES_BEFORE minutes before
     it starts. ntfy holds these and delivers them on time.

Usage:
  python notify.py                      # normal run
  python notify.py --dry-run            # print instead of sending
  python notify.py --date 2026-11-23    # pretend "today" is this date
"""
import argparse
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
NTFY_SERVER = "https://ntfy.sh"
REMINDER_MINUTES_BEFORE = 10
BASE = Path(__file__).parent
TIMETABLE_FILE = BASE / "timetable.json"
HOLIDAYS_FILE = BASE / "holidays.json"


# ---------------------------------------------------------------- data
def load_classes_for(day_name: str) -> list[dict]:
    with open(TIMETABLE_FILE, encoding="utf-8") as f:
        classes = json.load(f)
    todays = [c for c in classes if c["day"].lower() == day_name.lower()]
    return sorted(todays, key=lambda c: c["start"])


def get_holiday(date_str: str) -> str | None:
    """Return the holiday name if date_str (YYYY-MM-DD) is in holidays.json."""
    if not HOLIDAYS_FILE.exists():
        return None
    with open(HOLIDAYS_FILE, encoding="utf-8") as f:
        for h in json.load(f):
            if h.get("date") == date_str:
                return h.get("name", "Holiday")
    return None


# ---------------------------------------------------------- formatting
def pretty_subject(name: str) -> str:
    """'AIML-Elective-2(Applied Computer Vision) Group-A' -> 'Elective 2: Applied Computer Vision'"""
    m = re.match(r"AIML-Elective-(\d+)\((.+)\)\s*Group-[A-Za-z]", name)
    return f"Elective {m.group(1)}: {m.group(2)}" if m else name


def detail_line(c: dict) -> str:
    return " | ".join(x for x in [c.get("kind"), c.get("room")] if x)


def format_summary(classes: list[dict]) -> str:
    last_end = max(c["end"] for c in classes)
    lines = [f"{len(classes)} classes  |  {classes[0]['start']} to {last_end}", ""]
    for i, c in enumerate(classes, start=1):
        lines.append(f"{i}. {c['start']}-{c['end']}  {pretty_subject(c['subject'])}")
        d = detail_line(c)
        if d:
            lines.append(f"     {d}")
    return "\n".join(lines)


def format_single(c: dict) -> str:
    lines = [f"{c['start']}-{c['end']}", pretty_subject(c["subject"])]
    d = detail_line(c)
    if d:
        lines.append(d)
    return "\n".join(lines)


# -------------------------------------------------------------- ntfy
def ascii_safe(text: str) -> str:
    return text.encode("ascii", "ignore").decode()  # HTTP headers must be ASCII


def get_scheduled(topic: str) -> set[tuple]:
    """(time, title) of messages already waiting on ntfy, to avoid duplicates."""
    try:
        r = requests.get(f"{NTFY_SERVER}/{topic}/json",
                         params={"poll": "1", "scheduled": "1"}, timeout=15)
        r.raise_for_status()
        found = set()
        for line in r.text.splitlines():
            if line.strip():
                m = json.loads(line)
                found.add((m.get("time"), m.get("title")))
        return found
    except Exception as e:  # noqa: BLE001
        print(f"(Could not check existing scheduled messages: {e})")
        return set()


def send(topic: str, title: str, message: str, tags: str = "", priority: str = "",
         delay_ts: int | None = None, dry_run: bool = False) -> None:
    headers = {"Title": ascii_safe(title)}
    if tags:
        headers["Tags"] = tags
    if priority:
        headers["Priority"] = priority
    if delay_ts:
        headers["Delay"] = str(delay_ts)  # unix timestamp; ntfy delivers then

    if dry_run:
        when = (f"scheduled for {datetime.fromtimestamp(delay_ts, IST):%a %d %b %H:%M}"
                if delay_ts else "now")
        print(f"\n--- {title}  [{when}] ---\n{message}")
        return

    r = requests.post(f"{NTFY_SERVER}/{topic}", data=message.encode("utf-8"),
                      headers=headers, timeout=15)
    r.raise_for_status()
    print(f"Sent: {title}")


# --------------------------------------------------------------- main
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date", help="Pretend today is YYYY-MM-DD")
    args = parser.parse_args()

    topic = os.environ.get("NTFY_TOPIC", "dry-run-topic")
    if not args.dry_run and "NTFY_TOPIC" not in os.environ:
        raise SystemExit("Set the NTFY_TOPIC environment variable.")

    today = (datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=IST)
             if args.date else datetime.now(IST))
    tomorrow = today + timedelta(days=1)
    day_name = tomorrow.strftime("%A")
    nice_date = tomorrow.strftime("%a %d %b")

    # 1) Holiday?
    holiday = get_holiday(tomorrow.strftime("%Y-%m-%d"))
    if holiday:
        send(topic, f"Holiday tomorrow ({nice_date})",
             f"{holiday}\nNo classes. Enjoy!", tags="palm_tree", dry_run=args.dry_run)
        return

    classes = load_classes_for(day_name)
    if not classes:
        send(topic, f"No classes tomorrow ({day_name})",
             "Nothing scheduled. Enjoy!", tags="tada", dry_run=args.dry_run)
        return

    # 2) Evening summary, sent now
    send(topic, f"Tomorrow, {nice_date}: {len(classes)} classes",
         format_summary(classes), tags="calendar", dry_run=args.dry_run)

    # 3) One reminder per class, REMINDER_MINUTES_BEFORE minutes before it starts
    already = set() if args.dry_run else get_scheduled(topic)
    now = datetime.now(IST)
    for c in classes:
        h, m = map(int, c["start"].split(":"))
        start = tomorrow.replace(hour=h, minute=m, second=0, microsecond=0)
        remind_at = start - timedelta(minutes=REMINDER_MINUTES_BEFORE)
        ts = int(remind_at.timestamp())
        title = f"Class in {REMINDER_MINUTES_BEFORE} min: {pretty_subject(c['subject'])}"

        if not args.dry_run and remind_at <= now + timedelta(seconds=15):
            print(f"Skipped (time already passed): {title}")
            continue
        if (ts, ascii_safe(title)) in already:
            print(f"Skipped (already scheduled): {title}")
            continue

        send(topic, title, format_single(c), tags="alarm_clock", priority="high",
             delay_ts=ts, dry_run=args.dry_run)


if __name__ == "__main__":
    main()