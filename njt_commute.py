#!/usr/bin/env python3
"""Query live NJ TRANSIT station departures and the RVR-to-Secaucus commute."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
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
SECAUCUS_LEVEL_BY_DESTINATION = {
    "new york penn": "upper",
    "ny penn": "upper",
    "hoboken": "lower",
}


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
        self._token_lock = threading.Lock()

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
        if self._token:
            return self._token
        with self._token_lock:
            return self._token or self._issue_token()

    def call(self, operation: str, fields: dict[str, str] | None = None) -> object:
        request_fields = dict(fields or {})
        used_token = self.token()
        request_fields["token"] = used_token
        status, payload = self._post(operation, request_fields)

        # Tokens last for hours; mint a replacement only if the data API says the
        # cached token is unauthorized, then retry the read once.
        if status == 401:
            with self._token_lock:
                if self._token == used_token:
                    self._token = None
                    try:
                        self.cache_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    refreshed_token = self._issue_token()
                else:
                    refreshed_token = self._token or self._issue_token()
            request_fields["token"] = refreshed_token
            status, payload = self._post(operation, request_fields)

        if status >= 400:
            raise RuntimeError(f"NJ TRANSIT {operation} failed with HTTP {status}.")
        return payload


def find_secaucus_code(payload: object, destination: str = "New York Penn") -> str:
    stations = payload if isinstance(payload, list) else []
    destination_key = destination.casefold().strip()
    level = SECAUCUS_LEVEL_BY_DESTINATION.get(destination_key)
    if level is None:
        raise RuntimeError(f"No Secaucus platform mapping is configured for {destination}.")

    platform_entries_exist = False
    generic_codes: list[str] = []
    for station in stations:
        name = " ".join(
            str(value(station, field, ""))
            for field in ("STATIONNAME", "STATION_14CHAR")
        )
        lowered = name.casefold()
        if "secaucus" not in lowered:
            continue

        code = value(station, "STATION_2CHAR")
        if not code:
            continue
        if "upper" in lowered or "lower" in lowered:
            platform_entries_exist = True
            if level in lowered:
                return str(code)
        elif "concourse" not in lowered:
            generic_codes.append(str(code))

    # Some station lists expose only a single, unqualified Secaucus entry.
    # If platform-specific entries exist, never silently fall back to another
    # level or to the concourse.
    if not platform_entries_exist and generic_codes:
        return generic_codes[0]
    raise RuntimeError(f"The RailData station list did not contain Secaucus {level} level.")


def station_identity(name: str) -> str:
    """Normalize common station labels without losing ordinary station names."""
    lowered = name.casefold().strip()
    if "secaucus" in lowered:
        return "secaucus junction"
    if "hoboken" in lowered:
        return "hoboken"
    if "penn" in lowered and (
        "new york" in lowered
        or lowered.replace(" ", "") in {"nyp", "nypenn", "pennstation"}
    ):
        return "new york penn"
    if lowered.replace(" ", "") in {"nyp", "nypenn", "pennstation"}:
        return "new york penn"

    words = re.sub(r"[^a-z0-9]+", " ", lowered).split()
    abbreviations = {"jct": "junction", "stn": "station"}
    words = [abbreviations.get(word, word) for word in words]
    words = [word for word in words if word not in {"station", "junction", "terminal"}]
    return " ".join(words)


def resolve_station_code(
    payload: object,
    origin: str,
    destination: str,
) -> tuple[str, str]:
    """Resolve a user-facing station name to RailData's station code."""
    stations = payload if isinstance(payload, list) else []
    wanted = station_identity(origin)

    if wanted == "secaucus junction":
        destination_key = station_identity(destination)
        if destination_key == "hoboken":
            level = "lower"
        elif destination_key == "new york penn":
            level = "upper"
        else:
            raise RuntimeError(
                "Secaucus Junction has separate train platforms; use Hoboken or New York Penn as the destination."
            )
        for station in stations:
            name = str(value(station, "STATIONNAME", ""))
            lowered = name.casefold()
            if "secaucus" in lowered and level in lowered:
                code = value(station, "STATION_2CHAR")
                if code:
                    return str(code), name
        raise RuntimeError(f"The RailData station list did not contain the Secaucus {level} platform.")

    entries: list[tuple[str, str]] = []
    for station in stations:
        code = value(station, "STATION_2CHAR")
        name = str(value(station, "STATIONNAME", "")).strip()
        if code and name:
            entries.append((str(code), name))

    exact = [entry for entry in entries if station_identity(entry[1]) == wanted]
    unique_exact = list(dict.fromkeys(exact))
    if len(unique_exact) == 1:
        return unique_exact[0]
    if len(unique_exact) > 1:
        codes = {entry[0] for entry in unique_exact}
        if len(codes) == 1:
            return unique_exact[0]
        names = ", ".join(entry[1] for entry in unique_exact)
        raise RuntimeError(f"Station name '{origin}' is ambiguous in RailData: {names}.")

    partial = [
        entry for entry in entries
        if wanted and (wanted in station_identity(entry[1]) or station_identity(entry[1]) in wanted)
    ]
    unique_partial = list(dict.fromkeys(partial))
    if len({entry[0] for entry in unique_partial}) == 1 and unique_partial:
        return unique_partial[0]
    if unique_partial:
        names = ", ".join(entry[1] for entry in unique_partial[:8])
        raise RuntimeError(f"Station name '{origin}' is ambiguous in RailData; matches: {names}.")
    raise RuntimeError(f"RailData did not recognize the origin station '{origin}'.")


def as_rows(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = value(payload, "ITEMS", [])
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def destination_group(destination: str) -> str | None:
    identity = station_identity(destination)
    if identity == "hoboken":
        return "Hoboken"
    if identity == "new york penn":
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
        if station_identity(name) == station_identity(station):
            return stop
    return None


def row_serves_route(
    row: dict[str, object],
    origin: str,
    destination: str,
) -> bool:
    """Use the ordered stop list when present; otherwise match the board destination."""
    stops = value(row, "STOPS", [])
    if isinstance(stops, list):
        origin_index: int | None = None
        destination_index: int | None = None
        for index, stop in enumerate(stops):
            if not isinstance(stop, dict):
                continue
            name = str(value(stop, "STATIONNAME", ""))
            if station_identity(name) == station_identity(origin) and origin_index is None:
                origin_index = index
            if station_identity(name) == station_identity(destination) and destination_index is None:
                destination_index = index
        if destination_index is not None:
            return origin_index is None or destination_index > origin_index

    return station_identity(str(value(row, "DESTINATION", ""))) == station_identity(destination)


def format_delay_detail(raw_seconds: object) -> str:
    if raw_seconds is None or not str(raw_seconds).strip():
        return "delay unavailable"
    seconds = seconds_late(raw_seconds)
    if seconds > 0:
        return f"{max(1, round(seconds / 60))} min late"
    if seconds < 0:
        return f"{max(1, round(abs(seconds) / 60))} min early"
    return "0 sec delay field"


def timestamp_age_seconds(raw_timestamp: object, now: datetime) -> int | None:
    timestamp = parse_feed_datetime(raw_timestamp, now)
    if timestamp is None:
        return None
    return max(0, int((now - timestamp).total_seconds()))


def elapsed_since(raw_timestamp: object, now: datetime) -> str:
    seconds = timestamp_age_seconds(raw_timestamp, now)
    if seconds is None:
        return "更新时间未知"
    if seconds <= 30:
        label = "新鲜"
    elif seconds <= 60:
        label = "稍旧"
    elif seconds <= 120:
        label = "已旧"
    else:
        label = "很旧"
    return f"{seconds} 秒前更新（{label}）"


def stops_remaining(vehicle: dict[str, object], stops: object, destination: str) -> int | None:
    """Count stops from the vehicle's next stop through the destination, inclusive."""
    next_stop = str(value(vehicle, "NEXT_STOP", "") or value(vehicle, "NEXT_STATION", "")).strip()
    if not next_stop or not isinstance(stops, list):
        return None

    names = [
        str(value(stop, "STATIONNAME", "")).strip()
        for stop in stops
        if isinstance(stop, dict)
    ]
    next_identity = station_identity(next_stop)
    destination_identity = station_identity(destination)
    next_indices = [index for index, name in enumerate(names) if station_identity(name) == next_identity]
    destination_indices = [
        index for index, name in enumerate(names)
        if station_identity(name) == destination_identity
    ]
    if not next_indices or not destination_indices:
        return None

    next_index = next_indices[0]
    next_record = stop_for_station(stops, next_stop)
    if next_record and str(value(next_record, "DEPARTED", "")).strip().casefold() == "yes":
        return None
    destination_index = next((index for index in destination_indices if index >= next_index), None)
    if destination_index is None:
        return None
    return destination_index - next_index + 1


def select_gps_sample(row: dict[str, object], vehicle: dict[str, object], now: datetime) -> tuple[str, str, str]:
    """Choose a complete GPS sample; never attach another source's timestamp."""
    samples = [
        (value(vehicle, "LATITUDE", ""), value(vehicle, "LONGITUDE", ""), value(vehicle, "LAST_MODIFIED", "")),
        (value(row, "GPSLATITUDE", ""), value(row, "GPSLONGITUDE", ""), value(row, "GPSTIME", "")),
    ]
    valid = []
    for lat, lon, timestamp in samples:
        try:
            latitude, longitude = float(str(lat)), float(str(lon))
        except (TypeError, ValueError):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180) or (latitude == 0 and longitude == 0):
            continue
        age = timestamp_age_seconds(timestamp, now)
        valid.append((age if age is not None else float("inf"), str(lat), str(lon), str(timestamp or "")))
    if not valid:
        return "", "", ""
    _, latitude, longitude, timestamp = min(valid, key=lambda sample: sample[0])
    return latitude, longitude, timestamp


def markdown_cell(text: object) -> str:
    return " ".join(str(text).replace("|", "\\|").split())


def display_delay(raw_seconds: object) -> str:
    if raw_seconds is None or not str(raw_seconds).strip():
        return "—"
    seconds = seconds_late(raw_seconds)
    if seconds > 0:
        minutes, remainder = divmod(seconds, 60)
        return f"晚 {minutes}分{remainder:02d}秒"
    if seconds < 0:
        minutes, remainder = divmod(abs(seconds), 60)
        return f"早 {minutes}分{remainder:02d}秒"
    return "0 分钟"


def route_advisories(
    payload: object,
    origin: str,
    destination: str,
    departures: list[dict[str, object]],
) -> list[str]:
    notices = value(payload, "STATIONMSGS", [])
    if not isinstance(notices, list):
        return []
    destination_key = station_identity(destination)
    relevant_terms = {destination_key, "portal cutover", "cross honoring", "all rail service"}
    relevant_terms.update(str(item["line"]).casefold() for item in departures if item["line"])
    relevant_terms.update(str(item["train_id"]).casefold() for item in departures)
    if destination_key == "new york penn":
        relevant_terms.update({"penn", "psny"})
    if station_identity(origin) == "secaucus junction":
        relevant_terms.add("secaucus")
    route_train_ids = {
        str(value(row, "TRAIN_ID", "")).strip()
        for row in as_rows(payload)
        if row_serves_route(row, origin, destination)
    }

    messages: list[str] = []
    for notice in notices:
        message = " ".join(str(value(notice, "MSG_TEXT", "")).split())
        lowered = message.casefold()
        primary_train = re.search(r"train\s*#\s*(\d+)", lowered)
        if primary_train and primary_train.group(1) not in route_train_ids:
            continue
        if message and any(term and term in lowered for term in relevant_terms):
            messages.append(message)
    return sorted(list(dict.fromkeys(messages)), key=lambda message: 0 if "portal cutover" in message.casefold() else 1)


def print_route_board(
    origin: str,
    destination: str,
    station_label: str,
    board_payload: object,
    vehicles: list[dict[str, object]],
    now: datetime,
) -> None:
    rows = as_rows(board_payload)
    matching = [row for row in rows if row_serves_route(row, origin, destination)]
    vehicle_by_id = {str(value(vehicle, "ID", "")).strip(): vehicle for vehicle in vehicles}
    departures: list[dict[str, object]] = []

    for row in matching:
        status = str(value(row, "STATUS", "")).strip()
        if "cancel" in status.casefold():
            continue
        origin_stop = stop_for_station(value(row, "STOPS", []), origin)
        if origin_stop and "cancel" in str(value(origin_stop, "STOP_STATUS", "")).casefold():
            continue
        if origin_stop and str(value(origin_stop, "DEPARTED", "")).strip().casefold() == "yes":
            continue

        scheduled = parse_feed_datetime(value(row, "SCHED_DEP_DATE", ""), now)
        if scheduled is None and origin_stop:
            scheduled = parse_feed_datetime(
                value(origin_stop, "DEP_TIME", "") or value(origin_stop, "TIME", ""), now
            )
        if scheduled is None:
            continue

        raw_delay = value(row, "SEC_LATE", None)
        delay = seconds_late(raw_delay)
        board_estimate = scheduled + timedelta(seconds=delay)
        stop_departure = None
        if origin_stop:
            stop_departure = parse_feed_datetime(
                value(origin_stop, "DEP_TIME", "") or value(origin_stop, "TIME", ""), now
            )
        # DEP_TIME is the planned time; comparing it with a delay-adjusted
        # estimate is not evidence of a feed conflict. Keep them separate.
        estimated = board_estimate
        boarding_time = min(scheduled, estimated) if scheduled >= now else estimated
        if estimated < now:
            continue
        train_id = str(value(row, "TRAIN_ID", "")).strip()
        vehicle = vehicle_by_id.get(train_id, {})
        latitude, longitude, gps_time = select_gps_sample(row, vehicle, now)
        destination_stop = stop_for_station(value(row, "STOPS", []), destination)
        departures.append({
            "row": row,
            "train_id": train_id or "ID unavailable",
            "scheduled": scheduled,
            "estimated": estimated,
            "boarding_time": boarding_time,
            "board_estimate": board_estimate,
            "stop_departure": stop_departure,
            "raw_delay": raw_delay,
            "delay": delay,
            "status": status or "status unavailable",
            "line": str(value(row, "LINEABBREVIATION", "") or value(row, "LINE", "")).strip(),
            "origin_stop": origin_stop,
            "destination_stop": destination_stop,
            "latitude": latitude,
            "longitude": longitude,
            "gps_time": gps_time,
            "vehicle": vehicle,
            "next_stop_age": timestamp_age_seconds(value(vehicle, "LAST_MODIFIED", ""), now),
            "origin_remaining": stops_remaining(vehicle, value(row, "STOPS", []), origin),
            "stops_remaining": stops_remaining(
                vehicle,
                value(row, "STOPS", []),
                destination,
            ),
        })

    departures.sort(key=lambda item: item["estimated"])
    print(f"**NJ TRANSIT 实时班次：{origin} → {destination}**")
    print(
        f"数据快照：{now.strftime('%Y-%m-%d %I:%M:%S %p ET')} · "
        f"站台：{station_label} · 班次板 {len(rows)} 行，匹配 {len(matching)} 班"
    )

    if not departures:
        print("班次板没有返回符合路线的后续列车。")
    else:
        print()
        print(
            f"| 计划发车 (ET) | 估算发车 (ET) | 列车 / 线路 | "
            f"班次板延误 | 停站状态 | "
            f"当前 GPS 位置 / 下一站 | 距 {markdown_cell(origin)} 直线距离 | "
            f"余站：到上车站 / 到终点* |"
        )
        print("|---|---|---|---|---|---|---:|---:|")
        for item in departures[:8]:
            train = str(item["train_id"])
            if item["line"]:
                train += f" / {item['line']}"

            origin_stop = item["origin_stop"]
            stop_status = str(value(origin_stop, "STOP_STATUS", "") if origin_stop else "").strip() or "—"
            stop_departure = item["stop_departure"]
            board_estimate = item["board_estimate"]

            location = "GPS 不可用"
            next_stop = str(
                value(item["vehicle"], "NEXT_STOP", "")
                or value(item["vehicle"], "NEXT_STATION", "")
            ).strip()
            if item["latitude"] and item["longitude"]:
                try:
                    coordinate_text = f"{float(item['latitude']):.5f}, {float(item['longitude']):.5f}"
                except (TypeError, ValueError):
                    coordinate_text = f"{item['latitude']}, {item['longitude']}"
                location = coordinate_text
            if next_stop:
                location += f"<br>下一站：{markdown_cell(next_stop)}"
                if item["next_stop_age"] is not None:
                    location += f"（{item['next_stop_age']} 秒前）"
            if item["gps_time"]:
                location += f"<br>{elapsed_since(item['gps_time'], now)}"

            distance_text = "—"
            if item["latitude"] and item["longitude"] and station_identity(origin) == "secaucus junction":
                distance = straight_line_distance_miles(item["latitude"], item["longitude"])
                if distance is not None:
                    distance_text = f"{distance:.1f} mi / {distance * 1.609344:.1f} km"

            age = item["next_stop_age"]
            if item["stops_remaining"] is None or age is None or age > 120:
                remaining_text = "—"
            else:
                origin_count = item["origin_remaining"]
                prefix = "约 " if age > 60 else ""
                remaining_text = f"{prefix}{origin_count if origin_count is not None else '—'} / {item['stops_remaining']}"
            departure_time = item["estimated"].strftime("%I:%M:%S %p")
            print(
                f"| {item['scheduled'].strftime('%I:%M:%S %p')} | {departure_time} | {markdown_cell(train)} | "
                f"{display_delay(item['raw_delay'])} | {markdown_cell(stop_status)} | "
                f"{location} | {distance_text} | {remaining_text} |"
            )


        print()
        print("*估算发车 = 计划时间 + 班次板 SEC_LATE，非 GPS 推算；OnTime 是另一独立分类字段。GPS 坐标及时间取同一条完整记录。余站从 NEXT_STOP 开始，包含下一站与目标站；下一站记录超过 120 秒或与已出站记录冲突时不显示余站。")

        if station_identity(origin) == "secaucus junction":
            minimum_lead = timedelta(minutes=HOME_TO_PLATFORM_MINUTES + BOARDING_BUFFER_MINUTES)
            safe = next((item for item in departures if item["boarding_time"] >= now + minimum_lead), None)
            tight = next((item for item in departures if item["boarding_time"] >= now + timedelta(minutes=HOME_TO_PLATFORM_MINUTES)), None)
            if safe:
                leave_at = max(now, safe["boarding_time"] - minimum_lead)
                print(
                    f"RVR 出发建议：{leave_at.strftime('%I:%M:%S %p ET')} 前出发，"
                    f"赶 {safe['train_id']} 次（估算 {safe['estimated'].strftime('%I:%M:%S %p ET')}；"
                    f"出发建议保守按 {safe['boarding_time'].strftime('%I:%M:%S %p ET')} 留足 12 分钟，不依赖晚点持续）。"
                )
            elif tight:
                print(f"RVR 出发建议：没有留足缓冲的班次；{tight['train_id']} 只有现在立刻出发才可能赶上。")
            else:
                print("RVR 出发建议：当前列表里没有仍可从 RVR 赶上的班次。")

    advisories = route_advisories(board_payload, origin, destination, departures)
    if advisories:
        print()
        print(f"相关服务提示（{len(advisories)} 条，最多显示 2 条）：")
        for message in advisories[:2]:
            compact = message if len(message) <= 240 else message[:237].rstrip() + "…"
            print(f"- {compact}")


def run_route_query(origin: str, destination: str) -> int:
    username, password, api_base = credentials()
    client = RailDataClient(username, password, api_base)
    station_list = client.call("getStationList")
    station_code, station_label = resolve_station_code(station_list, origin, destination)

    # Warm the token before parallel independent reads so a missing/expired
    # token cannot cause duplicate issuance when the requests start together.
    client.token()
    with ThreadPoolExecutor(max_workers=2) as executor:
        board_future = executor.submit(client.call, "getTrainSchedule", {"station": station_code})
        vehicle_future = executor.submit(client.call, "getVehicleData")
        board_payload = board_future.result()
        vehicle_payload = vehicle_future.result()

    now = datetime.now(ZoneInfo("America/New_York"))
    print_route_board(
        origin,
        destination,
        station_label,
        board_payload,
        as_rows(vehicle_payload),
        now,
    )
    return 0


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
        latitude, longitude, gps_time = select_gps_sample(row, vehicle, now)
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
    parser = argparse.ArgumentParser(
        description="Show live NJ TRANSIT departures for an origin and destination station."
    )
    parser.add_argument("origin", nargs="?", help="origin station name")
    parser.add_argument("destination", nargs="?", help="destination station name")
    args = parser.parse_args()
    if bool(args.origin) != bool(args.destination):
        parser.error("provide both an origin and a destination station, or neither")

    try:
        if args.origin and args.destination:
            return run_route_query(args.origin, args.destination)

        username, password, api_base = credentials()
        client = RailDataClient(username, password, api_base)

        # Resolve the API's two-character station code from its own station list.
        station_code = find_secaucus_code(
            client.call("getStationList"), destination="New York Penn"
        )
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
