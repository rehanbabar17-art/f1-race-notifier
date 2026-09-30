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
