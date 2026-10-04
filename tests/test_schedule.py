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
            }
            for number in range(22, 0, -1)
        ]
        drivers = {number: f"Driver {number}" for number in range(1, 23)}
        message = notifier.format_result(session, results, drivers)
        self.assertEqual(message.count("— time "), 22)
        self.assertIn("1. Driver 1", message)
        self.assertIn("22. Driver 22", message)
        self.assertIn("Δ leader 0.000s", message)
        self.assertIn("Δ leader +5.500s", message)

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
