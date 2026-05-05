from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import LOCAL_BUILDING_SNAPSHOT_PATH, build_local_building_snapshot  # noqa: E402


def main() -> None:
    collection = build_local_building_snapshot()
    print(
        "Cache carte mis a jour. "
        f"Batiments en cache local: {len(collection.get('features', []))}. "
        f"Fichier: {LOCAL_BUILDING_SNAPSHOT_PATH}"
    )


if __name__ == "__main__":
    main()
