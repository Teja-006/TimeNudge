"""
update_timetable.py  -  build timetable.json from timetable images (free).

Run it locally:
    python update_timetable.py

It will:
  1. Ask you to pick one or more timetable images (or PDFs).
  2. Read them with Google's Gemini API (free tier).
  3. Keep ONLY the subjects listed below (theory + lab).
  4. Show you a preview, check for clashes, save timetable.json.
  5. Optionally commit and push to GitHub.

One-time setup:
    pip install google-genai
    Get a free key at https://aistudio.google.com  ->  "Get API key"
    The script will ask for it once and save it in a local .env file.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
TIMETABLE_FILE = HERE / "timetable.json"
ENV_FILE = HERE / ".env"

# ----------------------------------------------------------------------
# YOUR SUBJECTS  (edit here each semester)
# ----------------------------------------------------------------------
SUBJECTS = {
    "CORE": [
        "Brain Computer Interface",
        "Generative AI",
        "Natural Language Processing",
    ],
    "EXTRA": [
        "Career Readiness for Modern Workspace Theory",
        "2024-2028 Entrepreneurship Track, Research Track & Industry Track Theory"
    ],
    "ELECTIVES": [
        "AIML-Elective-1(Agentic AI) Group-A",
        "AIML-Elective-2(Applied Computer Vision) Group-A",
    ],
}
ALL_SUBJECTS = [s for group in SUBJECTS.values() for s in group]

# Models are tried in this order (names change over time; override with
# GEMINI_MODEL in your .env if one stops working).
MODELS = ["gemini-3.5-flash", "gemini-2.5-flash", "gemini-flash-latest"]

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".pdf": "application/pdf"}


# ----------------------------------------------------------------------
# API key handling
# ----------------------------------------------------------------------
def load_env() -> None:
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_api_key() -> str:
    load_env()
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    print("\nNo Gemini API key found.")
    print("Get a free one at https://aistudio.google.com (Get API key).")
    key = input("Paste your key here: ").strip()
    if not key:
        sys.exit("No key given.")
    with open(ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"GEMINI_API_KEY={key}\n")
    print("Saved to .env (this file is git-ignored, so it will not be pushed).")
    return key


# ----------------------------------------------------------------------
# Picking images
# ----------------------------------------------------------------------
def pick_images() -> list[Path]:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        files = filedialog.askopenfilenames(
            title="Select your timetable image(s) - hold Ctrl to pick several",
            filetypes=[("Images / PDF", "*.png *.jpg *.jpeg *.webp *.pdf")],
        )
        root.destroy()
        if files:
            return [Path(f) for f in files]
        print("No file chosen in the window.")
    except Exception:
        pass

    print("Type or drag-drop the image path, one per line. Empty line when done.")
    paths = []
    while True:
        p = input("Image path: ").strip().strip('"').strip("'")
        if not p:
            break
        path = Path(p)
        if path.exists() and path.suffix.lower() in MIME:
            paths.append(path)
        else:
            print("  Not found or unsupported type (use png/jpg/webp/pdf).")
    return paths


# ----------------------------------------------------------------------
# Reading a timetable with Gemini
# ----------------------------------------------------------------------
def build_prompt() -> str:
    return f"""You are reading a university timetable (image or PDF).

Extract ONLY the classes that belong to these subjects (canonical names):
{json.dumps(ALL_SUBJECTS, indent=2)}

Rules:
- The timetable may abbreviate or code the names (e.g. "BCI", "GenAI", "NLP",
  "CRMW"). Match them to the canonical names above.
- For the two electives, include ONLY the exact elective and group listed
  (Group-A). Ignore every other elective or group, even if similar.
- Ignore every subject not in the list (this includes any default elective
  that is not one of the two listed).
- A subject can appear as Theory and/or Lab. Set "kind" to "Theory" or "Lab"
  based on how the timetable shows it (labs are often marked Lab, Practical,
  or (L), and are often 2-3 periods long). If unclear, use "Theory".
- If a class spans several periods or merged cells, use the start time of the
  first period and the end time of the last period.
- Use 24-hour time in HH:MM format (e.g. 14:00, not 2 PM).
- "day" must be a full English weekday name (Monday ... Sunday).
- "room" is the room/lab number if shown, otherwise an empty string.
- Include every occurrence across the whole week.

Return ONLY a JSON array, no other text. Each item:
{{"day": "Monday", "start": "09:00", "end": "10:00",
  "subject": "<canonical name>", "kind": "Theory", "room": "B204"}}
"""


def call_model(client, types, parts) -> str:
    models = [os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL") else []
    models += MODELS
    last_error = None
    for model in models:
        for _ in range(2):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json", temperature=0),
                )
                print(f"  (read using {model})")
                return resp.text
            except Exception as e:  # noqa: BLE001
                last_error = e
                msg = str(e)
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    print("  Rate limit hit, waiting 20 seconds...")
                    time.sleep(20)
                    continue
                break  # try next model
    raise RuntimeError(f"All models failed. Last error: {last_error}")


def parse_json(text: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("classes") or next(iter(data.values()), [])
    return data


def clean_entries(raw: list[dict]) -> list[dict]:
    """Validate and normalise what the model returned."""
    canon = {s.lower(): s for s in ALL_SUBJECTS}
    day_map = {d.lower(): d for d in DAYS}
    out = []
    for item in raw:
        try:
            subject = canon[str(item["subject"]).strip().lower()]
            day = day_map[str(item["day"]).strip().lower()]
            times = []
            for key in ("start", "end"):
                h, m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(item[key]).strip()).groups()
                times.append(f"{int(h):02d}:{m}")
            if times[1] <= times[0]:
                raise ValueError("end before start")
            kind = "Lab" if str(item.get("kind", "")).strip().lower().startswith("lab") else "Theory"
            out.append({"day": day, "start": times[0], "end": times[1],
                        "subject": subject, "kind": kind,
                        "room": str(item.get("room", "") or "").strip()})
        except (KeyError, AttributeError, ValueError):
            print(f"  Skipped an unreadable row: {item}")
    return out


def extract_from_image(client, types, path: Path) -> list[dict]:
    print(f"\nReading {path.name} ...")
    part = types.Part.from_bytes(data=path.read_bytes(),
                                 mime_type=MIME[path.suffix.lower()])
    text = call_model(client, types, [build_prompt(), part])
    entries = clean_entries(parse_json(text))
    print(f"  Found {len(entries)} of your classes.")
    return entries


# ----------------------------------------------------------------------
# Merge, checks, preview
# ----------------------------------------------------------------------
def dedupe_and_sort(entries: list[dict]) -> list[dict]:
    seen, unique = set(), []
    for e in entries:
        key = (e["day"], e["start"], e["end"], e["subject"], e["kind"])
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return sorted(unique, key=lambda e: (DAYS.index(e["day"]), e["start"]))


def find_clashes(entries: list[dict]) -> list[str]:
    warnings = []
    for day in DAYS:
        todays = [e for e in entries if e["day"] == day]
        for a, b in zip(todays, todays[1:]):
            if b["start"] < a["end"]:
                warnings.append(f"{day}: '{a['subject']}' ({a['start']}-{a['end']}) "
                                f"overlaps '{b['subject']}' ({b['start']}-{b['end']})")
    return warnings


def preview(entries: list[dict]) -> None:
    print("\n========== YOUR TIMETABLE ==========")
    for day in DAYS:
        todays = [e for e in entries if e["day"] == day]
        if not todays:
            continue
        print(f"\n{day}")
        for i, e in enumerate(todays, 1):
            room = f" ({e['room']})" if e["room"] else ""
            print(f"  {i}. {e['start']}-{e['end']}  {e['subject']} [{e['kind']}]{room}")
    missing = [s for s in ALL_SUBJECTS if s not in {e["subject"] for e in entries}]
    print("\nSubjects with no classes found:", ", ".join(missing) if missing else "none")


# ----------------------------------------------------------------------
# Git
# ----------------------------------------------------------------------
def git_push() -> None:
    steps = [["git", "add", "timetable.json"],
             ["git", "commit", "-m", "Update timetable"],
             ["git", "push"]]
    for cmd in steps:
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0:
            if cmd[1] == "commit" and "nothing to commit" in out:
                print("No changes to commit, pushing anyway...")
                continue
            print(f"Git step failed: {' '.join(cmd)}\n{out}")
            return
    print("Pushed to GitHub.")


# ----------------------------------------------------------------------
def main() -> None:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        sys.exit("Run this first:  pip install google-genai")

    images = pick_images()
    if not images:
        sys.exit("No images selected.")

    client = genai.Client(api_key=get_api_key())

    all_entries: list[dict] = []
    for img in images:
        try:
            all_entries += extract_from_image(client, types, img)
        except Exception as e:  # noqa: BLE001
            print(f"  Could not read {img.name}: {e}")

    if not all_entries:
        sys.exit("\nNothing extracted. Try a clearer image.")

    replace = input("\nReplace existing timetable.json? (y = replace, n = add to it) [y]: ").strip().lower() != "n"
    if not replace and TIMETABLE_FILE.exists():
        all_entries += json.loads(TIMETABLE_FILE.read_text(encoding="utf-8"))

    final = dedupe_and_sort(all_entries)
    preview(final)

    clashes = find_clashes(final)
    if clashes:
        print("\nWARNING - possible clashes:")
        for c in clashes:
            print("  -", c)

    if input("\nSave this to timetable.json? [y/n]: ").strip().lower() != "y":
        sys.exit("Not saved.")

    TIMETABLE_FILE.write_text(json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8")
    print("Saved timetable.json")

    if input("Commit and push to GitHub now? [y/n]: ").strip().lower() == "y":
        git_push()


if __name__ == "__main__":
    main()