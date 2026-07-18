#!/usr/bin/env python3
"""Filter Moodle iCal feed -- keep assignments as VTODO tasks, drop attendance noise.

Fetches a raw Moodle calendar export, filters out noise (attendance events),
converts assignments to VTODO tasks with due dates, prepends course codes to
event summaries, and writes a clean .ics file.

Pipeline: fetch -> unfold -> split components -> classify/transform -> refold -> emit CRLF.

Run via cron every hour for automatic refresh.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------------------
# Default patterns -- override or extend via config.json
# ---------------------------------------------------------------------------

ASSIGNMENT_PATTERNS: list[str] = [
    r"assignment", r"homework", r"hw\d", r"quiz", r"exam",
    r"midterm", r"final", r"submission", r"due", r"project",
    r"paper", r"essay", r"lab\s*\d", r"programming",
    r"problem\s*set", r"ps\d",
]

DROP_PATTERNS: list[str] = [
    r"attendance", r"attenda",
]

# Common course-code department prefixes. Add your institution's codes in config.
COURSE_CODE_PREFIXES: str = (
    r"CS|LING|PHIL|MATH|ANTH|HIST|PSYC"
    r"|BIOL|CHEM|PHYS|ECON|SOC|ENG|COMP|INFO|STAT"
)


# ---------------------------------------------------------------------------
# RFC 5545 folding / unfolding
# ---------------------------------------------------------------------------

def unfold_lines(text: str) -> list[str]:
    """Join RFC 5545 continuation lines (leading space or tab) to their parent.

    Per RFC 5545 sec 3.1, long lines are folded by inserting a CRLF followed by
    a single whitespace character. The continuation line starts with that
    whitespace. We must join these before parsing property values.
    """
    # Normalise line endings to LF for splitting, then rejoin continuations.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = text.split("\n")
    result: list[str] = []
    for line in raw_lines:
        if line.startswith((" ", "\t")) and result:
            # Continuation: strip the leading whitespace char and append.
            result[-1] += line[1:]
        else:
            result.append(line)
    return result


def fold_line(line: str, max_octets: int = 75) -> str:
    """Fold a single content line to at most max_octets per line (RFC 5545).

    The first line gets the full budget. Each continuation line is indented
    with one space, so its content budget is max_octets - 1.
    Returns the folded line(s) joined with CRLF. Does NOT include a trailing CRLF.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= max_octets:
        return line

    pieces: list[str] = []
    # First chunk: up to max_octets bytes.
    first = _safe_cut(encoded, max_octets)
    pieces.append(first.decode("utf-8"))
    encoded = encoded[len(first):]

    # Subsequent chunks: each preceded by a space, so content budget is max_octets - 1.
    content_budget = max_octets - 1
    while encoded:
        chunk = _safe_cut(encoded, content_budget)
        pieces.append(" " + chunk.decode("utf-8"))
        encoded = encoded[len(chunk):]

    return "\r\n".join(pieces)


def _safe_cut(data: bytes, limit: int) -> bytes:
    """Cut bytes at limit without splitting a multi-byte UTF-8 character."""
    if limit >= len(data):
        return data
    # Walk backward from limit to find a valid UTF-8 char boundary.
    while limit > 0 and (data[limit] & 0xC0) == 0x80:
        limit -= 1
    return data[:limit]


# ---------------------------------------------------------------------------
# iCal property parsing
# ---------------------------------------------------------------------------

class Property:
    """A single iCal content line parsed into name, parameters, and value.

    Example: DTSTART;TZID=America/New_York:20260115T090000
      -> name="DTSTART", params={"TZID": "America/New_York"}, value="20260115T090000"
    """
    __slots__ = ("name", "params", "value")

    def __init__(self, name: str, params: dict[str, str], value: str):
        self.name = name
        self.params = dict(params)
        self.value = value

    def emit(self) -> str:
        """Re-serialize to a content line (without CRLF, without folding)."""
        if self.params:
            param_str = ";" + ";".join(f"{k}={v}" for k, v in self.params.items())
        else:
            param_str = ""
        return f"{self.name}{param_str}:{self.value}"


def parse_property(line: str) -> Property:
    """Parse an unfolded iCal content line into a Property."""
    # Split at the first unquoted colon to separate name+params from value.
    # Parameters can contain colons inside quoted strings, but Moodle doesn't
    # emit those in practice, so a simple split is safe here.
    colon_idx = _find_value_colon(line)
    head = line[:colon_idx]
    value = line[colon_idx + 1:]

    # Split head into property name and parameters.
    parts = head.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            pk, pv = part.split("=", 1)
            params[pk.upper()] = pv
        else:
            params[part.upper()] = ""
    return Property(name, params, value)


def _find_value_colon(line: str) -> int:
    """Find the colon that separates property name+params from the value.

    Skips colons inside double-quoted parameter values.
    """
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ':' and not in_quotes:
            return i
    # Fallback: if no colon found (shouldn't happen in valid iCal), return len.
    return len(line)


# ---------------------------------------------------------------------------
# Component extraction
# ---------------------------------------------------------------------------

class Component:
    """A VEVENT, VTIMEZONE, or other iCal component as a list of Property objects."""
    __slots__ = ("kind", "properties")

    def __init__(self, kind: str, properties: list[Property]):
        self.kind = kind
        self.properties = properties

    def get(self, name: str) -> Property | None:
        """Return the first property with the given name, or None."""
        name_upper = name.upper()
        for prop in self.properties:
            if prop.name == name_upper:
                return prop
        return None

    def get_value(self, name: str) -> str:
        """Return the value of the first property with the given name, or ""."""
        prop = self.get(name)
        return prop.value if prop else ""


def split_calendar(lines: list[str]) -> tuple[list[str], list[Component], list[str]]:
    """Split unfolded iCal lines into preamble, components, and trailer.

    Preamble: lines from BEGIN:VCALENDAR up to (but not including) the first
    BEGIN:V* component. Trailer: lines from after the last END:V* component
    through END:VCALENDAR.
    """
    preamble: list[str] = []
    trailer: list[str] = []
    components: list[Component] = []

    current_lines: list[str] | None = None
    current_kind: str | None = None
    past_last_component = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("BEGIN:") and stripped != "BEGIN:VCALENDAR":
            kind = stripped.split(":", 1)[1]
            # VTIMEZONE contains sub-components (STANDARD, DAYLIGHT). We treat
            # the whole VTIMEZONE as one opaque block -- its internal BEGIN/END
            # pairs are just lines within it.
            if current_lines is None or kind in ("VEVENT", "VTODO", "VTIMEZONE", "VJOURNAL", "VFREEBUSY"):
                current_kind = kind
                current_lines = [line]
                past_last_component = False
                continue
            # Sub-component inside a VTIMEZONE etc.: accumulate as raw line.
            if current_lines is not None:
                current_lines.append(line)
                continue
        elif stripped.startswith("END:") and stripped != "END:VCALENDAR":
            end_kind = stripped.split(":", 1)[1]
            if current_lines is not None:
                current_lines.append(line)
                if end_kind == current_kind:
                    # Parse properties (skip BEGIN/END wrapper lines).
                    props = [parse_property(l) for l in current_lines[1:-1]
                             if l.strip() and not l.strip().startswith("BEGIN:") and not l.strip().startswith("END:")]
                    # For VTIMEZONE, keep the raw lines as-is (sub-components).
                    if current_kind == "VTIMEZONE":
                        props = [Property("_RAW_LINE", {}, l) for l in current_lines[1:-1]]
                    components.append(Component(current_kind, props))
                    current_lines = None
                    current_kind = None
                    past_last_component = True
                continue
        elif current_lines is not None:
            current_lines.append(line)
            continue

        # Outside any component.
        if past_last_component:
            trailer.append(line)
        else:
            preamble.append(line)

    return preamble, components, trailer


# ---------------------------------------------------------------------------
# Classification and transformation
# ---------------------------------------------------------------------------

def matches_any(text: str, patterns: list[str]) -> bool:
    """Return True if text matches any of the given regex patterns (case-insensitive)."""
    lower = text.lower()
    return any(re.search(p, lower) for p in patterns)


def extract_course_code(component: Component) -> str | None:
    """Try to extract a course code like 'CS 201a' from categories, description, or summary."""
    pattern = rf"({COURSE_CODE_PREFIXES})[\s_-]*(\d{{1,3}}[A-Za-z]?)"
    for prop_name in ("CATEGORIES", "DESCRIPTION", "SUMMARY"):
        text = component.get_value(prop_name)
        if text:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return f"{match.group(1).upper()} {match.group(2)}"
    return None


def prepend_course_code(component: Component, course: str) -> None:
    """Prepend course code to SUMMARY if not already present."""
    summary_prop = component.get("SUMMARY")
    if summary_prop is None:
        return
    if not summary_prop.value.upper().startswith(course.upper()):
        summary_prop.value = f"{course}: {summary_prop.value}"


def extract_due_date(component: Component) -> str | None:
    """Derive a DUE value string from DTEND or DTSTART.

    Returns a (param_string, value) suitable for emitting as a DUE property.
    Prefers DTEND; falls back to DTSTART. Handles VALUE=DATE (all-day) and
    date-time (with or without TZID).
    """
    for prop_name in ("DTEND", "DTSTART"):
        prop = component.get(prop_name)
        if prop is not None and prop.value:
            return prop
    return None


def event_to_todo(component: Component) -> Component:
    """Transform a VEVENT Component into a VTODO Component with a DUE date."""
    new_props: list[Property] = []
    due_emitted = False

    # Determine the due-date source property.
    due_source = extract_due_date(component)

    for prop in component.properties:
        if prop.name == "DTEND":
            if not due_emitted and due_source is not None:
                # Emit DUE with the same params (VALUE=DATE, TZID, etc.).
                new_props.append(Property("DUE", dict(due_source.params), due_source.value))
                due_emitted = True
            # Either way, skip the original DTEND.
        elif prop.name == "DTSTART":
            if not due_emitted and due_source is not None and due_source.name == "DTSTART":
                # DTSTART is our only date source. Emit DUE from it.
                new_props.append(Property("DUE", dict(due_source.params), due_source.value))
                due_emitted = True
            # Skip DTSTART regardless (tasks don't need a start date).
        elif prop.name == "TRANSP":
            continue  # Not meaningful for tasks.
        else:
            new_props.append(prop)

    # If we never encountered DTEND or DTSTART in the loop (unusual), emit DUE now.
    if not due_emitted and due_source is not None:
        new_props.append(Property("DUE", dict(due_source.params), due_source.value))

    # Add STATUS:NEEDS-ACTION for task tracking.
    new_props.append(Property("STATUS", {}, "NEEDS-ACTION"))

    return Component("VTODO", new_props)


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

PRODID = "-//brezgis//moodle-calendar-filter//EN"
CALNAME = "Moodle (filtered)"


def emit_component(comp: Component) -> list[str]:
    """Emit a component as a list of folded content lines (no CRLF yet)."""
    lines = [f"BEGIN:{comp.kind}"]
    for prop in comp.properties:
        if prop.name == "_RAW_LINE":
            # VTIMEZONE raw lines -- pass through as-is.
            lines.append(prop.value)
        else:
            lines.append(fold_line(prop.emit()))
    lines.append(f"END:{comp.kind}")
    return lines


def emit_calendar(preamble: list[str], components: list[Component],
                  trailer: list[str]) -> str:
    """Assemble the final calendar text with CRLF line endings.

    Injects PRODID and X-WR-CALNAME into the preamble for nicer display in apps.
    """
    out_lines: list[str] = []

    for line in preamble:
        folded = fold_line(line)
        out_lines.append(folded)
        # Inject our PRODID and calendar name right after BEGIN:VCALENDAR.
        if line.strip() == "BEGIN:VCALENDAR":
            out_lines.append(fold_line(f"PRODID:{PRODID}"))
            out_lines.append(fold_line(f"X-WR-CALNAME:{CALNAME}"))

    for comp in components:
        out_lines.extend(emit_component(comp))

    for line in trailer:
        out_lines.append(fold_line(line))

    return "\r\n".join(out_lines) + "\r\n"


# ---------------------------------------------------------------------------
# Core filter pipeline
# ---------------------------------------------------------------------------

class FilterStats:
    """Tracks what happened during filtering for the end-of-run report."""
    __slots__ = ("total_events", "dropped", "kept_events", "tasks_created", "task_courses")

    def __init__(self):
        self.total_events = 0
        self.dropped = 0
        self.kept_events = 0
        self.tasks_created = 0
        self.task_courses: Counter[str] = Counter()


def filter_ical(raw: str, *, keep_events: bool = True, tasks_only: bool = False) -> tuple[str, FilterStats]:
    """Parse raw iCal text and return (filtered_output, stats).

    Pipeline: unfold -> split -> classify/transform -> refold -> CRLF.

    Args:
        raw: The raw iCal string from Moodle.
        keep_events: If True, non-assignment events are preserved as VEVENTs.
        tasks_only: If True, emit only VTODOs (no VEVENTs at all).
    """
    stats = FilterStats()

    # Step 1: Unfold continuation lines.
    lines = unfold_lines(raw)

    # Step 2: Split into structural parts.
    preamble, components, trailer = split_calendar(lines)

    # Remove any existing PRODID from preamble (we inject our own).
    preamble = [l for l in preamble if not l.strip().upper().startswith("PRODID:")]

    # Step 3: Classify and transform each component.
    output_components: list[Component] = []
    for comp in components:
        if comp.kind != "VEVENT":
            # Pass through VTIMEZONE etc. unchanged.
            output_components.append(comp)
            continue

        stats.total_events += 1
        summary = comp.get_value("SUMMARY")

        # Drop noise (attendance etc.).
        if matches_any(summary, DROP_PATTERNS):
            stats.dropped += 1
            continue

        # Extract and prepend course code.
        course = extract_course_code(comp)
        if course:
            prepend_course_code(comp, course)

        # Re-read summary after possible course-code prepend.
        summary = comp.get_value("SUMMARY")

        if matches_any(summary, ASSIGNMENT_PATTERNS):
            # Convert to VTODO task.
            todo = event_to_todo(comp)
            output_components.append(todo)
            stats.tasks_created += 1
            stats.task_courses[course or "(no course)"] += 1
        elif keep_events and not tasks_only:
            output_components.append(comp)
            stats.kept_events += 1
        # else: non-assignment event dropped by --no-keep-events or --tasks-only.

    # Step 4: Emit with folding + CRLF.
    result = emit_calendar(preamble, output_components, trailer)
    return result, stats


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def load_config(config_path: str | None = None) -> dict:
    """Load configuration with precedence: CLI args > env vars > config.json > defaults."""
    config: dict = {
        "moodle_url": "",
        "output": "moodle-filtered.ics",
    }

    # Load config file if it exists.
    if config_path is None:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            file_config = json.load(f)
        config.update(file_config)

    # Environment variables override file config.
    env_url = os.environ.get("MOODLE_CALENDAR_URL")
    if env_url:
        config["moodle_url"] = env_url
    env_output = os.environ.get("MOODLE_OUTPUT")
    if env_output:
        config["output"] = env_output

    # Apply custom patterns from config if present.
    global ASSIGNMENT_PATTERNS, DROP_PATTERNS, COURSE_CODE_PREFIXES
    if "assignment_patterns" in config:
        ASSIGNMENT_PATTERNS = config["assignment_patterns"]
    if "drop_patterns" in config:
        DROP_PATTERNS = config["drop_patterns"]
    if "course_code_prefixes" in config:
        COURSE_CODE_PREFIXES = config["course_code_prefixes"]

    return config


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------


def format_report(stats: FilterStats, output_path: Path, *, fancy: bool = False) -> str:
    """Build the end-of-run summary.

    fancy=True (TTY) gets a friendlier layout with some personality.
    fancy=False (cron/pipe) gets a compact single-line summary.
    """
    if not fancy:
        parts = [
            f"in={stats.total_events}",
            f"dropped={stats.dropped}",
            f"events={stats.kept_events}",
            f"tasks={stats.tasks_created}",
            f"out={output_path}",
        ]
        return "done: " + " ".join(parts)

    lines = [
        f"  {stats.total_events} events in",
        f"  {stats.dropped} dropped (noise)",
        f"  {stats.kept_events} kept as events",
        f"  {stats.tasks_created} converted to tasks",
    ]
    if stats.task_courses:
        lines.append("")
        lines.append("  tasks by course:")
        for course, count in stats.task_courses.most_common():
            lines.append(f"    {course}: {count}")
    lines.append("")
    lines.append(f"  written to {output_path}")
    return "\n".join(lines)


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
    # --keep-events / --no-keep-events as a proper boolean pair.
    parser.add_argument(
        "--keep-events",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep non-assignment events (default: yes). Use --no-keep-events to drop them.",
    )
    parser.add_argument(
        "--tasks-only",
        action="store_true",
        help="Output only VTODO items (assignments as tasks), no VEVENT",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress the summary report",
    )
    args = parser.parse_args()

    config = load_config(args.config)

    # CLI args take highest priority.
    url = args.url or config["moodle_url"]
    output_path = Path(args.output or config["output"])
    keep_events: bool = args.keep_events
    tasks_only: bool = args.tasks_only

    if not url:
        print("Error: No Moodle URL provided.", file=sys.stderr)
        print("Set it via --url, MOODLE_CALENDAR_URL env var, or config.json", file=sys.stderr)
        sys.exit(1)

    # Fetch the calendar.
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        print(f"Error fetching calendar: {e}", file=sys.stderr)
        sys.exit(1)

    # Filter.
    filtered, stats = filter_ical(raw, keep_events=keep_events, tasks_only=tasks_only)

    # Write output.
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(filtered.encode("utf-8"))

    # Report.
    if not args.quiet:
        fancy = sys.stdout.isatty()
        print(format_report(stats, output_path, fancy=fancy))


if __name__ == "__main__":
    main()
