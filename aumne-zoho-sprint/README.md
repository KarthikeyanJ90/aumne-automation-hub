# Aumne Zoho Sprints daily report

`zoho_daily_report.py` generates a Markdown report for the active sprint in the
`aumne-engine` project. It uses only Python's standard library.

## Report contents

- Ticket work hours per person with plain-text descriptions
- Completed-during-window, current In Progress, and current To-do counts
- Newly created, newly assigned, and moved-in tickets by final owner
- Configured priority bugs that are unassigned or assigned to Karthikeyan
- Ticket-level and person-level sprint capacity risks
- Regular team members available for new work
- Unfinished parent tickets without duration estimation

`IN REVIEW` is counted as In Progress. Subtasks are excluded. Shared tickets
count for every owner, while remaining effort is divided equally.

## Requirements

- Python 3.9 or newer
- Zoho Sprints API access enabled
- An access token with read access to projects, sprints, items, activities,
  priorities, and timesheets

The API uses Zoho OAuth 2.0 and the India data-centre URL. See the
[official Zoho Sprints API introduction](https://help.zoho.com/portal/en/kb/zoho-sprints/api-guide/articles/introduction-to-zoho-sprints-api).

## Run manually in PowerShell

Use either a fresh access token or the browser-based OAuth login flow.

### Option 1: Direct token

```powershell
$env:ZOHO_SPRINTS_ACCESS_TOKEN = "your-current-access-token"
python .\zoho_daily_report.py
```

### Option 2: Browser login flow

```powershell
$env:ZOHO_CLIENT_ID = "your-zoho-client-id"
$env:ZOHO_CLIENT_SECRET = "your-zoho-client-secret"
$env:ZOHO_REDIRECT_URI = "http://localhost:8765/callback"
python .\zoho_daily_report.py --login
```

The script opens the Zoho authorization page in your default browser, waits for the callback, exchanges the callback code for a fresh access token, saves it locally, and then continues generating the report in the same terminal session.

Before running this option, create a **Server-based Application** in the Zoho API Console for the India data centre and register this exact Authorized Redirect URI:

```
http://localhost:8765/callback
```

It must exactly match `ZOHO_REDIRECT_URI`: do not use Markdown link syntax, change the host to `127.0.0.1`, or add/remove a trailing slash. The script requests the Zoho Sprints project, sprint, item, timesheet, and settings scopes needed for this report.

The script creates:

- `daily_report_YYYY-MM-DD.md` — completed report
- `.zoho_report_state.json` — comparison baseline for assignments and late logs
- `zoho_daily_report.log` — execution and error log

Reports and state are written only after every required API request succeeds.
If a request remains incomplete after retries, the script exits with code 1
and does not publish a partial report.

## Configuration

Edit `report_config.json` to change team membership, hours, priorities, or
reporting rules. The supplied answers were ambiguous for “high priority”
(`No`), so `high_bug_priorities` defaults to `Highest`, matching the earlier
decision. Change it to `["High", "Highest"]` if both are required.

The first execution creates a baseline. Creation and move events can be read
from Zoho activity, but reliable reassignment comparison begins with the
second execution.

## Optional controls

```powershell
# Test a historical report cutoff without changing system time
python .\zoho_daily_report.py --as-of "2026-09-25T08:30:00+05:30"

# Increase logging detail
python .\zoho_daily_report.py --verbose
```

Environment tuning:

- `ZOHO_REQUEST_INTERVAL_SECONDS` — delay between API calls; default `0.1`
- `ZOHO_ACTIVITY_WORKERS` — concurrent item-activity requests; default `6`

## Tests

```powershell
python -m unittest discover -s tests -v
```
