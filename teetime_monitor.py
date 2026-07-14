#!/usr/bin/env python3
"""
MCG Golf / TenFore tee time monitor.

Polls the TenFore booking API for your target course(s) and notifies you
(via Pushover) when a new tee time appears inside your desired daily time
window. Designed to be run on a schedule (cron) rather than continuously.

Setup:
    1. pip install requests --break-system-packages   (or use a venv)
    2. Copy config.example.env to config.env and fill in your values
    3. Test manually:  python3 tee_time_monitor.py
    4. Add to cron once it's working (see README.md)
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment variables / config.env)
# ---------------------------------------------------------------------------

BASE_URL = "https://swan.tenfore.golf/api"
LOGIN_URL = f"{BASE_URL}/Auth/Login"
SEARCH_URL = f"{BASE_URL}/TeeTimes/Search"
APP_ID = os.environ.get("TENFORE_APP_ID", "23")  # seen in X-Tenfore-Appid header

MCG_EMAIL = os.environ.get("MCG_EMAIL")
MCG_PASSWORD = os.environ.get("MCG_PASSWORD")
# The login payload included a golfCourseID even though you're logging into
# a shared account - keeping it configurable in case it's required.
MCG_LOGIN_COURSE_ID = int(os.environ.get("MCG_LOGIN_COURSE_ID", "16518"))

# Known course name -> golfCourseId mapping (from observed search results).
# Falls Road and Sligo Creek appeared as filter buttons in the app but never
# showed up in the search data you shared, so their IDs aren't known yet -
# if you want to target them, grab the id from a Network tab search and add
# it here.
COURSE_NAME_TO_ID = {
    "fallsroad": "16503"
    "northwest": "16504",
    "hampshiregreens": "16506",
    "laytonsville": "16507",
    "littlebennett": "16508",
    "needwood": "16509",
    "crossvines": "16510",
    "rattlewood": "16511",
}

# Course name(s) to watch: a single name, comma-separated names, or "all"
TARGET_COURSES_RAW = os.environ.get("TARGET_COURSES", "needwood")

# Daily time window you care about, 24hr HH:MM
WINDOW_START = os.environ.get("WINDOW_START", "07:00")
WINDOW_END = os.environ.get("WINDOW_END", "09:00")

# Specific date(s) you want to play, YYYY-MM-DD, comma-separated for
# multiple candidate dates (e.g. a weekend you're flexible on).
TARGET_DATES_RAW = os.environ.get("TARGET_DATES", "")

PLAYERS = os.environ.get("PLAYERS", "1")
HOLES = os.environ.get("HOLES", "18")

# Where to persist auth token + seen-tee-time state between cron runs
STATE_DIR = Path(os.environ.get("STATE_DIR", str(Path(__file__).parent / "state")))
TOKEN_FILE = STATE_DIR / "token.json"
SEEN_FILE = STATE_DIR / "seen_teetimes.json"

# Pushover
PUSHOVER_USER_KEY = os.environ.get("PUSHOVER_USER_KEY")
PUSHOVER_API_TOKEN = os.environ.get("PUSHOVER_API_TOKEN")

LOG_FILE = os.environ.get("LOG_FILE", str(Path(__file__).parent / "tee_monitor.log"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("tee_monitor")


# ---------------------------------------------------------------------------
# Course name / target date resolution
# ---------------------------------------------------------------------------

def _normalize_course_name(name: str) -> str:
    return name.strip().lower().replace(" ", "").replace("-", "").replace("_", "")


def resolve_target_courses(raw: str) -> list[str] | None:
    """Turn 'needwood', 'needwood,northwest', or 'all' into a list of
    golfCourseIds. Returns None for 'all' (meaning: don't filter by course,
    let the API return everything)."""
    raw = raw.strip().lower()
    if raw in ("all", ""):
        return None

    ids = []
    for part in raw.split(","):
        key = _normalize_course_name(part)
        if key in COURSE_NAME_TO_ID:
            ids.append(COURSE_NAME_TO_ID[key])
        else:
            log.warning(
                "Unknown course name '%s' - skipping. Known names: %s",
                part.strip(),
                ", ".join(sorted(COURSE_NAME_TO_ID.keys())),
            )
    if not ids:
        log.error("No valid course names resolved from TARGET_COURSES='%s'", raw)
        sys.exit(1)
    return ids


def resolve_target_dates(raw: str) -> list:
    """Parse a comma-separated list of YYYY-MM-DD strings into date objects."""
    raw = raw.strip()
    if not raw:
        log.error(
            "TARGET_DATES is not set. Set it to a date (or comma-separated "
            "dates) you want to play, e.g. TARGET_DATES=2026-07-21"
        )
        sys.exit(1)

    dates = []
    for part in raw.split(","):
        part = part.strip()
        try:
            dates.append(datetime.strptime(part, "%Y-%m-%d").date())
        except ValueError:
            log.error("Could not parse date '%s' - use YYYY-MM-DD format.", part)
            sys.exit(1)
    return sorted(dates)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read %s, starting fresh", path)
        return default


def _save_json(path: Path, data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_token() -> str | None:
    data = _load_json(TOKEN_FILE, {})
    return data.get("token")


def save_token(token: str) -> None:
    _save_json(TOKEN_FILE, {"token": token, "saved_at": datetime.now().isoformat()})


def load_seen_ids() -> set:
    return set(_load_json(SEEN_FILE, []))


def save_seen_ids(ids: set) -> None:
    _save_json(SEEN_FILE, sorted(ids))


# ---------------------------------------------------------------------------
# TenFore API
# ---------------------------------------------------------------------------

def login() -> str:
    """Log in and return a fresh bearer token."""
    if not MCG_EMAIL or not MCG_PASSWORD:
        log.error("MCG_EMAIL / MCG_PASSWORD not set - cannot log in.")
        sys.exit(1)

    payload = {
        "username": MCG_EMAIL,
        "password": MCG_PASSWORD,
        "golfCourseID": MCG_LOGIN_COURSE_ID,
    }
    resp = requests.post(LOGIN_URL, json=payload, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    # The exact key holding the token wasn't confirmed - try common names.
    token = (
        data.get("token")
        or data.get("accessToken")
        or data.get("access_token")
        or data.get("jwt")
    )
    if not token:
        log.error("Login succeeded but no token found in response keys: %s", list(data.keys()))
        sys.exit(1)

    save_token(token)
    log.info("Logged in, got fresh token.")
    return token


def search_tee_times(
    token: str, course_ids: list[str] | None, date_from: datetime, date_to: datetime
) -> list[dict]:
    """Search target courses in one call. course_ids=None means all courses.
    Raises PermissionError on auth failure."""
    params = {
        "dateFrom": date_from.strftime("%Y-%m-%dT%H:%M:%S"),
        "dateTo": date_to.strftime("%Y-%m-%dT%H:%M:%S"),
        "players": PLAYERS,
        "holes": HOLES,
    }
    if course_ids:
        params["golfCourseIds"] = ",".join(course_ids)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Tenfore-Appid": APP_ID,
        "Accept": "application/json",
    }
    resp = requests.get(SEARCH_URL, params=params, headers=headers, timeout=15)

    if resp.status_code == 401:
        raise PermissionError("Token expired or invalid")

    resp.raise_for_status()
    return resp.json()


def get_valid_token() -> str:
    token = load_token()
    if token:
        return token
    return login()


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def parse_window(hhmm: str) -> dtime:
    h, m = hhmm.split(":")
    return dtime(int(h), int(m))


def in_time_window(dt: datetime, start: dtime, end: dtime) -> bool:
    t = dt.time()
    return start <= t <= end


def has_open_spot(tee_time: dict) -> bool:
    """The search endpoint already filters by 'players' requested, but this
    is a belt-and-suspenders check in case the API's semantics change."""
    return tee_time.get("bookedPlayers", 0) < tee_time.get("maxPlayers", 0)


def filter_tee_times(
    tee_times: list[dict],
    target_dates: list,
    window_start: dtime,
    window_end: dtime,
) -> list[dict]:
    target_date_set = set(target_dates)
    results = []
    for tt in tee_times:
        try:
            dt = datetime.fromisoformat(tt["dateScheduled"])
        except (KeyError, ValueError):
            continue
        if dt.date() not in target_date_set:
            continue
        if not in_time_window(dt, window_start, window_end):
            continue
        if not has_open_spot(tt):
            continue
        results.append(tt)
    return results


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def notify_pushover(title: str, message: str) -> None:
    if not PUSHOVER_USER_KEY or not PUSHOVER_API_TOKEN:
        log.warning("Pushover not configured - printing instead:\n%s\n%s", title, message)
        return
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": PUSHOVER_API_TOKEN,
                "user": PUSHOVER_USER_KEY,
                "title": title,
                "message": message,
                "priority": 0,
            },
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Pushover notification sent.")
    except requests.RequestException as e:
        log.error("Failed to send Pushover notification: %s", e)


def _format_line(tt: dict) -> str:
    dt = datetime.fromisoformat(tt["dateScheduled"])
    open_spots = tt.get("maxPlayers", 0) - tt.get("bookedPlayers", 0)
    return (
        f"{tt.get('golfCourseName', 'Unknown course')} - "
        f"{dt.strftime('%a %b %d, %I:%M %p')} - "
        f"{open_spots} spot(s) - ${tt.get('priceBeforeTax', '?')}"
    )


def build_notification(new_tee_times: list[dict], all_matching: list[dict]) -> tuple[str, str]:
    """Returns (title, message). Title calls out what's new; message leads
    with the new time(s) then lists everything currently available in the
    window for context."""
    new_sorted = sorted(new_tee_times, key=lambda tt: tt["dateScheduled"])
    all_sorted = sorted(all_matching, key=lambda tt: tt["dateScheduled"])

    if len(new_sorted) == 1:
        tt = new_sorted[0]
        dt = datetime.fromisoformat(tt["dateScheduled"])
        title = (
            f"⛳ New tee time at {tt.get('golfCourseName', 'Unknown course')} "
            f"at {dt.strftime('%I:%M %p')}"
        )
    else:
        title = f"⛳ {len(new_sorted)} new tee times available"

    lines = ["NEW:"]
    lines += [f"  {_format_line(tt)}" for tt in new_sorted]

    if len(all_sorted) > len(new_sorted):
        lines.append("")
        lines.append("All currently available in your window:")
        lines += [f"  {_format_line(tt)}" for tt in all_sorted]

    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    target_courses = resolve_target_courses(TARGET_COURSES_RAW)
    target_dates = resolve_target_dates(TARGET_DATES_RAW)
    window_start = parse_window(WINDOW_START)
    window_end = parse_window(WINDOW_END)

    now = datetime.now()
    # Search from now through the end of the latest target date. If your
    # earliest target date is already in the past (script run after that
    # date), that's fine - it just won't return anything for it.
    date_from = now
    date_to = datetime.combine(target_dates[-1], dtime(23, 59, 59))

    token = get_valid_token()

    try:
        raw_results = search_tee_times(token, target_courses, date_from, date_to)
    except PermissionError:
        log.info("Token expired, re-logging in...")
        token = login()
        raw_results = search_tee_times(token, target_courses, date_from, date_to)

    matching = filter_tee_times(raw_results, target_dates, window_start, window_end)
    matching_ids = {tt["teeTimeId"] for tt in matching}

    seen_ids = load_seen_ids()
    new_ids = matching_ids - seen_ids

    if new_ids:
        new_tee_times = [tt for tt in matching if tt["teeTimeId"] in new_ids]
        log.info("Found %d new tee time(s) in window.", len(new_tee_times))
        title, message = build_notification(new_tee_times, matching)
        notify_pushover(title=title, message=message)
    else:
        log.info("No new tee times in window (%d already-seen matches).", len(matching_ids))

    # Only keep IDs that are still relevant (still in the search window) so
    # the seen-set doesn't grow forever with times that have since passed.
    save_seen_ids(matching_ids)


if __name__ == "__main__":
    run()
