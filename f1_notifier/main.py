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
RACE_POSITION_POINTS = (25, 18, 15, 12, 10, 8, 6, 4, 2, 1)
SPRINT_POSITION_POINTS = (8, 7, 6, 5, 4, 3, 2, 1)
TEAM_ALIASES = {
    "red bull racing": ("red_bull", "Red Bull"),
    "red bull": ("red_bull", "Red Bull"),
    "racing bulls": ("rb", "RB F1 Team"),
    "rb f1 team": ("rb", "RB F1 Team"),
    "rb": ("rb", "RB F1 Team"),
    "visa cash app rb": ("rb", "RB F1 Team"),
    "visa cash app racing bulls": ("rb", "RB F1 Team"),
    "ferrari": ("ferrari", "Ferrari"),
    "scuderia ferrari": ("ferrari", "Ferrari"),
    "mercedes": ("mercedes", "Mercedes"),
    "mercedes-amg petronas f1 team": ("mercedes", "Mercedes"),
    "mercedes-amg petronas formula one team": ("mercedes", "Mercedes"),
    "mclaren": ("mclaren", "McLaren"),
    "mclaren f1 team": ("mclaren", "McLaren"),
    "alpine": ("alpine", "Alpine F1 Team"),
    "alpine f1 team": ("alpine", "Alpine F1 Team"),
    "haas": ("haas", "Haas F1 Team"),
    "haas f1 team": ("haas", "Haas F1 Team"),
    "audi": ("audi", "Audi"),
    "audi f1 team": ("audi", "Audi"),
    "williams": ("williams", "Williams"),
    "williams f1 team": ("williams", "Williams"),
    "aston martin": ("aston_martin", "Aston Martin"),
    "aston martin aramco": ("aston_martin", "Aston Martin"),
    "aston martin aramco f1 team": ("aston_martin", "Aston Martin"),
    "aston martin f1 team": ("aston_martin", "Aston Martin"),
    "cadillac": ("cadillac", "Cadillac F1 Team"),
    "cadillac f1 team": ("cadillac", "Cadillac F1 Team"),
}


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


def session_result_get(session_key: int | str) -> list[dict]:
    """Retry briefly when OpenF1 has not published a just-finished result yet."""
    last_error = None
    for attempt in range(3):
        try:
            return api_get("session_result", session_key=session_key)
        except requests.HTTPError as error:
            last_error = error
            if getattr(error.response, "status_code", None) != 404 or attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    raise last_error or RuntimeError(f"OpenF1 session result unavailable: {session_key}")


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


def driver_metadata_for(session: dict) -> dict[int, dict]:
    """Return OpenF1 driver/team metadata when the session has an OpenF1 key."""
    try:
        session_key = int(session["session_key"])
        rows = api_get("drivers", session_key=session_key)
    except (requests.RequestException, KeyError, TypeError, ValueError):
        return {}
    return {
        int(row["driver_number"]): row
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
        rows = session_result_get(session["session_key"])
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
            results = session_result_get(session["session_key"])
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


def standings_key(session: dict, category: str, phase: str = "provisional") -> str:
    return f"{result_key(session)}:standings:points-v3:{phase}:{category}"


def jolpica_result_rows(endpoint: str) -> list[tuple[int, dict]]:
    """Return all season result rows as (round, result) pairs, following pagination."""
    page_size = 100
    offset = 0
    output = []
    results_field = "Results" if endpoint == "results" else "SprintResults"
    while True:
        payload = jolpica_get(f"{YEAR}/{endpoint}.json?limit={page_size}&offset={offset}")
        data = payload.get("MRData", {})
        races = data.get("RaceTable", {}).get("Races", [])
        for race in races:
            try:
                round_number = int(race["round"])
            except (KeyError, TypeError, ValueError):
                continue
            output.extend((round_number, row) for row in race.get(results_field, []))
        try:
            total = int(data.get("total", 0))
            limit = int(data.get("limit", page_size))
        except (TypeError, ValueError):
            break
        if not races or offset + limit >= total:
            break
        offset += limit
    return output


def jolpica_round_result_rows(round_number: int, endpoint: str) -> list[dict]:
    """Fetch one round's point-bearing results to check event readiness."""
    payload = jolpica_get(f"{YEAR}/{round_number}/{endpoint}.json?limit=100")
    results_field = "Results" if endpoint == "results" else "SprintResults"
    output = []
    for race in payload.get("MRData", {}).get("RaceTable", {}).get("Races", []):
        try:
            if int(race.get("round", 0)) == round_number:
                output.extend(race.get(results_field, []))
        except (TypeError, ValueError):
            continue
    return output


def season_result_rows() -> tuple[list[tuple[int, dict]], list[tuple[int, dict]]]:
    return jolpica_result_rows("results"), jolpica_result_rows("sprint")


def session_round(session: dict) -> int:
    try:
        return int(session.get("round") or jolpica_round_for(session) or 0)
    except (TypeError, ValueError):
        return 0


def constructor_details(name: str | None, constructor_id: str | None = None) -> tuple[str, str]:
    display_name = str(name or constructor_id or "Unknown")
    if constructor_id:
        return str(constructor_id), display_name
    normalized = " ".join(display_name.lower().split())
    if normalized in TEAM_ALIASES:
        return TEAM_ALIASES[normalized]
    identifier = "".join(char.lower() if char.isalnum() else "_" for char in normalized).strip("_")
    while "__" in identifier:
        identifier = identifier.replace("__", "_")
    return identifier or "unknown", display_name


def add_standing_points(
    driver_totals: dict,
    constructor_totals: dict,
    driver: dict,
    constructor: dict,
    points: float,
    race_win: bool = False,
) -> tuple[str, str] | None:
    driver_key = str(driver.get("permanentNumber") or driver.get("driverId") or "")
    constructor_id = str(constructor.get("constructorId") or "")
    if not driver_key or not constructor_id:
        return None
    driver_entry = driver_totals.setdefault(
        driver_key, {"Driver": driver, "points": 0.0, "wins": 0}
    )
    driver_entry["points"] += points
    constructor_entry = constructor_totals.setdefault(
        constructor_id, {"Constructor": constructor, "points": 0.0, "wins": 0}
    )
    constructor_entry["points"] += points
    if race_win:
        driver_entry["wins"] += 1
        constructor_entry["wins"] += 1
    return driver_key, constructor_id


def standings_rows_from_totals(driver_totals: dict, constructor_totals: dict) -> tuple[list[dict], list[dict]]:
    ordered_drivers = sorted(
        driver_totals.values(),
        key=lambda item: (-item["points"], -item["wins"], item["Driver"].get("driverId", "")),
    )
    ordered_constructors = sorted(
        constructor_totals.values(),
        key=lambda item: (-item["points"], -item["wins"], item["Constructor"].get("constructorId", "")),
    )
    return (
        [
            {"position": position, "points": item["points"], "Driver": item["Driver"]}
            for position, item in enumerate(ordered_drivers, 1)
        ],
        [
            {"position": position, "points": item["points"], "Constructor": item["Constructor"]}
            for position, item in enumerate(ordered_constructors, 1)
        ],
    )


def standings_signature(drivers: list[dict], constructors: list[dict]) -> dict[str, dict[str, float]]:
    driver_points = {}
    for row in drivers:
        driver = row.get("Driver", {})
        key = str(driver.get("permanentNumber") or driver.get("driverId") or "")
        if key:
            driver_points[key] = round(float(row.get("points") or 0), 3)
    constructor_points = {}
    for row in constructors:
        constructor = row.get("Constructor", {})
        key = str(constructor.get("constructorId") or "")
        if key:
            constructor_points[key] = round(float(row.get("points") or 0), 3)
    return {"drivers": driver_points, "constructors": constructor_points}


def estimate_standings(
    session: dict,
    event_results: list[dict],
    event_drivers: dict[int, str],
    state: dict,
    season_rows: tuple[list[tuple[int, dict]], list[tuple[int, dict]]] | None = None,
) -> tuple[list[dict], list[dict], dict]:
    """Estimate championship totals from prior points and this event's finishing places."""
    round_number = session_round(session)
    session_name = session.get("session_name")
    if not round_number or session_name not in {"Sprint", "Race"} or not event_results:
        return [], [], {}
    race_rows, sprint_rows = season_rows or season_result_rows()
    driver_totals: dict[str, dict] = {}
    constructor_totals: dict[str, dict] = {}
    driver_by_number: dict[int, dict] = {}
    constructor_by_number: dict[int, dict] = {}

    def record_historical(round_id: int, row: dict, is_race: bool) -> bool:
        driver = dict(row.get("Driver", {}))
        constructor = dict(row.get("Constructor", {}))
        number = driver.get("permanentNumber") or row.get("number")
        if number is not None:
            try:
                driver_by_number[int(number)] = driver
                constructor_by_number[int(number)] = constructor
            except (TypeError, ValueError):
                pass
        if row.get("points") in {None, ""}:
            return False
        try:
            points = float(row.get("points") or 0)
        except (TypeError, ValueError):
            return False
        if constructor.get("constructorId") is None and constructor.get("name"):
            constructor_id, constructor_name = constructor_details(constructor["name"])
            constructor.update({"constructorId": constructor_id, "name": constructor_name})
        add_standing_points(
            driver_totals,
            constructor_totals,
            driver,
            constructor,
            points,
            race_win=is_race and str(row.get("position")) == "1",
        )
        return True

    for round_id, row in race_rows:
        if round_id < round_number:
            record_historical(round_id, row, True)
    for round_id, row in sprint_rows:
        if round_id < round_number:
            record_historical(round_id, row, False)

    # For a Grand Prix estimate, include this weekend's Sprint points if the
    # point feed is complete; otherwise use the saved Sprint estimate.
    if session_name == "Race":
        current_sprint_rows = [row for round_id, row in sprint_rows if round_id == round_number]
        sprint_complete = (
            len(current_sprint_rows) >= 18
            and all(row.get("points") not in {None, ""} for row in current_sprint_rows)
        )
        if sprint_complete:
            for row in current_sprint_rows:
                record_historical(round_number, row, False)
        else:
            snapshots = state.setdefault("standings_snapshots", {})
            for saved in snapshots.values():
                if (
                    saved.get("round") == round_number
                    and saved.get("session_name") == "Sprint"
                    and str(saved.get("meeting_key")) == str(session.get("meeting_key"))
                ):
                    for driver_key, entry in saved.get("event_driver_points", {}).items():
                        driver = dict(entry.get("Driver", {}))
                        driver.setdefault("permanentNumber", driver_key)
                        driver_totals.setdefault(
                            str(driver_key), {"Driver": driver, "points": 0.0, "wins": 0}
                        )["points"] += float(entry.get("points") or 0)
                    for constructor_id, entry in saved.get("event_constructor_points", {}).items():
                        constructor = dict(entry.get("Constructor", {}))
                        constructor.setdefault("constructorId", constructor_id)
                        constructor_totals.setdefault(
                            str(constructor_id), {"Constructor": constructor, "points": 0.0, "wins": 0}
                        )["points"] += float(entry.get("points") or 0)

    metadata = driver_metadata_for(session)
    endpoint = "sprint" if session_name == "Sprint" else "results"
    current_rows = [row for round_id, row in (sprint_rows if endpoint == "sprint" else race_rows) if round_id == round_number]
    jolpica_by_number = {}
    for row in current_rows:
        try:
            jolpica_by_number[int(row.get("number"))] = row
        except (TypeError, ValueError):
            continue

    point_table = SPRINT_POSITION_POINTS if session_name == "Sprint" else RACE_POSITION_POINTS
    event_driver_points = {}
    event_constructor_points = {}
    for result in event_results:
        try:
            number = int(result.get("driver_number"))
        except (TypeError, ValueError):
            continue
        try:
            position = int(result.get("position"))
        except (TypeError, ValueError):
            position = 0
        points = 0.0
        if not result.get("dsq") and not result.get("dns") and 1 <= position <= len(point_table):
            points = float(point_table[position - 1])

        openf1_driver = metadata.get(number, {})
        jolpica_result = jolpica_by_number.get(number, {})
        official_driver = jolpica_result.get("Driver", {})
        driver = dict(driver_by_number.get(number) or official_driver or {})
        if not driver:
            full_name = " ".join(filter(None, [openf1_driver.get("first_name"), openf1_driver.get("last_name")]))
            full_name = full_name or event_drivers.get(number) or f"Driver {number}"
            name_parts = humanize_name(full_name).split()
            driver = {
                "driverId": f"number_{number}",
                "permanentNumber": str(number),
                "givenName": name_parts[0] if name_parts else f"Driver {number}",
                "familyName": " ".join(name_parts[1:]),
            }
        driver["permanentNumber"] = str(driver.get("permanentNumber") or number)

        official_constructor = jolpica_result.get("Constructor", {})
        team_name = openf1_driver.get("team_name")
        if official_constructor.get("constructorId"):
            constructor_id, constructor_name = constructor_details(
                official_constructor.get("name"), official_constructor.get("constructorId")
            )
        elif team_name:
            constructor_id, constructor_name = constructor_details(team_name)
        else:
            historic_constructor = constructor_by_number.get(number, {})
            if not historic_constructor.get("constructorId") and not historic_constructor.get("name"):
                print(f"Cannot identify constructor for driver {number}; skipping provisional standings")
                return [], [], {}
            constructor_id, constructor_name = constructor_details(
                historic_constructor.get("name"), historic_constructor.get("constructorId")
            )
        constructor = {"constructorId": constructor_id, "name": constructor_name}
        if not constructor_id:
            return [], [], {}

        identity = add_standing_points(
            driver_totals,
            constructor_totals,
            driver,
            constructor,
            points,
            race_win=session_name == "Race" and position == 1,
        )
        if identity:
            driver_key, constructor_id = identity
            driver_entry = event_driver_points.setdefault(
                driver_key, {"Driver": driver, "points": 0.0}
            )
            driver_entry["points"] += points
            constructor_entry = event_constructor_points.setdefault(
                constructor_id, {"Constructor": constructor, "points": 0.0}
            )
            constructor_entry["points"] += points

    if not event_driver_points or not event_constructor_points:
        return [], [], {}
    driver_rows, constructor_rows = standings_rows_from_totals(driver_totals, constructor_totals)
    contribution = {
        "drivers": event_driver_points,
        "constructors": event_constructor_points,
    }
    print(
        f"Estimated standings through round {round_number} {session_name.lower()}: "
        f"{len(driver_rows)} drivers, {len(constructor_rows)} constructors"
    )
    return driver_rows, constructor_rows, contribution


def fetch_standings(
    session: dict,
    season_rows: tuple[list[tuple[int, dict]], list[tuple[int, dict]]] | None = None,
    require_sprint: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Calculate standings from point-bearing event results through this session."""
    try:
        round_number = int(session.get("round") or jolpica_round_for(session) or 0)
    except (TypeError, ValueError):
        round_number = 0
    if not round_number or session.get("session_name") not in {"Sprint", "Race"}:
        return [], []

    session_name = session["session_name"]
    current_endpoint = "sprint" if session_name == "Sprint" else "results"
    current_event_rows = jolpica_round_result_rows(round_number, current_endpoint)
    if (
        len(current_event_rows) < 18
        or any(row.get("points") in {None, ""} for row in current_event_rows)
    ):
        return [], []
    if session_name == "Race" and require_sprint:
        current_sprint = jolpica_round_result_rows(round_number, "sprint")
        if (
            len(current_sprint) < 18
            or any(row.get("points") in {None, ""} for row in current_sprint)
        ):
            return [], []

    race_rows, sprint_rows = season_rows if season_rows is not None else season_result_rows()
    if session_name == "Race" and require_sprint:
        season_sprint = [row for round_id, row in sprint_rows if round_id == round_number]
        if len(season_sprint) < 18 or any(row.get("points") in {None, ""} for row in season_sprint):
            return [], []
    current_season_rows = [
        row for round_id, row in (sprint_rows if session_name == "Sprint" else race_rows)
        if round_id == round_number
    ]
    # The season-wide endpoints can lag the round endpoint. Wait for both to
    # agree that the current event's point-bearing results are present.
    if (
        len(current_season_rows) < 18
        or any(row.get("points") in {None, ""} for row in current_season_rows)
    ):
        return [], []

    driver_totals: dict[str, dict] = {}
    constructor_totals: dict[str, dict] = {}
    included_rows = []
    included_rows.extend(
        (round_id, "Race", row) for round_id, row in race_rows
        if round_id < round_number or (round_id == round_number and session_name == "Race")
    )
    included_rows.extend(
        (round_id, "Sprint", row) for round_id, row in sprint_rows
        if round_id <= round_number
    )
    if any(row.get("points") in {None, ""} for _round_id, _kind, row in included_rows):
        return [], []

    for _round_id, kind, row in included_rows:
        driver = row.get("Driver", {})
        constructor = row.get("Constructor", {})
        driver_id = driver.get("driverId") or driver.get("permanentNumber") or row.get("number")
        constructor_id = constructor.get("constructorId") or constructor.get("name")
        if not driver_id or not constructor_id:
            continue
        try:
            points = float(row.get("points") or 0)
        except (TypeError, ValueError):
            return [], []
        driver_id = str(driver_id)
        constructor_id = str(constructor_id)
        driver_entry = driver_totals.setdefault(driver_id, {"Driver": driver, "points": 0.0, "wins": 0})
        driver_entry["points"] += points
        constructor_entry = constructor_totals.setdefault(
            constructor_id,
            {"Constructor": constructor, "points": 0.0, "wins": 0},
        )
        constructor_entry["points"] += points
        if kind == "Race" and str(row.get("position")) == "1":
            driver_entry["wins"] += 1
            constructor_entry["wins"] += 1

    driver_rows, constructor_rows = standings_rows_from_totals(driver_totals, constructor_totals)
    print(
        f"Calculated standings through round {round_number} {session_name.lower()}: "
        f"{len(driver_rows)} drivers, {len(constructor_rows)} constructors"
    )
    return driver_rows, constructor_rows


def format_standings(
    session: dict,
    drivers: list[dict],
    constructors: list[dict],
    phase: str | None = None,
) -> tuple[str, str]:
    """Format driver and constructor standings as two separate ntfy bodies."""
    def points_text(value) -> str:
        try:
            number = float(value)
            return str(int(number)) if number.is_integer() else f"{number:.1f}"
        except (TypeError, ValueError):
            return str(value or "0")

    label = session_label(session)
    phase_label = " (UNOFFICIAL ESTIMATE)" if phase == "provisional" else " (OFFICIAL POINTS UPDATE)" if phase == "official" else ""
    explanation = "Estimated from finishing positions; steward decisions and final scoring can change points." if phase == "provisional" else "Updated using published event points." if phase == "official" else None
    driver_lines = [f"🏆 {label} — driver standings{phase_label}", ""]
    if explanation:
        driver_lines.extend([explanation, ""])
    for row in drivers:
        driver = row.get("Driver", {})
        name = humanize_name(" ".join(filter(None, [driver.get("givenName"), driver.get("familyName")])) or "Unknown")
        driver_lines.append(f"  {row.get('position', '—')}. {name} — {points_text(row.get('points'))} pts")
    constructor_lines = [f"🏆 {label} — constructor standings{phase_label}", ""]
    if explanation:
        constructor_lines.extend([explanation, ""])
    for row in constructors:
        constructor = row.get("Constructor", {})
        name = constructor.get("name") or "Unknown"
        constructor_lines.append(f"  {row.get('position', '—')}. {name} — {points_text(row.get('points'))} pts")
    return "\n".join(driver_lines), "\n".join(constructor_lines)


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
        position = item.get("position")
        try:
            item["position"] = int(position) if position not in {None, ""} else None
        except (TypeError, ValueError):
            item["position"] = None
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
    ordered = sorted(
        results,
        key=lambda row: (
            row.get("position") if isinstance(row.get("position"), int) else 999,
            str(row.get("driver_number", "")),
        ),
    )
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


def publish_standings(
    session: dict,
    drivers: list[dict],
    constructors: list[dict],
    phase: str,
    sent: dict,
    now: datetime,
    state: dict,
    send_messages: bool = True,
) -> None:
    driver_message, constructor_message = format_standings(session, drivers, constructors, phase)
    phase_title = "unofficial estimate" if phase == "provisional" else "official update"
    for category, message in (("drivers", driver_message), ("constructors", constructor_message)):
        key = standings_key(session, category, phase)
        if key in sent:
            continue
        if send_messages:
            notify_result(
                message,
                f"F1 {category[:-1] if category == 'drivers' else 'constructor'} standings — "
                f"{session['session_name']} ({phase_title})",
                sent,
                key,
                now,
                state,
            )
        sent[key] = now.isoformat()
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

    # Keep the existing three-day result alert window, but retry Sprint/Race
    # standings for a week so delayed point feeds do not permanently suppress them.
    season_rows_cache = None
    for item in all_sessions:
        ended = parse_time(item["date_end"])
        if ended > now or now - ended > timedelta(days=7):
            continue
        result_window_open = now - ended <= timedelta(days=3)
        key = result_key(item)
        legacy_key = f"result:{item['session_key']}"
        result_already_sent = key in sent or legacy_key in sent
        is_scoring_session = item["session_name"] in {"Sprint", "Race"}
        provisional_driver_key = standings_key(item, "drivers", "provisional")
        provisional_constructor_key = standings_key(item, "constructors", "provisional")
        official_driver_key = standings_key(item, "drivers", "official")
        official_constructor_key = standings_key(item, "constructors", "official")
        if (not result_already_sent and result_window_open) or (
            is_scoring_session
            and (provisional_driver_key not in sent or provisional_constructor_key not in sent)
        ):
            results, event_drivers = fetch_results(item)
            results = normalize_results(results)
        else:
            results, event_drivers = [], {}

        if not result_already_sent and result_window_open and results:
            notify_result(
                format_result(item, results, event_drivers),
                f"F1 result — {item['session_name']}",
                sent,
                key,
                now,
                state,
            )
            sent[key] = now.isoformat()
            save_state(state)
        elif not result_already_sent and result_window_open:
            print(f"Result not available yet for {session_label(item)}")
        if not is_scoring_session:
            continue

        snapshots = state.setdefault("standings_snapshots", {})
        if not isinstance(snapshots, dict):
            snapshots = state["standings_snapshots"] = {}
        snapshot_key = result_key(item)
        snapshot = snapshots.setdefault(snapshot_key, {})
        round_number = session_round(item)
        if round_number:
            item["round"] = str(round_number)
        snapshot.update({
            "meeting_key": str(item.get("meeting_key", "")),
            "round": round_number,
            "session_name": item["session_name"],
            "updated_at": now.isoformat(),
        })

        # This event was already corrected with verified point rows under the
        # prior state-key scheme. Migrate its dedupe markers without replaying it.
        legacy_v2_keys = [
            f"{key}:standings:verified-points-v2:{category}"
            for category in ("drivers", "constructors")
        ]
        if all(old_key in sent for old_key in legacy_v2_keys):
            sent.setdefault(official_driver_key, now.isoformat())
            sent.setdefault(official_constructor_key, now.isoformat())
            save_state(state)
            continue

        if results and (provisional_driver_key not in sent or provisional_constructor_key not in sent):
            try:
                if season_rows_cache is None:
                    season_rows_cache = season_result_rows()
                estimated_drivers, estimated_constructors, contribution = estimate_standings(
                    item, results, event_drivers, state, season_rows_cache
                )
            except (requests.RequestException, KeyError, TypeError, ValueError) as error:
                print(f"Could not estimate standings for {session_label(item)}: {error}")
            else:
                if estimated_drivers and estimated_constructors:
                    snapshot["estimated"] = standings_signature(estimated_drivers, estimated_constructors)
                    snapshot["event_driver_points"] = contribution["drivers"]
                    snapshot["event_constructor_points"] = contribution["constructors"]
                    publish_standings(
                        item, estimated_drivers, estimated_constructors,
                        "provisional", sent, now, state,
                    )

        if official_driver_key in sent and official_constructor_key in sent:
            continue
        require_sprint = item["session_name"] == "Race" and any(
            other["session_name"] == "Sprint"
            and other.get("meeting_key") == item.get("meeting_key")
            and parse_time(other["date_end"]) <= now
            for other in all_sessions
        )
        try:
            if season_rows_cache is None:
                season_rows_cache = season_result_rows()
            official_drivers, official_constructors = fetch_standings(
                item, season_rows_cache, require_sprint=require_sprint
            )
        except (requests.RequestException, KeyError, TypeError, ValueError) as error:
            print(f"Official standings not available yet for {session_label(item)}: {error}")
            continue
        if not official_drivers or not official_constructors:
            print(f"Official standings not available yet for {session_label(item)}")
            continue

        official_signature = standings_signature(official_drivers, official_constructors)
        estimated_signature = snapshot.get("estimated")
        previous_official_signature = snapshot.get("official")
        points_differ = estimated_signature is None or estimated_signature != official_signature
        if previous_official_signature is not None and previous_official_signature != official_signature:
            points_differ = True
        snapshot["official"] = official_signature
        if points_differ:
            publish_standings(
                item, official_drivers, official_constructors,
                "official", sent, now, state,
            )
        else:
            publish_standings(
                item, official_drivers, official_constructors,
                "official", sent, now, state, send_messages=False,
            )

    # Keep the state compact while retaining a month of deduplication history.
    cutoff = now - timedelta(days=35)
    state["sent"] = {
        key: value for key, value in sent.items()
        if value and parse_time(value) >= cutoff
    }
    snapshots = state.get("standings_snapshots", {})
    if isinstance(snapshots, dict):
        state["standings_snapshots"] = {
            key: value for key, value in snapshots.items()
            if not value.get("updated_at") or parse_time(value["updated_at"]) >= cutoff
        }
    save_state(state)
    print(f"OK: checked {len(all_sessions)} sessions; sent-state entries={len(state['sent'])}")


if __name__ == "__main__":
    main()
