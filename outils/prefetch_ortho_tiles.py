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
    ORTHO_TILE_SOURCE_FALLBACK_SIGNATURE,
    clone_cached_ortho_generation,
    collect_missing_ortho_tiles,
    compare_remote_tiles_against_cached_generation,
    compare_remote_tiles_against_legacy_cache,
    get_current_ortho_source_signature,
    get_frontend_map_bounds,
    iter_ortho_tiles,
    migrate_legacy_ortho_tiles,
    normalize_ortho_source_signature,
    ortho_source_signature,
    probe_remote_ortho_source_state,
    run_ortho_tile_cache_job,
    save_ortho_source_state,
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
        "--all",
        action="store_true",
        help="Verifier toutes les tuiles du perimetre sans forcer leur retelechargement.",
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
    tiles = list(iter_ortho_tiles(bounds, min_zoom, max_zoom))
    stored_signature = get_current_ortho_source_signature()
    source_signature = stored_signature

    if stored_signature != ORTHO_TILE_SOURCE_FALLBACK_SIGNATURE:
        migrated_tiles = migrate_legacy_ortho_tiles(tiles, stored_signature)
    else:
        migrated_tiles = 0
    if migrated_tiles:
        print(
            f"Migration du cache ortho historique vers la generation {stored_signature[:12]}: "
            f"{migrated_tiles} tuiles rattachees."
        )

    try:
        remote_source_state = probe_remote_ortho_source_state()
        save_ortho_source_state(remote_source_state)
        source_signature = normalize_ortho_source_signature(ortho_source_signature(remote_source_state))
        print(f"Signature distante active: {source_signature[:12]}.")
    except Exception as error:
        print(
            "Verification de la signature distante impossible pour ce prechargement. "
            f"Utilisation de la signature locale {source_signature[:12]}: {error}"
        )

    if (
        stored_signature != ORTHO_TILE_SOURCE_FALLBACK_SIGNATURE
        and source_signature != ORTHO_TILE_SOURCE_FALLBACK_SIGNATURE
        and stored_signature != source_signature
    ):
        try:
            clone_verification = compare_remote_tiles_against_cached_generation(
                tiles,
                previous_signature=stored_signature,
            )
        except Exception as error:
            clone_verification = {"equivalent": False, "sampled": 0, "error": str(error)}

        if clone_verification.get("equivalent"):
            cloned_tiles = clone_cached_ortho_generation(
                tiles,
                previous_signature=stored_signature,
                current_signature=source_signature,
            )
            print(
                "La nouvelle signature distante correspond a l'ancienne generation sur l'echantillon teste. "
                f"{cloned_tiles} tuiles ont ete recopiees localement vers {source_signature[:12]}."
            )
    elif (
        stored_signature == ORTHO_TILE_SOURCE_FALLBACK_SIGNATURE
        and source_signature != ORTHO_TILE_SOURCE_FALLBACK_SIGNATURE
    ):
        try:
            legacy_verification = compare_remote_tiles_against_legacy_cache(tiles)
        except Exception as error:
            legacy_verification = {"equivalent": False, "sampled": 0, "error": str(error)}

        if legacy_verification.get("equivalent"):
            migrated_tiles = migrate_legacy_ortho_tiles(tiles, source_signature)
            if migrated_tiles:
                print(
                    "Le cache ortho historique non versionne correspond a la signature distante active. "
                    f"{migrated_tiles} tuiles ont ete rattachees a {source_signature[:12]}."
                )

    total_tiles, cached_tiles, missing_tiles = collect_missing_ortho_tiles(
        bounds,
        min_zoom,
        max_zoom,
        source_signature=source_signature,
    )
    print(
        f"Tuiles a verifier: {total_tiles} "
        f"(zoom {min_zoom} a {max_zoom}). Cache: {ORTHO_TILE_CACHE_DIR}"
    )
    print(f"Tuiles deja presentes: {cached_tiles}. Tuiles manquantes: {len(missing_tiles)}.")
    if args.dry_run:
        return

    if args.force or args.all:
        tiles_to_process = tiles
    else:
        tiles_to_process = missing_tiles

    print(
        "Mode de traitement: "
        + (
            "force (retelechargement complet)."
            if args.force
            else "verification complete sans retelechargement des tuiles deja a jour."
            if args.all
            else "telechargement des tuiles manquantes uniquement."
        )
    )
    result = run_ortho_tile_cache_job(
        tiles_to_process,
        force=args.force,
        source_signature=source_signature,
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
