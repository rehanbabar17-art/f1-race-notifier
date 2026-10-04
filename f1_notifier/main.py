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
F1_LIVETIMING = "https://livetiming.formula1.com/static"
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


def livetiming_get(path: str):
    response = requests.get(
        f"{F1_LIVETIMING}/{path.lstrip('/')}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    return json.loads(response.content.decode("utf-8-sig"))


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
        int(row["driver_number"]): humanize_name(row.get("full_name") or row.get("name_acronym") or str(row["driver_number"]))
        for row in rows
        if row.get("driver_number") is not None
    }


def jolpica_round_for(session: dict) -> str | None:
    """Find a Jolpica round for a schedule row that came from OpenF1."""
    try:
        payload = jolpica_get(f"{YEAR}.json")
    except requests.RequestException:
        return None
    target_date = str(session.get("date_start", ""))[:10]
    target_country = str(session.get("country_name", "")).lower()
    races = payload.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    for race in races:
        race_date = str(race.get("date", ""))[:10]
        race_name = race.get("raceName", "").replace(" Grand Prix", "").lower()
        if (target_date and race_date == target_date) or (target_country and target_country in race_name):
            return str(race.get("round"))
    return None


def fallback_results(session: dict) -> tuple[list[dict], dict[int, str]]:
    """Fetch race/qualifying/sprint results from Jolpica."""
    endpoint = {"Race": "results", "Qualifying": "qualifying", "Sprint": "sprint"}.get(session["session_name"])
    if not endpoint:
        return [], {}
    round_number = session.get("round") or jolpica_round_for(session)
    if not round_number:
        return [], {}
    payload = jolpica_get(f"{YEAR}/{round_number}/{endpoint}.json")
    races = payload.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    if not races:
        return [], {}
    rows = races[0].get("Results") or races[0].get("QualifyingResults") or races[0].get("SprintResults") or []
    results, drivers = [], {}
    for row in rows:
        driver = row.get("Driver", {})
        number = int(row.get("number", 0))
        drivers[number] = humanize_name(" ".join(filter(None, [driver.get("givenName"), driver.get("familyName")])) or str(number))
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
            "result_time": qualifying_time or race_time,
            "gap_to_leader": 0 if position == 1 else race_time if isinstance(race_time, str) and race_time.startswith("+") else None,
            "fastest_lap": fastest_lap,
        })
    return results, drivers


def official_livetiming_results(session: dict) -> tuple[list[dict], dict[int, str]]:
    """Read finalized positions and live lap timing from F1's official archive."""
    try:
        session_key = int(session["session_key"])
    except (KeyError, TypeError, ValueError):
        return [], {}
    index = livetiming_get(f"{YEAR}/Index.json")
    official_session = next(
        (
            item
            for meeting in index.get("Meetings", [])
            for item in meeting.get("Sessions", [])
            if item.get("Key") == session_key
        ),
        None,
    )
    if not official_session:
        return [], {}
    base = official_session.get("Path")
    if not base:
        return [], {}
    info = livetiming_get(f"{base}SessionInfo.json")
    if info.get("SessionStatus") not in {"Finalised", "Finalized"}:
        return [], {}
    timing = livetiming_get(f"{base}TimingData.json").get("Lines", {})
    driver_rows = livetiming_get(f"{base}DriverList.json")
    results, drivers = [], {}
    for number, row in timing.items():
        try:
            driver_number = int(row.get("RacingNumber", number))
        except (TypeError, ValueError):
            continue
        driver = driver_rows.get(str(driver_number), {})
        full_name = " ".join(filter(None, [driver.get("FirstName"), driver.get("LastName")]))
        drivers[driver_number] = humanize_name(full_name or driver.get("FullName") or driver.get("BroadcastName") or str(driver_number))
        try:
            position = int(row.get("Position"))
        except (TypeError, ValueError):
            position = None
        gap = row.get("GapToLeader") or (0 if position == 1 else None)
        last_lap = row.get("LastLapTime", {}).get("Value")
        best_lap = row.get("BestLapTime", {}).get("Value")
        results.append({
            "driver_number": driver_number,
            "position": position,
            "dnf": bool(row.get("Retired")) and not position,
            "dsq": False,
            "dns": False,
            "lap_time": last_lap,
            "result_time": None,
            "gap_to_leader": gap,
            "fastest_lap": best_lap,
        })
    return results, drivers


def official_fastest_laps(session: dict) -> dict[int, str]:
    """Read per-driver best laps even if the official session is not finalized yet."""
    try:
        session_key = int(session["session_key"])
        index = livetiming_get(f"{YEAR}/Index.json")
        official_session = next(
            (
                item
                for meeting in index.get("Meetings", [])
                for item in meeting.get("Sessions", [])
                if item.get("Key") == session_key
            ),
            None,
        )
        if not official_session or not official_session.get("Path"):
            return {}
        lines = livetiming_get(f"{official_session['Path']}TimingData.json").get("Lines", {})
        fastest = {}
        for number, row in lines.items():
            driver_number = int(row.get("RacingNumber", number))
            value = row.get("BestLapTime", {}).get("Value")
            if value:
                fastest[driver_number] = value
        return fastest
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return {}


def openf1_fastest_laps(session: dict) -> dict[int, float]:
    """Compute each driver's fastest completed lap from OpenF1 lap records."""
    try:
        rows = api_get("laps", session_key=session["session_key"])
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return {}
    fastest = {}
    for row in rows:
        number = row.get("driver_number")
        duration = row.get("lap_duration")
        if number is None or duration in {None, ""} or row.get("is_pit_out_lap"):
            continue
        try:
            duration = float(duration)
            number = int(number)
        except (TypeError, ValueError):
            continue
        if duration > 0 and (number not in fastest or duration < fastest[number]):
            fastest[number] = duration
    return fastest


def attach_official_fastest_laps(session: dict, results: list[dict]) -> list[dict]:
    official_fastest = official_fastest_laps(session)
    if not official_fastest:
        official_fastest = openf1_fastest_laps(session)
    if not official_fastest and session.get("session_name") in {"Race", "Qualifying", "Sprint"}:
        try:
            fallback, _ = fallback_results(session)
            official_fastest = {
                int(row["driver_number"]): row["fastest_lap"]
                for row in fallback
                if row.get("fastest_lap")
            }
        except (requests.RequestException, KeyError, TypeError, ValueError):
            official_fastest = {}
    if not official_fastest:
        return results
    enriched = []
    for result in results:
        item = dict(result)
        if not item.get("fastest_lap"):
            item["fastest_lap"] = official_fastest.get(int(item["driver_number"]))
        enriched.append(item)
    return enriched


def enrich_result_times(session: dict, results: list[dict]) -> list[dict]:
    """Add total session durations and official fastest laps when available."""
    results = attach_official_fastest_laps(session, results)
    try:
        rows = api_get("session_result", session_key=session["session_key"])
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return results
    by_driver = {int(row["driver_number"]): row for row in rows if row.get("driver_number") is not None}
    enriched = []
    for result in results:
        item = dict(result)
        source = by_driver.get(int(item["driver_number"]))
        if source:
            result_time = source.get("duration")
            if isinstance(result_time, list):
                result_time = next((value for value in reversed(result_time) if value is not None), None)
            item["result_time"] = result_time
            if item.get("gap_to_leader") in {None, ""}:
                item["gap_to_leader"] = source.get("gap_to_leader")
        enriched.append(item)
    return enriched


def fetch_results(session: dict) -> tuple[list[dict], dict[int, str]]:
    """Try official timing first, then OpenF1, then Jolpica."""
    errors = []
    try:
        results, drivers = official_livetiming_results(session)
        if results:
            return enrich_result_times(session, results), drivers
    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        errors.append(f"official F1 LiveTiming: {error}")
    try:
        if session.get("source") == "jolpica":
            results, drivers = fallback_results(session)
        else:
            results = api_get("session_result", session_key=session["session_key"])
            drivers = drivers_for(session["session_key"])
        if results:
            return attach_official_fastest_laps(session, results), drivers
    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        errors.append(f"OpenF1/Jolpica: {error}")
    if session.get("source") != "jolpica" and session.get("session_name") in {"Race", "Qualifying", "Sprint"}:
        try:
            results, drivers = fallback_results(session)
            if results:
                return results, drivers
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            errors.append(f"Jolpica fallback: {error}")
    if errors:
        print(f"No result source available for {session_label(session)}; " + " | ".join(errors))
    return [], {}


def session_label(session: dict) -> str:
    return f"{session['country_name']} GP — {session['session_name']}"


def humanize_name(name: str) -> str:
    """Use normal display casing without preserving feed-specific uppercase names."""
    return " ".join(part.capitalize() for part in str(name).split())


def result_key(session: dict) -> str:
    """Stable identity shared by OpenF1 and Jolpica schedule representations."""
    country = "".join(char.lower() if char.isalnum() else "-" for char in session.get("country_name", "f1"))
    date = str(session.get("date_start", ""))[:10]
    return f"result:{YEAR}:{country}:{date}:{session['session_name'].lower()}"


def time_value(value) -> float | None:
    """Convert a displayed lap time to seconds for fastest-lap comparison."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        parts = [float(part) for part in value.split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return None
    return None


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
        item.setdefault("result_time", item.get("duration"))
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
    fastest_values = [
        (time_value(row.get("fastest_lap")), int(row.get("driver_number", 0)))
        for row in ordered
        if time_value(row.get("fastest_lap")) is not None
    ]
    fastest_numbers = set()
    if fastest_values:
        fastest = min(value for value, _ in fastest_values)
        fastest_numbers = {number for value, number in fastest_values if abs(value - fastest) < 0.001}
    lines = [f"🏁 {session_label(session)} — result", ""]
    for row in ordered:
        number = int(row.get("driver_number", 0))
        name = humanize_name(drivers.get(number, f"Driver {number}"))
        fastest_marker = " ⚡" if number in fastest_numbers else ""
        position = row.get("position", "—")
        status = "DSQ" if row.get("dsq") else "DNF" if row.get("dnf") else "DNS" if row.get("dns") else str(position)
        gap = row.get("gap_to_leader")
        if isinstance(gap, (int, float)):
            gap_text = "0.000s" if not gap else f"+{gap:.3f}s"
        else:
            gap_text = str(gap) if gap else "—"
            if gap_text.startswith("+") and not gap_text.endswith("s"):
                gap_text += "s"
        medal = {1: "🥇 ", 2: "🥈 ", 3: "🥉 "}.get(position, "")
        display_time = row.get("result_time")
        if display_time is None:
            display_time = row.get("lap_time")
        lines.append(
            f"  {medal}{status}. {name}{fastest_marker} — {format_time(display_time)}"
            f" — Δ {gap_text}"
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


def notify_result(
    message: str,
    title: str,
    sent: dict | None = None,
    key: str | None = None,
    now: datetime | None = None,
    state: dict | None = None,
) -> None:
    chunks = split_notification(message)
    for index, chunk in enumerate(chunks, 1):
        chunk_title = f"{title} ({index}/{len(chunks)})" if len(chunks) > 1 else title
        chunk_key = f"{key}:part:{index}" if key else None
        if sent is not None and chunk_key in sent:
            continue
        notify(chunk, chunk_title)
        if sent is not None and chunk_key:
            sent[chunk_key] = (now or datetime.now(timezone.utc)).isoformat()
            if state is not None:
                save_state(state)


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
        save_state(state)

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
            save_state(state)

    # Publish each session result once, retrying naturally until OpenF1 has published it.
    for item in all_sessions:
        ended = parse_time(item["date_end"])
        if ended > now or now - ended > timedelta(days=3):
            continue
        key = result_key(item)
        legacy_key = f"result:{item['session_key']}"
        if key in sent or legacy_key in sent:
            continue
        results, drivers = fetch_results(item)
        if not results:
            print(f"Result not available yet for {session_label(item)}")
            continue
        results = normalize_results(results)
        notify_result(
            format_result(item, results, drivers),
            f"F1 result — {item['session_name']}",
            sent,
            key,
            now,
            state,
        )
        sent[key] = now.isoformat()
        save_state(state)

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
