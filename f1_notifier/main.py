#!/usr/bin/env python3
"""F1 schedule reminders and session-result notifications via ntfy."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API = "https://api.openf1.org/v1"
JOLPICA_API = "https://api.jolpi.ca/ergast/f1"
YEAR = int(os.environ.get("F1_YEAR", datetime.now(timezone.utc).year))
USER_TZ = ZoneInfo(os.environ.get("USER_TIMEZONE", "Asia/Karachi"))
STATE_FILE = Path(os.environ.get("F1_STATE_FILE", "/tmp/f1-state.json"))
USER_AGENT = "F1RaceNotifier/1.0 (+https://github.com/rehanbabar17-art/f1-race-notifier)"
REMINDER_TYPES = {"Sprint", "Qualifying", "Race"}
SCHEDULE_WINDOWS = (30, 14, 7)


def api_get(path: str, **params):
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(
                f"{API}/{path}",
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=20,
            )
            if response.status_code in {401, 408, 425, 429} or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                delay = min(float(retry_after), 20) if retry_after and retry_after.isdigit() else 2 ** attempt
                last_error = requests.HTTPError(
                    f"{response.status_code} Server/API response for {response.url}", response=response
                )
                if attempt < 2:
                    print(f"OpenF1 temporary response {response.status_code}; retrying in {delay}s")
                    time.sleep(delay)
                    continue
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
    raise last_error or RuntimeError(f"OpenF1 request failed: {path}")


def jolpica_get(path: str):
    response = requests.get(
        f"{JOLPICA_API}/{path}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"sent": {}}
    try:
        state = json.loads(STATE_FILE.read_text())
        if not isinstance(state, dict) or not isinstance(state.get("sent"), dict):
            raise ValueError
        return state
    except (OSError, ValueError, json.JSONDecodeError):
        return {"sent": {}}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def notify(message: str, title: str, priority: str = "default") -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        raise RuntimeError("NTFY_TOPIC is not configured")
    for attempt in range(3):
        try:
            response = requests.post(
                f"https://ntfy.sh/{topic}",
                data=message.encode("utf-8"),
                headers={
                    "User-Agent": USER_AGENT,
                    "Title": title.encode("ascii", "replace").decode("ascii"),
                    "Priority": priority,
                    "Tags": "checkered_flag",
                },
                timeout=20,
            )
            response.raise_for_status()
            return
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def sessions() -> list[dict]:
    try:
        data = api_get("sessions", year=YEAR)
    except requests.RequestException as error:
        # A transient upstream outage should not turn into repeated cron failures.
        # The next cron poll will retry and state remains unchanged.
        print(f"OpenF1 sessions unavailable; using Jolpica fallback: {error}")
        try:
            return fallback_sessions()
        except requests.RequestException as fallback_error:
            print(f"Jolpica schedule unavailable; skipping this poll: {fallback_error}")
            return []
    return [item for item in data if not item.get("is_cancelled")]


def fallback_sessions() -> list[dict]:
    """Convert Jolpica's race calendar into the session shape used by the notifier."""
    payload = jolpica_get(f"{YEAR}.json")
    races = payload.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    output = []
    session_fields = (
        ("FirstPractice", "Practice 1"),
        ("SecondPractice", "Practice 2"),
        ("ThirdPractice", "Practice 3"),
        ("SprintQualifying", "Sprint Qualifying"),
        ("Sprint", "Sprint"),
        ("Qualifying", "Qualifying"),
        ("date", "Race"),
    )
    for race in races:
        race_round = str(race["round"])
        country = race.get("raceName", "F1").replace(" Grand Prix", "")
        meeting_key = f"jolpica:{YEAR}:{race_round}"
        for field, session_name in session_fields:
            value = race.get(field)
            if not value:
                continue
            date = value if isinstance(value, str) else value.get("date")
            if not date:
                continue
            time_value = value.get("time") if isinstance(value, dict) else race.get("time")
            start = parse_time(f"{date}T{time_value or '12:00:00Z'}")
            output.append({
                "meeting_key": meeting_key,
                "session_key": f"{meeting_key}:{session_name.lower().replace(' ', '-')}",
                "session_name": session_name,
                "country_name": country,
                "date_start": start.isoformat(),
                "date_end": (start + timedelta(hours=2)).isoformat(),
                "source": "jolpica",
                "round": race_round,
            })
    return output


def drivers_for(session_key: int) -> dict[int, str]:
    try:
        rows = api_get("drivers", session_key=session_key)
    except requests.RequestException:
        return {}
    return {
        int(row["driver_number"]): row.get("full_name") or row.get("name_acronym") or str(row["driver_number"])
        for row in rows
        if row.get("driver_number") is not None
    }


def fallback_results(session: dict) -> tuple[list[dict], dict[int, str]]:
    """Fetch race/qualifying/sprint results from Jolpica."""
    endpoint = {"Race": "results", "Qualifying": "qualifying", "Sprint": "sprint"}.get(session["session_name"])
    if not endpoint:
        return [], {}
    payload = jolpica_get(f"{YEAR}/{session['round']}/{endpoint}.json")
    races = payload.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    if not races:
        return [], {}
    rows = races[0].get("Results") or races[0].get("QualifyingResults") or races[0].get("SprintResults") or []
    results, drivers = [], {}
    for row in rows:
        driver = row.get("Driver", {})
        number = int(row.get("number", 0))
        drivers[number] = " ".join(filter(None, [driver.get("givenName"), driver.get("familyName")])) or str(number)
        position = row.get("position")
        try:
            position = int(position)
        except (TypeError, ValueError):
            position = None
        status = row.get("status")
        race_time = row.get("Time", {}).get("time")
        fastest_lap = row.get("FastestLap", {}).get("Time", {}).get("time")
        if session["session_name"] == "Qualifying":
            qualifying_time = next(
                (row.get(key, {}).get("time") for key in ("Q3", "Q2", "Q1") if row.get(key)),
                None,
            )
        else:
            qualifying_time = None
        results.append({
            "driver_number": number,
            "position": position,
            "status": status,
            "dnf": status not in {None, "Finished"} and not position,
            "dsq": status == "Disqualified",
            "dns": status == "Did not start",
            "lap_time": qualifying_time or race_time,
            "gap_to_leader": 0 if position == 1 else race_time if isinstance(race_time, str) and race_time.startswith("+") else None,
            "fastest_lap": fastest_lap,
        })
    return results, drivers


def session_label(session: dict) -> str:
    return f"{session['country_name']} GP — {session['session_name']}"


def normalize_results(results: list[dict]) -> list[dict]:
    """Map OpenF1's result-time variants to the formatter's canonical fields."""
    normalized = []
    for row in results:
        item = dict(row)
        item.setdefault(
            "lap_time",
            next(
                (item.get(field) for field in ("lap_time", "lap_duration", "duration", "time") if item.get(field) is not None),
                None,
            ),
        )
        normalized.append(item)
    return normalized


def format_schedule(upcoming: list[dict], days: int) -> str:
    grouped: dict[str, list[dict]] = {}
    for item in upcoming:
        local = parse_time(item["date_start"]).astimezone(USER_TZ)
        grouped.setdefault(local.strftime("%A, %d %b"), []).append(item)
    lines = [f"🏎️ F1 races in the next {days} days", ""]
    for day, items in grouped.items():
        lines.append(day)
        for item in items:
            local = parse_time(item["date_start"]).astimezone(USER_TZ)
            lines.append(f"  • {item['country_name']} — {item['session_name']} at {local:%I:%M %p %Z}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_day_reminder(items: list[dict], now: datetime) -> str:
    lines = [f"⏰ F1 sessions today — {now.astimezone(USER_TZ):%A, %d %b}", ""]
    for item in sorted(items, key=lambda x: x["date_start"]):
        local = parse_time(item["date_start"]).astimezone(USER_TZ)
        lines.append(f"  • {item['country_name']} — {item['session_name']} at {local:%I:%M %p %Z}")
    return "\n".join(lines)


def format_time(value) -> str:
    """Render numeric seconds or an API time string without losing precision."""
    if value is None or value == "":
        return "—"
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds >= 3600:
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            return f"{int(hours)}:{int(minutes):02d}:{seconds:06.3f}"
        minutes, seconds = divmod(seconds, 60)
        return f"{int(minutes)}:{seconds:06.3f}"
    return str(value)


def format_result(session: dict, results: list[dict], drivers: dict[int, str]) -> str:
    ordered = sorted(results, key=lambda row: row.get("position") or 999)
    lines = [f"🏁 {session_label(session)} — result", ""]
    for row in ordered:
        number = int(row.get("driver_number", 0))
        name = drivers.get(number, f"Driver {number}")
        position = row.get("position", "—")
        status = "DSQ" if row.get("dsq") else "DNF" if row.get("dnf") else "DNS" if row.get("dns") else str(position)
        gap = row.get("gap_to_leader")
        if isinstance(gap, (int, float)):
            gap_text = "0.000s" if not gap else f"+{gap:.3f}s"
        else:
            gap_text = str(gap) if gap else "—"
        fastest = row.get("fastest_lap")
        fastest_text = f"; FL {format_time(fastest)}" if fastest else ""
        lines.append(
            f"  {status}. {name} — time {format_time(row.get('lap_time'))}"
            f" — Δ leader {gap_text}{fastest_text}"
        )
    return "\n".join(lines)


NTFY_BODY_LIMIT = 3500


def split_notification(message: str, limit: int = NTFY_BODY_LIMIT) -> list[str]:
    """Split on complete driver lines while staying below ntfy's body limit."""
    if len(message.encode("utf-8")) <= limit:
        return [message]
    lines = message.splitlines()
    chunks, current = [], []
    current_size = 0
    for line in lines:
        line_size = len((line + "\n").encode("utf-8"))
        if current and current_size + line_size > limit:
            chunks.append("\n".join(current))
            current, current_size = [], 0
        current.append(line)
        current_size += line_size
    if current:
        chunks.append("\n".join(current))
    return chunks


def notify_result(message: str, title: str) -> None:
    chunks = split_notification(message)
    for index, chunk in enumerate(chunks, 1):
        chunk_title = f"{title} ({index}/{len(chunks)})" if len(chunks) > 1 else title
        notify(chunk, chunk_title)


def main() -> None:
    now = datetime.now(timezone.utc)
    state = load_state()
    sent = state["sent"]
    all_sessions = sessions()
    upcoming_races = [item for item in all_sessions if item["session_name"] == "Race"]

    # Send a full window schedule the first time a new meeting appears in it.
    # Rechecking on each run lets newly added races trigger an updated schedule.
    for days in SCHEDULE_WINDOWS:
        deadline = now + timedelta(days=days)
        window_races = [
            item for item in upcoming_races
            if now <= parse_time(item["date_start"]) <= deadline
        ]
        pending_meetings = sorted({
            item["meeting_key"] for item in window_races
            if f"schedule:{YEAR}:{days}:{item['meeting_key']}" not in sent
        })
        if not pending_meetings:
            continue
        notify(format_schedule(window_races, days), f"F1 schedule — next {days} days")
        for meeting_key in pending_meetings:
            sent[f"schedule:{YEAR}:{days}:{meeting_key}"] = now.isoformat()

    # One morning reminder for Sprint, Qualifying, and Race sessions on the user's day.
    local_today = now.astimezone(USER_TZ).date()
    today = [
        item for item in all_sessions
        if parse_time(item["date_start"]).astimezone(USER_TZ).date() == local_today
        and item["session_name"] in REMINDER_TYPES
    ]
    for meeting_key in sorted({item["meeting_key"] for item in today}):
        key = f"morning:{YEAR}:{local_today.isoformat()}:{meeting_key}"
        if key not in sent:
            notify(format_day_reminder([item for item in today if item["meeting_key"] == meeting_key], now), "F1 sessions today", "high")
            sent[key] = now.isoformat()

    # Publish each session result once, retrying naturally until OpenF1 has published it.
    for item in all_sessions:
        ended = parse_time(item["date_end"])
        if ended > now or now - ended > timedelta(days=3):
            continue
        key = f"result:{item['session_key']}"
        if key in sent:
            continue
        try:
            if item.get("source") == "jolpica":
                results, drivers = fallback_results(item)
            else:
                results = api_get("session_result", session_key=item["session_key"])
                drivers = drivers_for(item["session_key"])
        except requests.RequestException as error:
            print(f"Result not available yet for {session_label(item)}: {error}")
            continue
        if not results:
            print(f"Result not available yet for {session_label(item)}")
            continue
        results = normalize_results(results)
        notify_result(format_result(item, results, drivers), f"F1 result — {item['session_name']}")
        sent[key] = now.isoformat()

    # Keep the state compact while retaining a month of deduplication history.
    cutoff = now - timedelta(days=35)
    state["sent"] = {
        key: value for key, value in sent.items()
        if value and parse_time(value) >= cutoff
    }
    save_state(state)
    print(f"OK: checked {len(all_sessions)} sessions; sent-state entries={len(state['sent'])}")


if __name__ == "__main__":
    main()
