#!/usr/bin/env python3
"""Filter Moodle iCal feed — keep assignments as VTODO tasks, drop attendance junk.

Fetches a raw Moodle calendar export, filters out noise (attendance events),
converts assignments to VTODO tasks with due dates, prepends course codes to
event summaries, and writes a clean .ics file.

Run via cron every hour for automatic refresh.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Default patterns — override or extend via config.json
# ---------------------------------------------------------------------------

ASSIGNMENT_PATTERNS = [
    r"assignment", r"homework", r"hw\d", r"quiz", r"exam",
    r"midterm", r"final", r"submission", r"due", r"project",
    r"paper", r"essay", r"lab\s*\d", r"programming",
    r"problem\s*set", r"ps\d",
]

DROP_PATTERNS = [
    r"attendance", r"attenda",
]

# Common course-code prefixes. Add your institution's codes here or in config.
COURSE_CODE_PREFIXES = (
    r"COSI|LING|PHIL|MATH|CS|ANTH|HIST|PSYC|NEJS|LGCS"
    r"|BIOL|CHEM|PHYS|ECON|SOC|ENG|COMP|INFO|STAT"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def is_assignment(summary: str) -> bool:
    """Return True if the summary matches any assignment pattern."""
    lower = summary.lower()
    return any(re.search(p, lower) for p in ASSIGNMENT_PATTERNS)


def should_drop(summary: str) -> bool:
    """Return True if the event should be dropped entirely."""
    lower = summary.lower()
    return any(re.search(p, lower) for p in DROP_PATTERNS)


def convert_event_to_todo(event_lines: list[str]) -> list[str]:
    """Convert a VEVENT block to a VTODO with an all-day due date."""
    todo_lines = []
    for line in event_lines:
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            todo_lines.append("BEGIN:VTODO")
        elif stripped == "END:VEVENT":
            todo_lines.append("END:VTODO")
        elif line.startswith("DTEND"):
            # Use DTEND as the due date, converting to all-day (DATE only)
            dtend_val = line.split(":", 1)[1].strip()
            date_only = dtend_val[:8]  # YYYYMMDD
            todo_lines.append(f"DUE;VALUE=DATE:{date_only}")
        elif line.startswith("DTSTART"):
            continue  # Tasks only need a due date
        elif line.startswith("TRANSP:"):
            continue  # Not relevant for tasks
        else:
            todo_lines.append(line)
    return todo_lines


def prepend_course_name(event_lines: list[str]) -> list[str]:
    """Extract a course code from CATEGORIES/DESCRIPTION and prepend to SUMMARY."""
    summary_idx = None
    summary = ""
    description = ""
    categories = ""

    for i, line in enumerate(event_lines):
        if line.startswith("SUMMARY:"):
            summary_idx = i
            summary = line[8:]
        elif line.startswith("DESCRIPTION:"):
            description = line[12:]
        elif line.startswith("CATEGORIES:"):
            categories = line[11:]

    if summary_idx is None:
        return event_lines

    pattern = rf"({COURSE_CODE_PREFIXES})[\s_-]*(\d{{1,3}}[A-Za-z]?)"
    for text in [categories, description, summary]:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            course = f"{match.group(1).upper()} {match.group(2)}"
            if not summary.upper().startswith(course.upper()):
                event_lines[summary_idx] = f"SUMMARY:{course}: {summary}"
            break

    return event_lines


# ---------------------------------------------------------------------------
# Core filter
# ---------------------------------------------------------------------------


def filter_ical(raw: str, *, keep_events: bool = True, tasks_only: bool = False) -> str:
    """Parse raw iCal text and return filtered output.

    Args:
        raw: The raw iCal string from Moodle.
        keep_events: If True, non-assignment events (classes, etc.) are kept.
                     If False, only assignment tasks appear in the output.
        tasks_only: If True, output *only* VTODO items (no VEVENT at all).
    """
    lines = raw.splitlines()
    output: list[str] = []
    in_event = False
    event_lines: list[str] = []

    for line in lines:
        if line.strip() == "BEGIN:VEVENT":
            in_event = True
            event_lines = [line]
        elif line.strip() == "END:VEVENT":
            event_lines.append(line)
            in_event = False

            # Read summary
            summary = ""
            for el in event_lines:
                if el.startswith("SUMMARY:"):
                    summary = el[8:]
                    break

            # Drop attendance and other noise
            if should_drop(summary):
                continue

            # Prepend course code
            event_lines = prepend_course_name(event_lines)

            # Re-read summary after prepend
            for el in event_lines:
                if el.startswith("SUMMARY:"):
                    summary = el[8:]
                    break

            if is_assignment(summary):
                output.extend(convert_event_to_todo(event_lines))
            elif keep_events and not tasks_only:
                output.extend(event_lines)
        elif in_event:
            event_lines.append(line)
        else:
            output.append(line)

    return "\n".join(output)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(config_path: str | None = None) -> dict:
    """Load configuration from config.json, environment variables, or defaults.

    Priority: CLI args > environment variables > config.json > defaults.
    """
    config = {
        "moodle_url": "",
        "output": "moodle-filtered.ics",
    }

    # Load config file if it exists
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            file_config = json.load(f)
        config.update(file_config)

    # Environment variables override file config
    env_url = os.environ.get("MOODLE_CALENDAR_URL")
    if env_url:
        config["moodle_url"] = env_url
    env_output = os.environ.get("MOODLE_OUTPUT")
    if env_output:
        config["output"] = env_output

    # Load custom patterns from config if present
    global ASSIGNMENT_PATTERNS, DROP_PATTERNS, COURSE_CODE_PREFIXES
    if "assignment_patterns" in config:
        ASSIGNMENT_PATTERNS = config["assignment_patterns"]
    if "drop_patterns" in config:
        DROP_PATTERNS = config["drop_patterns"]
    if "course_code_prefixes" in config:
        COURSE_CODE_PREFIXES = config["course_code_prefixes"]

    return config


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Filter a Moodle iCal feed: drop attendance, convert assignments to tasks."
    )
    parser.add_argument(
        "--url",
        help="Moodle calendar export URL (overrides config/env)",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output .ics file path (default: moodle-filtered.ics)",
    )
    parser.add_argument(
        "--config",
        help="Path to config.json (default: config.json in script directory)",
    )
    parser.add_argument(
        "--keep-events",
        action="store_true",
        default=True,
        help="Keep non-assignment events like classes (default: True)",
    )
    parser.add_argument(
        "--no-keep-events",
        action="store_true",
        help="Drop non-assignment events — only assignments are output",
    )
    parser.add_argument(
        "--tasks-only",
        action="store_true",
        help="Output only VTODO items (assignments as tasks), no VEVENT",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    # CLI args take highest priority
    url = args.url or config["moodle_url"]
    output_path = Path(args.output or config["output"])
    keep_events = not args.no_keep_events
    tasks_only = args.tasks_only

    if not url:
        print("Error: No Moodle URL provided.", file=sys.stderr)
        print("Set it via --url, MOODLE_CALENDAR_URL env var, or config.json", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching Moodle calendar...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        print(f"Error fetching calendar: {e}", file=sys.stderr)
        sys.exit(1)

    events_before = raw.count("BEGIN:VEVENT")
    filtered = filter_ical(raw, keep_events=keep_events, tasks_only=tasks_only)
    events_after = filtered.count("BEGIN:VEVENT")
    tasks = filtered.count("BEGIN:VTODO")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(filtered)

    print(f"Done: {events_before} events → {events_after} events + {tasks} tasks → {output_path}")


if __name__ == "__main__":
    main()
