from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import (  # noqa: E402
    ORTHO_TILE_CACHE_DIR,
    ORTHO_TILE_FRONT_MAX_ZOOM,
    ORTHO_TILE_FRONT_MIN_ZOOM,
    collect_missing_ortho_tiles,
    get_frontend_map_bounds,
    iter_ortho_tiles,
    run_ortho_tile_cache_job,
)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Precharge les tuiles satellite IGN du territoire de Douarnenez Communaute dans le cache disque local."
    )
    parser.add_argument(
        "--min-zoom",
        type=int,
        default=ORTHO_TILE_FRONT_MIN_ZOOM,
        help="Zoom minimum a precharger.",
    )
    parser.add_argument(
        "--max-zoom",
        type=int,
        default=ORTHO_TILE_FRONT_MAX_ZOOM,
        help="Zoom maximum a precharger.",
    )
    parser.add_argument("--force", action="store_true", help="Retelecharger les tuiles deja presentes.")
    parser.add_argument(
        "--refresh-stale",
        action="store_true",
        help="Verifier toutes les tuiles et retenter celles dont le cache est perime.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Afficher le volume sans telecharger.")
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Nombre de telechargements simultanes.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.015,
        help="Pause entre deux telechargements distants en mode mono-worker.",
    )
    args = parser.parse_args()

    min_zoom = max(0, min(args.min_zoom, args.max_zoom))
    max_zoom = min(21, max(args.min_zoom, args.max_zoom))

    bounds = get_frontend_map_bounds()

    total_tiles, cached_tiles, missing_tiles = collect_missing_ortho_tiles(bounds, min_zoom, max_zoom)
    print(
        f"Tuiles a verifier: {total_tiles} "
        f"(zoom {min_zoom} a {max_zoom}). Cache: {ORTHO_TILE_CACHE_DIR}"
    )
    print(f"Tuiles deja presentes: {cached_tiles}. Tuiles manquantes: {len(missing_tiles)}.")
    if args.dry_run:
        return

    if args.force or args.refresh_stale:
        tiles_to_process = list(iter_ortho_tiles(bounds, min_zoom, max_zoom))
    else:
        tiles_to_process = missing_tiles

    print(
        "Mode de traitement: "
        + (
            "force (retelechargement complet)."
            if args.force
            else "verification avec rafraichissement des tuiles anciennes."
            if args.refresh_stale
            else "telechargement des tuiles manquantes uniquement."
        )
    )
    result = run_ortho_tile_cache_job(
        tiles_to_process,
        force=args.force,
        refresh_stale=args.force or args.refresh_stale,
        workers=max(1, args.workers),
        progress_step=100,
        sleep_seconds=args.sleep if args.workers <= 1 else 0.0,
        label="Prechargement",
    )
    print(
        f"Prechargement termine. Statuts: {result['status_counts']}. "
        f"Echecs: {result['failures']}."
    )


if __name__ == "__main__":
    main()
