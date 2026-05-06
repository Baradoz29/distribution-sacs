from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database import bootstrap_demo_data  # noqa: E402


def main() -> None:
    inserted_addresses, inserted_residents = bootstrap_demo_data(force=False)
    print(
        f"Base initialisee. Nouvelles adresses: {inserted_addresses}. "
        f"Nouveaux habitants fictifs: {inserted_residents}."
    )


if __name__ == "__main__":
    main()

