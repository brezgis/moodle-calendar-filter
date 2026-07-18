# moodle-calendar-filter

A Python script that cleans up Moodle's iCal calendar feed -- it strips out attendance events, converts assignments into tasks (VTODO), prepends course codes to event names, and produces a clean `.ics` file you can subscribe to from any calendar app. No dependencies beyond Python 3.10+.

## Why

Moodle's calendar export is noisy. Every attendance check-in creates an event, assignments look the same as lectures, and there's no way to see *just* what's due. This script fetches your Moodle calendar, throws out the clutter, and gives you a focused feed where assignments show up as **tasks with due dates** and class events stay as regular calendar events.

## Quick Start

```bash
# 1. Clone
git clone https://github.com/brezgis/moodle-calendar-filter.git
cd moodle-calendar-filter

# 2. Configure
cp config.example.json config.json
# Edit config.json with your Moodle calendar URL (see Configuration below)

# 3. Run
python3 filter-moodle-calendar.py
```

Your filtered calendar is now at `moodle-filtered.ics`.

## Configuration

The script reads settings from three sources (highest priority first):

1. **Command-line arguments** -- `--url`, `--output`, `--tasks-only`, etc.
2. **Environment variables** -- `MOODLE_CALENDAR_URL`, `MOODLE_OUTPUT`
3. **`config.json`** -- all settings in one file

### Getting Your Moodle Calendar URL

1. Log in to your Moodle instance
2. Go to **Calendar** -> **Export calendar**
3. Choose what to export (e.g. **Events related to courses**) and a time period (e.g. **Recent and next 60 days**, or **Custom range**)
4. Click **Get calendar URL** -- copy the full URL (it contains your personal auth token, so treat it like a password)
5. Paste it into `config.json` as `moodle_url`

### Command-Line Arguments

```
--url URL              Moodle calendar export URL
--output, -o PATH      Output .ics file (default: moodle-filtered.ics)
--config PATH          Path to config.json
--keep-events          Keep class events alongside tasks (default)
--no-keep-events       Only output assignments -- drop class events
--tasks-only           Output only VTODO items, no VEVENT at all
--quiet, -q            Suppress the summary report
```

### config.json

```json
{
    "moodle_url": "https://your-moodle.edu/calendar/export_execute.php?userid=XXXXX&authtoken=YYYYY&preset_what=courses&preset_time=custom",
    "output": "moodle-filtered.ics"
}
```

You can also add custom filter patterns -- see [Customization](#customization).

## How It Works

The script processes a Moodle iCal export through a pipeline:

1. **Unfold** -- join RFC 5545 continuation lines (long lines Moodle splits across multiple lines)
2. **Parse** -- split the calendar into components, parse each property into name/params/value
3. **Classify** -- drop noise (attendance), detect assignments, extract course codes
4. **Transform** -- convert assignment events into VTODO tasks with due dates; prepend course codes to summaries
5. **Emit** -- re-fold long lines to 75 octets, write with CRLF line endings per the iCal spec

| Step | What happens |
|------|-------------|
| **Drop noise** | Events matching `DROP_PATTERNS` (attendance, etc.) are removed entirely |
| **Detect assignments** | Events matching `ASSIGNMENT_PATTERNS` (homework, quiz, exam, due, etc.) are flagged |
| **Prepend course codes** | A course code (e.g., `LING 220`, `CS 201a`) is extracted from categories/description/summary and prepended |
| **Convert to tasks** | Assignment events become `VTODO` items with due dates and `STATUS:NEEDS-ACTION` |
| **Keep the rest** | Non-assignment events (lectures, office hours) pass through as regular `VEVENT` entries |

### Due date handling

The script derives the `DUE` property from `DTEND` if present, otherwise from `DTSTART`. Both `VALUE=DATE` (all-day) and date-time forms (with or without `TZID`) are preserved as-is. This handles Moodle's inconsistency where some assignment-due events only have `DTSTART`.

### Output format

The output `.ics` file is RFC 5545 compliant: CRLF line endings, lines folded at 75 octets, proper `PRODID`, and a `X-WR-CALNAME` so calendar apps display a friendly name ("Moodle (filtered)").

## Calendar App Setup

Once you have a filtered `.ics` file, point your calendar app at it.

### BusyCal

BusyCal can subscribe to a local file, which is ideal for cron-refreshed calendars:

1. **File -> Subscribe...**
2. Enter the file path as a URL: `file:///path/to/moodle-filtered.ics`
3. Set refresh to **Every hour** (or let cron handle it and set to **Manually**)
4. Tasks from VTODO entries appear in BusyCal's task list with due dates

### Apple Calendar

**Option A -- Subscribe (recommended):**
1. **File -> New Calendar Subscription...**
2. Enter `file:///path/to/moodle-filtered.ics`
3. Set auto-refresh interval

**Option B -- Import (one-time):**
1. **File -> Import...** -> select the `.ics` file
2. Note: imported events won't update automatically

Apple Calendar shows VTODO tasks in the Reminders integration. Make sure Reminders is enabled in System Settings.

### Google Calendar

Google Calendar can't read local files, so you'll need to host the `.ics` somewhere accessible:

1. Host the file on a web server, Dropbox (public link), or a simple HTTP server
2. In Google Calendar: **Settings -> Add calendar -> From URL**
3. Paste the URL to your hosted `.ics` file
4. Google refreshes subscribed calendars every 12-24 hours

Google Calendar does not support VTODO items natively. Use `--tasks-only` to skip events, or stick with VEVENT output if you're using Google Calendar exclusively.

### Outlook

1. **File -> Open & Export -> Import/Export**
2. Select **Import an iCalendar (.ics) file**
3. Browse to `moodle-filtered.ics`
4. Choose **Open as New Calendar** to keep it separate

For Outlook on the web: **Add calendar -> Subscribe from web** with a hosted URL.

## Cron Setup

Auto-refresh your calendar every hour:

```bash
# Edit your crontab
crontab -e

# Add this line (adjust paths):
0 * * * * cd /path/to/moodle-calendar-filter && python3 filter-moodle-calendar.py >> /tmp/moodle-filter.log 2>&1
```

Or use the included setup helper:

```bash
chmod +x setup.sh
./setup.sh
```

When running under cron (non-TTY), the summary is a compact single line. In a terminal you get a friendlier breakdown with per-course task counts.

## Testing

```bash
python3 test_filter.py
```

Tests cover line unfolding/folding, property parsing, DTSTART-only due date derivation, attendance dropping, CRLF output, keep/no-keep/tasks-only flag logic, and course code extraction. Stdlib `unittest` only, no external dependencies.

## Customization

### Adding Filter Patterns

Edit `config.json` to add your own patterns:

```json
{
    "moodle_url": "https://...",
    "output": "moodle-filtered.ics",
    "assignment_patterns": [
        "assignment", "homework", "hw\\d", "quiz", "exam",
        "midterm", "final", "submission", "due", "project",
        "paper", "essay", "lab\\s*\\d", "programming",
        "problem\\s*set", "ps\\d",
        "response paper", "reading response"
    ],
    "drop_patterns": [
        "attendance", "attenda",
        "check-in"
    ],
    "course_code_prefixes": "CS|LING|PHIL|MATH|ANTH|BIOL|CHEM|ECON"
}
```

Patterns are Python regular expressions matched against the lowercased event summary, so write them in lowercase.

### Adding Course Code Prefixes

The script looks for course codes like `CS 201a` or `LING-220` in event metadata. If your institution uses different department codes, add them to `course_code_prefixes` in your config (pipe-separated):

```json
{
    "course_code_prefixes": "CS|MATH|ENG|HIST|BIO|PHYS"
}
```

## License

MIT -- see [LICENSE](LICENSE).
