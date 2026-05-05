from __future__ import annotations

from bisect import bisect_left
import random
import sqlite3
import unicodedata
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "waste_tracking.db"
INSEE_FIRST_NAMES_CACHE_PATH = DATA_DIR / "insee_first_names_weighted.json"
TERRITORY_COMMUNE_CONFIGS = (
    {
        "city_code": "29046",
        "city_name": "Douarnenez",
        "population_2022": 14188,
    },
    {
        "city_code": "29090",
        "city_name": "Kerlaz",
        "population_2022": 798,
    },
    {
        "city_code": "29087",
        "city_name": "Le Juch",
        "population_2022": 753,
    },
    {
        "city_code": "29224",
        "city_name": "Pouldergat",
        "population_2022": 1213,
    },
    {
        "city_code": "29226",
        "city_name": "Poullan-sur-Mer",
        "population_2022": 1460,
    },
)
TERRITORY_COMMUNES = tuple(config["city_name"] for config in TERRITORY_COMMUNE_CONFIGS)
CONNECTION_PRAGMAS = (
    "PRAGMA foreign_keys = ON",
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA temp_store = MEMORY",
    "PRAGMA cache_size = -8192",
    "PRAGMA mmap_size = 67108864",
    "PRAGMA busy_timeout = 5000",
)

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS city_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ref TEXT UNIQUE,
    house_number TEXT,
    street_name TEXT NOT NULL,
    full_address TEXT NOT NULL UNIQUE,
    postal_code TEXT,
    city TEXT NOT NULL DEFAULT 'Douarnenez',
    lon REAL NOT NULL,
    lat REAL NOT NULL,
    source TEXT NOT NULL DEFAULT 'demo',
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS residents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address_id INTEGER REFERENCES city_addresses(id) ON DELETE SET NULL,
    last_name TEXT NOT NULL,
    first_name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    address_line TEXT NOT NULL,
    postal_code TEXT,
    city TEXT NOT NULL DEFAULT 'Douarnenez',
    lon REAL NOT NULL,
    lat REAL NOT NULL,
    black_bags_received INTEGER NOT NULL DEFAULT 0,
    yellow_bags_received INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    building_ref TEXT,
    building_area_m2 REAL,
    dwelling_index INTEGER,
    source TEXT NOT NULL DEFAULT 'demo',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bag_distribution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resident_id INTEGER NOT NULL REFERENCES residents(id) ON DELETE CASCADE,
    distribution_date TEXT NOT NULL,
    black_bags INTEGER NOT NULL DEFAULT 0,
    yellow_bags INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS bag_stock_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    black_bags_in_stock INTEGER NOT NULL DEFAULT 0,
    yellow_bags_in_stock INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_city_addresses_street ON city_addresses(street_name);
CREATE INDEX IF NOT EXISTS idx_city_addresses_city ON city_addresses(city);
CREATE INDEX IF NOT EXISTS idx_city_addresses_city_sort ON city_addresses(city, street_name, house_number, full_address);
CREATE INDEX IF NOT EXISTS idx_city_addresses_city_source ON city_addresses(city, source);
CREATE INDEX IF NOT EXISTS idx_residents_name ON residents(last_name, first_name);
CREATE INDEX IF NOT EXISTS idx_residents_first_name ON residents(first_name, last_name);
CREATE INDEX IF NOT EXISTS idx_residents_name_address ON residents(last_name, first_name, address_line);
CREATE INDEX IF NOT EXISTS idx_residents_address ON residents(address_line);
CREATE INDEX IF NOT EXISTS idx_residents_address_id ON residents(address_id);
CREATE INDEX IF NOT EXISTS idx_residents_source ON residents(source);
CREATE INDEX IF NOT EXISTS idx_bag_distribution_resident_date ON bag_distribution_events(resident_id, distribution_date DESC);
CREATE INDEX IF NOT EXISTS idx_bag_distribution_resident_date_id ON bag_distribution_events(resident_id, distribution_date DESC, id DESC);
"""

SAMPLE_ADDRESSES = [
    {
        "source_ref": "demo-quai-rosmeur-12",
        "house_number": "12",
        "street_name": "Quai du Rosmeur",
        "full_address": "12 Quai du Rosmeur, 29100 Douarnenez",
        "postal_code": "29100",
        "city": "Douarnenez",
        "lon": -4.3337,
        "lat": 48.0959,
        "source": "demo",
    },
    {
        "source_ref": "demo-jean-bart-5",
        "house_number": "5",
        "street_name": "Rue Jean Bart",
        "full_address": "5 Rue Jean Bart, 29100 Douarnenez",
        "postal_code": "29100",
        "city": "Douarnenez",
        "lon": -4.3348,
        "lat": 48.0955,
        "source": "demo",
    },
    {
        "source_ref": "demo-voltaire-18",
        "house_number": "18",
        "street_name": "Rue Voltaire",
        "full_address": "18 Rue Voltaire, 29100 Douarnenez",
        "postal_code": "29100",
        "city": "Douarnenez",
        "lon": -4.3315,
        "lat": 48.0948,
        "source": "demo",
    },
    {
        "source_ref": "demo-duguay-trouin-3",
        "house_number": "3",
        "street_name": "Rue Duguay-Trouin",
        "full_address": "3 Rue Duguay-Trouin, 29100 Douarnenez",
        "postal_code": "29100",
        "city": "Douarnenez",
        "lon": -4.3294,
        "lat": 48.0952,
        "source": "demo",
    },
    {
        "source_ref": "demo-anatole-24",
        "house_number": "24",
        "street_name": "Rue Anatole Le Braz",
        "full_address": "24 Rue Anatole Le Braz, 29100 Douarnenez",
        "postal_code": "29100",
        "city": "Douarnenez",
        "lon": -4.3278,
        "lat": 48.0965,
        "source": "demo",
    },
    {
        "source_ref": "demo-port-rhu-7",
        "house_number": "7",
        "street_name": "Rue du Port-Rhu",
        "full_address": "7 Rue du Port-Rhu, 29100 Douarnenez",
        "postal_code": "29100",
        "city": "Douarnenez",
        "lon": -4.3362,
        "lat": 48.0938,
        "source": "demo",
    },
    {
        "source_ref": "demo-enfer-10",
        "house_number": "10",
        "street_name": "Place de l'Enfer",
        "full_address": "10 Place de l'Enfer, 29100 Douarnenez",
        "postal_code": "29100",
        "city": "Douarnenez",
        "lon": -4.3298,
        "lat": 48.0932,
        "source": "demo",
    },
    {
        "source_ref": "demo-gaulle-28",
        "house_number": "28",
        "street_name": "Rue du General de Gaulle",
        "full_address": "28 Rue du General de Gaulle, 29100 Douarnenez",
        "postal_code": "29100",
        "city": "Douarnenez",
        "lon": -4.3305,
        "lat": 48.0969,
        "source": "demo",
    },
]

FRENCH_MALE_FIRST_NAMES = [
    "Jean",
    "Pierre",
    "Louis",
    "Jules",
    "Arthur",
    "Lucas",
    "Hugo",
    "Gabriel",
    "Raphael",
    "Leo",
    "Nathan",
    "Paul",
    "Mathis",
    "Theo",
    "Tom",
    "Clement",
    "Antoine",
    "Nicolas",
    "Adrien",
    "Alexandre",
    "Martin",
    "Vincent",
    "Baptiste",
    "Samuel",
    "Noe",
    "Maxime",
    "Florian",
    "Damien",
    "Quentin",
    "Julien",
]

FRENCH_FEMALE_FIRST_NAMES = [
    "Jeanne",
    "Louise",
    "Emma",
    "Alice",
    "Marie",
    "Lea",
    "Clara",
    "Camille",
    "Sarah",
    "Julie",
    "Manon",
    "Nina",
    "Mila",
    "Lucie",
    "Pauline",
    "Mathilde",
    "Chloe",
    "Anais",
    "Elise",
    "Margaux",
    "Amandine",
    "Valentine",
    "Agathe",
    "Celine",
    "Caroline",
    "Emilie",
    "Marine",
    "Lena",
    "Helene",
    "Sophie",
]

BRETON_MALE_FIRST_NAMES = [
    "Erwan",
    "Malo",
    "Mael",
    "Loic",
    "Yann",
    "Goulven",
    "Tangi",
    "Gwenole",
    "Alan",
    "Ewen",
    "Soizic",
    "Kaelig",
]

BRETON_FEMALE_FIRST_NAMES = [
    "Nolwenn",
    "Enora",
    "Maelle",
    "Annaig",
    "Morgane",
    "Gaelle",
    "Gwenola",
    "Maiwenn",
    "Rozenn",
    "Aela",
    "Maelig",
    "Katell",
]

FRENCH_LAST_NAMES = [
    "Martin",
    "Bernard",
    "Dubois",
    "Thomas",
    "Robert",
    "Richard",
    "Petit",
    "Durand",
    "Moreau",
    "Simon",
    "Laurent",
    "Lefebvre",
    "Michel",
    "Garcia",
    "David",
    "Bertrand",
    "Roux",
    "Vincent",
    "Fournier",
    "Morel",
    "Girard",
    "Andre",
    "Leroy",
    "Mercier",
    "Bonnet",
    "Francois",
]

BRETON_LAST_NAMES = [
    "Le Goff",
    "Le Gall",
    "Le Berre",
    "Le Roux",
    "Tanguy",
    "Madec",
    "Riou",
    "Salaun",
    "Briand",
    "Bihan",
    "Menez",
    "Cariou",
    "Pennanec'h",
    "Gourmelen",
    "Kerbrat",
    "Guillou",
    "Coroller",
    "Quemeneur",
    "Kermarec",
    "Typhaine",
    "Rannou",
    "Le Pape",
    "Morvan",
    "Derrien",
    "Abiven",
    "Cloarec",
]

HOUSEHOLD_NOTES = [
    "Passe le samedi matin au local technique.",
    "Prefere un rappel telephonique avant la distribution.",
    "A verifier lors de la prochaine tournee de quartier.",
    "Retrait effectue au point de collecte municipal.",
    "Habitation avec acces par la ruelle arriere.",
    "Besoins stables depuis la derniere campagne.",
]

DISTRIBUTION_CAMPAIGNS = [
    ("2025-05-06", "Campagne de printemps."),
    ("2025-09-17", "Distribution de rentree."),
    ("2026-01-14", "Reassort hivernal."),
    ("2026-04-08", "Derniere campagne municipale."),
]

DEFAULT_CITY_POPULATION = 14068
DEFAULT_TERRITORY_POPULATION = sum(config["population_2022"] for config in TERRITORY_COMMUNE_CONFIGS)
DEFAULT_AVERAGE_HOUSEHOLD_SIZE = 2.04
_INSEE_FIRST_NAME_POOLS: dict[bool, tuple[tuple[str, ...], tuple[int, ...], int]] | None = None


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=20, cached_statements=256)
    connection.row_factory = sqlite3.Row
    for pragma_sql in CONNECTION_PRAGMAS:
        try:
            connection.execute(pragma_sql)
        except sqlite3.DatabaseError:
            continue
    return connection


def initialize_database(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    connection = get_connection(db_path)
    connection.executescript(SCHEMA_SQL)
    ensure_resident_schema(connection)
    ensure_database_indexes(connection)
    ensure_stock_state(connection)
    connection.commit()
    return connection


def ensure_resident_schema(connection: sqlite3.Connection) -> None:
    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(residents)").fetchall()
    }
    migrations = {
        "building_ref": "ALTER TABLE residents ADD COLUMN building_ref TEXT",
        "building_area_m2": "ALTER TABLE residents ADD COLUMN building_area_m2 REAL",
        "dwelling_index": "ALTER TABLE residents ADD COLUMN dwelling_index INTEGER",
    }
    for column_name, migration_sql in migrations.items():
        if column_name not in existing_columns:
            connection.execute(migration_sql)


def ensure_database_indexes(connection: sqlite3.Connection) -> None:
    connection.execute("CREATE INDEX IF NOT EXISTS idx_residents_building_ref ON residents(building_ref)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_residents_first_name ON residents(first_name, last_name)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_residents_name_address ON residents(last_name, first_name, address_line)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_residents_address_id ON residents(address_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_residents_source ON residents(source)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_bag_distribution_resident_date_id "
        "ON bag_distribution_events(resident_id, distribution_date DESC, id DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_city_addresses_city_sort "
        "ON city_addresses(city, street_name, house_number, full_address)"
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_city_addresses_city_source ON city_addresses(city, source)")


def ensure_stock_state(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS bag_stock_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            black_bags_in_stock INTEGER NOT NULL DEFAULT 0,
            yellow_bags_in_stock INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        """
        INSERT INTO bag_stock_state (id, black_bags_in_stock, yellow_bags_in_stock)
        VALUES (1, 0, 0)
        ON CONFLICT(id) DO NOTHING
        """
    )


def seed_demo_addresses(connection: sqlite3.Connection) -> int:
    inserted = 0
    for address in SAMPLE_ADDRESSES:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO city_addresses (
                source_ref, house_number, street_name, full_address,
                postal_code, city, lon, lat, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                address["source_ref"],
                address["house_number"],
                address["street_name"],
                address["full_address"],
                address["postal_code"],
                address["city"],
                address["lon"],
                address["lat"],
                address["source"],
            ),
        )
        inserted += cursor.rowcount
    connection.commit()
    return inserted


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return normalized.lower().replace(" ", ".").replace("'", "").replace("-", ".")


def _coerce_positive_int(value: object, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _build_weighted_name_pool(entries: list[object]) -> tuple[tuple[str, ...], tuple[int, ...], int]:
    names: list[str] = []
    cumulative_weights: list[int] = []
    running_total = 0

    for entry in entries:
        if isinstance(entry, str):
            name = entry.strip()
            weight = 1
        elif isinstance(entry, dict):
            name = str(
                entry.get("name")
                or entry.get("prenom")
                or entry.get("first_name")
                or ""
            ).strip()
            weight = _coerce_positive_int(
                entry.get("weight")
                or entry.get("count")
                or entry.get("value")
                or 1,
                default=1,
            )
        else:
            continue

        if not name or weight <= 0:
            continue

        running_total += weight
        names.append(name)
        cumulative_weights.append(running_total)

    return tuple(names), tuple(cumulative_weights), running_total


def _load_insee_first_name_pools() -> dict[bool, tuple[tuple[str, ...], tuple[int, ...], int]]:
    global _INSEE_FIRST_NAME_POOLS

    if _INSEE_FIRST_NAME_POOLS is not None:
        return _INSEE_FIRST_NAME_POOLS

    if not INSEE_FIRST_NAMES_CACHE_PATH.exists():
        _INSEE_FIRST_NAME_POOLS = {}
        return _INSEE_FIRST_NAME_POOLS

    try:
        payload = json.loads(INSEE_FIRST_NAMES_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _INSEE_FIRST_NAME_POOLS = {}
        return _INSEE_FIRST_NAME_POOLS

    male_entries = payload.get("male") or payload.get("1") or []
    female_entries = payload.get("female") or payload.get("2") or []
    male_pool = _build_weighted_name_pool(list(male_entries))
    female_pool = _build_weighted_name_pool(list(female_entries))

    pools: dict[bool, tuple[tuple[str, ...], tuple[int, ...], int]] = {}
    if male_pool[2] > 0:
        pools[False] = male_pool
    if female_pool[2] > 0:
        pools[True] = female_pool

    _INSEE_FIRST_NAME_POOLS = pools
    return _INSEE_FIRST_NAME_POOLS


def _pick_weighted_name(
    rng: random.Random,
    pool: tuple[tuple[str, ...], tuple[int, ...], int],
) -> str:
    names, cumulative_weights, total_weight = pool
    if not names or total_weight <= 0:
        raise ValueError("Weighted first-name pool is empty.")

    draw = rng.randint(1, total_weight)
    index = bisect_left(cumulative_weights, draw)
    return names[index]


def _make_phone(
    rng: random.Random,
    used_phone_numbers: set[str] | None = None,
) -> str:
    used_numbers = used_phone_numbers if used_phone_numbers is not None else set()

    while True:
        prefix = "06" if rng.random() < 0.62 else "07"
        candidate = (
            f"{prefix} "
            f"{rng.randrange(100):02d} "
            f"{rng.randrange(100):02d} "
            f"{rng.randrange(100):02d} "
            f"{rng.randrange(100):02d}"
        )
        if candidate in used_numbers:
            continue
        used_numbers.add(candidate)
        return candidate


def _pick_household_surname(rng: random.Random) -> str:
    pool = BRETON_LAST_NAMES if rng.random() < 0.24 else FRENCH_LAST_NAMES
    return rng.choice(pool)


def _pick_first_name(
    rng: random.Random,
    *,
    female: bool | None = None,
    prefer_breton: bool | None = None,
) -> str:
    if female is None:
        female = rng.random() < 0.5

    weighted_pools = _load_insee_first_name_pools()
    weighted_pool = weighted_pools.get(bool(female))
    if weighted_pool is not None:
        return _pick_weighted_name(rng, weighted_pool)

    if prefer_breton is None:
        prefer_breton = rng.random() < 0.22

    if female:
        pool = BRETON_FEMALE_FIRST_NAMES if prefer_breton else FRENCH_FEMALE_FIRST_NAMES
    else:
        pool = BRETON_MALE_FIRST_NAMES if prefer_breton else FRENCH_MALE_FIRST_NAMES
    return rng.choice(pool)


def _target_household_count(address_count: int, target_population: int) -> int:
    if address_count <= 0 or target_population <= 0:
        return 0

    estimated = round(target_population / DEFAULT_AVERAGE_HOUSEHOLD_SIZE)
    return max(1, min(address_count, target_population, estimated))


def _allocate_household_sizes(
    household_count: int,
    target_population: int,
    rng: random.Random,
) -> list[int]:
    if household_count <= 0 or target_population <= 0:
        return []

    weighted_sizes = [1, 2, 3, 4, 5, 6]
    weighted_distribution = [45, 28, 14, 8, 4, 1]
    household_sizes = rng.choices(weighted_sizes, weights=weighted_distribution, k=household_count)

    current_population = sum(household_sizes)
    max_household_size = 6

    while current_population < target_population:
        candidates = [index for index, size in enumerate(household_sizes) if size < max_household_size]
        if not candidates:
            max_household_size += 1
            continue
        chosen_index = rng.choice(candidates)
        household_sizes[chosen_index] += 1
        current_population += 1

    while current_population > target_population:
        candidates = [index for index, size in enumerate(household_sizes) if size > 1]
        if not candidates:
            break
        chosen_index = rng.choice(candidates)
        household_sizes[chosen_index] -= 1
        current_population -= 1

    rng.shuffle(household_sizes)
    return household_sizes


def _household_note(rng: random.Random, household_size: int) -> str:
    base_note = rng.choice(HOUSEHOLD_NOTES)
    return f"{base_note} Foyer fictif de {household_size} personne(s)."


def _household_bag_counts(rng: random.Random, household_size: int) -> tuple[int, int]:
    black_bags = max(0, min(12, int(round(rng.gauss(0.9 + household_size * 0.9, 1.3)))))
    yellow_bags = max(0, min(10, int(round(rng.gauss(0.7 + household_size * 0.75, 1.1)))))
    return black_bags, yellow_bags


def _build_household_residents(
    rng: random.Random,
    *,
    address: sqlite3.Row,
    household_index: int,
    household_size: int,
    resident_index_start: int,
    used_phone_numbers: set[str],
) -> list[tuple[object, ...]]:
    surname = _pick_household_surname(rng)
    alternate_surname = _pick_household_surname(rng) if rng.random() < 0.14 else surname
    household_note = _household_note(rng, household_size)
    black_bags, yellow_bags = _household_bag_counts(rng, household_size)
    residents_to_insert: list[tuple[object, ...]] = []
    used_first_names: set[str] = set()

    if household_size == 1:
        gender_pattern = [rng.random() < 0.52]
    elif household_size == 2 and rng.random() < 0.72:
        gender_pattern = [False, True]
    else:
        adult_count = 2 if household_size >= 3 and rng.random() < 0.82 else 1
        gender_pattern = []
        for adult_index in range(adult_count):
            if adult_index == 1 and rng.random() < 0.72:
                gender_pattern.append(not gender_pattern[0])
            else:
                gender_pattern.append(rng.random() < 0.5)
        while len(gender_pattern) < household_size:
            gender_pattern.append(rng.random() < 0.5)

    for member_index in range(household_size):
        female = gender_pattern[member_index] if member_index < len(gender_pattern) else rng.random() < 0.5

        first_name = _pick_first_name(rng, female=female)
        for _ in range(4):
            if first_name not in used_first_names:
                break
            first_name = _pick_first_name(rng, female=female)
        used_first_names.add(first_name)

        last_name = surname
        if member_index == 1 and alternate_surname != surname:
            last_name = alternate_surname
        elif member_index > 1 and alternate_surname != surname and rng.random() < 0.08:
            last_name = alternate_surname

        resident_serial = resident_index_start + member_index
        email = (
            f"{_slugify(first_name)}.{_slugify(last_name)}."
            f"{household_index:05d}.{member_index + 1}@example.local"
        )
        residents_to_insert.append(
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
                black_bags,
                yellow_bags,
                household_note,
                "demo",
            )
        )

    return residents_to_insert


def seed_city_population(
    connection: sqlite3.Connection,
    *,
    target_population: int = DEFAULT_CITY_POPULATION,
    seed: int = 42,
    force: bool = False,
    max_households: int | None = None,
) -> int:
    if target_population <= 0:
        return 0

    if force:
        connection.execute(
            """
            DELETE FROM bag_distribution_events
            WHERE resident_id IN (SELECT id FROM residents WHERE source = 'demo')
            """
        )
        connection.execute("DELETE FROM residents WHERE source = 'demo'")
        connection.commit()

    existing_demo_count = connection.execute(
        "SELECT COUNT(*) FROM residents WHERE source = 'demo'"
    ).fetchone()[0]
    if existing_demo_count > 0:
        return 0

    addresses = connection.execute(
        """
        SELECT *
        FROM city_addresses
        ORDER BY CASE WHEN source = 'BAN' THEN 0 ELSE 1 END, street_name, house_number, full_address
        """
    ).fetchall()

    if not addresses:
        seed_demo_addresses(connection)
        addresses = connection.execute(
            "SELECT * FROM city_addresses ORDER BY street_name, house_number, full_address"
        ).fetchall()

    if not addresses:
        return 0

    rng = random.Random(seed)
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
    shuffled_addresses = list(addresses)
    rng.shuffle(shuffled_addresses)

    household_count = _target_household_count(len(shuffled_addresses), target_population)
    if max_households is not None:
        household_count = min(household_count, max(1, max_households))
    selected_addresses = shuffled_addresses[:household_count]
    household_sizes = _allocate_household_sizes(len(selected_addresses), target_population, rng)

    residents_to_insert: list[tuple[object, ...]] = []
    resident_index = 1
    for household_index, (address, household_size) in enumerate(
        zip(selected_addresses, household_sizes),
        start=1,
    ):
        household_residents = _build_household_residents(
            rng,
            address=address,
            household_index=household_index,
            household_size=household_size,
            resident_index_start=resident_index,
            used_phone_numbers=used_phone_numbers,
        )
        resident_index += len(household_residents)
        residents_to_insert.extend(household_residents)

    connection.executemany(
        """
        INSERT INTO residents (
            address_id, last_name, first_name, phone, email, address_line,
            postal_code, city, lon, lat, black_bags_received, yellow_bags_received,
            notes, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        residents_to_insert,
    )
    connection.commit()
    return len(residents_to_insert)


def seed_fake_residents(
    connection: sqlite3.Connection,
    *,
    seed: int = 42,
    max_households: int = 8,
    force: bool = False,
) -> int:
    target_population = max(max_households, round(max_households * DEFAULT_AVERAGE_HOUSEHOLD_SIZE))
    return seed_city_population(
        connection,
        target_population=target_population,
        seed=seed,
        force=force,
        max_households=max_households,
    )


def seed_distribution_history(
    connection: sqlite3.Connection,
    *,
    seed: int = 42,
    force: bool = False,
) -> int:
    if force:
        connection.execute(
            """
            DELETE FROM bag_distribution_events
            WHERE resident_id IN (SELECT id FROM residents WHERE source = 'demo')
            """
        )
        connection.commit()

    resident_rows = connection.execute(
        """
        SELECT id, black_bags_received, yellow_bags_received, notes
        FROM residents
        WHERE source = 'demo'
        ORDER BY id
        """
    ).fetchall()

    if not resident_rows:
        return 0

    existing_demo_events = connection.execute(
        """
        SELECT COUNT(*)
        FROM bag_distribution_events
        WHERE resident_id IN (SELECT id FROM residents WHERE source = 'demo')
        """
    ).fetchone()[0]
    if existing_demo_events > 0:
        return 0

    rng = random.Random(seed + 1000)
    events_to_insert: list[tuple[object, ...]] = []

    for resident in resident_rows:
        campaign_count = rng.randint(2, len(DISTRIBUTION_CAMPAIGNS))
        selected_campaigns = DISTRIBUTION_CAMPAIGNS[:campaign_count]

        for campaign_index, (distribution_date, campaign_note) in enumerate(selected_campaigns):
            if campaign_index == campaign_count - 1:
                black_bags = int(resident["black_bags_received"])
                yellow_bags = int(resident["yellow_bags_received"])
                note_parts = [campaign_note, resident["notes"] or ""]
            else:
                black_bags = rng.randint(0, 6)
                yellow_bags = rng.randint(0, 6)
                note_parts = [campaign_note]

            events_to_insert.append(
                (
                    resident["id"],
                    distribution_date,
                    black_bags,
                    yellow_bags,
                    " ".join(part.strip() for part in note_parts if part and part.strip()),
                )
            )

    connection.executemany(
        """
        INSERT INTO bag_distribution_events (
            resident_id, distribution_date, black_bags, yellow_bags, notes
        ) VALUES (?, ?, ?, ?, ?)
        """,
        events_to_insert,
    )
    connection.commit()
    return len(events_to_insert)


def bootstrap_demo_data(db_path: Path | str = DEFAULT_DB_PATH, *, force: bool = False) -> tuple[int, int]:
    connection = initialize_database(db_path)
    try:
        existing_address_count = connection.execute("SELECT COUNT(*) FROM city_addresses").fetchone()[0]
        inserted_addresses = 0
        if existing_address_count == 0:
            inserted_addresses = seed_demo_addresses(connection)
        inserted_residents = seed_city_population(connection, force=force)
        seed_distribution_history(connection, force=force)
        return inserted_addresses, inserted_residents
    finally:
        connection.close()
