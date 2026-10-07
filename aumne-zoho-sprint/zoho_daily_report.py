#!/usr/bin/env python3
"""Generate a daily Markdown report from the active Zoho Sprints sprint.

The script deliberately uses only Python's standard library. Authentication is
read from ZOHO_SPRINTS_ACCESS_TOKEN. It writes the report, execution log, and
state snapshot beside this file.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import http.server
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone, tzinfo
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "report_config.json"
STATE_PATH = SCRIPT_DIR / ".zoho_report_state.json"
LOG_PATH = SCRIPT_DIR / "zoho_daily_report.log"
TOKEN_PATH = SCRIPT_DIR / ".zoho_access_token.json"
ACTIVE_STATUSES = {"to do", "in progress", "in review"}
IN_PROGRESS_STATUSES = {"in progress", "in review"}
DEFAULT_OAUTH_SCOPE = (
    "ZohoSprints.projects.ALL,ZohoSprints.sprints.ALL,ZohoSprints.items.ALL,"
    "ZohoSprints.timesheets.ALL,ZohoSprints.settings.READ"
)


class ReportError(RuntimeError):
    """Raised when a complete, trustworthy report cannot be produced."""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(value: Any) -> str:
    parser = _TextExtractor()
    parser.feed(str(value or ""))
    text = html.unescape(" ".join(parser.parts))
    return re.sub(r"\s+", " ", text).strip()


def parse_iso(value: Any) -> datetime | None:
    if not value or value == "-1":
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_duration_hours(value: Any, hours_per_day: float = 8.0) -> float | None:
    """Parse Zoho durations such as 1w 2d 3h 30m or 08:30."""
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw or raw in {"-1", "0", "0h", "0m", "none", "null"}:
        return None
    if re.fullmatch(r"\d{1,3}:\d{2}", raw):
        hours, minutes = raw.split(":", 1)
        result = int(hours) + int(minutes) / 60.0
        return result if result > 0 else None
    matches = re.findall(r"(\d+(?:\.\d+)?)\s*(w|d|h|m)", raw)
    if not matches:
        return None
    factors = {"w": hours_per_day * 5, "d": hours_per_day, "h": 1.0, "m": 1 / 60}
    result = sum(float(amount) * factors[unit] for amount, unit in matches)
    return result if result > 0 else None


def previous_working_day(day: date, working_weekdays: set[int]) -> date:
    cursor = day - timedelta(days=1)
    while cursor.weekday() not in working_weekdays:
        cursor -= timedelta(days=1)
    return cursor


def subtract_working_days(day: date, count: int, working_weekdays: set[int]) -> date:
    cursor = day
    remaining = count
    while remaining:
        cursor -= timedelta(days=1)
        if cursor.weekday() in working_weekdays:
            remaining -= 1
    return cursor


def count_working_days(start: date, end: date, working_weekdays: set[int]) -> int:
    if end < start:
        return 0
    return sum(
        1
        for offset in range((end - start).days + 1)
        if (start + timedelta(days=offset)).weekday() in working_weekdays
    )


def load_timezone(name: str) -> tzinfo:
    """Return the configured timezone without requiring the optional tzdata wheel."""
    normalized = name.casefold().replace("_", "/")
    if normalized in {"asia/kolkata", "asia/calcutta", "ist"}:
        return timezone(timedelta(hours=5, minutes=30), name="IST")
    raise ReportError(
        f"Unsupported timezone {name!r}. This dependency-free build supports Asia/Kolkata/IST."
    )


def local_day_from_zoho(value: Any, tz: tzinfo) -> date | None:
    parsed = parse_iso(value)
    return parsed.astimezone(tz).date() if parsed else None


def md(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def fmt_hours(value: float) -> str:
    if abs(value - round(value)) < 0.001:
        return f"{int(round(value))}h"
    return f"{value:.1f}h"


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, newline="\n"
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


@dataclass(frozen=True)
class Config:
    team_id: str
    project_id: str
    project_name: str
    zoho_domain: str
    report_time: dt_time
    tz: tzinfo
    working_weekdays: set[int]
    hours_per_day: float
    review_remaining_hours: float
    stale_working_days: int
    high_bug_priorities: set[str]
    my_user_id: str
    unassigned_user_ids: set[str]
    regular_users: dict[str, str]
    lead_users: dict[str, str]
    excluded_user_ids: set[str]

    @property
    def known_users(self) -> dict[str, str]:
        return {**self.regular_users, **self.lead_users}

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReportError(f"Unable to read configuration {path}: {exc}") from exc
        hour, minute = map(int, raw["report_time"].split(":"))
        return cls(
            team_id=str(raw["team_id"]),
            project_id=str(raw["project_id"]),
            project_name=str(raw["project_name"]),
            zoho_domain=str(raw.get("zoho_domain", "in")),
            report_time=dt_time(hour, minute),
            tz=load_timezone(raw.get("timezone", "Asia/Kolkata")),
            working_weekdays={int(x) for x in raw.get("working_weekdays", range(5))},
            hours_per_day=float(raw.get("hours_per_day", 8)),
            review_remaining_hours=float(raw.get("review_remaining_hours", 1)),
            stale_working_days=int(raw.get("stale_working_days", 2)),
            high_bug_priorities={str(x).casefold() for x in raw.get("high_bug_priorities", ["Highest"])},
            my_user_id=str(raw["my_user_id"]),
            unassigned_user_ids={str(x) for x in raw.get("unassigned_user_ids", [])},
            regular_users={str(k): str(v) for k, v in raw["regular_users"].items()},
            lead_users={str(k): str(v) for k, v in raw.get("lead_users", {}).items()},
            excluded_user_ids={str(x) for x in raw.get("excluded_user_ids", [])},
        )


class ZohoClient:
    def __init__(self, config: Config, access_token: str) -> None:
        self.config = config
        # self.base_url = f"https://sprintsapi.zoho.{config.zoho_domain}/zsapi"
        self.base_url = f"https://sprintsapi.zoho.com/zsapi"
        self.access_token = access_token
        self._request_lock = threading.Lock()
        self._last_request = 0.0
        self.minimum_interval = float(os.getenv("ZOHO_REQUEST_INTERVAL_SECONDS", "0.1"))

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode({k: str(v) for k, v in (params or {}).items()})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        print(f"Requesting {url}...")
        headers = {
            "Authorization": f"Zoho-oauthtoken {self.access_token}",
            "X-ZA-CONVERT-RESPONSE": "true",
            "X-ZA-UI-VERSION": "v2",
            "X-ZA-REQSIZE": "large",
            "Accept": "application/json",
            "User-Agent": "aumne-zoho-daily-report/1.0",
        }
        for attempt in range(4):
            with self._request_lock:
                delay = self.minimum_interval - (time.monotonic() - self._last_request)
                if delay > 0:
                    time.sleep(delay)
                self._last_request = time.monotonic()
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=60
                ) as response:
                    body = response.read().decode("utf-8")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ReportError(f"Unexpected response shape from {path}")
                if str(data.get("status", "success")).casefold() == "failure":
                    raise ReportError(f"Zoho API failure for {path}: {data}")
                return data
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code == 401:
                    try:
                        refreshed = fetch_access_token_with_browser(self.config, TOKEN_PATH)
                    except ReportError:
                        refreshed = ""
                    if refreshed:
                        self.access_token = refreshed
                        continue
                    raise ReportError(
                        "Zoho access token is invalid or expired. Re-run the script after browser login "
                        "or set ZOHO_SPRINTS_ACCESS_TOKEN to a fresh token."
                    ) from exc
                if exc.code != 429 and exc.code < 500:
                    raise ReportError(f"Zoho API HTTP {exc.code} for {path}: {body}") from exc
                retry_after = int(exc.headers.get("Retry-After", "0") or 0)
                wait = retry_after or 2**attempt
            except (urllib.error.URLError, TimeoutError) as exc:
                wait = 2**attempt
                if attempt == 3:
                    raise ReportError(f"Zoho API request failed for {path}: {exc}") from exc
            if attempt == 3:
                raise ReportError(f"Zoho API did not recover after retries: {path}")
            logging.warning("Retrying %s in %ss", path, wait)
            time.sleep(wait)
        raise AssertionError("unreachable")

    def active_sprint(self) -> dict[str, Any]:
        path = f"/team/{self.config.team_id}/projects/{self.config.project_id}/sprints/"
        data = self.get(path, {"action": "data", "type": "[2]", "index": 1, "range": 250})
        sprints = data.get("sprints") or []
        if len(sprints) != 1:
            raise ReportError(f"Expected exactly one active sprint, found {len(sprints)}")
        return sprints[0]

    def items(self, sprint_id: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
        path = (
            f"/team/{self.config.team_id}/projects/{self.config.project_id}"
            f"/sprints/{sprint_id}/item/"
        )
        records: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        index = 1
        for _ in range(20):
            data = self.get(path, {"action": "data", "index": index, "range": 250})
            page = data.get("items") or []
            if not isinstance(page, list):
                raise ReportError("Zoho item response did not contain an item list")
            records.extend(page)
            names.update({str(k): str(v) for k, v in (data.get("userDisplayName") or {}).items()})
            if not data.get("next"):
                break
            index = int(data.get("nextIndex") or index + len(page))
        else:
            raise ReportError("Item pagination exceeded the safety limit")
        # API defaults to parents only; this defensive filter enforces the answer.
        parents = [
            item
            for item in records
            if str(item.get("depth", "0")) in {"", "0"}
            and not item.get("immediateParentId")
            and not item.get("parentItem")
        ]
        return parents, names

    def priorities(self) -> dict[str, str]:
        path = f"/team/{self.config.team_id}/projects/{self.config.project_id}/priority/"
        data = self.get(path, {"action": "data", "index": 1, "range": 250})
        result: dict[str, str] = {}
        for priority_id, values in (data.get("projPriorityJObj") or {}).items():
            if isinstance(values, list) and values:
                result[str(priority_id)] = str(values[0])
        return result

    def logs(self, sprint_start: date) -> list[dict[str, Any]]:
        path = f"/team/{self.config.team_id}/projects/{self.config.project_id}/loghours/"
        records: list[dict[str, Any]] = []
        index = 1
        for _ in range(40):
            data = self.get(
                path,
                {
                    "action": "data",
                    "index": index,
                    "range": 250,
                    "listviewtype": 0,
                    "logtypes": "[0]",
                },
            )
            page = data.get("logs") or []
            if not isinstance(page, list):
                raise ReportError("Zoho timesheet response did not contain a log list")
            records.extend(page)
            if not data.get("next"):
                break
            # Date view is descending; once a full page is older than the sprint,
            # no later page can contain a current-sprint work date.
            page_days = [local_day_from_zoho(x.get("logDate"), self.config.tz) for x in page]
            known_days = [x for x in page_days if x]
            if known_days and max(known_days) < sprint_start:
                break
            index = int(data.get("nextIndex") or index + len(page))
        else:
            raise ReportError("Log pagination exceeded the safety limit")
        return records

    def item_activity(self, sprint_id: str, item_id: str) -> dict[str, Any]:
        path = (
            f"/team/{self.config.team_id}/projects/{self.config.project_id}"
            f"/sprints/{sprint_id}/item/{item_id}/activity/"
        )
        return self.get(path, {"index": 1, "range": 250})


def reporting_window(now: datetime, config: Config) -> tuple[datetime, datetime]:
    local_now = now.astimezone(config.tz)
    end_day = local_now.date()
    scheduled = datetime.combine(end_day, config.report_time, config.tz)
    if local_now < scheduled:
        end_day = previous_working_day(end_day, config.working_weekdays)
        scheduled = datetime.combine(end_day, config.report_time, config.tz)
    while end_day.weekday() not in config.working_weekdays:
        end_day = previous_working_day(end_day, config.working_weekdays)
        scheduled = datetime.combine(end_day, config.report_time, config.tz)
    start_day = previous_working_day(end_day, config.working_weekdays)
    return datetime.combine(start_day, config.report_time, config.tz), scheduled


def activity_entries(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for day in (payload.get("auditJObj") or {}).values():
        if not isinstance(day, dict):
            continue
        for value in (day.get("auditObj") or {}).values():
            if not isinstance(value, list):
                continue
            entries.append(
                {
                    "action": value[0] if len(value) > 0 else None,
                    "display": strip_html(value[3] if len(value) > 3 else ""),
                    "actor": str(value[5]) if len(value) > 5 else "",
                    "time": parse_iso(value[7] if len(value) > 7 else None),
                }
            )
    return entries


def log_hash(entry: Mapping[str, Any]) -> str:
    fields = {
        key: entry.get(key)
        for key in ("logId", "Owner", "logDate", "logTime", "logNotes", "logEntityId", "lastUpdatedTime")
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


def is_unassigned(owners: Sequence[str], config: Config) -> bool:
    return not owners or all(owner in config.unassigned_user_ids for owner in owners)


def real_owners(item: Mapping[str, Any], config: Config) -> list[str]:
    owners = [str(x) for x in (item.get("ownerId") or [])]
    return [x for x in owners if x not in config.unassigned_user_ids and x not in config.excluded_user_ids]


def status_name(item: Mapping[str, Any]) -> str:
    return str(item.get("statusName") or "").strip()


def item_label(item: Mapping[str, Any]) -> str:
    return f"#{item.get('itemNo', '?')} — {md(item.get('itemName', 'Unnamed ticket'))}"


def generate_report(
    config: Config,
    sprint: Mapping[str, Any],
    items: list[dict[str, Any]],
    api_names: Mapping[str, str],
    priorities: Mapping[str, str],
    logs: list[dict[str, Any]],
    activities: Mapping[str, list[dict[str, Any]]],
    previous_state: Mapping[str, Any],
    window_start: datetime,
    window_end: datetime,
) -> tuple[str, dict[str, Any]]:
    users = {**api_names, **config.known_users}
    item_by_id = {str(item.get("itemId")): item for item in items}
    current_item_ids = set(item_by_id)
    previous_items = previous_state.get("items") or {}
    previous_logs = previous_state.get("logs") or {}
    sprint_start = local_day_from_zoho(sprint.get("startDate"), config.tz) or window_start.date()
    sprint_end = local_day_from_zoho(sprint.get("endDate"), config.tz) or window_end.date()
    report_days = {
        window_start.date() + timedelta(days=offset)
        for offset in range((window_end.date() - window_start.date()).days)
    }

    sprint_logs = [
        entry
        for entry in logs
        if str(entry.get("logEntityId")) in current_item_ids
        and (local_day_from_zoho(entry.get("logDate"), config.tz) or date.min) >= sprint_start
    ]
    report_logs: list[tuple[dict[str, Any], bool]] = []
    for entry in sprint_logs:
        work_day = local_day_from_zoho(entry.get("logDate"), config.tz)
        entry_id = str(entry.get("logId") or entry.get("tLogId") or "")
        changed = entry_id not in previous_logs or previous_logs.get(entry_id) != log_hash(entry)
        updated = parse_iso(entry.get("lastUpdatedTime"))
        updated_local = updated.astimezone(config.tz) if updated else None
        in_report_days = work_day in report_days
        late = bool(changed and updated_local and window_start <= updated_local < window_end and not in_report_days)
        if in_report_days or late:
            report_logs.append((entry, late))

    # Log totals and descriptions.
    hours_by_user: dict[str, float] = defaultdict(float)
    log_lines_by_user: dict[str, list[str]] = defaultdict(list)
    item_logged_ms: dict[str, int] = defaultdict(int)
    today_logged_hours: dict[str, float] = defaultdict(float)
    for entry in sprint_logs:
        owner = str(entry.get("Owner") or entry.get("ownerId") or "")
        item_id = str(entry.get("logEntityId") or "")
        millis = int(entry.get("logTime") or 0)
        item_logged_ms[item_id] += millis
        if local_day_from_zoho(entry.get("logDate"), config.tz) == window_end.date():
            today_logged_hours[owner] += millis / 3_600_000
    for entry, late in report_logs:
        owner = str(entry.get("Owner") or entry.get("ownerId") or "")
        millis = int(entry.get("logTime") or 0)
        hours = millis / 3_600_000
        hours_by_user[owner] += hours
        ticket = item_by_id.get(str(entry.get("logEntityId") or ""), {})
        description = strip_html(entry.get("logNotes")) or "No description"
        late_text = " — **late entry**" if late else ""
        log_lines_by_user[owner].append(
            f"- {item_label(ticket)} — {fmt_hours(hours)} — {md(description)}{late_text}"
        )

    # Ticket counts. Completed is window-specific; other statuses are current.
    completed_by_user: dict[str, int] = defaultdict(int)
    in_progress_by_user: dict[str, int] = defaultdict(int)
    todo_by_user: dict[str, int] = defaultdict(int)
    unassigned_counts = {"completed": 0, "in_progress": 0, "todo": 0}
    for item in items:
        owners = real_owners(item, config)
        completed = parse_iso(item.get("completedDate"))
        completed_local = completed.astimezone(config.tz) if completed else None
        state = status_name(item).casefold()
        if is_unassigned([str(x) for x in (item.get("ownerId") or [])], config):
            if completed_local and window_start <= completed_local < window_end:
                unassigned_counts["completed"] += 1
            if state in IN_PROGRESS_STATUSES:
                unassigned_counts["in_progress"] += 1
            if state == "to do":
                unassigned_counts["todo"] += 1
        for owner in owners:
            if completed_local and window_start <= completed_local < window_end:
                completed_by_user[owner] += 1
            if state in IN_PROGRESS_STATUSES:
                in_progress_by_user[owner] += 1
            if state == "to do":
                todo_by_user[owner] += 1

    # New assignments: snapshots are authoritative after the first run.
    assignments: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
    for item_id, item in item_by_id.items():
        owners = real_owners(item, config)
        prior = previous_items.get(item_id)
        created = parse_iso(item.get("createdTime"))
        created_local = created.astimezone(config.tz) if created else None
        reason = ""
        added: set[str] = set()
        if prior:
            prior_owners = {str(x) for x in prior.get("owners", [])}
            added = set(owners) - prior_owners
            if added:
                reason = "assigned/reassigned"
        elif previous_state:
            if created_local and window_start <= created_local < window_end:
                reason = "newly created"
                added = set(owners)
            else:
                moved = any(
                    entry.get("time")
                    and window_start <= entry["time"].astimezone(config.tz) < window_end
                    and "item moved" in entry.get("display", "").casefold()
                    for entry in activities.get(item_id, [])
                )
                if moved:
                    reason = "moved into sprint"
                    added = set(owners)
        else:
            # First execution has no comparison snapshot. Use activity evidence.
            relevant = [
                entry
                for entry in activities.get(item_id, [])
                if entry.get("time") and window_start <= entry["time"].astimezone(config.tz) < window_end
            ]
            display = " ".join(x.get("display", "") for x in relevant).casefold()
            if created_local and window_start <= created_local < window_end:
                reason, added = "newly created", set(owners)
            elif "item moved" in display:
                reason, added = "moved into sprint", set(owners)
            elif any(word in display for word in ("assign", "owner", "user")):
                reason, added = "assigned/reassigned", set(owners)
        for owner in added:
            assignments[owner].append((item, reason))

    # Remaining effort, risk, and unestimated work.
    last_activity: dict[str, datetime | None] = {}
    for item_id, entries in activities.items():
        times = [entry.get("time") for entry in entries if entry.get("time")]
        last_activity[item_id] = max(times) if times else None
    last_log_update: dict[str, datetime] = {}
    for entry in sprint_logs:
        item_id = str(entry.get("logEntityId") or "")
        updated = parse_iso(entry.get("lastUpdatedTime")) or parse_iso(entry.get("logDate"))
        if updated and (item_id not in last_log_update or updated > last_log_update[item_id]):
            last_log_update[item_id] = updated

    stale_day = subtract_working_days(window_end.date(), config.stale_working_days, config.working_weekdays)
    stale_cutoff = datetime.combine(stale_day, config.report_time, config.tz)
    working_days_left = count_working_days(window_end.date(), sprint_end, config.working_weekdays)
    base_capacity = working_days_left * config.hours_per_day
    remaining_by_item: dict[str, float | None] = {}
    remaining_by_user: dict[str, float] = defaultdict(float)
    unknown_estimate_by_user: set[str] = set()
    unestimated: list[dict[str, Any]] = []
    ticket_risks: list[tuple[dict[str, Any], float, float, list[str]]] = []

    for item_id, item in item_by_id.items():
        state = status_name(item).casefold()
        if state not in ACTIVE_STATUSES:
            remaining_by_item[item_id] = 0.0
            continue
        estimate = parse_duration_hours(item.get("duration"), config.hours_per_day)
        owners = real_owners(item, config)
        if estimate is None:
            remaining_by_item[item_id] = None
            unestimated.append(item)
            for owner in owners:
                unknown_estimate_by_user.add(owner)
            continue
        logged = item_logged_ms[item_id] / 3_600_000
        if state == "to do":
            remaining = estimate
        elif state == "in review":
            remaining = min(config.review_remaining_hours, max(estimate - logged, 0.0))
        else:
            remaining = max(estimate - logged, 0.0)
        remaining_by_item[item_id] = remaining
        share_count = max(len(owners), 1)
        for owner in owners:
            remaining_by_user[owner] += remaining / share_count

        created = parse_iso(item.get("createdTime"))
        recent = max(
            [x for x in (last_activity.get(item_id), last_log_update.get(item_id), created) if x],
            default=None,
        )
        stale = bool(recent and recent.astimezone(config.tz) < stale_cutoff)
        blocked = bool(
            item.get("blockedReason")
            or item.get("blockedOn")
            or (isinstance(item.get("blockedByObj"), dict) and item.get("blockedByObj"))
        )
        overdue = bool(item.get("isOverDue"))
        owner_capacity = sum(
            max(base_capacity - today_logged_hours.get(owner, 0.0), 0.0) for owner in owners
        )
        reasons: list[str] = []
        if is_unassigned([str(x) for x in (item.get("ownerId") or [])], config):
            reasons.append("unassigned")
        if remaining > owner_capacity:
            reasons.append("remaining effort exceeds sprint capacity")
        if overdue:
            reasons.append("overdue")
        if blocked:
            reasons.append("blocked")
        if stale:
            reasons.append("stale")
        if reasons:
            ticket_risks.append((item, remaining, owner_capacity, reasons))

    person_risks: list[tuple[str, float, float]] = []
    for owner, remaining in remaining_by_user.items():
        capacity = max(base_capacity - today_logged_hours.get(owner, 0.0), 0.0)
        if remaining > capacity:
            person_risks.append((owner, remaining, capacity))

    available: list[tuple[str, float, float, str]] = []
    for owner, name in config.regular_users.items():
        if owner in unknown_estimate_by_user:
            continue
        active_owned = [item for item in items if owner in real_owners(item, config) and status_name(item).casefold() in ACTIVE_STATUSES]
        capacity_today = max(config.hours_per_day - today_logged_hours.get(owner, 0.0), 0.0)
        remaining = remaining_by_user.get(owner, 0.0)
        if not active_owned and capacity_today > 0:
            available.append((name, remaining, capacity_today, "no active tickets"))
        elif remaining <= capacity_today:
            available.append((name, remaining, capacity_today, "can finish estimated work today"))

    # Highest-only by default; configurable because the supplied answer was ambiguous.
    high_bugs: list[dict[str, Any]] = []
    for item in items:
        owners_all = [str(x) for x in (item.get("ownerId") or [])]
        priority = priorities.get(str(item.get("projPriorityId")), "")
        if (
            str(item.get("itemTypeName", "")).casefold() == "bug"
            and status_name(item).casefold() in ACTIVE_STATUSES
            and priority.casefold() in config.high_bug_priorities
            and (is_unassigned(owners_all, config) or config.my_user_id in owners_all)
        ):
            high_bugs.append(item)

    # Leads appear only if there is relevant activity/tickets/logs.
    included_users = dict(config.regular_users)
    for owner, name in config.lead_users.items():
        if hours_by_user.get(owner) or completed_by_user.get(owner) or in_progress_by_user.get(owner) or todo_by_user.get(owner):
            included_users[owner] = name

    lines: list[str] = [
        f"# Zoho Daily Report — {window_end:%d %B %Y}",
        "",
        f"**Project:** {md(config.project_name)}  ",
        f"**Sprint:** {md(sprint.get('sprintName'))}  ",
        f"**Window:** {window_start:%d %b %Y %I:%M %p} → {window_end:%d %b %Y %I:%M %p} IST  ",
        f"**Sprint end:** {sprint_end:%d %b %Y}  ",
        "",
        "## 1. Log hours",
        "",
        "| Person | Ticket log hours |",
        "|---|---:|",
    ]
    for owner, name in included_users.items():
        lines.append(f"| {md(name)} | {fmt_hours(hours_by_user.get(owner, 0.0))} |")
    lines += ["", "### Log descriptions", ""]
    description_found = False
    for owner, name in included_users.items():
        if log_lines_by_user.get(owner):
            description_found = True
            lines.append(f"**{md(name)} — {fmt_hours(hours_by_user[owner])}**")
            lines.extend(log_lines_by_user[owner])
            lines.append("")
    if not description_found:
        lines.append("No current-sprint ticket logs were found for the reporting window.\n")

    lines += [
        "## 2. Person-wise ticket counts",
        "",
        "Completed is limited to the reporting window. In Progress includes `IN REVIEW`.",
        "",
        "| Person | Completed | In Progress | To do |",
        "|---|---:|---:|---:|",
    ]
    for owner, name in included_users.items():
        lines.append(
            f"| {md(name)} | {completed_by_user.get(owner, 0)} | "
            f"{in_progress_by_user.get(owner, 0)} | {todo_by_user.get(owner, 0)} |"
        )
    lines.append(
        f"| **Unassigned** | {unassigned_counts['completed']} | "
        f"{unassigned_counts['in_progress']} | {unassigned_counts['todo']} |"
    )

    lines += ["", "## 3. Newly assigned tickets", ""]
    if not assignments:
        baseline = " Initial execution: reassignment comparison begins after this snapshot." if not previous_state else ""
        lines.append(f"No new assignments were detected.{baseline}\n")
    else:
        for owner in sorted(assignments, key=lambda x: users.get(x, x).casefold()):
            lines.append(f"**{md(users.get(owner, owner))}**")
            for item, reason in assignments[owner]:
                shared = " — shared" if len(real_owners(item, config)) > 1 else ""
                lines.append(f"- {item_label(item)} — {reason}{shared}")
            lines.append("")

    priority_label = ", ".join(sorted(x.title() for x in config.high_bug_priorities))
    lines += ["## 4. Priority bugs assigned to me or unassigned", "", f"Configured priorities: **{priority_label}**.", ""]
    if not high_bugs:
        lines.append("No matching unfinished bugs were found.\n")
    else:
        for item in sorted(high_bugs, key=lambda x: str(x.get("itemNo"))):
            owners = real_owners(item, config)
            owner_text = ", ".join(users.get(x, x) for x in owners) or "Unassigned"
            lines.append(
                f"- {item_label(item)} — {md(status_name(item))} — {md(owner_text)}"
            )
        lines.append("")

    lines += ["## 5. Work unlikely to fit in the sprint", ""]
    lines.append(
        f"Remaining working days including report day: **{working_days_left}**; "
        f"base capacity per person: **{fmt_hours(base_capacity)}**."
    )
    lines.append("")
    lines.append("### Ticket-level risks")
    lines.append("")
    if not ticket_risks:
        lines.append("No estimated active tickets were marked at risk.\n")
    else:
        lines += ["| Ticket | Status | Remaining | Owner capacity | Reason |", "|---|---|---:|---:|---|"]
        for item, remaining, capacity, reasons in sorted(ticket_risks, key=lambda x: str(x[0].get("itemNo"))):
            lines.append(
                f"| {item_label(item)} | {md(status_name(item))} | {fmt_hours(remaining)} | "
                f"{fmt_hours(capacity)} | {md(', '.join(reasons))} |"
            )
    lines += ["", "### Person-level capacity risks", ""]
    if not person_risks:
        lines.append("No person’s known remaining effort exceeds their remaining sprint capacity.\n")
    else:
        lines += ["| Person | Remaining assigned effort | Remaining capacity |", "|---|---:|---:|"]
        for owner, remaining, capacity in sorted(person_risks, key=lambda x: x[1] - x[2], reverse=True):
            lines.append(f"| {md(users.get(owner, owner))} | {fmt_hours(remaining)} | {fmt_hours(capacity)} |")

    lines += ["", "## 6. People available for new work", ""]
    if not available:
        lines.append("No regular participant satisfies the configured availability rules.\n")
    else:
        lines += ["| Person | Known remaining effort | Capacity left today | Reason |", "|---|---:|---:|---|"]
        for name, remaining, capacity, reason in sorted(available):
            lines.append(f"| {md(name)} | {fmt_hours(remaining)} | {fmt_hours(capacity)} | {md(reason)} |")

    lines += ["", "## 7. Unfinished tickets without duration estimation", ""]
    if not unestimated:
        lines.append("All unfinished parent tickets have duration estimates.\n")
    else:
        lines += ["| Ticket | Status | Owners |", "|---|---|---|"]
        for item in sorted(unestimated, key=lambda x: str(x.get("itemNo"))):
            owners = real_owners(item, config)
            owner_text = ", ".join(users.get(x, x) for x in owners) or "Unassigned"
            lines.append(f"| {item_label(item)} | {md(status_name(item))} | {md(owner_text)} |")

    lines += [
        "",
        "## Calculation notes",
        "",
        "- Parent tickets only; subtasks are excluded.",
        "- Shared tickets count for every owner; remaining effort is divided equally.",
        "- `IN REVIEW` counts as In Progress and is assigned at most the configured review remainder.",
        "- To-do effort is full duration; In Progress effort is duration minus current-sprint ticket logs.",
        "- Overdue, blocked, or stale estimated tickets are marked at risk even when effort fits.",
        "- People with any unfinished ticket lacking duration are excluded from availability recommendations.",
        "- Leads are excluded from new-task recommendations.",
        "",
    ]

    state = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sprint_id": str(sprint.get("sprintId")),
        "items": {
            item_id: {
                "owners": real_owners(item, config),
                "status": status_name(item),
                "name": item.get("itemName"),
                "item_no": item.get("itemNo"),
            }
            for item_id, item in item_by_id.items()
        },
        "logs": {
            str(entry.get("logId") or entry.get("tLogId")): log_hash(entry)
            for entry in sprint_logs
            if entry.get("logId") or entry.get("tLogId")
        },
    }
    return "\n".join(lines), state


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"State file is unreadable; refusing an unreliable report: {exc}") from exc


def build_oauth_authorize_url(
    zoho_domain: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
) -> str:
    params = {
        "client_id": client_id,
        "scope": scope,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"https://accounts.zoho.{zoho_domain}/oauth/v2/auth?{urllib.parse.urlencode(params)}"


def validate_redirect_uri(redirect_uri: str) -> urllib.parse.ParseResult:
    """Validate the local callback URI before sending the user to Zoho."""
    if "[" in redirect_uri or "](" in redirect_uri:
        raise ReportError(
            "ZOHO_REDIRECT_URI must be a plain URL, not Markdown. Use exactly "
            'http://localhost:8765/callback'
        )
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ReportError(
            "ZOHO_REDIRECT_URI must be an absolute URL, for example "
            "http://localhost:8765/callback"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ReportError("ZOHO_REDIRECT_URI must not contain credentials, a query, or a fragment")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ReportError(
            "This login flow only supports a local callback. Use http://localhost:8765/callback"
        )
    return parsed


def load_saved_access_token(token_path: Path) -> str:
    if not token_path.exists():
        return ""
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    token = str(data.get("access_token", "")).strip()
    return token


def save_access_token(access_token: str, token_path: Path) -> None:
    payload = {"access_token": access_token, "saved_at": datetime.now(timezone.utc).isoformat()}
    atomic_write_json(token_path, payload)


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        self.server.code = query.get("code", [None])[0]
        self.server.error = query.get("error", [None])[0]
        self.server.state = query.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><p>Authentication complete. You can close this tab and return to the terminal.</p></body></html>"
        )
        self.server.shutdown()

    def log_message(self, format: str, *args: object) -> None:
        return


def fetch_access_token_with_browser(config: Config, token_path: Path) -> str:
    client_id = os.getenv("ZOHO_CLIENT_ID", "").strip()
    client_secret = os.getenv("ZOHO_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("ZOHO_REDIRECT_URI", "http://localhost:8765/callback").strip()
    scope = os.getenv("ZOHO_SCOPE", DEFAULT_OAUTH_SCOPE).strip()
    if not client_id or not client_secret:
        raise ReportError(
            "Browser login requires ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET; "
            "or set ZOHO_SPRINTS_ACCESS_TOKEN directly."
        )

    parsed_redirect = validate_redirect_uri(redirect_uri)
    port = parsed_redirect.port or 8765
    state = secrets.token_hex(16)
    auth_url = build_oauth_authorize_url(config.zoho_domain, client_id, redirect_uri, scope, state)
    server = http.server.ThreadingHTTPServer((parsed_redirect.hostname, port), OAuthCallbackHandler)
    server.code = None
    server.error = None
    server.state = None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("Open this URL in your browser to authorize Zoho Sprints:")
    print(auth_url)
    try:
        webbrowser.open(auth_url)
        print("Waiting for browser callback...")
    except Exception:
        pass
    print(f"If the browser does not open, visit the URL above manually. Listening on port {port} for callback...")
    try:
        while server.code is None and server.error is None:
            time.sleep(0.2)
    finally:
        server.server_close()
    print("Browser callback received.")
    if server.error:
        raise ReportError(f"Zoho OAuth denied: {server.error}")
    if not server.code:
        raise ReportError("Zoho OAuth callback did not return an authorization code")
    if server.state != state:
        raise ReportError("Zoho OAuth state mismatch; the browser callback was not for this session")

    token_payload = urllib.parse.urlencode(
        {
            "code": server.code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    token_url = f"https://accounts.zoho.{config.zoho_domain}/oauth/v2/token"
    request = urllib.request.Request(
        token_url,
        data=token_payload,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        token_data = json.loads(response.read().decode("utf-8"))
    access_token = str(token_data.get("access_token", "")).strip()
    if not access_token:
        raise ReportError(f"Zoho OAuth returned no access token: {token_data}")
    save_access_token(access_token, token_path)
    print("Zoho access token refreshed successfully and saved for future runs.")
    return access_token


def resolve_access_token(config: Config, token_path: Path) -> str:
    access_token = os.getenv("ZOHO_SPRINTS_ACCESS_TOKEN", "").strip()
    if access_token:
        return access_token
    saved = load_saved_access_token(token_path)
    if saved:
        return saved
    return fetch_access_token_with_browser(config, token_path)


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )


def run(args: argparse.Namespace) -> Path:
    config = Config.load(Path(args.config))
    access_token = resolve_access_token(config, Path(args.token_file))
    now = datetime.now(config.tz)
    if args.as_of:
        parsed = datetime.fromisoformat(args.as_of)
        now = parsed.replace(tzinfo=config.tz) if parsed.tzinfo is None else parsed.astimezone(config.tz)
    window_start, window_end = reporting_window(now, config)
    previous_state = load_state()
    client = ZohoClient(config, access_token)

    sprint = client.active_sprint()
    sprint_id = str(sprint.get("sprintId"))
    print("sprint = ",sprint)
    print("sprint_id = ",sprint_id)
    if not sprint_id:
        raise ReportError("Active sprint response did not contain sprintId")
    if previous_state and previous_state.get("sprint_id") != sprint_id:
        logging.info("Active sprint changed; starting a new comparison baseline")
        previous_state = {}
    items, names = client.items(sprint_id)
    priorities = client.priorities()
    sprint_start = local_day_from_zoho(sprint.get("startDate"), config.tz) or window_start.date()
    print("sprint_start = ",sprint_start)
    logs = client.logs(sprint_start)

    active_items = [item for item in items if status_name(item).casefold() in ACTIVE_STATUSES]
    activities: dict[str, list[dict[str, Any]]] = {}
    workers = min(int(os.getenv("ZOHO_ACTIVITY_WORKERS", "6")), 12)
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        futures = {
            pool.submit(client.item_activity, sprint_id, str(item.get("itemId"))): str(item.get("itemId"))
            for item in active_items
        }
        for future in as_completed(futures):
            item_id = futures[future]
            try:
                activities[item_id] = activity_entries(future.result())
            except Exception as exc:
                raise ReportError(f"Activity retrieval failed for item {item_id}: {exc}") from exc

    report, state = generate_report(
        config,
        sprint,
        items,
        names,
        priorities,
        logs,
        activities,
        previous_state,
        window_start,
        window_end,
    )
    output = SCRIPT_DIR / f"daily_report_{window_end:%Y-%m-%d}.md"
    atomic_write_text(output, report)
    atomic_write_json(STATE_PATH, state)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH), help="Path to JSON configuration")
    parser.add_argument("--as-of", help="Override current time with an ISO datetime (testing/backfill)")
    parser.add_argument("--login", action="store_true", help="Open the browser-based Zoho OAuth flow before running the report")
    parser.add_argument("--token-file", default=str(TOKEN_PATH), help="Path to a saved access token JSON file")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    configure_logging(args.verbose)
    try:
        if args.login:
            config = Config.load(Path(args.config))
            access_token = fetch_access_token_with_browser(config, Path(args.token_file))
            os.environ["ZOHO_SPRINTS_ACCESS_TOKEN"] = access_token
        output = run(args)
    except Exception as exc:
        logging.exception("Report generation failed: %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Report written to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
