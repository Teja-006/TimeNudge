"""
Timetable notifier using ntfy.sh

Run once a day at 9 PM IST. It:
  1. Sends tomorrow's class list immediately.
  2. Schedules a reminder (via ntfy's delayed delivery) for 1 hour
     before the first class tomorrow.

Usage:
  python notify.py                      # normal run
  python notify.py --dry-run            # print instead of sending
  python notify.py --date 2026-11-23    # pretend "today" is this date
"""
import argparse
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
NTFY_SERVER = "https://ntfy.sh"
REMINDER_MINUTES_BEFORE = 60
TIMETABLE_FILE = Path(__file__).parent / "timetable.json"


def load_classes_for(day_name: str) -> list[dict]:
    with open(TIMETABLE_FILE, encoding="utf-8") as f:
        classes = json.load(f)
    todays = [c for c in classes if c["day"].lower() == day_name.lower()]
    return sorted(todays, key=lambda c: c["start"])


def format_classes(classes: list[dict]) -> str:
    lines = []
    for c in classes:
        room = f" ({c['room']})" if c.get("room") else ""
        lines.append(f"{c['start']}-{c['end']}  {c['subject']}{room}")
    return "\n".join(lines)


def send(topic: str, title: str, message: str, delay_ts: int | None = None,
         dry_run: bool = False) -> None:
    headers = {"Title": title}  # keep headers ASCII-only
    if delay_ts:
        headers["Delay"] = str(delay_ts)  # unix timestamp, ntfy delivers then

    if dry_run:
        when = f"(scheduled for {datetime.fromtimestamp(delay_ts, IST):%a %d %b %H:%M})" if delay_ts else "(now)"
        print(f"\n--- {title} {when} ---\n{message}")
        return

    r = requests.post(f"{NTFY_SERVER}/{topic}", data=message.encode("utf-8"),
                      headers=headers, timeout=15)
    r.raise_for_status()
    print(f"Sent: {title}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--date", help="Pretend today is YYYY-MM-DD")
    args = parser.parse_args()

    topic = os.environ.get("NTFY_TOPIC", "dry-run-topic")
    if not args.dry_run and "NTFY_TOPIC" not in os.environ:
        raise SystemExit("Set the NTFY_TOPIC environment variable.")

    if args.date:
        today = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=IST)
    else:
        today = datetime.now(IST)

    tomorrow = today + timedelta(days=1)
    day_name = tomorrow.strftime("%A")
    classes = load_classes_for(day_name)

    if not classes:
        send(topic, f"{day_name}: no classes", "Nothing scheduled tomorrow. Enjoy!",
             dry_run=args.dry_run)
        return

    # 1) Evening summary, sent now
    send(topic, f"Tomorrow ({day_name}): {len(classes)} classes",
         format_classes(classes), dry_run=args.dry_run)

    # 2) Morning reminder, scheduled for 1 hour before the first class
    first = classes[0]
    h, m = map(int, first["start"].split(":"))
    class_start = tomorrow.replace(hour=h, minute=m, second=0, microsecond=0)
    remind_at = class_start - timedelta(minutes=REMINDER_MINUTES_BEFORE)

    msg = f"First class at {first['start']}: {first['subject']}"
    if first.get("room"):
        msg += f" in {first['room']}"
    msg += "\n\nToday's full schedule:\n" + format_classes(classes)

    send(topic, "Class in 1 hour", msg,
         delay_ts=int(remind_at.timestamp()), dry_run=args.dry_run)


if __name__ == "__main__":
    main()
