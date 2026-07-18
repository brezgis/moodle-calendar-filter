#!/usr/bin/env python3
"""Tests for filter-moodle-calendar.py -- stdlib unittest, no external deps.

Run: python3 test_filter.py
"""

import importlib.util
import os
import sys
import unittest

# Import the filter script by file path (it has a hyphenated name).
_script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "filter-moodle-calendar.py")
_spec = importlib.util.spec_from_file_location("filter_moodle_calendar", _script_path)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

# Pull out the names we need.
unfold_lines = _mod.unfold_lines
fold_line = _mod.fold_line
filter_ical = _mod.filter_ical
parse_property = _mod.parse_property
Property = _mod.Property
split_calendar = _mod.split_calendar
extract_course_code = _mod.extract_course_code
event_to_todo = _mod.event_to_todo
Component = _mod.Component


# ---------------------------------------------------------------------------
# Synthetic iCal fixtures
# ---------------------------------------------------------------------------

# A minimal but realistic Moodle-style calendar with:
#   1. A folded SUMMARY (long line broken per RFC 5545)
#   2. An assignment with only DTSTART (no DTEND)
#   3. An attendance event (should be dropped)
#   4. A normal class event (should be kept)
#   5. An assignment with DTEND (normal case)
SAMPLE_ICAL = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Moodle//NONSGML Moodle Calendar//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:event-1@moodle\r\n"
    "DTSTAMP:20260115T120000Z\r\n"
    # Folded SUMMARY: the second line starts with a space (continuation).
    "SUMMARY:CS 201a - Introduction to Data Structures and Algorithms - Homework\r\n"
    "  3 Essay on Amortized Analysis and Hash Table Implementations\r\n"
    "DESCRIPTION:Submit your essay on amortized analysis.\r\n"
    "DTSTART:20260201T235900Z\r\n"
    "DTEND:20260202T000000Z\r\n"
    "CATEGORIES:CS 201a\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:event-2@moodle\r\n"
    "DTSTAMP:20260115T120000Z\r\n"
    "SUMMARY:LING 220 - Final project submission\r\n"
    "DESCRIPTION:Upload your final project.\r\n"
    "DTSTART;VALUE=DATE:20260315\r\n"
    "CATEGORIES:LING 220\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:event-3@moodle\r\n"
    "DTSTAMP:20260115T120000Z\r\n"
    "SUMMARY:CS 201a - Attendance\r\n"
    "DESCRIPTION:Attendance check-in for today's lecture.\r\n"
    "DTSTART:20260120T140000Z\r\n"
    "DTEND:20260120T153000Z\r\n"
    "CATEGORIES:CS 201a\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:event-4@moodle\r\n"
    "DTSTAMP:20260115T120000Z\r\n"
    "SUMMARY:PHIL 101 - Lecture: Epistemology\r\n"
    "DESCRIPTION:Regular class session.\r\n"
    "DTSTART:20260122T100000Z\r\n"
    "DTEND:20260122T115000Z\r\n"
    "CATEGORIES:PHIL 101\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:event-5@moodle\r\n"
    "DTSTAMP:20260115T120000Z\r\n"
    "SUMMARY:MATH 220 - Quiz 2\r\n"
    "DESCRIPTION:In-class quiz.\r\n"
    "DTSTART;TZID=America/New_York:20260205T140000\r\n"
    "DTEND;TZID=America/New_York:20260205T150000\r\n"
    "CATEGORIES:MATH 220\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


class TestUnfoldLines(unittest.TestCase):
    """RFC 5545 line unfolding."""

    def test_basic_continuation(self):
        text = "SUMMARY:Hello\r\n World\r\nDTSTART:20260101\r\n"
        lines = unfold_lines(text)
        self.assertEqual(lines[0], "SUMMARY:HelloWorld")
        self.assertEqual(lines[1], "DTSTART:20260101")

    def test_tab_continuation(self):
        text = "SUMMARY:Hello\r\n\tWorld\r\n"
        lines = unfold_lines(text)
        self.assertEqual(lines[0], "SUMMARY:HelloWorld")

    def test_multi_continuation(self):
        text = "SUMMARY:A\r\n B\r\n C\r\nNEXT:val\r\n"
        lines = unfold_lines(text)
        self.assertEqual(lines[0], "SUMMARY:ABC")
        self.assertEqual(lines[1], "NEXT:val")

    def test_no_continuation(self):
        text = "SUMMARY:Short\r\nDTSTART:20260101\r\n"
        lines = unfold_lines(text)
        self.assertEqual(lines[0], "SUMMARY:Short")

    def test_lf_only_input(self):
        """Handles LF-only input (non-compliant but common)."""
        text = "SUMMARY:Hello\n World\nDTSTART:20260101\n"
        lines = unfold_lines(text)
        self.assertEqual(lines[0], "SUMMARY:HelloWorld")


class TestFoldLine(unittest.TestCase):
    """RFC 5545 line folding."""

    def test_short_line_unchanged(self):
        line = "SUMMARY:Short"
        self.assertEqual(fold_line(line), "SUMMARY:Short")

    def test_long_line_folded(self):
        line = "SUMMARY:" + "A" * 100
        folded = fold_line(line)
        raw_lines = folded.split("\r\n")
        # First line: at most 75 bytes.
        self.assertLessEqual(len(raw_lines[0].encode("utf-8")), 75)
        # Continuation lines start with a space.
        for cont in raw_lines[1:]:
            self.assertTrue(cont.startswith(" "), f"Continuation missing leading space: {cont!r}")
            # Content + space: at most 75 bytes.
            self.assertLessEqual(len(cont.encode("utf-8")), 75)

    def test_roundtrip(self):
        """Unfold(fold(line)) == line."""
        original = "SUMMARY:" + "X" * 200
        folded = fold_line(original)
        # Unfold by joining continuation lines.
        unfolded = unfold_lines(folded)
        self.assertEqual(len(unfolded), 1)
        self.assertEqual(unfolded[0], original)


class TestParseProperty(unittest.TestCase):

    def test_simple(self):
        prop = parse_property("SUMMARY:Hello World")
        self.assertEqual(prop.name, "SUMMARY")
        self.assertEqual(prop.params, {})
        self.assertEqual(prop.value, "Hello World")

    def test_with_params(self):
        prop = parse_property("DTSTART;TZID=America/New_York:20260115T090000")
        self.assertEqual(prop.name, "DTSTART")
        self.assertEqual(prop.params["TZID"], "America/New_York")
        self.assertEqual(prop.value, "20260115T090000")

    def test_value_date(self):
        prop = parse_property("DTSTART;VALUE=DATE:20260315")
        self.assertEqual(prop.name, "DTSTART")
        self.assertEqual(prop.params["VALUE"], "DATE")
        self.assertEqual(prop.value, "20260315")

    def test_emit_roundtrip(self):
        line = "DTSTART;TZID=America/New_York:20260115T090000"
        prop = parse_property(line)
        self.assertEqual(prop.emit(), line)


class TestFoldedSummaryDetection(unittest.TestCase):
    """Bug #1: folded summaries must be unfolded before course-code extraction."""

    def test_folded_summary_course_detected(self):
        """A folded SUMMARY should have its course code detected and prepended."""
        result, stats = filter_ical(SAMPLE_ICAL)
        # Event 1 has a folded SUMMARY with "CS 201a" -- it's an assignment.
        # The full unfolded summary mentions "Homework" -> assignment pattern.
        # It should become a VTODO with the course code prepended.
        self.assertGreater(stats.tasks_created, 0, "No tasks created -- folded summary not detected?")
        # The course code should appear in the output.
        self.assertIn("CS 201", result)

    def test_folded_summary_full_text_preserved(self):
        """After unfolding, the full summary text should be in the output."""
        result, stats = filter_ical(SAMPLE_ICAL)
        # The folded continuation "3 Essay on Amortized Analysis" should be joined.
        self.assertIn("Amortized Analysis", result)


class TestDtStartOnlyDueDate(unittest.TestCase):
    """Bug #2: events with only DTSTART must still produce a VTODO with DUE."""

    def test_dtstart_only_produces_due(self):
        """Event 2 has DTSTART;VALUE=DATE:20260315 but no DTEND."""
        result, stats = filter_ical(SAMPLE_ICAL)
        # LING 220 final project should be a task.
        self.assertIn("LING 220", result)
        # It must have a DUE property.
        self.assertIn("DUE", result)
        # The date should be 20260315.
        self.assertIn("20260315", result)

    def test_dtend_preferred_over_dtstart(self):
        """Event 5 has both DTSTART and DTEND -- DUE should use DTEND."""
        result, stats = filter_ical(SAMPLE_ICAL)
        # MATH 220 Quiz 2 has DTEND on 20260205T150000 with TZID.
        self.assertIn("MATH 220", result)
        self.assertIn("20260205T150000", result)

    def test_value_date_preserved(self):
        """VALUE=DATE parameter should be preserved on DUE."""
        result, stats = filter_ical(SAMPLE_ICAL)
        # Event 2 has VALUE=DATE -- the DUE should also have VALUE=DATE.
        self.assertIn("DUE;VALUE=DATE:20260315", result)

    def test_tzid_preserved(self):
        """TZID parameter should be preserved on DUE."""
        result, stats = filter_ical(SAMPLE_ICAL)
        self.assertIn("TZID=America/New_York", result)

    def test_vtodo_has_status(self):
        """VTODOs should have STATUS:NEEDS-ACTION."""
        result, stats = filter_ical(SAMPLE_ICAL)
        self.assertIn("STATUS:NEEDS-ACTION", result)

    def test_vtodo_preserves_uid(self):
        """VTODOs should preserve their UID from the source event."""
        result, stats = filter_ical(SAMPLE_ICAL)
        self.assertIn("UID:event-2@moodle", result)

    def test_vtodo_preserves_dtstamp(self):
        """VTODOs should preserve DTSTAMP."""
        result, stats = filter_ical(SAMPLE_ICAL)
        self.assertIn("DTSTAMP:20260115T120000Z", result)


class TestAttendanceDropped(unittest.TestCase):
    """Bug/feature: attendance events must be dropped."""

    def test_attendance_not_in_output(self):
        result, stats = filter_ical(SAMPLE_ICAL)
        self.assertNotIn("event-3@moodle", result)
        self.assertEqual(stats.dropped, 1)

    def test_attendance_summary_not_in_output(self):
        result, _ = filter_ical(SAMPLE_ICAL)
        self.assertNotIn("Attendance check-in", result)


class TestCRLFOutput(unittest.TestCase):
    """Bug #3: output must use CRLF line endings."""

    def test_output_uses_crlf(self):
        result, _ = filter_ical(SAMPLE_ICAL)
        # Every line should end with \r\n.
        # Split on \r\n and check there are no bare \n.
        without_crlf = result.replace("\r\n", "")
        self.assertNotIn("\n", without_crlf,
                         "Found bare LF in output -- should be CRLF only")

    def test_output_not_empty(self):
        result, _ = filter_ical(SAMPLE_ICAL)
        self.assertTrue(len(result) > 0)

    def test_folded_lines_within_limit(self):
        """All raw lines in the output should be at most 75 octets."""
        result, _ = filter_ical(SAMPLE_ICAL)
        raw_lines = result.split("\r\n")
        for line in raw_lines:
            self.assertLessEqual(
                len(line.encode("utf-8")), 75,
                f"Line exceeds 75 octets: {line!r}"
            )


class TestKeepEventsFlag(unittest.TestCase):
    """Bug #4: --keep-events / --no-keep-events / --tasks-only logic."""

    def test_default_keeps_non_assignment_events(self):
        result, stats = filter_ical(SAMPLE_ICAL, keep_events=True)
        # PHIL 101 lecture should be kept.
        self.assertIn("PHIL 101", result)
        self.assertIn("BEGIN:VEVENT", result)
        self.assertGreater(stats.kept_events, 0)

    def test_no_keep_events_drops_non_assignments(self):
        result, stats = filter_ical(SAMPLE_ICAL, keep_events=False)
        # PHIL 101 lecture should NOT be in output.
        self.assertNotIn("PHIL 101", result)
        self.assertNotIn("BEGIN:VEVENT", result)
        self.assertEqual(stats.kept_events, 0)
        # But tasks should still be there.
        self.assertGreater(stats.tasks_created, 0)

    def test_tasks_only(self):
        result, stats = filter_ical(SAMPLE_ICAL, tasks_only=True)
        self.assertNotIn("BEGIN:VEVENT", result)
        self.assertNotIn("PHIL 101", result)
        self.assertEqual(stats.kept_events, 0)
        self.assertIn("BEGIN:VTODO", result)


class TestCalendarMetadata(unittest.TestCase):
    """PRODID and X-WR-CALNAME injection."""

    def test_prodid_present(self):
        result, _ = filter_ical(SAMPLE_ICAL)
        self.assertIn("PRODID:-//brezgis//moodle-calendar-filter//EN", result)

    def test_calname_present(self):
        result, _ = filter_ical(SAMPLE_ICAL)
        self.assertIn("X-WR-CALNAME:Moodle (filtered)", result)

    def test_original_prodid_replaced(self):
        result, _ = filter_ical(SAMPLE_ICAL)
        self.assertNotIn("Moodle//NONSGML", result)


class TestFilterStats(unittest.TestCase):
    """End-of-run report statistics."""

    def test_stats_counts(self):
        _, stats = filter_ical(SAMPLE_ICAL)
        self.assertEqual(stats.total_events, 5)
        self.assertEqual(stats.dropped, 1)
        # 3 assignments (event 1 homework, event 2 submission, event 5 quiz)
        # 1 kept event (event 4 lecture)
        self.assertEqual(stats.tasks_created, 3)
        self.assertEqual(stats.kept_events, 1)

    def test_task_courses_breakdown(self):
        _, stats = filter_ical(SAMPLE_ICAL)
        self.assertIn("CS 201a", stats.task_courses)
        self.assertIn("LING 220", stats.task_courses)
        self.assertIn("MATH 220", stats.task_courses)


class TestEventToTodo(unittest.TestCase):
    """Unit tests for the event-to-todo transform."""

    def test_dtstart_only(self):
        props = [
            Property("UID", {}, "test-uid"),
            Property("DTSTAMP", {}, "20260101T000000Z"),
            Property("SUMMARY", {}, "Assignment due"),
            Property("DTSTART", {"VALUE": "DATE"}, "20260315"),
        ]
        comp = Component("VEVENT", props)
        todo = event_to_todo(comp)
        self.assertEqual(todo.kind, "VTODO")
        due = todo.get("DUE")
        self.assertIsNotNone(due, "VTODO missing DUE property")
        self.assertEqual(due.value, "20260315")
        self.assertEqual(due.params.get("VALUE"), "DATE")
        # UID preserved.
        self.assertEqual(todo.get_value("UID"), "test-uid")
        # STATUS added.
        self.assertEqual(todo.get_value("STATUS"), "NEEDS-ACTION")
        # No DTSTART in output.
        self.assertIsNone(todo.get("DTSTART"))

    def test_dtend_and_dtstart(self):
        props = [
            Property("UID", {}, "test-uid"),
            Property("DTSTAMP", {}, "20260101T000000Z"),
            Property("SUMMARY", {}, "Quiz"),
            Property("DTSTART", {"TZID": "America/New_York"}, "20260205T140000"),
            Property("DTEND", {"TZID": "America/New_York"}, "20260205T150000"),
        ]
        comp = Component("VEVENT", props)
        todo = event_to_todo(comp)
        due = todo.get("DUE")
        self.assertIsNotNone(due)
        # Should use DTEND.
        self.assertEqual(due.value, "20260205T150000")
        self.assertEqual(due.params.get("TZID"), "America/New_York")


if __name__ == "__main__":
    unittest.main()
