from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import (  # noqa: E402
    LOCAL_BUILDING_SNAPSHOT_PATH,
    compute_feature_bbox,
    feature_contains_point,
    feature_distance_to_point,
    _feature_key,
)
from database import (  # noqa: E402
    DEFAULT_DB_PATH,
    DEFAULT_AVERAGE_HOUSEHOLD_SIZE,
    DEFAULT_TERRITORY_POPULATION,
    TERRITORY_COMMUNE_CONFIGS,
    initialize_database,
    _allocate_household_sizes,
    _make_phone,
    _pick_first_name,
    _pick_household_surname,
    _slugify,
)


EXCLUDED_NATURE_TERMS = (
    "eglise",
    "église",
    "chapelle",
    "serre",
    "tribune",
    "fort",
    "blockhaus",
    "casemate",
    "tour",
    "donjon",
)
EXCLUDED_USAGE_TERMS = (
    "agricole",
    "industriel",
    "religieux",
    "sportif",
)
COMMERCIAL_USAGE = "Commercial et services"
RESIDENTIAL_USAGE = "Résidentiel"
ANNUAL_2026_DOUARNENEZ_DATES = (
    "2026-03-09",
    "2026-03-10",
    "2026-03-11",
    "2026-03-12",
    "2026-03-13",
    "2026-03-16",
    "2026-03-17",
    "2026-03-18",
    "2026-03-19",
    "2026-03-20",
)
WEDNESDAY_PICKUP_2026_DATES = (
    "2026-03-25",
    "2026-04-01",
    "2026-04-08",
    "2026-04-15",
    "2026-04-22",
)
ANNUAL_2025_DOUARNENEZ_DATES = (
    "2025-03-10",
    "2025-03-11",
    "2025-03-12",
    "2025-03-13",
    "2025-03-14",
    "2025-03-17",
    "2025-03-18",
    "2025-03-19",
    "2025-03-20",
    "2025-03-21",
)


@dataclass(frozen=True)
class BuildingInfo:
    ref: str
    feature: dict
    bbox: tuple[float, float, float, float]
    area_m2: float
    logement_count: int | None
    estimated_dwellings: int
    floor_count: int
    is_mixed_use: bool


@dataclass(frozen=True)
class DwellingSlot:
    building: BuildingInfo
    address: sqlite3.Row
    dwelling_index: int
    weight: float


def ring_area_m2(ring: list[list[float]]) -> float:
    if len(ring) < 4:
        return 0.0

    lat0 = math.radians(sum(float(point[1]) for point in ring) / len(ring))
    meters_per_lon = 111_320 * math.cos(lat0)
    meters_per_lat = 110_540
    points = [
        (float(point[0]) * meters_per_lon, float(point[1]) * meters_per_lat)
        for point in ring
    ]
    return abs(
        sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])
        )
    ) / 2


def polygon_area_m2(polygon: list[list[list[float]]]) -> float:
    if not polygon:
        return 0.0
    outer_area = ring_area_m2(polygon[0])
    hole_area = sum(ring_area_m2(ring) for ring in polygon[1:])
    return max(0.0, outer_area - hole_area)


def feature_area_m2(feature: dict) -> float:
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    geometry_type = geometry.get("type")
    if geometry_type == "Polygon":
        return polygon_area_m2(coordinates)
    if geometry_type == "MultiPolygon":
        return sum(polygon_area_m2(polygon) for polygon in coordinates)
    return 0.0


def parse_positive_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else 0


def building_floor_count(properties: dict) -> int:
    raw_floor_count = parse_positive_int(properties.get("nombre_d_etages"))
    if raw_floor_count:
        return max(1, min(raw_floor_count + 1, 12))

    height = properties.get("hauteur")
    try:
        height_value = float(height)
    except (TypeError, ValueError):
        return 1

    return max(1, min(round(height_value / 3.0), 8))


def building_is_excluded(properties: dict) -> bool:
    if properties.get("construction_legere"):
        return True
    if properties.get("etat_de_l_objet") != "En service":
        return True

    text = " ".join(
        str(properties.get(key) or "").lower()
        for key in ("nature", "usage_1", "usage_2")
    )
    return any(term in text for term in EXCLUDED_NATURE_TERMS + EXCLUDED_USAGE_TERMS)


def building_is_habitable(properties: dict, area_m2: float) -> bool:
    logement_count = parse_positive_int(properties.get("nombre_de_logements"))
    usages = {properties.get("usage_1"), properties.get("usage_2")}

    if building_is_excluded(properties):
        return False
    if logement_count == 0:
        return False
    if logement_count and logement_count > 0:
        return True
    if RESIDENTIAL_USAGE in usages and area_m2 >= 22:
        return True
    return False


def estimate_dwellings(properties: dict, area_m2: float) -> int:
    logement_count = parse_positive_int(properties.get("nombre_de_logements"))
    if logement_count and logement_count > 0:
        return max(1, min(logement_count, 70))

    floors = building_floor_count(properties)
    effective_floor_area = area_m2 * floors
    if effective_floor_area < 145:
        estimated = 1
    elif effective_floor_area < 260:
        estimated = 2
    else:
        estimated = round(effective_floor_area / 78)

    if properties.get("usage_1") == COMMERCIAL_USAGE or properties.get("usage_2") == COMMERCIAL_USAGE:
        estimated = max(1, round(estimated * 0.65))

    return max(1, min(estimated, 50))


def load_habitable_buildings() -> list[BuildingInfo]:
    payload = json.loads(LOCAL_BUILDING_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    buildings: list[BuildingInfo] = []
    for feature in payload.get("features", []):
        properties = feature.get("properties") or {}
        area_m2 = feature_area_m2(feature)
        if not building_is_habitable(properties, area_m2):
            continue

        logement_count = parse_positive_int(properties.get("nombre_de_logements"))
        buildings.append(
            BuildingInfo(
                ref=_feature_key(feature),
                feature=feature,
                bbox=compute_feature_bbox(feature),
                area_m2=area_m2,
                logement_count=logement_count if logement_count and logement_count > 0 else None,
                estimated_dwellings=estimate_dwellings(properties, area_m2),
                floor_count=building_floor_count(properties),
                is_mixed_use=COMMERCIAL_USAGE in {properties.get("usage_1"), properties.get("usage_2")},
            )
        )
    return buildings


def bbox_cells(bbox: tuple[float, float, float, float], cell_size: float) -> set[tuple[int, int]]:
    min_lon, min_lat, max_lon, max_lat = bbox
    min_x = math.floor(min_lon / cell_size)
    max_x = math.floor(max_lon / cell_size)
    min_y = math.floor(min_lat / cell_size)
    max_y = math.floor(max_lat / cell_size)
    return {
        (x, y)
        for x in range(min_x, max_x + 1)
        for y in range(min_y, max_y + 1)
    }


def point_neighbor_cells(lon: float, lat: float, cell_size: float) -> set[tuple[int, int]]:
    center_x = math.floor(lon / cell_size)
    center_y = math.floor(lat / cell_size)
    return {
        (center_x + dx, center_y + dy)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
    }


def build_spatial_index(buildings: list[BuildingInfo], cell_size: float = 0.001) -> dict[tuple[int, int], list[BuildingInfo]]:
    index: dict[tuple[int, int], list[BuildingInfo]] = {}
    for building in buildings:
        for cell in bbox_cells(building.bbox, cell_size):
            index.setdefault(cell, []).append(building)
    return index


def building_match_threshold(building: BuildingInfo) -> float:
    min_lon, min_lat, max_lon, max_lat = building.bbox
    max_dimension = max(max_lon - min_lon, max_lat - min_lat)
    return max(min(max_dimension * 0.65, 0.00012), 0.000055)


def match_addresses_to_buildings(
    addresses: list[sqlite3.Row],
    buildings: list[BuildingInfo],
) -> dict[str, list[sqlite3.Row]]:
    cell_size = 0.001
    spatial_index = build_spatial_index(buildings, cell_size=cell_size)
    matched: dict[str, list[sqlite3.Row]] = {building.ref: [] for building in buildings}

    for address in addresses:
        lon = float(address["lon"])
        lat = float(address["lat"])
        candidates_by_ref: dict[str, BuildingInfo] = {}
        for cell in point_neighbor_cells(lon, lat, cell_size):
            for building in spatial_index.get(cell, []):
                candidates_by_ref[building.ref] = building

        candidates = list(candidates_by_ref.values())
        containing = [
            building
            for building in candidates
            if feature_contains_point(building.feature, (lon, lat))
        ]
        if containing:
            selected = min(containing, key=lambda building: building.area_m2)
            matched[selected.ref].append(address)
            continue

        nearest = sorted(
            candidates,
            key=lambda building: feature_distance_to_point(building.feature, (lon, lat)),
        )
        if nearest:
            best = nearest[0]
            if feature_distance_to_point(best.feature, (lon, lat)) <= building_match_threshold(best):
                matched[best.ref].append(address)

    return {building_ref: rows for building_ref, rows in matched.items() if rows}


def build_dwelling_slots(
    buildings: list[BuildingInfo],
    matched_addresses: dict[str, list[sqlite3.Row]],
) -> list[DwellingSlot]:
    building_by_ref = {building.ref: building for building in buildings}
    slots: list[DwellingSlot] = []

    for building_ref, addresses in matched_addresses.items():
        building = building_by_ref[building_ref]
        ordered_addresses = sorted(
            addresses,
            key=lambda row: (row["street_name"] or "", row["house_number"] or "", row["full_address"]),
        )
        dwelling_count = max(len(ordered_addresses), building.estimated_dwellings)
        dwelling_count = min(dwelling_count, max(len(ordered_addresses), 70))
        weight = max(0.25, min(4.0, math.sqrt(max(building.area_m2, 18) / 85)))
        if building.logement_count and building.logement_count >= 4:
            weight *= 1.25
        if building.is_mixed_use:
            weight *= 0.8

        for dwelling_index in range(1, dwelling_count + 1):
            address = ordered_addresses[(dwelling_index - 1) % len(ordered_addresses)]
            slots.append(
                DwellingSlot(
                    building=building,
                    address=address,
                    dwelling_index=dwelling_index,
                    weight=weight,
                )
            )

    return slots


def weighted_sample_slots(
    slots: list[DwellingSlot],
    target_count: int,
    rng: random.Random,
) -> list[DwellingSlot]:
    if target_count >= len(slots):
        selected = list(slots)
        rng.shuffle(selected)
        return selected

    scored_slots = [
        (rng.random() ** (1.0 / max(slot.weight, 0.05)), slot)
        for slot in slots
    ]
    scored_slots.sort(key=lambda item: item[0], reverse=True)
    selected = [slot for _, slot in scored_slots[:target_count]]
    rng.shuffle(selected)
    return selected


def default_territory_population_targets() -> dict[str, int]:
    return {
        str(config["city_name"]): int(config["population_2022"])
        for config in TERRITORY_COMMUNE_CONFIGS
    }


def household_bag_allocation(household_size: int, rng: random.Random, *, year: int) -> tuple[int, int]:
    year_adjustment = 0 if year == 2026 else rng.choice((-1, 0, 0, 1))
    black_bags = round(1.25 + household_size * 0.95 + year_adjustment + rng.choice((-1, 0, 0, 0, 1)))
    yellow_bags = round(1.15 + household_size * 0.9 + year_adjustment + rng.choice((-1, 0, 0, 1, 1)))
    return (
        max(1, min(9, black_bags)),
        max(1, min(9, yellow_bags)),
    )


def build_distribution_events(
    household_size: int,
    rng: random.Random,
) -> tuple[list[tuple[str, int, int, str]], str]:
    history: list[tuple[str, int, int, str]] = []
    black_2025, yellow_2025 = household_bag_allocation(household_size, rng, year=2025)
    history.append(
        (
            rng.choice(ANNUAL_2025_DOUARNENEZ_DATES),
            black_2025,
            yellow_2025,
            "Campagne annuelle 2025 Douarnenez - retrait au siege communautaire.",
        )
    )

    pickup_roll = rng.random()
    if pickup_roll < 0.88:
        black_2026, yellow_2026 = household_bag_allocation(household_size, rng, year=2026)
        history.append(
            (
                rng.choice(ANNUAL_2026_DOUARNENEZ_DATES),
                black_2026,
                yellow_2026,
                (
                    "Campagne annuelle 2026 Douarnenez, du 9 au 20 mars, "
                    "8h30-12h30 et 13h30-17h30 au 75 rue Ar Veret."
                ),
            )
        )
        status_note = "Distribution 2026 retiree pendant la campagne annuelle."
    elif pickup_roll < 0.96:
        black_2026, yellow_2026 = household_bag_allocation(household_size, rng, year=2026)
        history.append(
            (
                rng.choice(WEDNESDAY_PICKUP_2026_DATES),
                black_2026,
                yellow_2026,
                (
                    "Retrait de rattrapage le mercredi apres-midi, "
                    "13h30-17h30 a l'accueil de Douarnenez Communaute."
                ),
            )
        )
        status_note = "Distribution 2026 retiree en rattrapage du mercredi apres-midi."
    else:
        status_note = "Distribution 2026 non encore retiree au 23/04/2026."

    history.sort(key=lambda event: event[0])
    return history, status_note


def build_household_member_rows(
    *,
    slot: DwellingSlot,
    household_size: int,
    household_index: int,
    resident_index_start: int,
    latest_black_bags: int,
    latest_yellow_bags: int,
    status_note: str,
    used_phone_numbers: set[str],
    rng: random.Random,
) -> list[tuple[object, ...]]:
    surname = _pick_household_surname(rng)
    alternate_surname = _pick_household_surname(rng) if rng.random() < 0.12 else surname
    used_first_names: set[str] = set()
    rows: list[tuple[object, ...]] = []
    address = slot.address
    building = slot.building
    gender_pattern = [rng.random() < 0.5 for _ in range(household_size)]
    if household_size == 2 and rng.random() < 0.72:
        gender_pattern = [False, True]

    note = (
        f"{status_note} Foyer fictif de {household_size} personne(s). "
        f"Batiment residentiel {building.area_m2:.0f} m2, "
        f"{building.estimated_dwellings} logement(s) estime(s)."
    )
    if slot.dwelling_index > 1:
        note += f" Appartement fictif {slot.dwelling_index}."

    for member_index in range(household_size):
        female = gender_pattern[member_index]
        first_name = _pick_first_name(rng, female=female)
        for _ in range(4):
            if first_name not in used_first_names:
                break
            first_name = _pick_first_name(rng, female=female)
        used_first_names.add(first_name)

        last_name = surname
        if member_index == 1 and alternate_surname != surname:
            last_name = alternate_surname
        elif member_index > 1 and alternate_surname != surname and rng.random() < 0.06:
            last_name = alternate_surname

        email = (
            f"{_slugify(first_name)}.{_slugify(last_name)}."
            f"{household_index:05d}.{member_index + 1}@example.local"
        )
        rows.append(
            (
                address["id"],
                last_name,
                first_name,
                _make_phone(rng, used_phone_numbers),
                email,
                address["full_address"],
                address["postal_code"],
                address["city"],
                address["lon"],
                address["lat"],
                latest_black_bags,
                latest_yellow_bags,
                note,
                building.ref,
                round(building.area_m2, 2),
                slot.dwelling_index,
                "demo",
            )
        )

    return rows


def clear_demo_population(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        DELETE FROM bag_distribution_events
        WHERE resident_id IN (SELECT id FROM residents WHERE source = 'demo')
        """
    )
    connection.execute("DELETE FROM residents WHERE source = 'demo'")
    connection.execute("DELETE FROM sqlite_sequence WHERE name IN ('residents', 'bag_distribution_events')")
    connection.commit()


def insert_realistic_population(
    connection: sqlite3.Connection,
    *,
    target_population: int,
    seed: int,
    force: bool,
    dry_run: bool,
    population_targets_by_city: dict[str, int] | None = None,
) -> dict[str, int]:
    rng = random.Random(seed)
    addresses = connection.execute(
        """
        SELECT *
        FROM city_addresses
        WHERE source = 'BAN'
        ORDER BY street_name, house_number, full_address
        """
    ).fetchall()
    buildings = load_habitable_buildings()
    matched_addresses = match_addresses_to_buildings(addresses, buildings)
    slots = build_dwelling_slots(buildings, matched_addresses)
    target_households = min(len(slots), round(target_population / DEFAULT_AVERAGE_HOUSEHOLD_SIZE))
    occupied_slots = weighted_sample_slots(slots, target_households, rng)
    household_sizes = _allocate_household_sizes(len(occupied_slots), target_population, rng)

    metrics = {
        "addresses": len(addresses),
        "habitable_buildings": len(buildings),
        "matched_buildings": len(matched_addresses),
        "matched_addresses": sum(len(rows) for rows in matched_addresses.values()),
        "dwelling_slots": len(slots),
        "occupied_households": 0,
        "residents": 0,
        "events": 0,
    }

    household_plan: list[tuple[DwellingSlot, int]] = []
    if population_targets_by_city:
        for city_name, city_target_population in population_targets_by_city.items():
            normalized_city_name = str(city_name)
            target_for_city = max(0, int(city_target_population))
            city_slots = [
                slot
                for slot in slots
                if str(slot.address["city"] or "").strip() == normalized_city_name
            ]
            if not city_slots or target_for_city <= 0:
                continue

            city_household_count = min(
                len(city_slots),
                round(target_for_city / DEFAULT_AVERAGE_HOUSEHOLD_SIZE),
            )
            if city_household_count <= 0:
                continue

            city_occupied_slots = weighted_sample_slots(city_slots, city_household_count, rng)
            city_household_sizes = _allocate_household_sizes(
                len(city_occupied_slots),
                target_for_city,
                rng,
            )
            household_plan.extend(zip(city_occupied_slots, city_household_sizes))
    else:
        target_households = min(len(slots), round(target_population / DEFAULT_AVERAGE_HOUSEHOLD_SIZE))
        occupied_slots = weighted_sample_slots(slots, target_households, rng)
        household_sizes = _allocate_household_sizes(len(occupied_slots), target_population, rng)
        household_plan.extend(zip(occupied_slots, household_sizes))

    rng.shuffle(household_plan)
    metrics["occupied_households"] = len(household_plan)
    metrics["residents"] = sum(household_size for _, household_size in household_plan)

    if dry_run:
        return metrics

    if force:
        clear_demo_population(connection)
    elif connection.execute("SELECT COUNT(*) FROM residents WHERE source = 'demo'").fetchone()[0]:
        raise RuntimeError("Des habitants demo existent deja. Relancez avec --force pour les regenerer.")

    used_phone_numbers = {
        str(row[0]).strip()
        for row in connection.execute(
            """
            SELECT phone
            FROM residents
            WHERE phone IS NOT NULL AND TRIM(phone) <> ''
            """
        ).fetchall()
        if row[0]
    }
    resident_index = 1
    event_rows: list[tuple[object, ...]] = []
    with connection:
        for household_index, (slot, household_size) in enumerate(household_plan, start=1):
            history, status_note = build_distribution_events(household_size, rng)
            latest_event = history[-1]
            resident_rows = build_household_member_rows(
                slot=slot,
                household_size=household_size,
                household_index=household_index,
                resident_index_start=resident_index,
                latest_black_bags=latest_event[1],
                latest_yellow_bags=latest_event[2],
                status_note=status_note,
                used_phone_numbers=used_phone_numbers,
                rng=rng,
            )
            resident_index += len(resident_rows)

            resident_ids: list[int] = []
            for resident_row in resident_rows:
                cursor = connection.execute(
                    """
                    INSERT INTO residents (
                        address_id, last_name, first_name, phone, email, address_line,
                        postal_code, city, lon, lat, black_bags_received, yellow_bags_received,
                        notes, building_ref, building_area_m2, dwelling_index, source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    resident_row,
                )
                resident_ids.append(int(cursor.lastrowid))

            for resident_id in resident_ids:
                for distribution_date, black_bags, yellow_bags, notes in history:
                    event_rows.append((resident_id, distribution_date, black_bags, yellow_bags, notes))

        connection.executemany(
            """
            INSERT INTO bag_distribution_events (
                resident_id, distribution_date, black_bags, yellow_bags, notes
            ) VALUES (?, ?, ?, ?, ?)
            """,
            event_rows,
        )

    metrics["events"] = len(event_rows)
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Regenere les habitants fictifs en les rattachant aux batiments residentiels plausibles."
    )
    parser.add_argument("--force", action="store_true", help="Supprimer les habitants fictifs existants.")
    parser.add_argument("--dry-run", action="store_true", help="Analyser sans modifier la base.")
    parser.add_argument(
        "--population",
        type=int,
        default=DEFAULT_TERRITORY_POPULATION,
        help="Population cible du territoire.",
    )
    parser.add_argument("--seed", type=int, default=20260423, help="Graine deterministe.")
    parser.add_argument(
        "--by-city",
        action="store_true",
        help="Respecter les populations cibles par commune du territoire.",
    )
    args = parser.parse_args()

    connection = initialize_database(DEFAULT_DB_PATH)
    try:
        metrics = insert_realistic_population(
            connection,
            target_population=max(1, args.population),
            seed=args.seed,
            force=args.force,
            dry_run=args.dry_run,
            population_targets_by_city=default_territory_population_targets() if args.by_city else None,
        )
    finally:
        connection.close()

    prefix = "Analyse terminee" if args.dry_run else "Regeneration terminee"
    print(prefix + ".")
    for key, value in metrics.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
