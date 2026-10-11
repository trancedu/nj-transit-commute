#!/usr/bin/env python3
"""Recommend a Secaucus Junction departure for New York Penn."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_API_BASE = "https://raildata.njtransit.com/api/TrainData"
TIMEOUT_SECONDS = 20
HOME_TO_PLATFORM_MINUTES = 10  # 7 minutes driving + 3 minutes to the platform.
BOARDING_BUFFER_MINUTES = 2
# Reference point for the Secaucus Junction rail station (approximately).
SECAUCUS_LATITUDE = 40.76128
SECAUCUS_LONGITUDE = -74.07569
EARTH_RADIUS_MILES = 3958.7613


def read_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without printing or exporting secret values."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key.upper()] = value
    return values


def credentials() -> tuple[str, str, str]:
    env_path = Path(os.environ.get("NJT_ENV_FILE", ROOT / ".env")).expanduser()
    file_values = read_env_file(env_path)

    def pick(*names: str) -> str:
        for name in names:
            value = os.environ.get(name) or file_values.get(name)
            if value:
                return value.strip()
        return ""

    username = pick("NJT_USERNAME", "NJT_API_USERNAME", "NJTRANSIT_USERNAME", "API_USERNAME")
    password = pick("NJT_PASSWORD", "NJT_API_PASSWORD", "NJTRANSIT_PASSWORD", "API_PASSWORD")

    # These generic names are accepted only when they came from the .env file;
    # avoid accidentally using the operating system's USER/USERNAME variables.
    username = username or file_values.get("USERNAME", "") or file_values.get("ACCOUNT", "")
    password = password or file_values.get("PASSWORD", "")

    api_base = pick("NJT_API_BASE") or DEFAULT_API_BASE
    if not username or not password:
        raise RuntimeError(
            f"No NJ TRANSIT credentials found in {env_path}. "
            "Use NJT_USERNAME and NJT_PASSWORD in that file."
        )
    return username, password, api_base.rstrip("/")


def value(obj: object, key: str, default: object = "") -> object:
    if isinstance(obj, dict):
        for actual_key, actual_value in obj.items():
            if str(actual_key).casefold() == key.casefold():
                return actual_value
    return default


class RailDataClient:
    def __init__(self, username: str, password: str, api_base: str) -> None:
        self.username = username
        self.password = password
        self.api_base = api_base
        self.cache_path = ROOT / ".njt_token.json"
        cache_identity = f"{username}\0{api_base}"
        self.username_fingerprint = hashlib.sha256(cache_identity.encode("utf-8")).hexdigest()
        self._token: str | None = self._load_cached_token()

    def _load_cached_token(self) -> str | None:
        try:
            cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if cached.get("username_sha256") != self.username_fingerprint:
            return None
        token = cached.get("token")
        return token if isinstance(token, str) and token else None

    def _save_cached_token(self, token: str) -> None:
        self.cache_path.write_text(
            json.dumps(
                {"username_sha256": self.username_fingerprint, "token": token},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            self.cache_path.chmod(0o600)
        except OSError:
            pass

    def _post(self, operation: str, fields: dict[str, str]) -> tuple[int, object | None]:
        # Current RailData Swagger declares multipart/form-data for these operations.
        boundary = "----NJT" + secrets.token_hex(16)
        chunks: list[bytes] = []
        for key, raw_value in fields.items():
            safe_key = key.replace('"', "")
            field_value = str(raw_value).replace("\r", "").replace("\n", "")
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="{safe_key}"\r\n\r\n'.encode("ascii"),
                    field_value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        request = Request(
            f"{self.api_base}/{operation}",
            data=b"".join(chunks),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json, text/plain, */*",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                status = response.status
                body = response.read()
        except HTTPError as exc:
            status, body = exc.code, exc.read()
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"Network or timeout error calling RailData {operation}.") from exc

        if status >= 400:
            return status, None
        try:
            return status, json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"NJ TRANSIT {operation} returned an unreadable response.") from exc

    def _issue_token(self) -> str:
        status, payload = self._post(
            "getToken",
            {"username": self.username, "password": self.password},
        )
        if status >= 400:
            raise RuntimeError(f"NJ TRANSIT getToken failed with HTTP {status}.")
        token = value(payload, "UserToken")
        authenticated = str(value(payload, "Authenticated", "")).casefold()
        if not isinstance(token, str) or not token or authenticated in {"false", "0", "no"}:
            raise RuntimeError("NJ TRANSIT did not issue a token; check the approved API credentials.")
        self._token = token
        self._save_cached_token(token)
        return token

    def token(self) -> str:
        return self._token or self._issue_token()

    def call(self, operation: str, fields: dict[str, str] | None = None) -> object:
        request_fields = dict(fields or {})
        request_fields["token"] = self.token()
        status, payload = self._post(operation, request_fields)

        # Tokens last for hours; mint a replacement only if the data API says the
        # cached token is unauthorized, then retry the read once.
        if status == 401:
            self._token = None
            try:
                self.cache_path.unlink(missing_ok=True)
            except OSError:
                pass
            request_fields["token"] = self._issue_token()
            status, payload = self._post(operation, request_fields)

        if status >= 400:
            raise RuntimeError(f"NJ TRANSIT {operation} failed with HTTP {status}.")
        return payload


def find_secaucus_code(payload: object) -> str:
    stations = payload if isinstance(payload, list) else []
    for station in stations:
        name = " ".join(
            str(value(station, field, ""))
            for field in ("STATIONNAME", "STATION_14CHAR")
        )
        if "secaucus" in name.casefold():
            code = value(station, "STATION_2CHAR")
            if code:
                return str(code)
    raise RuntimeError("The RailData station list did not contain Secaucus Junction.")


def as_rows(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = value(payload, "ITEMS", [])
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def destination_group(destination: str) -> str | None:
    lowered = destination.casefold()
    if "hoboken" in lowered:
        return "Hoboken"
    if "new york" in lowered or "penn" in lowered or lowered in {"nyp", "ny penn"}:
        return "New York Penn"
    return None


def format_delay(raw_seconds: object) -> str:
    try:
        seconds = int(float(str(raw_seconds).strip()))
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    minutes = max(1, round(seconds / 60))
    return f"{minutes} min late"


def parse_feed_datetime(raw: object, now: datetime) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed.replace(tzinfo=now.tzinfo) if parsed.tzinfo is None else parsed.astimezone(now.tzinfo)

    formats = (
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
        "%d-%b-%Y %I:%M:%S %p",
        "%d-%b-%Y %I:%M %p",
        "%Y-%m-%d %H:%M:%S",
        "%I:%M:%S %p",
        "%I:%M %p",
        "%H:%M:%S",
        "%H:%M",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.year == 1900:
            parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
            parsed = parsed.replace(tzinfo=now.tzinfo)
            if parsed < now - timedelta(hours=12):
                parsed += timedelta(days=1)
        elif parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=now.tzinfo)
        return parsed
    return None


def seconds_late(raw: object) -> int:
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return 0


def straight_line_distance_miles(latitude: object, longitude: object) -> float | None:
    """Return great-circle distance from a train GPS point to Secaucus Junction."""
    try:
        train_lat = float(str(latitude).strip())
        train_lon = float(str(longitude).strip())
    except (TypeError, ValueError):
        return None
    if not (-90 <= train_lat <= 90 and -180 <= train_lon <= 180):
        return None

    lat1, lat2 = math.radians(SECAUCUS_LATITUDE), math.radians(train_lat)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(train_lon - SECAUCUS_LONGITUDE)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    angle = 2 * math.asin(math.sqrt(min(1.0, haversine)))
    return EARTH_RADIUS_MILES * angle


def stop_for_station(
    stops: object,
    station: str,
) -> dict[str, object] | None:
    if not isinstance(stops, list):
        return None
    for stop in stops:
        if not isinstance(stop, dict):
            continue
        name = str(value(stop, "STATIONNAME", "")).casefold()
        if station == "Secaucus Junction" and "secaucus" in name:
            return stop
        if station == "New York Penn" and (
            ("new york" in name and "penn" in name)
            or name.strip() in {"nyp", "ny penn", "ny penn station", "penn station"}
        ):
            return stop
    return None


def print_departure_recommendation(
    rows: list[dict[str, object]],
    vehicles: list[dict[str, object]],
    now: datetime,
    source_label: str = "station board",
) -> bool:
    vehicle_by_id = {str(value(v, "ID", "")).strip(): v for v in vehicles}
    departures: list[dict[str, object]] = []
    for row in rows:
        if destination_group(str(value(row, "DESTINATION", ""))) != "New York Penn":
            continue
        status = str(value(row, "STATUS", "")).strip()
        if "cancel" in status.casefold():
            continue
        scheduled = parse_feed_datetime(value(row, "SCHED_DEP_DATE", ""), now)
        if scheduled is None:
            continue
        delay_seconds = seconds_late(value(row, "SEC_LATE", ""))
        estimated = scheduled + timedelta(seconds=delay_seconds)
        if estimated < now:
            continue

        train_id = str(value(row, "TRAIN_ID", "")).strip()
        vehicle = vehicle_by_id.get(train_id, {})
        latitude = str(value(row, "GPSLATITUDE", "") or value(vehicle, "LATITUDE", "")).strip()
        longitude = str(value(row, "GPSLONGITUDE", "") or value(vehicle, "LONGITUDE", "")).strip()
        gps_time = str(value(row, "GPSTIME", "") or value(vehicle, "LAST_MODIFIED", "")).strip()
        departures.append({
            "row": row,
            "train_id": train_id,
            "scheduled": scheduled,
            "estimated": estimated,
            "delay_seconds": delay_seconds,
            "status": status,
            "line": str(value(row, "LINEABBREVIATION", "") or value(row, "LINE", "")).strip(),
            "latitude": latitude,
            "longitude": longitude,
            "gps_time": gps_time,
            "vehicle": vehicle,
        })

    departures.sort(key=lambda item: item["estimated"])
    if not departures:
        return False

    print(f"\nUpcoming Secaucus → New York Penn trains ({source_label}):")

    for index, departure in enumerate(departures[:5]):
        estimated = departure["estimated"]
        minutes_until = (estimated - now).total_seconds() / 60
        reachability = (
            "too soon to reach Secaucus" if minutes_until < HOME_TO_PLATFORM_MINUTES
            else "tight boarding buffer" if minutes_until < HOME_TO_PLATFORM_MINUTES + BOARDING_BUFFER_MINUTES
            else "reachable"
        )
        details = [
            f"train {departure['train_id'] or 'ID unavailable'}",
            str(departure["line"]),
            str(departure["status"]),
            reachability,
        ]
        delay_text = format_delay(departure["delay_seconds"])
        if delay_text:
            if source_label == "station board":
                details.append(f"Secaucus board {delay_text}")
            else:
                details.append(f"current vehicle {delay_text} (used in departure estimate)")
        details = [item for item in details if item]
        if departure["latitude"] and departure["longitude"]:
            gps = f"GPS {departure['latitude']}, {departure['longitude']}"
            if departure["gps_time"]:
                gps += f" @ {departure['gps_time']}"
            details.append(gps)
            distance = straight_line_distance_miles(
                departure["latitude"], departure["longitude"]
            )
            if distance is not None:
                miles = "<0.1 mi" if distance < 0.05 else f"{distance:.1f} mi"
                kilometers = "<0.1 km" if distance * 1.609344 < 0.1 else f"{distance * 1.609344:.1f} km"
                details.append(
                    f"{miles} ({kilometers}) straight-line to Secaucus Junction"
                )
        next_stop = str(value(departure["vehicle"], "NEXT_STOP", "")).strip()
        if next_stop:
            details.append(f"next stop {next_stop}")

        stops = value(departure["row"], "STOPS", [])
        secaucus_stop = stop_for_station(stops, "Secaucus Junction")
        if secaucus_stop:
            sec_status = str(value(secaucus_stop, "STOP_STATUS", "")).strip()
            sec_departure = str(
                value(secaucus_stop, "DEP_TIME", "") or value(secaucus_stop, "TIME", "")
            ).strip()
            sec_departed = str(value(secaucus_stop, "DEPARTED", "")).strip()
            sec_details = [
                item for item in (
                    f"departure {sec_departure}" if sec_departure else "",
                    f"status {sec_status}" if sec_status else "",
                    f"departed {sec_departed}" if sec_departed else "",
                ) if item
            ]
            if sec_details:
                details.append(f"Secaucus stop: {' · '.join(sec_details)}")

        penn_stop = stop_for_station(stops, "New York Penn")
        if penn_stop:
            penn_status = str(value(penn_stop, "STOP_STATUS", "")).strip()
            penn_time = str(value(penn_stop, "TIME", "")).strip()
            penn_details = [
                item for item in (
                    f"feed time {penn_time}" if penn_time else "",
                    f"status {penn_status}" if penn_status else "",
                ) if item
            ]
            if penn_details:
                details.append(f"NY Penn stop: {' · '.join(penn_details)}")
        if index:
            headway = round((estimated - departures[index - 1]["estimated"]).total_seconds() / 60)
            details.append(f"{headway} min after prior listed train")
        print(f"  {estimated.strftime('%I:%M:%S %p ET')} · {' · '.join(details)}")

    minimum_lead = timedelta(minutes=HOME_TO_PLATFORM_MINUTES + BOARDING_BUFFER_MINUTES)
    safe = next((item for item in departures if item["estimated"] >= now + minimum_lead), None)
    if safe is None:
        print("\nRecommendation: no listed train leaves at least 12 minutes from now.")
        print("  10 minutes covers your drive and walk; the extra 2 minutes is a boarding buffer.")
        return True

    leave_at = safe["estimated"] - minimum_lead
    recommendation = "leave now" if leave_at <= now else f"leave at {leave_at.strftime('%I:%M:%S %p ET')}"
    print(
        f"\nRecommendation: {recommendation} for train {safe['train_id'] or '(ID unavailable)'}, "
        f"scheduled to leave Secaucus around {safe['estimated'].strftime('%I:%M:%S %p ET')} "
        f"after reported delay. This uses 7 min driving + 3 min to the platform + "
        f"{BOARDING_BUFFER_MINUTES} min boarding buffer."
    )
    return True


def vehicle_departure_rows(
    client: RailDataClient,
    vehicles: list[dict[str, object]],
    now: datetime,
) -> list[dict[str, object]]:
    """Build a fallback board from live vehicles' individual stop lists."""
    departures: list[dict[str, object]] = []
    seen: set[str] = set()
    for vehicle in vehicles:
        train_id = str(value(vehicle, "ID", "")).strip()
        if not train_id or train_id in seen:
            continue
        seen.add(train_id)
        try:
            trip = client.call("getTrainStopList", {"train": train_id})
        except RuntimeError:
            continue
        destination = str(value(trip, "DESTINATION", "")).strip()
        if destination_group(destination) != "New York Penn":
            continue

        stops = value(trip, "STOPS", [])
        if not isinstance(stops, list):
            continue
        secaucus_stop = next(
            (
                stop for stop in stops
                if isinstance(stop, dict)
                and "secaucus" in str(value(stop, "STATIONNAME", "")).casefold()
            ),
            None,
        )
        if not secaucus_stop:
            continue
        if str(value(secaucus_stop, "DEPARTED", "")).strip().casefold() == "yes":
            continue
        status = str(value(secaucus_stop, "STOP_STATUS", "")).strip()
        if "cancel" in status.casefold():
            continue

        dep_time = value(secaucus_stop, "DEP_TIME", "")
        scheduled = parse_feed_datetime(dep_time, now)
        if scheduled is None:
            continue
        delay_seconds = seconds_late(value(vehicle, "SEC_LATE", ""))
        estimated = scheduled + timedelta(seconds=delay_seconds)
        if estimated < now or estimated > now + timedelta(hours=2):
            continue

        departures.append({
            "SCHED_DEP_DATE": scheduled.isoformat(),
            "DESTINATION": destination,
            "TRAIN_ID": train_id,
            "LINE": value(vehicle, "TRAIN_LINE", ""),
            "STATUS": status,
            "SEC_LATE": delay_seconds,
            "GPSLATITUDE": value(vehicle, "LATITUDE", ""),
            "GPSLONGITUDE": value(vehicle, "LONGITUDE", ""),
            "GPSTIME": value(vehicle, "LAST_MODIFIED", ""),
            "NEXT_STOP": value(vehicle, "NEXT_STOP", ""),
            "STOPS": stops,
        })
    return departures


def print_train(row: dict[str, object]) -> None:
    departure = str(value(row, "SCHED_DEP_DATE", "time unavailable"))
    destination = str(value(row, "DESTINATION", "destination unavailable"))
    line = str(value(row, "LINEABBREVIATION", "") or value(row, "LINE", ""))
    track = str(value(row, "TRACK", ""))
    status = str(value(row, "STATUS", ""))
    delay = format_delay(value(row, "SEC_LATE", ""))
    inline_message = str(value(row, "INLINEMSG", ""))
    train_id = str(value(row, "TRAIN_ID", ""))

    details = [item for item in (line, f"track {track}" if track else "", status, delay) if item]
    if train_id:
        details.append(f"train {train_id}")
    if inline_message:
        details.append(inline_message)
    latitude = str(value(row, "GPSLATITUDE", "")).strip()
    longitude = str(value(row, "GPSLONGITUDE", "")).strip()
    gps_time = str(value(row, "GPSTIME", "")).strip()
    if latitude and longitude:
        gps = f"GPS {latitude}, {longitude}"
        if gps_time:
            gps += f" @ {gps_time}"
        details.append(gps)

    stops = value(row, "STOPS", [])
    if isinstance(stops, list):
        secaucus_stop = next(
            (
                stop for stop in stops
                if isinstance(stop, dict)
                and "secaucus" in str(value(stop, "STATIONNAME", "")).casefold()
            ),
            None,
        )
        if secaucus_stop:
            stop_time = str(value(secaucus_stop, "TIME", "")).strip()
            stop_status = str(value(secaucus_stop, "STOP_STATUS", "")).strip()
            departed = str(value(secaucus_stop, "DEPARTED", "")).strip()
            stop_details = [item for item in (stop_time, stop_status, f"departed {departed}" if departed else "") if item]
            if stop_details:
                details.append(f"Secaucus stop {' · '.join(stop_details)}")

    suffix = f" — {' · '.join(details)}" if details else ""
    print(f"  {departure}  {destination}{suffix}")


def main() -> int:
    try:
        username, password, api_base = credentials()
        client = RailDataClient(username, password, api_base)

        # Resolve the API's two-character station code from its own station list.
        station_code = find_secaucus_code(client.call("getStationList"))
        board = client.call("getTrainSchedule", {"station": station_code})
        rows = as_rows(board)
        now = datetime.now(ZoneInfo("America/New_York"))
        try:
            vehicle_payload = client.call("getVehicleData")
            vehicles = as_rows(vehicle_payload)
        except RuntimeError as exc:
            vehicle_payload = None
            vehicles = []
            vehicle_error = str(exc)
        else:
            vehicle_error = ""

        print("NJ TRANSIT RailData · Secaucus Junction")
        print("Checked:", now.strftime("%Y-%m-%d %I:%M %p ET"))
        print("Feed values may be delayed or incomplete; use NJ TRANSIT alerts for travel decisions.")

        new_york_rows = [
            row for row in rows
            if destination_group(str(value(row, "DESTINATION", ""))) == "New York Penn"
        ]
        with_coordinates = sum(
            bool(value(vehicle, "LATITUDE", "") and value(vehicle, "LONGITUDE", ""))
            for vehicle in vehicles
        )
        print(f"Live vehicle feed: {len(vehicles)} vehicles; {with_coordinates} have coordinates.")

        board_used = print_departure_recommendation(new_york_rows, vehicles, now)
        if not board_used:
            fallback_rows = vehicle_departure_rows(client, vehicles, now)
            if fallback_rows:
                print("\nStation board had no usable Penn-bound departures; using each live train's stop list instead.")
                print("Fallback departure estimates combine the train's Secaucus DEP_TIME with current delay seconds.")
                print_departure_recommendation(
                    fallback_rows,
                    vehicles,
                    now,
                    source_label="live vehicles + train stop lists",
                )
            else:
                print("\nNo usable upcoming New York Penn departures were returned by the board or vehicle stop lists.")

        if not rows:
            print("\nThe station board returned no departures in this snapshot.")

        notices = value(board, "STATIONMSGS", [])
        if isinstance(notices, list) and notices:
            print("\nSecaucus service messages:")
            for notice in notices:
                message = str(value(notice, "MSG_TEXT", "")).strip()
                if message:
                    published = str(value(notice, "MSG_PUBDATE", "")).strip()
                    kind = str(value(notice, "MSG_TYPE", "")).strip()
                    prefix = " · ".join(item for item in (kind, published) if item)
                    print(f"  {f'[{prefix}] ' if prefix else ''}{message}")
        else:
            print("\nSecaucus service messages: none returned with this board.")

        if vehicle_error:
            print(f"\nLive vehicle feed unavailable: {vehicle_error}")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
