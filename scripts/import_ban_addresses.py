from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import sys
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database import DEFAULT_DB_PATH, initialize_database  # noqa: E402

BAN_URL_TEMPLATE = "https://adresse.data.gouv.fr/data/ban/adresses/latest/csv/adresses-{department}.csv.gz"


def value_from_row(row: dict[str, str], *names: str) -> str:
    for name in names:
        if name in row and row[name]:
            return row[name].strip()
    return ""


def normalise_full_address(number: str, repetition: str, street_name: str, postal_code: str, city: str) -> str:
    head = " ".join(part for part in [number, repetition, street_name] if part).strip()
    tail = " ".join(part for part in [postal_code, city] if part).strip()
    if head and tail:
        return f"{head}, {tail}"
    return head or tail


def import_ban_addresses(
    *,
    city_code: str,
    department: str,
    city_name: str,
    output_preview: bool = True,
) -> tuple[int, int]:
    connection = initialize_database(DEFAULT_DB_PATH)
    processed = 0
    preview_rows: list[dict[str, object]] = []

    request_url = BAN_URL_TEMPLATE.format(department=department)
    request = urllib.request.Request(
        request_url,
        headers={"User-Agent": "DouarnenezWasteTracker/1.0"},
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        compressed_stream = io.BytesIO(response.read())

    with gzip.open(compressed_stream, mode="rt", encoding="utf-8", newline="") as csv_stream:
        reader = csv.DictReader(csv_stream, delimiter=";")
        connection.execute(
            "DELETE FROM city_addresses WHERE source = 'BAN' AND city = ?",
            (city_name,),
        )

        for row in reader:
            code_insee = value_from_row(row, "code_insee", "insee_com", "commune_insee", "code_insee_commune")
            if code_insee != city_code:
                continue

            street_name = value_from_row(row, "nom_voie", "voie_nom")
            if not street_name:
                continue

            number = value_from_row(row, "numero")
            repetition = value_from_row(row, "rep")
            postal_code = value_from_row(row, "code_postal")
            city = value_from_row(row, "nom_commune", "commune_nom") or city_name
            longitude = value_from_row(row, "lon", "longitude")
            latitude = value_from_row(row, "lat", "latitude")
            source_ref = value_from_row(row, "id", "id_ban_adresse", "cle_interop")
            full_address = normalise_full_address(number, repetition, street_name, postal_code, city)

            if not full_address or not longitude or not latitude:
                continue

            connection.execute(
                """
                INSERT INTO city_addresses (
                    source_ref, house_number, street_name, full_address,
                    postal_code, city, lon, lat, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'BAN')
                ON CONFLICT(full_address) DO UPDATE SET
                    source_ref = excluded.source_ref,
                    house_number = excluded.house_number,
                    street_name = excluded.street_name,
                    full_address = excluded.full_address,
                    postal_code = excluded.postal_code,
                    city = excluded.city,
                    lon = excluded.lon,
                    lat = excluded.lat,
                    source = 'BAN'
                """,
                (
                    source_ref or full_address,
                    number,
                    street_name,
                    full_address,
                    postal_code,
                    city,
                    float(longitude),
                    float(latitude),
                ),
            )
            processed += 1

            if len(preview_rows) < 100:
                preview_rows.append(
                    {
                        "full_address": full_address,
                        "street_name": street_name,
                        "postal_code": postal_code,
                        "city": city,
                        "lon": float(longitude),
                        "lat": float(latitude),
                    }
                )

        connection.commit()

        if output_preview:
            preview_path = ROOT_DIR / "data" / "douarnenez_addresses_preview.json"
            preview_path.write_text(json.dumps(preview_rows, ensure_ascii=True, indent=2), encoding="utf-8")

        total = connection.execute(
            "SELECT COUNT(*) FROM city_addresses WHERE source = 'BAN' AND city = ?",
            (city_name,),
        ).fetchone()[0]

    connection.close()
    return processed, total


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Importe les adresses publiques BAN d'une commune dans la base locale."
    )
    parser.add_argument("--city-code", default="29046", help="Code INSEE de la commune.")
    parser.add_argument("--department", default="29", help="Code departemental.")
    parser.add_argument("--city-name", default="Douarnenez", help="Nom de la commune.")
    args = parser.parse_args()

    inserted, total = import_ban_addresses(
        city_code=args.city_code,
        department=args.department,
        city_name=args.city_name,
    )
    print(f"Import termine. Lignes inserees ou mises a jour: {inserted}. Total BAN en base: {total}.")


if __name__ == "__main__":
    main()
