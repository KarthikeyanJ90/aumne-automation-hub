import unittest
from datetime import datetime, time, timedelta, timezone

from zoho_daily_report import (
    Config,
    build_oauth_authorize_url,
    generate_report,
    parse_duration_hours,
    reporting_window,
    strip_html,
    validate_redirect_uri,
)


IST = timezone(timedelta(hours=5, minutes=30), name="IST")


class DurationTests(unittest.TestCase):
    def test_duration_units(self):
        self.assertEqual(parse_duration_hours("1w 2d 3h 30m"), 59.5)
        self.assertEqual(parse_duration_hours("1d"), 8)
        self.assertEqual(parse_duration_hours("08:30"), 8.5)

    def test_missing_duration(self):
        for value in (None, "", "-1", "0h"):
            self.assertIsNone(parse_duration_hours(value))


class TextTests(unittest.TestCase):
    def test_html_is_plain_text(self):
        self.assertEqual(strip_html("<div>Fixed&nbsp;<b>login</b></div>"), "Fixed login")


class OAuthTests(unittest.TestCase):
    def test_oauth_url_includes_client_and_redirect_values(self):
        url = build_oauth_authorize_url(
            "in",
            "test-client-id",
            "http://localhost:8765/callback",
            "ZohoSprints.items.read",
            "abc123",
        )
        self.assertIn("https://accounts.zoho.in/oauth/v2/auth", url)
        self.assertIn("client_id=test-client-id", url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback", url)
        self.assertIn("prompt=consent", url)
        self.assertIn("state=abc123", url)

    def test_rejects_markdown_redirect_uri(self):
        with self.assertRaisesRegex(RuntimeError, "plain URL"):
            validate_redirect_uri("[http://localhost:8765/callback](http://localhost:8765/callback)")


class WindowTests(unittest.TestCase):
    def make_config(self):
        return Config(
            team_id="1",
            project_id="2",
            project_name="test",
            zoho_domain="in",
            report_time=time(8, 30),
            tz=IST,
            working_weekdays={0, 1, 2, 3, 4},
            hours_per_day=8,
            review_remaining_hours=1,
            stale_working_days=2,
            high_bug_priorities={"highest"},
            my_user_id="me",
            unassigned_user_ids={"none"},
            regular_users={},
            lead_users={},
            excluded_user_ids=set(),
        )

    def test_tuesday_window_starts_monday(self):
        start, end = reporting_window(
            datetime(2026, 9, 22, 9, 0, tzinfo=IST),
            self.make_config(),
        )
        self.assertEqual(start.isoformat(), "2026-09-21T08:30:00+05:30")
        self.assertEqual(end.isoformat(), "2026-09-22T08:30:00+05:30")

    def test_monday_window_starts_friday(self):
        start, end = reporting_window(
            datetime(2026, 9, 21, 9, 0, tzinfo=IST),
            self.make_config(),
        )
        self.assertEqual(start.isoformat(), "2026-09-18T08:30:00+05:30")
        self.assertEqual(end.isoformat(), "2026-09-21T08:30:00+05:30")


class ReportTests(unittest.TestCase):
    def test_report_applies_status_log_priority_and_estimation_rules(self):
        config = Config(
            team_id="1",
            project_id="2",
            project_name="aumne-engine",
            zoho_domain="in",
            report_time=time(8, 30),
            tz=IST,
            working_weekdays={0, 1, 2, 3, 4},
            hours_per_day=8,
            review_remaining_hours=1,
            stale_working_days=2,
            high_bug_priorities={"highest"},
            my_user_id="me",
            unassigned_user_ids={"none"},
            regular_users={"me": "Me", "other": "Other"},
            lead_users={},
            excluded_user_ids=set(),
        )
        sprint = {
            "sprintId": "s1",
            "sprintName": "Sprint 1",
            "startDate": "2026-09-21T18:29:59.999Z",
            "endDate": "2026-09-25T18:29:59.999Z",
        }
        common = {
            "depth": "0",
            "immediateParentId": "",
            "parentItem": "",
            "blockedByObj": {},
            "isOverDue": False,
        }
        items = [
            {
                **common,
                "itemId": "i1",
                "itemNo": "1",
                "itemName": "Critical bug",
                "itemTypeName": "Bug",
                "projPriorityId": "p1",
                "statusName": "To do",
                "ownerId": ["me"],
                "duration": "1d",
                "createdTime": "2026-09-24T05:00:00Z",
                "completedDate": "-1",
            },
            {
                **common,
                "itemId": "i2",
                "itemNo": "2",
                "itemName": "Shared review",
                "itemTypeName": "Task",
                "projPriorityId": "p2",
                "statusName": "IN REVIEW",
                "ownerId": ["me", "other"],
                "duration": "4h",
                "createdTime": "2026-09-22T05:00:00Z",
                "completedDate": "-1",
            },
            {
                **common,
                "itemId": "i3",
                "itemNo": "3",
                "itemName": "No estimate",
                "itemTypeName": "Task",
                "projPriorityId": "p2",
                "statusName": "In progress",
                "ownerId": ["other"],
                "duration": "-1",
                "createdTime": "2026-09-23T05:00:00Z",
                "completedDate": "-1",
            },
        ]
        logs = [
            {
                "logId": "l1",
                "Owner": "me",
                "logDate": "2026-09-24T18:29:59.999Z",
                "logTime": "7200000",
                "logNotes": "<b>Investigated</b> issue",
                "logEntityId": "i1",
                "lastUpdatedTime": "2026-09-24T12:00:00Z",
            }
        ]
        report, state = generate_report(
            config,
            sprint,
            items,
            {"me": "Me", "other": "Other"},
            {"p1": "Highest", "p2": "Medium"},
            logs,
            {"i1": [], "i2": [], "i3": []},
            {},
            datetime(2026, 9, 24, 8, 30, tzinfo=IST),
            datetime(2026, 9, 25, 8, 30, tzinfo=IST),
        )
        self.assertIn("| Me | 2h |", report)
        self.assertIn("Investigated issue", report)
        self.assertNotIn("<b>", report)
        self.assertIn("#1 — Critical bug", report)
        self.assertIn("#3 — No estimate", report)
        self.assertEqual(state["sprint_id"], "s1")


if __name__ == "__main__":
    unittest.main()
