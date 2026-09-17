# DEPRECATED

The booking API has added a reCAPTCHA token to prevent bots from any type of automation and
non-browser requests. Although this code was created only for good intent to notify of availble
tee times and not to be used to book tee times maliciously, I did not try and pursue any type
of software to get around the reCAPTCHA.


# MCG Golf Tee Time Monitor

Polls the TenFore booking API for your chosen course(s) and sends a Pushover
notification when a **new** tee time appears inside your desired daily time
window. Meant to run on a cron schedule, not continuously.

## 1. Set up the environment

Create a virtual environment inside this folder, so all dependencies stay
self-contained here instead of touching your system Python:

```bash
cd tee_monitor
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### Using the venv

There are two ways to use it, depending on what you're doing:

**Option A - activate it (good for interactive testing/poking around):**

```bash
source venv/bin/activate
```

Your terminal prompt will change to show `(venv)` at the start of the line,
confirming it's active. While active, `python` and `pip` both point at the
venv's copies automatically:

```bash
python tee_time_monitor.py
```

When you're done, deactivate it with:

```bash
deactivate
```

**Option B - call the venv's python directly (no activation needed - this
is what cron will use, since cron doesn't run your shell profile so
`activate` isn't reliable there):**

```bash
./venv/bin/python tee_time_monitor.py
```

Both approaches run the exact same isolated environment - Option A is more
convenient for a terminal session where you're running things repeatedly;
Option B is more explicit and is what's used in the cron examples below
(and in the "test manually" step in the next section) precisely so you
don't have to remember to activate first.

## 2. Configure

```bash
cp config.example.env config.env
```

Edit `config.env` and fill in:
- `MCG_EMAIL` / `MCG_PASSWORD` - your MCG Golf login
- `TARGET_COURSES` - which course(s) to watch, by name: `needwood`,
  `needwood,northwest`, or `all`. Known names: `northwest`,
  `hampshiregreens`, `laytonsville`, `littlebennett`, `needwood`,
  `crossvines`, `rattlewood`. (Falls Road and Sligo Creek IDs aren't known
  yet - add them to `COURSE_NAME_TO_ID` near the top of the script if you
  want to target those.)
- `TARGET_DATES` - the date(s) you actually want to play, `YYYY-MM-DD`,
  comma-separated for more than one (e.g. checking both days of a weekend)
- `WINDOW_START` / `WINDOW_END` - the daily time window you care about
- `PUSHOVER_USER_KEY` / `PUSHOVER_API_TOKEN` - from your Pushover account
  (create a free account at pushover.net, install the app, and register an
  "Application" to get the API token)

`config.env` is never read directly by Python - you load it into your shell
environment before running the script (see below), so the actual secrets
never live inside the script file itself.

## 3. Test it manually

```bash
set -a; source config.env; set +a
./venv/bin/python tee_time_monitor.py
```

Check `tee_monitor.log` for output. First run will log in fresh and record
whatever's currently available as "seen" (you won't get notified for tee
times that already existed before your first run - only new ones going
forward).

If notifications aren't firing when you expect, try temporarily emptying
`state/seen_teetimes.json` (or just delete the file) to force it to treat
everything currently open as "new" for one test run.

## 4. Schedule it with cron

Edit your crontab:

```bash
crontab -e
```

Add a line to run every 5 minutes (adjust the path to wherever you put this
folder):

```
*/5 * * * * cd /home/youruser/tee_monitor && set -a && source config.env && set +a && ./venv/bin/python tee_time_monitor.py >> cron.log 2>&1
```

Since tee times reportedly release ~7 days + 3 hours in advance, you may
want a *tighter* polling interval (e.g. every 1-2 minutes) around that
specific release time each day, and a looser interval (every 10-15 minutes)
the rest of the time, to be respectful of the site's servers. You can do
this with two separate cron lines with different schedules, e.g.:

```
# Every minute from 8:55-9:05 PM (adjust to your course's actual release time)
55-59 20 * * * cd /home/youruser/tee_monitor && set -a && source config.env && set +a && ./venv/bin/python tee_time_monitor.py >> cron.log 2>&1
0-5 21 * * * cd /home/youruser/tee_monitor && set -a && source config.env && set +a && ./venv/bin/python tee_time_monitor.py >> cron.log 2>&1

# Every 10 min the rest of the day, to catch cancellations
*/10 * * * * cd /home/youruser/tee_monitor && set -a && source config.env && set +a && ./venv/bin/python tee_time_monitor.py >> cron.log 2>&1
```

## What this does (and doesn't do)

This script only ever calls the *search* endpoint - it reads availability
and notifies you. It never calls a booking/reservation endpoint or attempts
to hold or purchase a tee time. When you get notified, you still open the
app yourself, round up your group, and book it manually.

## How it works

1. Logs in once and caches the bearer token in `state/token.json`
2. Each run, searches your target courses from now through the end of the
   latest `TARGET_DATES` entry
3. Filters results to your time window and to tee times with an open spot
4. Compares against `state/seen_teetimes.json` (tee time IDs seen last run)
5. Notifies via Pushover only for IDs that are new since the last run
6. Updates the seen-list so you don't get repeat notifications

## Notes / things you may want to tune

- **Token expiry**: if the API returns a 401, the script automatically
  re-logs in and retries once. If logins start failing outright, the
  password or login endpoint may have changed - check by re-inspecting the
  Network tab.
- **Response field names**: `login()` tries a few common key names
  (`token`, `accessToken`, `access_token`, `jwt`) for the token in the login
  response. If it exits with "no token found," check the actual response
  body and adjust that list.
- **Rate limiting**: keep polling intervals reasonable. Hammering the API
  every few seconds risks tripping anti-bot protections or violating the
  site's terms.
