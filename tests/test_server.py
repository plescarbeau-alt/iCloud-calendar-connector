import unittest
from datetime import datetime
from types import SimpleNamespace

from icalendar import Calendar, Event

from scripts import server


class DateTimeTests(unittest.TestCase):
    def test_named_timezone_survives_icalendar_round_trip(self):
        start = server._parse_datetime(
            "2026-08-01T17:00:00-04:00", "America/Toronto"
        )
        event = Event()
        event.add("uid", "test")
        event.add("dtstart", start)
        calendar = Calendar()
        calendar.add("prodid", "test")
        calendar.add("version", "2.0")
        calendar.add_component(event)

        parsed = Calendar.from_ical(calendar.to_ical()).walk("VEVENT")[0]["dtstart"].dt

        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.isoformat(), "2026-08-01T17:00:00-04:00")

    def test_rejects_offset_that_disagrees_with_named_timezone(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            server._parse_datetime(
                "2026-08-01T17:00:00-05:00", "America/Toronto"
            )

    def test_legacy_floating_event_matches_requested_local_time(self):
        component = Event()
        component.add("summary", "Test Zoom v10")
        component.add("dtstart", datetime(2026, 8, 1, 17, 0))
        component.add("dtend", datetime(2026, 8, 1, 17, 15))
        start = server._parse_datetime("2026-08-01T17:00:00-04:00")
        end = server._parse_datetime("2026-08-01T17:15:00-04:00")

        self.assertTrue(
            server._component_matches_exactly(
                component, "Test Zoom v10", start, end
            )
        )


class DeleteTests(unittest.TestCase):
    def test_uid_lookup_uses_bounded_search_after_report_error(self):
        start = server._parse_datetime("2026-08-06T17:00:00-04:00")
        end = server._parse_datetime("2026-08-06T17:15:00-04:00")
        wanted = Event()
        wanted.add("uid", "wanted")
        wanted.add("dtstart", start)
        wanted.add("dtend", end)
        other = Event()
        other.add("uid", "other")
        other.add("dtstart", start)
        other.add("dtend", end)

        class Resource:
            def __init__(self, component):
                self.component = component

            def get_icalendar_component(self):
                return self.component

        class Calendar:
            search_calls = []

            def get_event_by_uid(self, _):
                raise RuntimeError("412 Precondition Failed")

            def search(self, **kwargs):
                self.search_calls.append(kwargs)
                return [Resource(other), Resource(wanted)]

            def events(self):
                raise AssertionError("the whole calendar must never be listed")

        calendar = Calendar()
        found = server._resource_by_uid(calendar, "wanted", start, end)

        self.assertEqual(str(found.get_icalendar_component()["uid"]), "wanted")
        self.assertEqual(len(calendar.search_calls), 1)

    def test_window_search_retries_with_bounded_padding(self):
        class Calendar:
            calls = []

            def search(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) < 3:
                    raise RuntimeError("412 Precondition Failed")
                return []

            def events(self):
                raise AssertionError("the whole calendar must never be listed")

        calendar = Calendar()
        start = server._parse_datetime("2026-08-06T17:00:00-04:00")
        end = server._parse_datetime("2026-08-06T17:15:00-04:00")

        self.assertEqual(
            server._calendar_resources_in_window(
                calendar, start, end, expand=False
            ),
            [],
        )
        self.assertEqual(len(calendar.calls), 3)
        self.assertEqual(calendar.calls[-1]["start"], start - server.timedelta(days=7))

    def test_delete_uses_current_etag_and_schedule_tag(self):
        calls = []

        class Client:
            def request(self, url, method, body, headers):
                calls.append((url, method, body, headers))
                return SimpleNamespace(status=204)

        class Resource:
            url = "https://caldav.example/event.ics"
            client = Client()
            etag = None
            schedule_tag = None

            def get_properties(self, _):
                self.etag = '"fresh-etag"'
                self.schedule_tag = '"fresh-schedule-tag"'

        status = server._resource_delete_status(Resource())

        self.assertEqual(status, 204)
        self.assertEqual(calls[0][1], "DELETE")
        self.assertEqual(calls[0][3]["if-match"], '"fresh-etag"')
        self.assertEqual(
            calls[0][3]["if-schedule-tag-match"], '"fresh-schedule-tag"'
        )

    def test_delete_does_not_treat_412_as_success(self):
        class Client:
            def request(self, *_):
                return SimpleNamespace(status=412)

        class Resource:
            url = "https://caldav.example/event.ics"
            client = Client()
            etag = '"fresh-etag"'
            schedule_tag = None

            def get_properties(self, _):
                return {}

        self.assertEqual(server._resource_delete_status(Resource()), 412)


if __name__ == "__main__":
    unittest.main()
