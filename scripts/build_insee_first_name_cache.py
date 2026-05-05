from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import re
import sys
import unicodedata
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database import INSEE_FIRST_NAMES_CACHE_PATH  # noqa: E402


INSEE_FIRST_NAMES_PAGE_URL = "https://www.insee.fr/fr/statistiques/8595130"
DEFAULT_DEPARTMENT_CODE = "29"
DEFAULT_REGION_CODE = "53"
USER_AGENT = "Douarnenez-Sac-Poubelles/1.0 (+https://www.insee.fr/)"
CSV_FILE_HINT_PATTERN = re.compile(
    r"""(?P<href>(?:https://www\.insee\.fr)?/fr/statistiques/fichier/8595130/[^"'<>]+)""",
    re.IGNORECASE,
)
LIGHT_DEPARTMENT_HINTS = ("2000", "allege", "light")


def normalize_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return normalized.lower().replace(" ", "_").replace("-", "_")


def normalize_first_name(value: object) -> str | None:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    if raw_value.upper() == "_PRENOMS_RARES_":
        return None

    normalized_spaces = " ".join(raw_value.replace("\xa0", " ").split())
    return normalized_spaces.lower().title()


def parse_positive_int(value: object) -> int:
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits else 0


def download_bytes(url: str, *, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def extract_candidate_urls(page_html: str, *, page_url: str) -> list[str]:
    urls: list[str] = []
    seen_urls: set[str] = set()

    for match in CSV_FILE_HINT_PATTERN.finditer(page_html):
        candidate = urljoin(page_url, unescape(match.group("href")))
        path = urlparse(candidate).path.lower()
        if not path.endswith((".csv", ".zip", ".gz")):
            continue
        if candidate in seen_urls:
            continue
        seen_urls.add(candidate)
        urls.append(candidate)

    return urls


def iter_csv_assets(source_url: str, raw_bytes: bytes) -> list[tuple[str, bytes]]:
    path = urlparse(source_url).path.lower()

    if zipfile.is_zipfile(io.BytesIO(raw_bytes)):
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
            return [
                (f"{source_url}::{member_name}", archive.read(member_name))
                for member_name in archive.namelist()
                if member_name.lower().endswith(".csv")
            ]

    if path.endswith(".gz"):
        archive_name = Path(path).name.removesuffix(".gz")
        return [(f"{source_url}::{archive_name}", gzip.decompress(raw_bytes))]

    return [(source_url, raw_bytes)]


def read_csv_rows(csv_bytes: bytes) -> tuple[csv.DictReader, dict[str, str]]:
    sample_text = csv_bytes[:8192].decode("utf-8-sig", errors="replace")
    delimiter = ";" if sample_text.count(";") >= sample_text.count(",") else ","
    text_stream = io.StringIO(csv_bytes.decode("utf-8-sig", errors="replace"))
    reader = csv.DictReader(text_stream, delimiter=delimiter)
    key_map = {
        normalize_key(field_name): field_name
        for field_name in (reader.fieldnames or [])
        if field_name
    }
    return reader, key_map


def detect_dataset_kind(key_map: dict[str, str], source_label: str) -> str | None:
    has_sex = "sexe" in key_map
    has_name = "prenom" in key_map or "preusuel" in key_map
    has_weight = "valeur" in key_map or "nombre" in key_map
    has_department = "dpt" in key_map
    has_region = "reg" in key_map

    if not has_sex or not has_name:
        return None
    if not has_weight:
        return "list"
    if has_department:
        source_path = urlparse(source_label).path.lower()
        if any(hint in source_path for hint in LIGHT_DEPARTMENT_HINTS):
            return "department_weighted_light"
        return "department_weighted"
    if has_region:
        return "region_weighted"
    return "national_weighted"


def dataset_rank(dataset_kind: str) -> int:
    ranks = {
        "department_weighted": 4,
        "department_weighted_light": 3,
        "region_weighted": 2,
        "national_weighted": 1,
        "list": 0,
    }
    return ranks.get(dataset_kind, -1)


def aggregate_dataset(
    *,
    dataset_kind: str,
    source_label: str,
    csv_bytes: bytes,
    department_code: str,
    region_code: str,
) -> dict[str, dict[str, int]]:
    reader, key_map = read_csv_rows(csv_bytes)
    name_column = key_map.get("prenom") or key_map.get("preusuel")
    sex_column = key_map.get("sexe")
    value_column = key_map.get("valeur") or key_map.get("nombre")
    department_column = key_map.get("dpt")
    region_column = key_map.get("reg")

    if not name_column or not sex_column:
        return {"1": {}, "2": {}}

    counts: dict[str, defaultdict[str, int]] = {
        "1": defaultdict(int),
        "2": defaultdict(int),
    }

    for row in reader:
        sex = str(row.get(sex_column) or "").strip()
        if sex not in {"1", "2"}:
            continue

        if dataset_kind.startswith("department_weighted") and department_column:
            if str(row.get(department_column) or "").strip() != department_code:
                continue
        elif dataset_kind == "region_weighted" and region_column:
            if str(row.get(region_column) or "").strip() != region_code:
                continue

        first_name = normalize_first_name(row.get(name_column))
        if not first_name:
            continue

        weight = 1
        if value_column:
            weight = parse_positive_int(row.get(value_column))
            if weight <= 0:
                continue

        counts[sex][first_name] += weight

    return {
        "1": dict(counts["1"]),
        "2": dict(counts["2"]),
    }


def serialize_counts(name_counts: dict[str, int]) -> list[dict[str, object]]:
    return [
        {"name": first_name, "weight": weight}
        for first_name, weight in sorted(
            name_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]


def build_cache_payload(
    *,
    page_url: str,
    source_label: str,
    dataset_kind: str,
    male_counts: dict[str, int],
    female_counts: dict[str, int],
    department_code: str,
    region_code: str,
) -> dict[str, object]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_page": page_url,
        "source_file": source_label,
        "source_kind": dataset_kind,
        "department_code": department_code if dataset_kind.startswith("department_") else None,
        "region_code": region_code if dataset_kind == "region_weighted" else None,
        "male": serialize_counts(male_counts),
        "female": serialize_counts(female_counts),
    }


def build_cache(
    *,
    page_url: str,
    department_code: str,
    region_code: str,
    timeout: int,
) -> dict[str, object]:
    page_html = download_bytes(page_url, timeout=timeout).decode("utf-8", errors="replace")
    candidate_urls = extract_candidate_urls(page_html, page_url=page_url)
    if not candidate_urls:
        raise RuntimeError("Aucun lien CSV Insee n'a ete detecte sur la page officielle.")

    best_payload: dict[str, object] | None = None
    best_rank = -1

    for candidate_url in candidate_urls:
        raw_bytes = download_bytes(candidate_url, timeout=timeout)
        for source_label, csv_bytes in iter_csv_assets(candidate_url, raw_bytes):
            _, key_map = read_csv_rows(csv_bytes)
            dataset_kind = detect_dataset_kind(key_map, source_label)
            if dataset_kind is None:
                continue

            aggregated = aggregate_dataset(
                dataset_kind=dataset_kind,
                source_label=source_label,
                csv_bytes=csv_bytes,
                department_code=department_code,
                region_code=region_code,
            )
            if not aggregated["1"] and not aggregated["2"]:
                continue

            current_rank = dataset_rank(dataset_kind)
            if current_rank < best_rank:
                continue

            best_payload = build_cache_payload(
                page_url=page_url,
                source_label=source_label,
                dataset_kind=dataset_kind,
                male_counts=aggregated["1"],
                female_counts=aggregated["2"],
                department_code=department_code,
                region_code=region_code,
            )
            best_rank = current_rank

    if best_payload is None:
        raise RuntimeError("Impossible de construire un cache de prenoms a partir des fichiers Insee.")

    return best_payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Construit un cache local de prenoms ponderes a partir des fichiers officiels de l'Insee."
    )
    parser.add_argument("--page-url", default=INSEE_FIRST_NAMES_PAGE_URL, help="Page officielle Insee a analyser.")
    parser.add_argument("--department", default=DEFAULT_DEPARTMENT_CODE, help="Code departement a privilegier.")
    parser.add_argument("--region", default=DEFAULT_REGION_CODE, help="Code region de repli.")
    parser.add_argument("--output", default=str(INSEE_FIRST_NAMES_CACHE_PATH), help="Fichier JSON de sortie.")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout reseau en secondes.")
    args = parser.parse_args()

    payload = build_cache(
        page_url=args.page_url,
        department_code=str(args.department).strip(),
        region_code=str(args.region).strip(),
        timeout=max(5, int(args.timeout)),
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Cache des prenoms Insee ecrit.")
    print(f"output: {output_path}")
    print(f"source_kind: {payload['source_kind']}")
    print(f"source_file: {payload['source_file']}")
    print(f"male_names: {len(payload['male'])}")
    print(f"female_names: {len(payload['female'])}")


if __name__ == "__main__":
    main()
