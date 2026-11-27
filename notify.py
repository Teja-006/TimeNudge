"""
TimeNudge - timetable notifier using ntfy.sh

HOW IT WORKS
Every run looks ahead HORIZON_DAYS days (today + the next 2) and hands ntfy
everything with an exact delivery time:
  - the class summary for each day, delivered at 9 PM the evening before
  - one reminder per class, REMINDER_MINUTES_BEFORE minutes before it starts
  - a holiday notice instead of the above on days in holidays.json

ntfy holds these messages and delivers them on time. So it does not matter if
GitHub starts this job hours late: as long as it runs once a day or so, every
message still arrives on time. Repeat runs skip anything already scheduled.

Usage:
  python notify.py                      # normal run
  python notify.py --dry-run            # print instead of sending
  python notify.py --dry-run --date 2026-11-25   # pretend today is this date (12:00)
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
SUMMARY_HOUR = 21      # the summary for tomorrow is delivered at 21:00 IST tonight
HORIZON_DAYS = 3       # today + next 2 days (ntfy can hold messages up to 3 days)
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


def fetch_cache(topic: str) -> tuple[set, set]:
    """What ntfy already holds: ({(time, title)}, {title}), delivered + scheduled."""
    r = requests.get(f"{NTFY_SERVER}/{topic}/json",
                     params={"poll": "1", "scheduled": "1"}, timeout=20)
    r.raise_for_status()
    keys, titles = set(), set()
    for line in r.text.splitlines():
        if line.strip():
            m = json.loads(line)
            if m.get("event", "message") == "message":
                keys.add((m.get("time"), m.get("title")))
                titles.add(m.get("title"))
    return keys, titles


def send(topic: str, title: str, message: str, tags: str = "", priority: str = "",
         delay_ts: int | None = None, dry_run: bool = False) -> None:
    headers = {"Title": ascii_safe(title)}
    if tags:
        headers["Tags"] = tags
    if priority:
        headers["Priority"] = priority
    if delay_ts:
        headers["Delay"] = str(delay_ts)  # unix timestamp; ntfy delivers then

    when = (f"delivers {datetime.fromtimestamp(delay_ts, IST):%a %d %b %H:%M}"
            if delay_ts else "sent now")
    if dry_run:
        print(f"\n--- {title}  [{when}] ---\n{message}")
        return

    r = requests.post(f"{NTFY_SERVER}/{topic}", data=message.encode("utf-8"),
                      headers=headers, timeout=20)
    r.raise_for_status()
    print(f"OK ({when}): {title}")


# ------------------------------------------------------------ planning
def plan_day(topic: str, day: datetime, now: datetime, keys: set, titles: set,
             dry_run: bool) -> None:
    """Queue the summary and reminders for one day (day = midnight IST)."""
    day_name = day.strftime("%A")
    nice = day.strftime("%a %d %b")
    holiday = get_holiday(day.strftime("%Y-%m-%d"))
    classes = [] if holiday else load_classes_for(day_name)
    print(f"\n== {nice} ==")

    # ---- evening-before message (summary / holiday / no classes)
    if holiday:
        title, body, tags = f"Holiday tomorrow ({nice})", f"{holiday}\nNo classes. Enjoy!", "palm_tree"
    elif not classes:
        title, body, tags = f"No classes tomorrow ({nice})", "Nothing scheduled. Enjoy!", "tada"
    else:
        title, body, tags = (f"Tomorrow, {nice}: {len(classes)} classes",
                             format_summary(classes), "calendar")

    summary_at = (day - timedelta(days=1)).replace(hour=SUMMARY_HOUR, minute=0)
    if ascii_safe(title) in titles:
        print(f"Skipped (already queued/sent): {title}")
    elif summary_at > now + timedelta(seconds=15):
        send(topic, title, body, tags=tags, delay_ts=int(summary_at.timestamp()),
             dry_run=dry_run)
    elif now < day:  # it is still the evening before: send straight away
        send(topic, title, body, tags=tags, dry_run=dry_run)
    else:
        print(f"Skipped (the evening-before time has passed): {title}")

    # ---- one reminder per class
    for c in classes:
        h, m = map(int, c["start"].split(":"))
        start = day.replace(hour=h, minute=m)
        remind_at = start - timedelta(minutes=REMINDER_MINUTES_BEFORE)
        ts = int(remind_at.timestamp())
        rtitle = f"Class in {REMINDER_MINUTES_BEFORE} min: {pretty_subject(c['subject'])}"

        if remind_at <= now + timedelta(seconds=15):
            print(f"Skipped (time already passed): {rtitle}")
        elif (ts, ascii_safe(rtitle)) in keys:
            print(f"Skipped (already scheduled): {rtitle}")
        else:
            send(topic, rtitle, format_single(c), tags="alarm_clock", priority="high",
                 delay_ts=ts, dry_run=dry_run)


# --------------------------------------------------------------- main
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date", help="Pretend today is YYYY-MM-DD (12:00 IST)")
    parser.add_argument("--days", type=int, default=HORIZON_DAYS,
                        help="How many days to prepare (default 3, max 3)")
    args = parser.parse_args()

    topic = os.environ.get("NTFY_TOPIC", "dry-run-topic")
    if not args.dry_run and "NTFY_TOPIC" not in os.environ:
        raise SystemExit("Set the NTFY_TOPIC environment variable.")

    if args.date:
        now = datetime.strptime(args.date, "%Y-%m-%d").replace(hour=12, tzinfo=IST)
    else:
        now = datetime.now(IST)

    keys, titles = (set(), set()) if args.dry_run else fetch_cache(topic)

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(min(args.days, HORIZON_DAYS)):
        plan_day(topic, midnight + timedelta(days=offset), now, keys, titles, args.dry_run)


if __name__ == "__main__":
    main()