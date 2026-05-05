from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from database import DEFAULT_DB_PATH, TERRITORY_COMMUNES, bootstrap_demo_data, get_connection

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
HOST = "127.0.0.1"
PORT = 8000
CPU_COUNT = max(1, os.cpu_count() or 1)
GEOPF_WFS_URL = "https://data.geopf.fr/wfs/ows"
DOUARNENEZ_CENTER = {"lat": 48.0952, "lon": -4.3316}
DOUARNENEZ_BOUNDS_FALLBACK = {
    "min_lon": -4.3920,
    "min_lat": 48.0690,
    "max_lon": -4.2650,
    "max_lat": 48.1230,
}
DOUARNENEZ_COMMUNAUTE_BOUNDS = {
    "min_lon": -4.4520,
    "min_lat": 48.0180,
    "max_lon": -4.2300,
    "max_lat": 48.1330,
}
TERRITORY_CITY_PLACEHOLDERS = ", ".join("?" for _ in TERRITORY_COMMUNES)
REMOTE_BUILDING_PAGE_SIZE = 1200
REMOTE_BUILDING_MAX_PAGES = 18
LOCAL_BUILDING_SNAPSHOT_PATH = DATA_DIR / "douarnenez_buildings_geopf_snapshot.geojson"
GEOPF_ORTHO_WMTS_URL = "https://data.geopf.fr/wmts"
ORTHO_TILE_CACHE_DIR = DATA_DIR / "tile_cache"
ORTHO_TILE_SOURCE_STATE_PATH = ORTHO_TILE_CACHE_DIR / "_source_state.json"
ORTHO_CAPABILITIES_URL = "https://data.geopf.fr/annexes/ressources/wmts/ortho.xml"
ORTHO_CAPABILITIES_LAYER_IDENTIFIER = "ORTHOIMAGERY.ORTHOPHOTOS"
ORTHO_TILE_DISK_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
ORTHO_TILE_BROWSER_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
ORTHO_TILE_FRONT_MIN_ZOOM = 13
ORTHO_TILE_FRONT_MAX_ZOOM = 19
ORTHO_TILE_STARTUP_WORKERS = min(4, CPU_COUNT)
ORTHO_TILE_PROGRESS_STEP = 250
FILE_STREAM_CHUNK_SIZE = 256 * 1024
LOCAL_BUILDING_GRID_COLUMNS = 5
LOCAL_BUILDING_GRID_ROWS = 4
REMOTE_BUILDING_VIEW_CACHE_MAX_ENTRIES = 8
BAN_BUILDING_CACHE: dict[str, object] = {
    "indexed_features": None,
    "source": None,
    "fallback_reason": None,
    "city_bounds": None,
}
LOCAL_BUILDING_SNAPSHOT_CACHE: dict[str, object] = {
    "indexed_features": None,
    "source": None,
    "generated_at": None,
    "city_bounds": None,
}
REMOTE_BUILDING_VIEW_CACHE: list[dict[str, object]] = []
ADDRESS_SEARCH_CACHE: list[dict[str, object]] | None = None
ADDRESS_STREET_CACHE: dict[str, list[dict[str, object]]] | None = None
NAME_SUGGESTION_CACHE: dict[str, list[str] | None] = {
    "name": None,
    "first_name": None,
}
RESIDENT_SUGGESTION_CACHE: list[dict[str, object]] | None = None
FEATURE_ADDRESS_MATCH_CACHE: dict[str, list[int]] = {}
CITY_BOUNDS_CACHE: dict[str, float] | None = None
OVERVIEW_CACHE: dict[str, object] | None = None
APP_CACHE_LOCK = threading.RLock()
ADDRESS_OFFSET_PATTERN = (
    (0, 0),
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
    (1, 1),
    (-1, 1),
    (1, -1),
    (-1, -1),
    (2, 0),
    (-2, 0),
    (0, 2),
    (0, -2),
)


def parse_bbox(raw_value: str | None) -> tuple[float, float, float, float]:
    if not raw_value:
        raise ValueError("bbox manquant")
    parts = [float(part) for part in raw_value.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox invalide")
    min_lon, min_lat, max_lon, max_lat = parts
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("bbox incoherent")
    return min_lon, min_lat, max_lon, max_lat


def parse_ortho_tile_route(route: str) -> tuple[int, int, int] | None:
    parts = route.strip("/").split("/")
    if len(parts) != 5 or parts[:2] != ["tiles", "ortho"]:
        return None

    y_part = parts[4]
    if not y_part.endswith(".jpg"):
        return None

    try:
        z = int(parts[2])
        x = int(parts[3])
        y = int(y_part.removesuffix(".jpg"))
    except ValueError:
        return None

    if z < 0 or z > 21:
        return None

    tile_limit = 2**z
    if x < 0 or y < 0 or x >= tile_limit or y >= tile_limit:
        return None

    return z, x, y


def json_response(handler: SimpleHTTPRequestHandler, payload: dict | list, *, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        return


def binary_response(
    handler: SimpleHTTPRequestHandler,
    body: bytes,
    *,
    content_type: str,
    status: int = 200,
    extra_headers: dict[str, str] | None = None,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    if extra_headers:
        for header_name, header_value in extra_headers.items():
            handler.send_header(header_name, header_value)
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        return


def file_response(
    handler: SimpleHTTPRequestHandler,
    body_path: Path,
    *,
    content_type: str,
    status: int = 200,
    extra_headers: dict[str, str] | None = None,
) -> None:
    file_stat = body_path.stat()
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(file_stat.st_size))
    if extra_headers:
        for header_name, header_value in extra_headers.items():
            handler.send_header(header_name, header_value)
    handler.end_headers()

    try:
        with body_path.open("rb") as file_handle:
            shutil.copyfileobj(file_handle, handler.wfile, length=FILE_STREAM_CHUNK_SIZE)
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
        return


def get_db_connection() -> sqlite3.Connection:
    return get_connection(DEFAULT_DB_PATH)


def row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def normalize_text(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    lowered = ascii_value.lower().replace("-", " ").replace("'", " ")
    return re.sub(r"[^a-z0-9]+", " ", lowered).strip()


def normalize_address_text(value: str) -> str:
    normalized = normalize_text(value)
    normalized = re.sub(r"\b29100\b", " ", normalized)
    normalized = re.sub(r"\bdouarnenez\b", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def short_address_label(house_number: str | None, street_name: str | None, full_address: str | None = None) -> str:
    label = " ".join(part for part in [house_number or "", street_name or ""] if part).strip()
    if label:
        return label
    if full_address:
        return full_address.split(",")[0].strip()
    return ""


def _token_matches(query_token: str, candidate_token: str) -> bool:
    if not query_token or not candidate_token:
        return False
    if candidate_token.startswith(query_token) or query_token in candidate_token:
        return True
    if len(query_token) <= 2:
        return False
    return difflib.SequenceMatcher(None, query_token, candidate_token).ratio() >= 0.8


def quick_candidate_match(query: str, candidate: str) -> bool:
    query = query.strip()
    candidate = candidate.strip()
    if not query or not candidate:
        return False

    if query == candidate or candidate.startswith(query) or query in candidate:
        return True

    query_tokens = [token for token in query.split() if token]
    candidate_tokens = [token for token in candidate.split() if token]
    if not query_tokens or not candidate_tokens:
        return False

    for query_token in query_tokens:
        prefix_length = 1 if len(query_token) <= 3 else 2
        prefix = query_token[:prefix_length]
        if prefix and any(prefix in candidate_token for candidate_token in candidate_tokens):
            continue
        return False

    return True


def match_score(query: str, candidate: str) -> tuple[bool, float]:
    query = query.strip()
    candidate = candidate.strip()
    if not query or not candidate:
        return False, 0.0

    if query == candidate:
        return True, 120.0
    if candidate.startswith(query):
        return True, 108.0 - min(len(candidate) - len(query), 12)
    if query in candidate:
        return True, 95.0 - min(candidate.index(query), 18)

    query_tokens = [token for token in query.split() if token]
    candidate_tokens = [token for token in candidate.split() if token]
    if query_tokens and all(
        any(_token_matches(query_token, candidate_token) for candidate_token in candidate_tokens)
        for query_token in query_tokens
    ):
        return True, 88.0 + min(sum(len(token) for token in query_tokens), 20)

    ratios = [difflib.SequenceMatcher(None, query, candidate).ratio()]
    ratios.extend(
        difflib.SequenceMatcher(None, query, candidate_token).ratio() for candidate_token in candidate_tokens
    )
    best_ratio = max(ratios)
    threshold = 0.9 if len(query) <= 3 else 0.74 if len(query) <= 6 else 0.67
    return (best_ratio >= threshold, best_ratio * 100.0)


def pad_bounds(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> dict[str, float]:
    lon_padding = max((max_lon - min_lon) * 0.08, 0.003)
    lat_padding = max((max_lat - min_lat) * 0.08, 0.0025)
    return {
        "min_lon": min_lon - lon_padding,
        "min_lat": min_lat - lat_padding,
        "max_lon": max_lon + lon_padding,
        "max_lat": max_lat + lat_padding,
    }


def get_city_bounds(connection: sqlite3.Connection) -> dict[str, float]:
    imported_bounds = connection.execute(
        f"""
        SELECT
            COUNT(*) AS row_count,
            MIN(lon) AS min_lon,
            MIN(lat) AS min_lat,
            MAX(lon) AS max_lon,
            MAX(lat) AS max_lat
        FROM city_addresses
        WHERE city IN ({TERRITORY_CITY_PLACEHOLDERS}) AND source = 'BAN'
        """,
        TERRITORY_COMMUNES,
    ).fetchone()

    if imported_bounds["row_count"] and imported_bounds["row_count"] >= 100:
        return pad_bounds(
            imported_bounds["min_lon"],
            imported_bounds["min_lat"],
            imported_bounds["max_lon"],
            imported_bounds["max_lat"],
        )

    any_bounds = connection.execute(
        f"""
        SELECT
            COUNT(*) AS row_count,
            MIN(lon) AS min_lon,
            MIN(lat) AS min_lat,
            MAX(lon) AS max_lon,
            MAX(lat) AS max_lat
        FROM city_addresses
        WHERE city IN ({TERRITORY_CITY_PLACEHOLDERS})
        """,
        TERRITORY_COMMUNES,
    ).fetchone()

    if any_bounds["row_count"] and any_bounds["row_count"] >= 100:
        return pad_bounds(
            any_bounds["min_lon"],
            any_bounds["min_lat"],
            any_bounds["max_lon"],
            any_bounds["max_lat"],
        )

    return dict(DOUARNENEZ_BOUNDS_FALLBACK)


def get_cached_city_bounds() -> dict[str, float]:
    global CITY_BOUNDS_CACHE

    with APP_CACHE_LOCK:
        if CITY_BOUNDS_CACHE is not None:
            return CITY_BOUNDS_CACHE

    connection = get_db_connection()
    try:
        city_bounds = get_city_bounds(connection)
    finally:
        connection.close()

    with APP_CACHE_LOCK:
        if CITY_BOUNDS_CACHE is None:
            CITY_BOUNDS_CACHE = city_bounds
        return CITY_BOUNDS_CACHE


def get_frontend_map_bounds() -> dict[str, float]:
    city_bounds = get_cached_city_bounds()
    return {
        "min_lon": min(DOUARNENEZ_COMMUNAUTE_BOUNDS["min_lon"], city_bounds["min_lon"]),
        "min_lat": min(DOUARNENEZ_COMMUNAUTE_BOUNDS["min_lat"], city_bounds["min_lat"]),
        "max_lon": max(DOUARNENEZ_COMMUNAUTE_BOUNDS["max_lon"], city_bounds["max_lon"]),
        "max_lat": max(DOUARNENEZ_COMMUNAUTE_BOUNDS["max_lat"], city_bounds["max_lat"]),
    }


def load_address_search_cache() -> list[dict[str, object]]:
    global ADDRESS_SEARCH_CACHE, ADDRESS_STREET_CACHE

    with APP_CACHE_LOCK:
        if ADDRESS_SEARCH_CACHE is not None:
            return ADDRESS_SEARCH_CACHE

    connection = get_db_connection()
    try:
        rows = connection.execute(
            f"""
            SELECT
                id, source_ref, full_address, street_name, house_number,
                postal_code, city, lon, lat, source
            FROM city_addresses
            WHERE city IN ({TERRITORY_CITY_PLACEHOLDERS})
            ORDER BY street_name, house_number, full_address
            """,
            TERRITORY_COMMUNES,
        ).fetchall()
    finally:
        connection.close()

    address_search_cache: list[dict[str, object]] = []
    for row in rows:
        short_label = short_address_label(row["house_number"], row["street_name"], row["full_address"])
        address_search_cache.append(
            {
                "id": row["id"],
                "source_ref": row["source_ref"],
                "full_address": row["full_address"],
                "short_address": short_label,
                "street_name": row["street_name"],
                "house_number": row["house_number"],
                "postal_code": row["postal_code"],
                "city": row["city"],
                "lon": row["lon"],
                "lat": row["lat"],
                "source": row["source"],
                "norm_full": normalize_address_text(row["full_address"]),
                "norm_short": normalize_address_text(short_label),
                "norm_street": normalize_address_text(row["street_name"] or ""),
                "norm_house_number": normalize_address_text(row["house_number"] or ""),
            }
        )

    address_street_cache: dict[str, list[dict[str, object]]] = {}
    for entry in address_search_cache:
        address_street_cache.setdefault(str(entry["norm_street"]), []).append(entry)

    with APP_CACHE_LOCK:
        if ADDRESS_SEARCH_CACHE is None:
            ADDRESS_SEARCH_CACHE = address_search_cache
            ADDRESS_STREET_CACHE = address_street_cache
        return ADDRESS_SEARCH_CACHE


def bbox_intersects(
    left: tuple[float, float, float, float] | dict[str, float],
    right: tuple[float, float, float, float] | dict[str, float],
) -> bool:
    if isinstance(left, dict):
        left_values = (left["min_lon"], left["min_lat"], left["max_lon"], left["max_lat"])
    else:
        left_values = left

    if isinstance(right, dict):
        right_values = (right["min_lon"], right["min_lat"], right["max_lon"], right["max_lat"])
    else:
        right_values = right

    left_min_lon, left_min_lat, left_max_lon, left_max_lat = left_values
    right_min_lon, right_min_lat, right_max_lon, right_max_lat = right_values
    return not (
        left_max_lon < right_min_lon
        or right_max_lon < left_min_lon
        or left_max_lat < right_min_lat
        or right_max_lat < left_min_lat
    )


def bbox_contains(
    outer: tuple[float, float, float, float] | dict[str, float],
    inner: tuple[float, float, float, float] | dict[str, float],
) -> bool:
    if isinstance(outer, dict):
        outer_values = (outer["min_lon"], outer["min_lat"], outer["max_lon"], outer["max_lat"])
    else:
        outer_values = outer

    if isinstance(inner, dict):
        inner_values = (inner["min_lon"], inner["min_lat"], inner["max_lon"], inner["max_lat"])
    else:
        inner_values = inner

    outer_min_lon, outer_min_lat, outer_max_lon, outer_max_lat = outer_values
    inner_min_lon, inner_min_lat, inner_max_lon, inner_max_lat = inner_values
    return (
        outer_min_lon <= inner_min_lon
        and outer_min_lat <= inner_min_lat
        and outer_max_lon >= inner_max_lon
        and outer_max_lat >= inner_max_lat
    )


def expand_bbox(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    city_bounds: dict[str, float],
) -> tuple[float, float, float, float]:
    lon_padding = max((max_lon - min_lon) * 0.12, 0.0012)
    lat_padding = max((max_lat - min_lat) * 0.12, 0.001)
    return (
        max(city_bounds["min_lon"], min_lon - lon_padding),
        max(city_bounds["min_lat"], min_lat - lat_padding),
        min(city_bounds["max_lon"], max_lon + lon_padding),
        min(city_bounds["max_lat"], max_lat + lat_padding),
    )


def _iter_points(coordinates: list) -> list[tuple[float, float]]:
    if not coordinates:
        return []
    if isinstance(coordinates[0], (int, float)):
        lon = coordinates[0]
        lat = coordinates[1]
        return [(float(lon), float(lat))]

    points: list[tuple[float, float]] = []
    for child in coordinates:
        points.extend(_iter_points(child))
    return points


def compute_feature_bbox(feature: dict) -> tuple[float, float, float, float]:
    geometry = feature.get("geometry") or {}
    points = _iter_points(geometry.get("coordinates") or [])
    if not points:
        return (0.0, 0.0, 0.0, 0.0)

    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    return (min(longitudes), min(latitudes), max(longitudes), max(latitudes))


def point_in_ring(point: tuple[float, float], ring: list[list[float]]) -> bool:
    inside = False
    previous_index = len(ring) - 1
    for index, current in enumerate(ring):
        current_lon = float(current[0])
        current_lat = float(current[1])
        previous_point = ring[previous_index]
        previous_lon = float(previous_point[0])
        previous_lat = float(previous_point[1])
        intersects = (current_lat > point[1]) != (previous_lat > point[1]) and point[0] < (
            ((previous_lon - current_lon) * (point[1] - current_lat)) / ((previous_lat - current_lat) or 1e-12)
            + current_lon
        )
        if intersects:
            inside = not inside
        previous_index = index
    return inside


def polygon_contains_point(point: tuple[float, float], polygon: list[list[list[float]]]) -> bool:
    if not polygon or not point_in_ring(point, polygon[0]):
        return False
    for ring in polygon[1:]:
        if point_in_ring(point, ring):
            return False
    return True


def feature_contains_point(feature: dict, point: tuple[float, float]) -> bool:
    geometry = feature.get("geometry") or {}
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if geometry_type == "Polygon":
        return polygon_contains_point(point, coordinates)
    if geometry_type == "MultiPolygon":
        return any(polygon_contains_point(point, polygon) for polygon in coordinates)
    return False


def feature_distance_to_point(feature: dict, point: tuple[float, float]) -> float:
    points = _iter_points((feature.get("geometry") or {}).get("coordinates") or [])
    if not points:
        return float("inf")

    min_lon = min(lon for lon, _ in points)
    min_lat = min(lat for _, lat in points)
    max_lon = max(lon for lon, _ in points)
    max_lat = max(lat for _, lat in points)
    clamped_lon = max(min_lon, min(point[0], max_lon))
    clamped_lat = max(min_lat, min(point[1], max_lat))
    return ((point[0] - clamped_lon) ** 2 + (point[1] - clamped_lat) ** 2) ** 0.5


def _feature_key(feature: dict) -> str:
    properties = feature.get("properties") or {}
    if feature.get("id"):
        return str(feature["id"])
    for candidate in ("cleabs", "objectid", "id", "label"):
        if properties.get(candidate):
            return str(properties[candidate])
    bbox = compute_feature_bbox(feature)
    return f"{bbox[0]:.7f}:{bbox[1]:.7f}:{bbox[2]:.7f}:{bbox[3]:.7f}"


def build_overview() -> dict:
    global OVERVIEW_CACHE

    with APP_CACHE_LOCK:
        if OVERVIEW_CACHE is not None:
            return OVERVIEW_CACHE

    connection = get_db_connection()
    try:
        resident_metrics = connection.execute(
            """
            SELECT
                COUNT(*) AS resident_count,
                COALESCE(SUM(black_bags_received), 0) AS black_bags_total,
                COALESCE(SUM(yellow_bags_received), 0) AS yellow_bags_total
            FROM residents
            """
        ).fetchone()
        address_metrics = connection.execute(
            """
            SELECT
                COUNT(*) AS address_count,
                COUNT(DISTINCT street_name) AS street_count,
                SUM(CASE WHEN source = 'BAN' THEN 1 ELSE 0 END) AS imported_address_count
            FROM city_addresses
            """
        ).fetchone()
        overview = {
            "resident_count": resident_metrics["resident_count"],
            "black_bags_total": resident_metrics["black_bags_total"],
            "yellow_bags_total": resident_metrics["yellow_bags_total"],
            "address_count": address_metrics["address_count"],
            "street_count": address_metrics["street_count"],
            "imported_address_count": address_metrics["imported_address_count"] or 0,
            "default_center": DOUARNENEZ_CENTER,
            "city_bounds": get_cached_city_bounds(),
            "map_bounds": get_frontend_map_bounds(),
            "territory_communes": list(TERRITORY_COMMUNES),
        }
    finally:
        connection.close()

    with APP_CACHE_LOCK:
        OVERVIEW_CACHE = overview
        return OVERVIEW_CACHE


def invalidate_overview_cache() -> None:
    global OVERVIEW_CACHE
    with APP_CACHE_LOCK:
        OVERVIEW_CACHE = None


RESIDENT_SELECT_COLUMNS = """
    r.id,
    r.address_id,
    r.last_name,
    r.first_name,
    r.phone,
    r.email,
    r.address_line,
    r.postal_code,
    r.city,
    r.lon,
    r.lat,
    r.black_bags_received,
    r.yellow_bags_received,
    r.notes,
    r.building_ref,
    r.building_area_m2,
    r.dwelling_index,
    r.updated_at,
    latest.distribution_date AS last_distribution_date,
    latest.black_bags AS last_distribution_black_bags,
    latest.yellow_bags AS last_distribution_yellow_bags,
    latest.notes AS last_distribution_notes
"""

RESIDENT_FROM_CLAUSE = """
    FROM residents r
    LEFT JOIN bag_distribution_events latest
        ON latest.id = (
            SELECT e.id
            FROM bag_distribution_events e
            WHERE e.resident_id = r.id
            ORDER BY e.distribution_date DESC, e.id DESC
            LIMIT 1
        )
"""


def search_residents(
    name: str = "",
    first_name: str = "",
    address: str = "",
    *,
    limit: int | None = 100,
) -> list[dict]:
    normalized_name = normalize_text(name)
    normalized_first_name = normalize_text(first_name)
    normalized_address = normalize_address_text(address)

    if not any((normalized_name, normalized_first_name, normalized_address)):
        query = f"""
            SELECT {RESIDENT_SELECT_COLUMNS}
            {RESIDENT_FROM_CLAUSE}
            ORDER BY r.last_name, r.first_name
        """
        if limit is not None:
            query += " LIMIT ?"

        connection = get_db_connection()
        try:
            params: tuple[object, ...] = () if limit is None else (limit,)
            rows = connection.execute(query, params).fetchall()
            return [row_to_dict(row) for row in rows]
        finally:
            connection.close()

    scored_rows: list[tuple[float, str, str, int]] = []
    for row in load_resident_suggestion_cache():
        total_score = 0.0

        if normalized_name:
            candidate_last_name = str(row["norm_last_name"])
            if not quick_candidate_match(normalized_name, candidate_last_name):
                continue
            matched, score = match_score(normalized_name, candidate_last_name)
            if not matched:
                continue
            total_score += score

        if normalized_first_name:
            candidate_first_name = str(row["norm_first_name"])
            if not quick_candidate_match(normalized_first_name, candidate_first_name):
                continue
            matched, score = match_score(normalized_first_name, candidate_first_name)
            if not matched:
                continue
            total_score += score

        if normalized_address:
            address_candidates = [str(row["norm_address"]), str(row["norm_short_address"])]
            if not any(
                quick_candidate_match(normalized_address, candidate)
                for candidate in address_candidates
                if candidate
            ):
                continue
            address_matches = [match_score(normalized_address, candidate) for candidate in address_candidates if candidate]
            valid_matches = [score for matched, score in address_matches if matched]
            if not valid_matches:
                continue
            total_score += max(valid_matches)

        scored_rows.append(
            (
                total_score,
                str(row["norm_last_name"]),
                str(row["norm_first_name"]),
                int(row["id"]),
            )
        )

    scored_rows.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    selected_rows = scored_rows if limit is None else scored_rows[:limit]
    return get_residents_by_ids([resident_id for _, _, _, resident_id in selected_rows])


def get_residents_by_ids(resident_ids: list[int]) -> list[dict]:
    if not resident_ids:
        return []

    placeholders = ", ".join("?" for _ in resident_ids)
    connection = get_db_connection()
    try:
        rows = connection.execute(
            f"""
            SELECT {RESIDENT_SELECT_COLUMNS}
            {RESIDENT_FROM_CLAUSE}
            WHERE r.id IN ({placeholders})
            """,
            tuple(resident_ids),
        ).fetchall()
    finally:
        connection.close()

    rows_by_id = {int(row["id"]): row_to_dict(row) for row in rows}
    return [rows_by_id[resident_id] for resident_id in resident_ids if resident_id in rows_by_id]


def get_resident(resident_id: int) -> dict | None:
    connection = get_db_connection()
    try:
        row = connection.execute(
            f"""
            SELECT {RESIDENT_SELECT_COLUMNS}
            {RESIDENT_FROM_CLAUSE}
            WHERE r.id = ?
            """,
            (resident_id,),
        ).fetchone()
        return row_to_dict(row) if row else None
    finally:
        connection.close()


def get_resident_history(resident_id: int) -> list[dict]:
    connection = get_db_connection()
    try:
        rows = connection.execute(
            """
            SELECT
                id,
                resident_id,
                distribution_date,
                black_bags,
                yellow_bags,
                notes,
                created_at
            FROM bag_distribution_events
            WHERE resident_id = ?
            ORDER BY distribution_date DESC, id DESC
            """,
            (resident_id,),
        ).fetchall()
        return [row_to_dict(row) for row in rows]
    finally:
        connection.close()


def normalize_distribution_date(value: str | None) -> str:
    if not value:
        return datetime.now().date().isoformat()

    return datetime.strptime(value, "%Y-%m-%d").date().isoformat()


def stock_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    return {
        "black_bags_in_stock": int(row["black_bags_in_stock"] or 0),
        "yellow_bags_in_stock": int(row["yellow_bags_in_stock"] or 0),
        "updated_at": row["updated_at"],
    }


def ensure_stock_row(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO bag_stock_state (id, black_bags_in_stock, yellow_bags_in_stock)
        VALUES (1, 0, 0)
        ON CONFLICT(id) DO NOTHING
        """
    )


def get_bag_stock_from_connection(connection: sqlite3.Connection) -> dict[str, object]:
    ensure_stock_row(connection)
    row = connection.execute(
        """
        SELECT black_bags_in_stock, yellow_bags_in_stock, updated_at
        FROM bag_stock_state
        WHERE id = 1
        """
    ).fetchone()
    if row is None:
        raise RuntimeError("Stock introuvable.")
    return stock_row_to_dict(row)


def get_bag_stock() -> dict[str, object]:
    connection = get_db_connection()
    try:
        return get_bag_stock_from_connection(connection)
    finally:
        connection.close()


def adjust_bag_stock_from_connection(
    connection: sqlite3.Connection,
    *,
    black_delta: int = 0,
    yellow_delta: int = 0,
) -> dict[str, object]:
    ensure_stock_row(connection)
    connection.execute(
        """
        UPDATE bag_stock_state
        SET
            black_bags_in_stock = black_bags_in_stock + ?,
            yellow_bags_in_stock = yellow_bags_in_stock + ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
        """,
        (black_delta, yellow_delta),
    )
    return get_bag_stock_from_connection(connection)


def add_bag_stock(black_bags: int, yellow_bags: int) -> dict[str, object]:
    connection = get_db_connection()
    try:
        payload = adjust_bag_stock_from_connection(
            connection,
            black_delta=black_bags,
            yellow_delta=yellow_bags,
        )
        connection.commit()
        return payload
    finally:
        connection.close()


def refresh_resident_latest_bags(connection: sqlite3.Connection, resident_id: int) -> None:
    latest_event = connection.execute(
        """
        SELECT black_bags, yellow_bags, notes
        FROM bag_distribution_events
        WHERE resident_id = ?
        ORDER BY distribution_date DESC, id DESC
        LIMIT 1
        """,
        (resident_id,),
    ).fetchone()

    if latest_event is None:
        black_bags = 0
        yellow_bags = 0
        notes = ""
    else:
        black_bags = latest_event["black_bags"]
        yellow_bags = latest_event["yellow_bags"]
        notes = latest_event["notes"] or ""

    connection.execute(
        """
        UPDATE residents
        SET
            black_bags_received = ?,
            yellow_bags_received = ?,
            notes = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (black_bags, yellow_bags, notes, resident_id),
    )


def get_resident_from_connection(connection: sqlite3.Connection, resident_id: int) -> dict | None:
    row = connection.execute(
        f"""
        SELECT {RESIDENT_SELECT_COLUMNS}
        {RESIDENT_FROM_CLAUSE}
        WHERE r.id = ?
        """,
        (resident_id,),
    ).fetchone()
    return row_to_dict(row) if row else None


def update_resident_bags(
    resident_id: int,
    black_bags: int,
    yellow_bags: int,
    notes: str | None,
    distribution_date: str | None = None,
) -> dict | None:
    connection = get_db_connection()
    try:
        resident_exists = connection.execute(
            """
            SELECT id
            FROM residents
            WHERE id = ?
            """,
            (resident_id,),
        ).fetchone()
        if resident_exists is None:
            return None

        normalized_date = normalize_distribution_date(distribution_date)
        connection.execute(
            """
            INSERT INTO bag_distribution_events (
                resident_id, distribution_date, black_bags, yellow_bags, notes
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (resident_id, normalized_date, black_bags, yellow_bags, notes or ""),
        )

        refresh_resident_latest_bags(connection, resident_id)
        adjust_bag_stock_from_connection(
            connection,
            black_delta=-black_bags,
            yellow_delta=-yellow_bags,
        )
        connection.commit()
        invalidate_overview_cache()
        return get_resident_from_connection(connection, resident_id)
    finally:
        connection.close()


def delete_resident_bag_event(resident_id: int, event_id: int) -> dict | None:
    connection = get_db_connection()
    try:
        event = connection.execute(
            """
            SELECT id, black_bags, yellow_bags
            FROM bag_distribution_events
            WHERE id = ? AND resident_id = ?
            """,
            (event_id, resident_id),
        ).fetchone()
        if event is None:
            return None

        connection.execute(
            """
            DELETE FROM bag_distribution_events
            WHERE id = ? AND resident_id = ?
            """,
            (event_id, resident_id),
        )
        refresh_resident_latest_bags(connection, resident_id)
        adjust_bag_stock_from_connection(
            connection,
            black_delta=int(event["black_bags"] or 0),
            yellow_delta=int(event["yellow_bags"] or 0),
        )
        connection.commit()
        invalidate_overview_cache()
        return get_resident_from_connection(connection, resident_id)
    finally:
        connection.close()


def search_address_directory(query_text: str = "") -> list[dict]:
    entries = load_address_search_cache()
    normalized_query = normalize_address_text(query_text)

    if not normalized_query:
        return entries[:25]

    query_tokens = [token for token in normalized_query.split() if token]
    house_number_tokens = [token for token in query_tokens if token.isdigit()]
    street_query = " ".join(token for token in query_tokens if not token.isdigit()).strip()
    street_cache = ADDRESS_STREET_CACHE or {}
    scored_entries: list[tuple[float, dict[str, object]]] = []

    if street_query:
        matched_streets: list[tuple[float, str]] = []
        for normalized_street in street_cache:
            matched, score = match_score(street_query, normalized_street)
            if matched:
                matched_streets.append((score, normalized_street))

        matched_streets.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
        for street_score, normalized_street in matched_streets[:35]:
            for entry in street_cache.get(normalized_street, []):
                score = street_score
                norm_short = str(entry["norm_short"])
                norm_full = str(entry["norm_full"])

                if normalized_query == norm_short or normalized_query == norm_full:
                    score += 80
                elif norm_short.startswith(normalized_query):
                    score += 46
                elif normalized_query in norm_short:
                    score += 30

                if house_number_tokens:
                    house_number = str(entry["norm_house_number"])
                    if house_number in house_number_tokens:
                        score += 70
                    elif any(house_number.startswith(token) for token in house_number_tokens):
                        score += 20
                    else:
                        score -= 22

                scored_entries.append((score, entry))

    if not scored_entries:
        for entry in entries:
            short_candidate = str(entry["norm_short"])
            full_candidate = str(entry["norm_full"])
            street_candidate = str(entry["norm_street"])
            score = 0.0

            if normalized_query in short_candidate:
                score = 115.0 - min(short_candidate.index(normalized_query), 20)
            elif normalized_query in full_candidate:
                score = 104.0 - min(full_candidate.index(normalized_query), 24)
            elif street_query and street_query in street_candidate:
                score = 88.0
            else:
                continue

            if house_number_tokens and str(entry["norm_house_number"]) in house_number_tokens:
                score += 70
            scored_entries.append((score, entry))

    if not scored_entries and street_query:
        for entry in entries:
            candidates = [entry["norm_short"], entry["norm_full"], entry["norm_street"]]
            matches = [match_score(normalized_query, candidate) for candidate in candidates if candidate]
            valid_scores = [score for matched, score in matches if matched]
            if not valid_scores:
                continue

            score = max(valid_scores)
            if house_number_tokens and str(entry["norm_house_number"]) in house_number_tokens:
                score += 70
            scored_entries.append((score, entry))

    deduped_scores: dict[int, tuple[float, dict[str, object]]] = {}
    for score, entry in scored_entries:
        entry_id = int(entry["id"])
        existing = deduped_scores.get(entry_id)
        if existing is None or score > existing[0]:
            deduped_scores[entry_id] = (score, entry)

    scored_entries = list(deduped_scores.values())

    scored_entries.sort(
        key=lambda item: (
            -item[0],
            len(str(item[1]["short_address"])),
            str(item[1]["norm_short"]),
        )
    )
    return [entry for _, entry in scored_entries[:50]]


def compact_building_feature(feature: dict, source: str) -> dict:
    properties = feature.get("properties") or {}
    geometry = feature.get("geometry")
    feature_id = str(feature.get("id") or properties.get("id") or properties.get("cleabs") or _feature_key(feature))

    if source == "ban" or properties.get("address_id"):
        kept_properties = {
            "id": properties.get("id") or feature_id,
            "address_id": properties.get("address_id"),
            "label": properties.get("label"),
            "street_name": properties.get("street_name"),
            "house_number": properties.get("house_number"),
            "resident_count": properties.get("resident_count"),
            "source": properties.get("source") or "ban",
            "point_lon": properties.get("point_lon"),
            "point_lat": properties.get("point_lat"),
        }
    else:
        kept_properties = {
            "id": feature_id,
            "cleabs": properties.get("cleabs"),
            "source": source,
        }

    return {
        "type": "Feature",
        "id": feature_id,
        "geometry": geometry,
        "properties": {
            key: value
            for key, value in kept_properties.items()
            if value not in (None, "")
        },
    }


def compact_building_features(features: list[dict], source: str) -> list[dict]:
    return [compact_building_feature(feature, source) for feature in features]


def load_resident_suggestion_cache() -> list[dict[str, object]]:
    global RESIDENT_SUGGESTION_CACHE

    with APP_CACHE_LOCK:
        if RESIDENT_SUGGESTION_CACHE is not None:
            return RESIDENT_SUGGESTION_CACHE

    connection = get_db_connection()
    try:
        rows = connection.execute(
            """
            SELECT
                r.id,
                r.address_id,
                r.last_name,
                r.first_name,
                r.address_line,
                r.lon,
                r.lat,
                a.house_number,
                a.street_name,
                a.full_address
            FROM residents r
            LEFT JOIN city_addresses a ON a.id = r.address_id
            ORDER BY r.last_name, r.first_name, r.address_line
            """
        ).fetchall()
    finally:
        connection.close()

    resident_suggestion_cache: list[dict[str, object]] = []
    for row in rows:
        address_label = short_address_label(row["house_number"], row["street_name"], row["address_line"])
        full_address = row["full_address"] or row["address_line"]
        resident_suggestion_cache.append(
            {
                "id": row["id"],
                "address_id": row["address_id"],
                "last_name": row["last_name"],
                "first_name": row["first_name"],
                "address_line": row["address_line"],
                "short_address": address_label,
                "full_address": full_address,
                "lon": row["lon"],
                "lat": row["lat"],
                "norm_last_name": normalize_text(row["last_name"]),
                "norm_first_name": normalize_text(row["first_name"]),
                "norm_address": normalize_address_text(row["address_line"]),
                "norm_short_address": normalize_address_text(address_label),
            }
        )

    with APP_CACHE_LOCK:
        if RESIDENT_SUGGESTION_CACHE is None:
            RESIDENT_SUGGESTION_CACHE = resident_suggestion_cache
        return RESIDENT_SUGGESTION_CACHE


def row_matches_search_context(
    row: dict[str, object],
    *,
    name: str = "",
    first_name: str = "",
    address: str = "",
) -> bool:
    return _row_matches_search_context(
        row,
        normalized_name=normalize_text(name),
        normalized_first_name=normalize_text(first_name),
        normalized_address=normalize_address_text(address),
    )


def _row_matches_search_context(
    row: dict[str, object],
    *,
    normalized_name: str = "",
    normalized_first_name: str = "",
    normalized_address: str = "",
) -> bool:

    if normalized_name:
        candidate_last_name = str(row["norm_last_name"])
        if not quick_candidate_match(normalized_name, candidate_last_name):
            return False
        matched, _ = match_score(normalized_name, candidate_last_name)
        if not matched:
            return False

    if normalized_first_name:
        candidate_first_name = str(row["norm_first_name"])
        if not quick_candidate_match(normalized_first_name, candidate_first_name):
            return False
        matched, _ = match_score(normalized_first_name, candidate_first_name)
        if not matched:
            return False

    if normalized_address:
        address_candidates = [str(row["norm_address"]), str(row["norm_short_address"])]
        if not any(quick_candidate_match(normalized_address, candidate) for candidate in address_candidates if candidate):
            return False
        if not any(match_score(normalized_address, candidate)[0] for candidate in address_candidates if candidate):
            return False

    return True


def filtered_resident_suggestion_rows(
    *,
    name: str = "",
    first_name: str = "",
    address: str = "",
) -> list[dict[str, object]]:
    normalized_name = normalize_text(name)
    normalized_first_name = normalize_text(first_name)
    normalized_address = normalize_address_text(address)

    if not any((normalized_name, normalized_first_name, normalized_address)):
        return load_resident_suggestion_cache()

    return [
        row
        for row in load_resident_suggestion_cache()
        if _row_matches_search_context(
            row,
            normalized_name=normalized_name,
            normalized_first_name=normalized_first_name,
            normalized_address=normalized_address,
        )
    ]


def load_name_suggestion_cache(field_name: str) -> list[str]:
    if field_name not in NAME_SUGGESTION_CACHE:
        return []

    with APP_CACHE_LOCK:
        cached_values = NAME_SUGGESTION_CACHE[field_name]
        if cached_values is not None:
            return cached_values

    column = "last_name" if field_name == "name" else "first_name"
    connection = get_db_connection()
    try:
        rows = connection.execute(f"SELECT DISTINCT {column} AS value FROM residents ORDER BY {column}").fetchall()
    finally:
        connection.close()

    cached_values = [str(row["value"]) for row in rows if row["value"]]
    with APP_CACHE_LOCK:
        if NAME_SUGGESTION_CACHE[field_name] is None:
            NAME_SUGGESTION_CACHE[field_name] = cached_values
        return NAME_SUGGESTION_CACHE[field_name] or []


def unique_scored_values(
    rows: list[dict[str, object]],
    *,
    value_key: str,
    normalized_value_key: str,
    normalized_query: str,
    label_key: str | None = None,
    limit: int = 8,
) -> list[dict[str, str]]:
    best_values: dict[str, tuple[float, str, str]] = {}
    for row in rows:
        raw_value = str(row[value_key])
        if not raw_value.strip():
            continue
        normalized_value = str(row[normalized_value_key])
        if not quick_candidate_match(normalized_query, normalized_value):
            continue
        matched, score = match_score(normalized_query, normalized_value)
        if not matched:
            continue

        label = str(row[label_key]) if label_key else raw_value
        existing = best_values.get(raw_value)
        if existing is None or score > existing[0]:
            best_values[raw_value] = (score, raw_value, label or raw_value)

    return [
        {"value": value, "label": label}
        for _, value, label in sorted(
            best_values.values(),
            key=lambda item: (-item[0], normalize_text(item[1])),
        )[:limit]
    ]


def get_search_suggestions(
    field_name: str,
    query_text: str,
    *,
    name: str = "",
    first_name: str = "",
    address: str = "",
) -> list[dict[str, str]]:
    normalized_query = normalize_address_text(query_text) if field_name == "address" else normalize_text(query_text)
    if len(normalized_query) < 2:
        return []

    context_rows = filtered_resident_suggestion_rows(
        name=name if field_name != "name" else "",
        first_name=first_name if field_name != "first_name" else "",
        address=address if field_name != "address" else "",
    )

    if field_name == "address":
        if any((name.strip(), first_name.strip())):
            return unique_scored_values(
                context_rows,
                value_key="short_address",
                normalized_value_key="norm_short_address",
                normalized_query=normalized_query,
                label_key="full_address",
                limit=8,
            )

        return [
            {
                "value": str(entry["short_address"]),
                "label": str(entry["full_address"]),
            }
            for entry in search_address_directory(query_text)[:8]
        ]

    if field_name == "name":
        return unique_scored_values(
            context_rows,
            value_key="last_name",
            normalized_value_key="norm_last_name",
            normalized_query=normalized_query,
            limit=8,
        )

    return unique_scored_values(
        context_rows,
        value_key="first_name",
        normalized_value_key="norm_first_name",
        normalized_query=normalized_query,
        limit=8,
    )


def get_residents_by_address_ids(address_ids: list[int]) -> list[dict]:
    if not address_ids:
        return []

    placeholders = ", ".join("?" for _ in address_ids)
    connection = get_db_connection()
    try:
        rows = connection.execute(
            f"""
            SELECT {RESIDENT_SELECT_COLUMNS}
            {RESIDENT_FROM_CLAUSE}
            WHERE r.address_id IN ({placeholders})
            ORDER BY r.last_name, r.first_name
            """,
            tuple(address_ids),
        ).fetchall()
        return [row_to_dict(row) for row in rows]
    finally:
        connection.close()


def get_residents_by_building_ref(building_ref: str) -> list[dict]:
    if not building_ref:
        return []

    connection = get_db_connection()
    try:
        rows = connection.execute(
            f"""
            SELECT {RESIDENT_SELECT_COLUMNS}
            {RESIDENT_FROM_CLAUSE}
            WHERE r.building_ref = ?
            ORDER BY r.last_name, r.first_name
            """,
            (building_ref,),
        ).fetchall()
        return [row_to_dict(row) for row in rows]
    finally:
        connection.close()


def _wfs_params(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    srs_name: str,
    *,
    count: int,
    start_index: int,
) -> dict[str, str]:
    return {
        "SERVICE": "WFS",
        "REQUEST": "GetFeature",
        "VERSION": "2.0.0",
        "TYPENAMES": "BDTOPO_V3:batiment",
        "OUTPUTFORMAT": "application/json",
        "COUNT": str(count),
        "STARTINDEX": str(start_index),
        "SRSNAME": srs_name,
        "BBOX": f"{min_lon},{min_lat},{max_lon},{max_lat},{srs_name}",
    }


def _fetch_wfs_geojson(params: dict[str, str]) -> dict:
    request_url = f"{GEOPF_WFS_URL}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        request_url,
        headers={
            "User-Agent": "DouarnenezWasteTracker/1.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_remote_features_for_srs(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    srs_name: str,
) -> list[dict]:
    features: list[dict] = []
    seen_keys: set[str] = set()

    for page_index in range(REMOTE_BUILDING_MAX_PAGES):
        start_index = page_index * REMOTE_BUILDING_PAGE_SIZE
        payload = _fetch_wfs_geojson(
            _wfs_params(
                min_lon,
                min_lat,
                max_lon,
                max_lat,
                srs_name,
                count=REMOTE_BUILDING_PAGE_SIZE,
                start_index=start_index,
            )
        )
        page_features = payload.get("features", [])
        if not page_features:
            break

        page_added = 0
        for feature in page_features:
            feature_key = _feature_key(feature)
            if feature_key in seen_keys:
                continue
            seen_keys.add(feature_key)
            features.append(feature)
            page_added += 1

        if len(page_features) < REMOTE_BUILDING_PAGE_SIZE or page_added == 0:
            break

    return features


def fetch_remote_buildings(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> dict:
    errors: list[str] = []

    for srs_name in ("CRS:84", "EPSG:4326"):
        try:
            features = _fetch_remote_features_for_srs(min_lon, min_lat, max_lon, max_lat, srs_name)
            if features:
                return {
                    "type": "FeatureCollection",
                    "source": "geopf",
                    "features": features,
                }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            errors.append(str(error))

    raise RuntimeError("; ".join(errors) or "Aucun batiment distant disponible")


def create_indexed_feature_cache(features: list[dict]) -> list[dict[str, object]]:
    return [
        {
            "feature": feature,
            "bbox": compute_feature_bbox(feature),
        }
        for feature in features
    ]


def load_local_building_snapshot(city_bounds: dict[str, float] | None = None) -> dict[str, object] | None:
    global LOCAL_BUILDING_SNAPSHOT_CACHE

    with APP_CACHE_LOCK:
        if LOCAL_BUILDING_SNAPSHOT_CACHE["indexed_features"] is not None:
            return LOCAL_BUILDING_SNAPSHOT_CACHE

    if not LOCAL_BUILDING_SNAPSHOT_PATH.exists():
        return None

    try:
        payload = json.loads(LOCAL_BUILDING_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    features = payload.get("features") or []
    if not features:
        return None

    snapshot_cache = {
        "indexed_features": create_indexed_feature_cache(features),
        "source": payload.get("source") or "local-geopf",
        "generated_at": payload.get("generated_at"),
        "city_bounds": city_bounds,
    }
    with APP_CACHE_LOCK:
        if LOCAL_BUILDING_SNAPSHOT_CACHE["indexed_features"] is None:
            LOCAL_BUILDING_SNAPSHOT_CACHE = snapshot_cache
        return LOCAL_BUILDING_SNAPSHOT_CACHE


def save_local_building_snapshot(collection: dict) -> Path:
    global LOCAL_BUILDING_SNAPSHOT_CACHE

    DATA_DIR.mkdir(exist_ok=True)
    payload = {
        "type": "FeatureCollection",
        "source": collection.get("source") or "local-geopf",
        "generated_at": collection.get("generated_at") or datetime.now(timezone.utc).isoformat(),
        "features": collection.get("features") or [],
    }
    LOCAL_BUILDING_SNAPSHOT_PATH.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with APP_CACHE_LOCK:
        LOCAL_BUILDING_SNAPSHOT_CACHE["indexed_features"] = create_indexed_feature_cache(payload["features"])
        LOCAL_BUILDING_SNAPSHOT_CACHE["source"] = payload["source"]
        LOCAL_BUILDING_SNAPSHOT_CACHE["generated_at"] = payload["generated_at"]
    return LOCAL_BUILDING_SNAPSHOT_PATH


def iter_city_grid_bboxes(
    city_bounds: dict[str, float],
    *,
    columns: int = LOCAL_BUILDING_GRID_COLUMNS,
    rows: int = LOCAL_BUILDING_GRID_ROWS,
) -> list[tuple[float, float, float, float]]:
    lon_step = (city_bounds["max_lon"] - city_bounds["min_lon"]) / max(columns, 1)
    lat_step = (city_bounds["max_lat"] - city_bounds["min_lat"]) / max(rows, 1)
    lon_overlap = max(lon_step * 0.08, 0.00022)
    lat_overlap = max(lat_step * 0.08, 0.00018)
    cells: list[tuple[float, float, float, float]] = []

    for row_index in range(rows):
        for column_index in range(columns):
            min_lon = city_bounds["min_lon"] + (column_index * lon_step)
            max_lon = city_bounds["min_lon"] + ((column_index + 1) * lon_step)
            min_lat = city_bounds["min_lat"] + (row_index * lat_step)
            max_lat = city_bounds["min_lat"] + ((row_index + 1) * lat_step)
            cells.append(
                (
                    max(city_bounds["min_lon"], min_lon - lon_overlap),
                    max(city_bounds["min_lat"], min_lat - lat_overlap),
                    min(city_bounds["max_lon"], max_lon + lon_overlap),
                    min(city_bounds["max_lat"], max_lat + lat_overlap),
                )
            )

    return cells


def build_local_building_snapshot() -> dict:
    city_bounds = get_cached_city_bounds()
    features: list[dict] = []
    seen_keys: set[str] = set()
    errors: list[str] = []

    for cell_bbox in iter_city_grid_bboxes(city_bounds):
        try:
            cell_collection = fetch_remote_buildings(*cell_bbox)
        except Exception as error:  # pragma: no cover - depends on network
            errors.append(str(error))
            continue

        for feature in cell_collection.get("features", []):
            feature_key = _feature_key(feature)
            if feature_key in seen_keys:
                continue
            seen_keys.add(feature_key)
            features.append(feature)

    if not features:
        raise RuntimeError("; ".join(errors) or "Aucun batiment distant recupere pour le cache local")

    collection = {
        "type": "FeatureCollection",
        "source": "local-geopf",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "features": features,
        "city_bounds": city_bounds,
    }
    save_local_building_snapshot(collection)
    return collection


def square_polygon(lon: float, lat: float, width: float = 0.00008, height: float = 0.00005) -> list[list[float]]:
    half_width = width / 2
    half_height = height / 2
    return [
        [lon - half_width, lat - half_height],
        [lon + half_width, lat - half_height],
        [lon + half_width, lat + half_height],
        [lon - half_width, lat + half_height],
        [lon - half_width, lat - half_height],
    ]


def offset_address_point(lon: float, lat: float, duplicate_index: int) -> tuple[float, float]:
    if duplicate_index < len(ADDRESS_OFFSET_PATTERN):
        x_factor, y_factor = ADDRESS_OFFSET_PATTERN[duplicate_index]
    else:
        ring = (duplicate_index // 8) + 2
        step = duplicate_index % 8
        pattern = (
            (ring, 0),
            (-ring, 0),
            (0, ring),
            (0, -ring),
            (ring, ring),
            (-ring, ring),
            (ring, -ring),
            (-ring, -ring),
        )
        x_factor, y_factor = pattern[step]

    return (
        lon + (x_factor * 0.000018),
        lat + (y_factor * 0.000014),
    )


def fetch_address_buildings(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> dict:
    connection = get_db_connection()
    try:
        rows = connection.execute(
            """
            SELECT
                a.id,
                a.source_ref,
                a.full_address,
                a.street_name,
                a.house_number,
                a.lon,
                a.lat,
                a.source,
                COUNT(r.id) AS resident_count
            FROM city_addresses a
            LEFT JOIN residents r ON r.address_id = a.id
            WHERE a.lon BETWEEN ? AND ?
              AND a.lat BETWEEN ? AND ?
            GROUP BY a.id, a.full_address, a.street_name, a.house_number, a.lon, a.lat
            ORDER BY a.street_name, a.house_number
            """,
            (min_lon, max_lon, min_lat, max_lat),
        ).fetchall()

        features = []
        duplicate_counts: dict[tuple[float, float], int] = {}
        for row in rows:
            group_key = (round(row["lon"], 7), round(row["lat"], 7))
            duplicate_index = duplicate_counts.get(group_key, 0)
            duplicate_counts[group_key] = duplicate_index + 1
            adjusted_lon, adjusted_lat = offset_address_point(row["lon"], row["lat"], duplicate_index)
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [square_polygon(adjusted_lon, adjusted_lat, width=0.00005, height=0.000034)],
                    },
                    "properties": {
                        "id": f"address-{row['id']}",
                        "address_id": row["id"],
                        "source_ref": row["source_ref"],
                        "label": row["full_address"],
                        "street_name": row["street_name"],
                        "house_number": row["house_number"],
                        "resident_count": row["resident_count"],
                        "source": str(row["source"]).lower(),
                        "point_lon": row["lon"],
                        "point_lat": row["lat"],
                    },
                }
            )

        return {
            "type": "FeatureCollection",
            "source": "ban",
            "features": features,
        }
    finally:
        connection.close()


def load_address_building_cache(city_bounds: dict[str, float] | None = None) -> dict[str, object]:
    global BAN_BUILDING_CACHE

    with APP_CACHE_LOCK:
        if BAN_BUILDING_CACHE["indexed_features"] is not None:
            return BAN_BUILDING_CACHE

    if city_bounds is None:
        city_bounds = get_cached_city_bounds()

    collection = fetch_address_buildings(
        city_bounds["min_lon"],
        city_bounds["min_lat"],
        city_bounds["max_lon"],
        city_bounds["max_lat"],
    )
    source = "ban"
    fallback_reason = None

    building_cache = {
        "indexed_features": create_indexed_feature_cache(collection["features"]),
        "source": source,
        "fallback_reason": fallback_reason,
        "city_bounds": city_bounds,
    }
    with APP_CACHE_LOCK:
        if BAN_BUILDING_CACHE["indexed_features"] is None:
            BAN_BUILDING_CACHE = building_cache
        return BAN_BUILDING_CACHE


def filter_indexed_features(
    indexed_features: list[dict[str, object]],
    bbox: tuple[float, float, float, float],
) -> list[dict]:
    return [
        indexed["feature"]
        for indexed in indexed_features
        if bbox_intersects(indexed["bbox"], bbox)
    ]


def get_remote_view_cache_entry(
    bbox: tuple[float, float, float, float],
) -> dict[str, object] | None:
    with APP_CACHE_LOCK:
        for entry in reversed(REMOTE_BUILDING_VIEW_CACHE):
            if bbox_contains(entry["bbox"], bbox):
                return entry
    return None


def store_remote_view_cache(
    bbox: tuple[float, float, float, float],
    features: list[dict],
) -> dict[str, object]:
    entry = {
        "bbox": bbox,
        "indexed_features": create_indexed_feature_cache(features),
    }
    with APP_CACHE_LOCK:
        REMOTE_BUILDING_VIEW_CACHE.append(entry)
        if len(REMOTE_BUILDING_VIEW_CACHE) > REMOTE_BUILDING_VIEW_CACHE_MAX_ENTRIES:
            del REMOTE_BUILDING_VIEW_CACHE[:-REMOTE_BUILDING_VIEW_CACHE_MAX_ENTRIES]
    return entry


def get_cached_buildings(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> dict:
    city_bounds = get_cached_city_bounds()
    buffered_bbox = expand_bbox(min_lon, min_lat, max_lon, max_lat, city_bounds)

    local_snapshot = load_local_building_snapshot(city_bounds)
    if local_snapshot is not None:
        indexed_features = local_snapshot["indexed_features"] or []
        filtered_features = filter_indexed_features(indexed_features, buffered_bbox)
        return {
            "type": "FeatureCollection",
            "source": "local-geopf",
            "features": compact_building_features(filtered_features, "local-geopf"),
            "total_city_buildings": len(indexed_features),
            "city_bounds": city_bounds,
            "generated_at": local_snapshot.get("generated_at"),
        }

    cached_remote_entry = get_remote_view_cache_entry(buffered_bbox)
    if cached_remote_entry is not None:
        indexed_features = cached_remote_entry["indexed_features"] or []
        filtered_features = filter_indexed_features(indexed_features, buffered_bbox)
        return {
            "type": "FeatureCollection",
            "source": "geopf",
            "features": compact_building_features(filtered_features, "geopf"),
            "total_city_buildings": len(indexed_features),
            "city_bounds": city_bounds,
            "cache_mode": "memory",
        }

    try:
        collection = fetch_remote_buildings(*buffered_bbox)
        store_remote_view_cache(buffered_bbox, collection["features"])
        return {
            "type": "FeatureCollection",
            "source": "geopf",
            "features": compact_building_features(collection["features"], "geopf"),
            "total_city_buildings": len(collection["features"]),
            "city_bounds": city_bounds,
            "cache_mode": "live",
        }
    except Exception as error:
        cache = load_address_building_cache(city_bounds)
        indexed_features = cache["indexed_features"] or []
        filtered_features = filter_indexed_features(indexed_features, buffered_bbox)
        return {
            "type": "FeatureCollection",
            "source": "ban",
            "features": compact_building_features(filtered_features, "ban"),
            "total_city_buildings": len(indexed_features),
            "city_bounds": city_bounds,
            "fallback_reason": str(error),
        }


def find_feature_address_ids(feature: dict) -> list[int]:
    feature_key = _feature_key(feature)
    cached_ids = FEATURE_ADDRESS_MATCH_CACHE.get(feature_key)
    if cached_ids is not None:
        return cached_ids

    properties = feature.get("properties") or {}
    raw_address_id = properties.get("address_id")
    if raw_address_id not in (None, ""):
        try:
            address_ids = [int(raw_address_id)]
            FEATURE_ADDRESS_MATCH_CACHE[feature_key] = address_ids
            return address_ids
        except (TypeError, ValueError):
            pass

    feature_bbox = compute_feature_bbox(feature)
    min_lon, min_lat, max_lon, max_lat = feature_bbox
    lon_margin = max((max_lon - min_lon) * 0.18, 0.00022)
    lat_margin = max((max_lat - min_lat) * 0.18, 0.00018)
    candidate_entries = [
        entry
        for entry in load_address_search_cache()
        if (min_lon - lon_margin) <= float(entry["lon"]) <= (max_lon + lon_margin)
        and (min_lat - lat_margin) <= float(entry["lat"]) <= (max_lat + lat_margin)
    ]
    if not candidate_entries:
        candidate_entries = load_address_search_cache()

    address_ids = [
        int(entry["id"])
        for entry in candidate_entries
        if feature_contains_point(feature, (float(entry["lon"]), float(entry["lat"])))
    ]
    if address_ids:
        deduped_ids = sorted(set(address_ids))
        FEATURE_ADDRESS_MATCH_CACHE[feature_key] = deduped_ids
        return deduped_ids

    threshold = max(min(max(max_lon - min_lon, max_lat - min_lat) * 0.65, 0.00012), 0.000055)
    nearby_entries = sorted(
        candidate_entries,
        key=lambda entry: feature_distance_to_point(feature, (float(entry["lon"]), float(entry["lat"]))),
    )
    deduped_ids: list[int] = []
    if nearby_entries:
        nearest_entry = nearby_entries[0]
        nearest_distance = feature_distance_to_point(
            feature,
            (float(nearest_entry["lon"]), float(nearest_entry["lat"])),
        )
        if nearest_distance <= threshold:
            deduped_ids = [int(nearest_entry["id"])]

    FEATURE_ADDRESS_MATCH_CACHE[feature_key] = deduped_ids
    return deduped_ids


def get_residents_for_feature(feature: dict) -> list[dict]:
    residents = get_residents_by_building_ref(_feature_key(feature))
    if residents:
        return residents

    address_ids = find_feature_address_ids(feature)
    return get_residents_by_address_ids(address_ids)


def get_ortho_tile_cache_path(z: int, x: int, y: int) -> Path:
    return ORTHO_TILE_CACHE_DIR / f"zoom_{z}" / str(x) / f"{y}.jpg"


def get_legacy_ortho_tile_cache_path(z: int, x: int, y: int) -> Path:
    return ORTHO_TILE_CACHE_DIR / "ign_ortho" / f"zoom_{z}" / str(x) / f"{y}.jpg"


def get_older_ortho_tile_cache_path(z: int, x: int, y: int) -> Path:
    return ORTHO_TILE_CACHE_DIR / "ign_ortho" / str(z) / str(x) / f"{y}.jpg"


def load_ortho_source_state() -> dict[str, object] | None:
    if not ORTHO_TILE_SOURCE_STATE_PATH.exists():
        return None
    try:
        payload = json.loads(ORTHO_TILE_SOURCE_STATE_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def save_ortho_source_state(state: dict[str, object]) -> None:
    ORTHO_TILE_SOURCE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = ORTHO_TILE_SOURCE_STATE_PATH.with_name(
        f"{ORTHO_TILE_SOURCE_STATE_PATH.stem}.{time.time_ns()}.tmp"
    )
    temp_path.write_text(json.dumps(state, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
    temp_path.replace(ORTHO_TILE_SOURCE_STATE_PATH)


def lon_lat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
    lat = max(min(lat, 85.05112878), -85.05112878)
    lat_rad = math.radians(lat)
    tile_count = 2**zoom
    x_tile = int((lon + 180.0) / 360.0 * tile_count)
    y_tile = int(
        (1.0 - math.log(math.tan(lat_rad) + (1.0 / math.cos(lat_rad))) / math.pi)
        / 2.0
        * tile_count
    )
    return (
        max(0, min(tile_count - 1, x_tile)),
        max(0, min(tile_count - 1, y_tile)),
    )


def iter_ortho_tiles(bounds: dict[str, float], min_zoom: int, max_zoom: int):
    for zoom in range(min_zoom, max_zoom + 1):
        min_x, max_y = lon_lat_to_tile(bounds["min_lon"], bounds["min_lat"], zoom)
        max_x, min_y = lon_lat_to_tile(bounds["max_lon"], bounds["max_lat"], zoom)
        x_start, x_end = sorted((min_x, max_x))
        y_start, y_end = sorted((min_y, max_y))
        for x_tile in range(x_start, x_end + 1):
            for y_tile in range(y_start, y_end + 1):
                yield zoom, x_tile, y_tile


def resolve_cached_ortho_tile_path(z: int, x: int, y: int) -> Path | None:
    tile_path = get_ortho_tile_cache_path(z, x, y)
    if tile_path.exists():
        return tile_path

    legacy_tile_paths = [
        get_legacy_ortho_tile_cache_path(z, x, y),
        get_older_ortho_tile_cache_path(z, x, y),
    ]
    for legacy_tile_path in legacy_tile_paths:
        if legacy_tile_path.exists():
            tile_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_tile_path.replace(tile_path)
            return tile_path

    return None


def build_ortho_capabilities_fallback_url() -> str:
    params = {
        "SERVICE": "WMTS",
        "REQUEST": "GetCapabilities",
        "VERSION": "1.0.0",
    }
    return f"{GEOPF_ORTHO_WMTS_URL}?{urllib.parse.urlencode(params)}"


def fetch_ortho_capabilities_document(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "DouarnenezWasteTracker/1.0",
            "Accept": "application/xml,text/xml;q=0.9,*/*;q=0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read()
        if not body:
            raise RuntimeError("Le document de capacites WMTS est vide")
        return body


def strip_xml_whitespace_nodes(element: ET.Element) -> None:
    for node in element.iter():
        if node.text is not None and not node.text.strip():
            node.text = None
        if node.tail is not None and not node.tail.strip():
            node.tail = None


def hash_xml_element(element: ET.Element) -> str:
    clone = ET.fromstring(ET.tostring(element, encoding="utf-8"))
    strip_xml_whitespace_nodes(clone)
    return hashlib.sha256(ET.tostring(clone, encoding="utf-8")).hexdigest()


def extract_ortho_capabilities_signature(document_bytes: bytes) -> dict[str, object]:
    signature: dict[str, object] = {
        "document_sha256": hashlib.sha256(document_bytes).hexdigest(),
        "document_size": len(document_bytes),
    }

    root = ET.fromstring(document_bytes)
    namespaces = {
        "wmts": "http://www.opengis.net/wmts/1.0",
        "ows": "http://www.opengis.net/ows/1.1",
    }
    for layer in root.findall(".//wmts:Layer", namespaces):
        identifier = layer.find("ows:Identifier", namespaces)
        if identifier is not None and (identifier.text or "").strip() == ORTHO_CAPABILITIES_LAYER_IDENTIFIER:
            signature["layer_sha256"] = hash_xml_element(layer)
            title = layer.find("ows:Title", namespaces)
            if title is not None and (title.text or "").strip():
                signature["layer_title"] = title.text.strip()
            break

    return signature


def ortho_source_signature(state: dict[str, object] | None) -> str:
    if not state:
        return ""
    layer_hash = str(state.get("layer_sha256") or "").strip()
    if layer_hash:
        return layer_hash
    return str(state.get("document_sha256") or "").strip()


def probe_remote_ortho_source_state() -> dict[str, object]:
    attempts = [ORTHO_CAPABILITIES_URL, build_ortho_capabilities_fallback_url()]
    last_error: Exception | None = None

    for url in attempts:
        try:
            document_bytes = fetch_ortho_capabilities_document(url)
            signature = extract_ortho_capabilities_signature(document_bytes)
            return {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "source_url": url,
                **signature,
            }
        except Exception as error:
            last_error = error

    raise RuntimeError(f"Impossible de recuperer les capacites WMTS ortho: {last_error}")


def build_ortho_tile_url(z: int, x: int, y: int) -> str:
    params = {
        "SERVICE": "WMTS",
        "REQUEST": "GetTile",
        "VERSION": "1.0.0",
        "LAYER": "ORTHOIMAGERY.ORTHOPHOTOS",
        "STYLE": "normal",
        "FORMAT": "image/jpeg",
        "TILEMATRIXSET": "PM",
        "TILEMATRIX": str(z),
        "TILEROW": str(y),
        "TILECOL": str(x),
    }
    return f"{GEOPF_ORTHO_WMTS_URL}?{urllib.parse.urlencode(params)}"


def fetch_remote_ortho_tile(z: int, x: int, y: int) -> bytes:
    request = urllib.request.Request(
        build_ortho_tile_url(z, x, y),
        headers={
            "User-Agent": "DouarnenezWasteTracker/1.0",
            "Accept": "image/jpeg,image/*;q=0.9,*/*;q=0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        content_type = response.headers.get("Content-Type", "")
        body = response.read()
        if "image" not in content_type.lower() or not body:
            raise RuntimeError("La tuile distante n'est pas une image valide")
        return body


def ensure_cached_ortho_tile(
    z: int,
    x: int,
    y: int,
    *,
    force: bool = False,
    refresh_stale: bool = True,
) -> tuple[Path, str]:
    tile_path = resolve_cached_ortho_tile_path(z, x, y) or get_ortho_tile_cache_path(z, x, y)

    if tile_path.exists() and not force:
        if not refresh_stale:
            return tile_path, "hit"
        age = time.time() - tile_path.stat().st_mtime
        if age <= ORTHO_TILE_DISK_MAX_AGE_SECONDS:
            return tile_path, "hit"

    try:
        tile_bytes = fetch_remote_ortho_tile(z, x, y)
        tile_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = tile_path.with_name(f"{tile_path.stem}.{time.time_ns()}.tmp")
        temp_path.write_bytes(tile_bytes)
        temp_path.replace(tile_path)
        return tile_path, "fetched"
    except Exception:
        if tile_path.exists():
            return tile_path, "stale"
        raise


def collect_missing_ortho_tiles(
    bounds: dict[str, float],
    min_zoom: int,
    max_zoom: int,
) -> tuple[int, int, list[tuple[int, int, int]]]:
    total_tiles = 0
    cached_tiles = 0
    missing_tiles: list[tuple[int, int, int]] = []

    for tile in iter_ortho_tiles(bounds, min_zoom, max_zoom):
        total_tiles += 1
        if resolve_cached_ortho_tile_path(*tile) is not None:
            cached_tiles += 1
        else:
            missing_tiles.append(tile)

    return total_tiles, cached_tiles, missing_tiles


def run_ortho_tile_cache_job(
    tiles: list[tuple[int, int, int]],
    *,
    force: bool = False,
    refresh_stale: bool = True,
    workers: int = 1,
    progress_step: int = 100,
    sleep_seconds: float = 0.0,
    label: str = "Cache tuiles",
) -> dict[str, object]:
    status_counts: dict[str, int] = {}
    failures = 0
    total_tiles = len(tiles)

    if total_tiles == 0:
        return {
            "total": 0,
            "status_counts": status_counts,
            "failures": failures,
        }

    worker_count = max(1, workers)
    if worker_count == 1:
        for index, (zoom, x_tile, y_tile) in enumerate(tiles, start=1):
            try:
                _, status = ensure_cached_ortho_tile(
                    zoom,
                    x_tile,
                    y_tile,
                    force=force,
                    refresh_stale=refresh_stale,
                )
                status_counts[status] = status_counts.get(status, 0) + 1
                if status == "fetched" and sleep_seconds > 0:
                    time.sleep(sleep_seconds)
            except Exception as error:
                failures += 1
                print(f"Echec tuile z{zoom}/{x_tile}/{y_tile}: {error}")

            if index == total_tiles or index % progress_step == 0:
                print(f"{label}: {index}/{total_tiles} - {status_counts} - echecs: {failures}")
        return {
            "total": total_tiles,
            "status_counts": status_counts,
            "failures": failures,
        }

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                ensure_cached_ortho_tile,
                zoom,
                x_tile,
                y_tile,
                force=force,
                refresh_stale=refresh_stale,
            ): (zoom, x_tile, y_tile)
            for zoom, x_tile, y_tile in tiles
        }
        for index, future in enumerate(as_completed(future_map), start=1):
            zoom, x_tile, y_tile = future_map[future]
            try:
                _, status = future.result()
                status_counts[status] = status_counts.get(status, 0) + 1
            except Exception as error:
                failures += 1
                print(f"Echec tuile z{zoom}/{x_tile}/{y_tile}: {error}")

            if index == total_tiles or index % progress_step == 0:
                print(f"{label}: {index}/{total_tiles} - {status_counts} - echecs: {failures}")

    return {
        "total": total_tiles,
        "status_counts": status_counts,
        "failures": failures,
    }


def ensure_frontend_ortho_tile_cache(bounds: dict[str, float]) -> None:
    total_tiles, cached_tiles, missing_tiles = collect_missing_ortho_tiles(
        bounds,
        ORTHO_TILE_FRONT_MIN_ZOOM,
        ORTHO_TILE_FRONT_MAX_ZOOM,
    )
    local_source_state = load_ortho_source_state()
    source_signature_changed = False

    try:
        remote_source_state = probe_remote_ortho_source_state()
        remote_signature = ortho_source_signature(remote_source_state)
        local_signature = ortho_source_signature(local_source_state)
        save_ortho_source_state(remote_source_state)

        if remote_signature:
            if local_signature and local_signature != remote_signature:
                source_signature_changed = True
                print(
                    "Source WMTS orthophoto modifiee depuis la derniere verification. "
                    "Un rafraichissement complet du cache local est necessaire."
                )
            elif local_signature == remote_signature:
                print("Source WMTS orthophoto inchangee depuis la derniere verification.")
            else:
                print("Empreinte source WMTS initialisee pour les prochains lancements.")
        else:
            print("Capacites WMTS lues, mais sans signature exploitable pour comparer les versions.")
    except Exception as error:
        print(
            "Impossible de verifier la version distante de la couche ortho. "
            f"Le cache local existant sera conserve tel quel: {error}"
        )

    print(
        f"Cache satellite frontal: {cached_tiles}/{total_tiles} tuiles presentes "
        f"(zoom {ORTHO_TILE_FRONT_MIN_ZOOM} a {ORTHO_TILE_FRONT_MAX_ZOOM})."
    )
    if not missing_tiles and not source_signature_changed:
        print("Cache satellite deja complet pour un usage hors ligne.")
        return

    if source_signature_changed:
        tiles_to_process = list(
            iter_ortho_tiles(bounds, ORTHO_TILE_FRONT_MIN_ZOOM, ORTHO_TILE_FRONT_MAX_ZOOM)
        )
        print(
            "Rafraichissement complet des tuiles locales pour aligner le cache "
            "avec la nouvelle version distante."
        )
    else:
        tiles_to_process = missing_tiles
        print(
            f"Cache satellite incomplet: {len(missing_tiles)} tuiles manquantes. "
            "Telechargement des tuiles absentes en arriere-plan..."
        )

    probe_count = min(3, len(tiles_to_process))
    processed_probe_tiles: set[tuple[int, int, int]] = set()
    probe_status_counts: dict[str, int] = {}
    probe_failures: list[tuple[tuple[int, int, int], Exception]] = []

    for tile in tiles_to_process[:probe_count]:
        processed_probe_tiles.add(tile)
        try:
            _, status = ensure_cached_ortho_tile(*tile, force=source_signature_changed, refresh_stale=False)
            probe_status_counts[status] = probe_status_counts.get(status, 0) + 1
        except Exception as error:
            probe_failures.append((tile, error))

    if not probe_status_counts and probe_failures:
        failed_tile, failed_error = probe_failures[0]
        print(
            "Verification des tuiles interrompue: impossible de telecharger les tuiles manquantes "
            f"(premier echec z{failed_tile[0]}/{failed_tile[1]}/{failed_tile[2]}: {failed_error})."
        )
        print(f"L'application reste utilisable avec {cached_tiles}/{total_tiles} tuiles deja disponibles.")
        return

    remaining_tiles = [tile for tile in tiles_to_process if tile not in processed_probe_tiles]
    result = run_ortho_tile_cache_job(
        remaining_tiles,
        force=source_signature_changed,
        refresh_stale=False,
        workers=ORTHO_TILE_STARTUP_WORKERS,
        progress_step=ORTHO_TILE_PROGRESS_STEP,
        label="Cache satellite",
    )

    status_counts = dict(result["status_counts"])
    for status, count in probe_status_counts.items():
        status_counts[status] = status_counts.get(status, 0) + count
    failures = int(result["failures"]) + len(probe_failures)
    if source_signature_changed:
        available_tiles = total_tiles - failures
    else:
        available_tiles = cached_tiles + len(missing_tiles) - failures

    print(
        f"Verification satellite terminee: {available_tiles}/{total_tiles} tuiles disponibles. "
        f"Statuts: {status_counts}. Echecs: {failures}."
    )


def send_ortho_tile_response(handler: SimpleHTTPRequestHandler, z: int, x: int, y: int) -> None:
    try:
        tile_path, cache_status = ensure_cached_ortho_tile(z, x, y, refresh_stale=False)
    except Exception as error:
        json_response(handler, {"error": f"Tuile satellite indisponible: {error}"}, status=502)
        return

    file_response(
        handler,
        tile_path,
        content_type="image/jpeg",
        extra_headers={
            "Cache-Control": f"public, max-age={ORTHO_TILE_BROWSER_MAX_AGE_SECONDS}",
            "X-Tile-Cache": cache_status,
        },
    )


class AppHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        ortho_tile = parse_ortho_tile_route(route)
        if ortho_tile is not None:
            send_ortho_tile_response(self, *ortho_tile)
            return

        if route == "/api/overview":
            json_response(self, build_overview())
            return

        if route == "/api/stock":
            json_response(self, get_bag_stock())
            return

        if route == "/api/residents":
            raw_limit = query.get("limit", [""])[0].strip().lower()
            limit: int | None
            if raw_limit == "all":
                limit = None
            elif raw_limit:
                try:
                    limit = max(1, int(raw_limit))
                except ValueError:
                    json_response(self, {"error": "Parametre limit invalide."}, status=400)
                    return
            else:
                limit = 100

            residents = search_residents(
                name=query.get("name", [""])[0].strip(),
                first_name=query.get("first_name", [""])[0].strip(),
                address=query.get("address", [""])[0].strip(),
                limit=limit,
            )
            json_response(self, {"items": residents})
            return

        if route.startswith("/api/residents/") and route.endswith("/history"):
            resident_id = route.removeprefix("/api/residents/").removesuffix("/history").strip("/")
            if not resident_id.isdigit():
                json_response(self, {"error": "Identifiant habitant invalide."}, status=400)
                return
            resident = get_resident(int(resident_id))
            if resident is None:
                json_response(self, {"error": "Habitant introuvable."}, status=404)
                return
            json_response(
                self,
                {
                    "resident": resident,
                    "history": get_resident_history(int(resident_id)),
                },
            )
            return

        if route.startswith("/api/residents/"):
            resident_id = route.removeprefix("/api/residents/")
            if not resident_id.isdigit():
                json_response(self, {"error": "Identifiant habitant invalide."}, status=400)
                return
            resident = get_resident(int(resident_id))
            if resident is None:
                json_response(self, {"error": "Habitant introuvable."}, status=404)
                return
            json_response(self, resident)
            return

        if route == "/api/address-directory":
            directory = search_address_directory(query.get("q", [""])[0].strip())
            json_response(self, {"items": directory})
            return

        if route == "/api/address-residents":
            address_id = query.get("address_id", [""])[0].strip()
            if not address_id.isdigit():
                json_response(self, {"error": "Identifiant d'adresse invalide."}, status=400)
                return
            residents = get_residents_by_address_ids([int(address_id)])
            json_response(self, {"items": residents})
            return

        if route == "/api/search-suggestions":
            field_name = query.get("field", [""])[0].strip()
            if field_name not in {"name", "first_name", "address"}:
                json_response(self, {"error": "Champ de suggestion invalide."}, status=400)
                return
            suggestions = get_search_suggestions(
                field_name,
                query.get("q", [""])[0].strip(),
                name=query.get("name", [""])[0].strip(),
                first_name=query.get("first_name", [""])[0].strip(),
                address=query.get("address", [""])[0].strip(),
            )
            json_response(self, {"items": suggestions})
            return

        if route == "/api/buildings":
            try:
                min_lon, min_lat, max_lon, max_lat = parse_bbox(query.get("bbox", [""])[0])
            except ValueError as error:
                json_response(self, {"error": str(error)}, status=400)
                return

            try:
                payload = get_cached_buildings(min_lon, min_lat, max_lon, max_lat)
            except Exception as error:
                json_response(self, {"error": f"Chargement batiments impossible: {error}"}, status=500)
                return
            json_response(self, payload)
            return

        if route == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if route == "/api/stock/add":
            content_length = int(self.headers.get("Content-Length", "0"))
            try:
                body = self.rfile.read(content_length)
                payload = json.loads(body.decode("utf-8"))
                black_bags = int(payload.get("black_bags", 0))
                yellow_bags = int(payload.get("yellow_bags", 0))
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "Corps de requete invalide."}, status=400)
                return

            if black_bags < 0 or yellow_bags < 0:
                json_response(self, {"error": "Le stock ajoute ne peut pas etre negatif."}, status=400)
                return
            if black_bags == 0 and yellow_bags == 0:
                json_response(self, {"error": "Indiquez au moins un sac a ajouter."}, status=400)
                return

            json_response(self, add_bag_stock(black_bags, yellow_bags))
            return

        if route == "/api/building-residents":
            content_length = int(self.headers.get("Content-Length", "0"))
            try:
                body = self.rfile.read(content_length)
                payload = json.loads(body.decode("utf-8"))
                feature = payload.get("feature")
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "Corps de requete invalide."}, status=400)
                return

            if not isinstance(feature, dict):
                json_response(self, {"error": "Feature GeoJSON manquante."}, status=400)
                return

            residents = get_residents_for_feature(feature)
            json_response(
                self,
                {
                    "items": residents,
                    "matched_address_ids": find_feature_address_ids(feature),
                },
            )
            return

        if route.startswith("/api/residents/") and route.endswith("/bags"):
            resident_id = route.removeprefix("/api/residents/").removesuffix("/bags").strip("/")
            if not resident_id.isdigit():
                json_response(self, {"error": "Identifiant habitant invalide."}, status=400)
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            try:
                body = self.rfile.read(content_length)
                payload = json.loads(body.decode("utf-8"))
                black_bags = int(payload.get("black_bags", payload.get("black_bags_received", 0)))
                yellow_bags = int(payload.get("yellow_bags", payload.get("yellow_bags_received", 0)))
                notes = str(payload.get("notes", "")).strip()
                distribution_date = str(payload.get("distribution_date", "")).strip() or None
            except (ValueError, json.JSONDecodeError):
                json_response(self, {"error": "Corps de requete invalide."}, status=400)
                return

            if black_bags < 0 or yellow_bags < 0:
                json_response(self, {"error": "Les quantites ne peuvent pas etre negatives."}, status=400)
                return
            if black_bags == 0 and yellow_bags == 0:
                json_response(self, {"error": "Indiquez au moins un sac remis."}, status=400)
                return
            if black_bags > 200 or yellow_bags > 200:
                json_response(self, {"error": "La quantite de sacs semble trop elevee."}, status=400)
                return
            try:
                normalized_distribution_date = normalize_distribution_date(distribution_date)
            except ValueError:
                json_response(self, {"error": "Date de remise invalide. Format attendu: AAAA-MM-JJ."}, status=400)
                return

            resident = update_resident_bags(
                int(resident_id),
                black_bags,
                yellow_bags,
                notes,
                normalized_distribution_date,
            )
            if resident is None:
                json_response(self, {"error": "Habitant introuvable."}, status=404)
                return
            json_response(self, resident)
            return

        json_response(self, {"error": "Route introuvable."}, status=404)

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        parts = route.strip("/").split("/")

        if (
            len(parts) == 5
            and parts[0] == "api"
            and parts[1] == "residents"
            and parts[3] == "history"
            and parts[2].isdigit()
            and parts[4].isdigit()
        ):
            resident_id = int(parts[2])
            event_id = int(parts[4])
            resident = delete_resident_bag_event(resident_id, event_id)
            if resident is None:
                json_response(self, {"error": "Reception introuvable pour cet habitant."}, status=404)
                return
            json_response(
                self,
                {
                    "resident": resident,
                    "history": get_resident_history(resident_id),
                },
            )
            return

        json_response(self, {"error": "Route introuvable."}, status=404)

    def log_message(self, format: str, *args) -> None:
        return


class AppServer(ThreadingHTTPServer):
    daemon_threads = True


def warm_application_caches() -> None:
    started_at = time.perf_counter()
    try:
        load_address_search_cache()
        load_resident_suggestion_cache()
        city_bounds = get_cached_city_bounds()
        load_local_building_snapshot(city_bounds)
        ensure_frontend_ortho_tile_cache(get_frontend_map_bounds())
    except Exception as error:
        print(f"Prechargement des caches incomplet: {error}")
        return

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    print(f"Caches precharges en {elapsed_ms:.0f} ms")


def start_background_cache_warmup() -> threading.Thread:
    warmup_thread = threading.Thread(
        target=warm_application_caches,
        name="cache-warmup",
        daemon=True,
    )
    warmup_thread.start()
    return warmup_thread


def main() -> None:
    bootstrap_demo_data()
    server = AppServer((HOST, PORT), AppHandler)
    print(f"Serveur pret sur http://{HOST}:{PORT}")
    start_background_cache_warmup()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
