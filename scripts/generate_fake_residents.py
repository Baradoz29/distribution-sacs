from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database import (  # noqa: E402
    DEFAULT_DB_PATH,
    DEFAULT_TERRITORY_POPULATION,
    initialize_database,
    seed_city_population,
    seed_demo_addresses,
    seed_distribution_history,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Genere des habitants fictifs et les rattache aux adresses disponibles."
    )
    parser.add_argument("--force", action="store_true", help="Regenerer les habitants fictifs.")
    parser.add_argument(
        "--households",
        type=int,
        default=None,
        help="Limiter le nombre d'adresses peuplees.",
    )
    parser.add_argument(
        "--population",
        type=int,
        default=DEFAULT_TERRITORY_POPULATION,
        help="Population fictive totale a generer sur le territoire.",
    )
    parser.add_argument("--seed", type=int, default=42, help="Graine aleatoire deterministe.")
    args = parser.parse_args()

    connection = initialize_database(DEFAULT_DB_PATH)
    try:
        existing_address_count = connection.execute("SELECT COUNT(*) FROM city_addresses").fetchone()[0]
        if existing_address_count == 0:
            seed_demo_addresses(connection)
        inserted = seed_city_population(
            connection,
            target_population=max(1, args.population),
            seed=args.seed,
            max_households=max(1, args.households) if args.households else None,
            force=args.force,
        )
        inserted_events = seed_distribution_history(
            connection,
            seed=args.seed,
            force=args.force,
        )
    finally:
        connection.close()

    print(
        f"Generation terminee. Habitants ajoutes: {inserted}. "
        f"Evenements ajoutes: {inserted_events}. Population cible: {max(1, args.population)}."
    )


if __name__ == "__main__":
    main()
