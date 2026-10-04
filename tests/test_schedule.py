from datetime import datetime as RealDateTime, timedelta, timezone
import unittest
from unittest.mock import patch

import f1_notifier.main as notifier


NOW = RealDateTime(2026, 1, 1, 12, tzinfo=timezone.utc)


class FrozenDateTime(RealDateTime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def race(meeting_key, country, days):
    start = NOW + timedelta(days=days)
    end = start + timedelta(hours=2)
    return {
        "meeting_key": meeting_key,
        "session_key": meeting_key,
        "session_name": "Race",
        "country_name": country,
        "date_start": start.isoformat(),
        "date_end": end.isoformat(),
    }


class ScheduleTests(unittest.TestCase):
    def run_notifier(self, state, races, notify):
        with (
            patch.object(notifier, "datetime", FrozenDateTime),
            patch.object(notifier, "YEAR", 2026),
            patch.object(notifier, "sessions", return_value=races),
            patch.object(notifier, "load_state", return_value=state),
            patch.object(notifier, "save_state"),
            patch.object(notifier, "notify", side_effect=notify),
        ):
            notifier.main()

    def test_schedule_lists_races_for_requested_window(self):
        message = notifier.format_schedule([race(1, "Bahrain", 12)], 14)
        self.assertIn("F1 races in the next 14 days", message)
        self.assertIn("Bahrain — Race", message)

    def test_result_includes_all_drivers_times_and_leader_delta(self):
        session = {"country_name": "Bahrain", "session_name": "Race"}
        results = [
            {
                "driver_number": number,
                "position": number,
                "lap_time": 5400 + number,
                "gap_to_leader": 0 if number == 1 else number * 0.25,
                "fastest_lap": "1:20.000" if number == 2 else "1:21.000",
            }
            for number in range(22, 0, -1)
        ]
        drivers = {number: f"Driver {number}" for number in range(1, 23)}
        message = notifier.format_result(session, results, drivers)
        self.assertEqual(message.count(" — Δ "), 22)
        self.assertIn("🥇 1. Driver 1 — 1:30:01.000 — Δ 0.000s", message)
        self.assertIn("🥈 2. Driver 2 ⚡ — 1:30:02.000 — Δ +0.500s", message)
        self.assertIn("🥉 3. Driver 3 — 1:30:03.000 — Δ +0.750s", message)
        self.assertIn("22. Driver 22 — 1:30:22.000 — Δ +5.500s", message)
        self.assertIn("🥈 2. Driver 2 ⚡ — 1:30:02.000 — Δ +0.500s", message)

    def test_result_names_are_normalized_without_uppercase_feed_formatting(self):
        message = notifier.format_result(
            {"country_name": "Bahrain", "session_name": "Race"},
            [{"driver_number": 1, "position": 1, "lap_time": "1:20.000", "gap_to_leader": 0}],
            {1: "MAX VERSTAPPEN"},
        )
        self.assertIn("Max Verstappen", message)
        self.assertNotIn("MAX VERSTAPPEN", message)

    def test_result_notification_skips_previously_sent_chunks(self):
        message = "🏁 result\n\n" + "\n".join(f"  {i}. Driver {i}" for i in range(1, 8))
        sent = {"result:test:part:1": NOW.isoformat()}
        state = {"sent": sent}
        calls = []
        with patch.object(notifier, "notify", side_effect=lambda *args: calls.append(args)):
            notifier.notify_result(message, "F1 result", sent, "result:test", NOW, state)
        self.assertEqual(len(calls), len(notifier.split_notification(message)) - 1)
        self.assertNotIn("result:test:part:1", [args[0] for args in calls])

    def test_forget_latest_race_result_removes_base_and_split_keys_only(self):
        older = (NOW - timedelta(minutes=10)).isoformat()
        latest = (NOW - timedelta(minutes=2)).isoformat()
        state = {"sent": {
            "result:2026:bahrain:2026-10-04:race": latest,
            "result:2026:bahrain:2026-10-04:race:part:1": latest,
            "result:2026:bahrain:2026-10-04:race:part:2": latest,
            "result:2026:bahrain:2026-10-04:qualifying": older,
        }}
        forgotten = notifier.forget_latest_race_result(state)
        self.assertEqual(forgotten, "result:2026:bahrain:2026-10-04:race")
        self.assertEqual(state["sent"], {"result:2026:bahrain:2026-10-04:qualifying": older})

    def test_long_result_is_split_without_dropping_driver_lines(self):
        message = "🏁 test\n\n" + "\n".join(
            f"  {i}. Driver {i} — time 1:22.123 — Δ leader +{i}.123s"
            for i in range(1, 23)
        )
        chunks = notifier.split_notification(message, limit=200)
        self.assertGreater(len(chunks), 1)
        combined = "\n".join(chunks)
        for number in range(1, 23):
            self.assertEqual(combined.count(f"  {number}. Driver {number}"), 1)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 200 for chunk in chunks))

    def test_result_sources_prefer_official_livetiming(self):
        session = {"session_key": 123, "session_name": "Race", "country_name": "Bahrain"}
        official = ([{"driver_number": 1, "position": 1, "lap_time": "1:30.000"}], {1: "Driver 1"})
        with (
            patch.object(notifier, "official_livetiming_results", return_value=official) as official_call,
            patch.object(notifier, "enrich_result_times", return_value=official[0]),
            patch.object(notifier, "api_get") as openf1_call,
            patch.object(notifier, "fallback_results") as jolpica_call,
        ):
            result = notifier.fetch_results(session)
        self.assertEqual(result, official)
        official_call.assert_called_once_with(session)
        openf1_call.assert_not_called()
        jolpica_call.assert_not_called()

    def test_result_sources_reach_jolpica_when_openf1_is_empty(self):
        session = {"session_key": 123, "session_name": "Race", "country_name": "Bahrain"}
        fallback = ([{"driver_number": 1, "position": 1, "lap_time": "1:30.000"}], {1: "Driver 1"})
        with (
            patch.object(notifier, "official_livetiming_results", return_value=([], {})),
            patch.object(notifier, "api_get", return_value=[]),
            patch.object(notifier, "drivers_for", return_value={}),
            patch.object(notifier, "fallback_results", return_value=fallback) as jolpica_call,
        ):
            result = notifier.fetch_results(session)
        self.assertEqual(result, fallback)
        jolpica_call.assert_called_once_with(session)

    def test_new_race_updates_each_applicable_window_once(self):
        state = {"sent": {}}
        first_race = race(1, "Bahrain", 10)
        messages = []
        self.run_notifier(state, [first_race], lambda *args, **kwargs: messages.append(args))

        self.assertEqual(len(messages), 2)
        self.assertIn("F1 schedule — next 30 days", [args[1] for args in messages])
        self.assertIn("F1 schedule — next 14 days", [args[1] for args in messages])
        self.assertNotIn("schedule:2026:7:1", state["sent"])

        # A new meeting added after the first poll updates all windows it belongs to.
        messages.clear()
        second_race = race(2, "Australia", 6)
        self.run_notifier(state, [first_race, second_race], lambda *args, **kwargs: messages.append(args))
        titles = [args[1] for args in messages]
        self.assertCountEqual(titles, [
            "F1 schedule — next 30 days",
            "F1 schedule — next 14 days",
            "F1 schedule — next 7 days",
        ])
        for body, title in messages:
            self.assertIn("Australia — Race", body)
            if "30 days" in title or "14 days" in title:
                self.assertIn("Bahrain — Race", body)
            else:
                self.assertNotIn("Bahrain — Race", body)

        # Another run with the same schedule does not resend messages.
        messages.clear()
        self.run_notifier(state, [first_race, second_race], lambda *args, **kwargs: messages.append(args))
        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main()
