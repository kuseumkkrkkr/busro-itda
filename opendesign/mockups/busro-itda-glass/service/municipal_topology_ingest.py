"""Strict offline importer for official municipal route-stop CSV files.

The importer never infers identifiers across providers.  A municipal stop ID
is stored exactly as published, and a complete file is validated before one
atomic activation of its directed route sequences.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable

from network_catalog import CatalogError, CatalogLimitError, CatalogValidationError, NetworkCatalog


CHUNCHEON_COLUMNS = (
    "노선번호",
    "노선",
    "정류장순서",
    "정류장",
    "정류장명",
    "경도",
    "위도",
    "데이터기준일",
)
DEFAULT_MAX_CSV_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_ROWS = 100_000
DEFAULT_MAX_ROUTES = 5_000
DEFAULT_MAX_ROUTE_STOPS = 2_000
MAX_CELL_CHARS = 512
_IDENTIFIER = re.compile(r"^[0-9A-Za-z_.:-]{1,96}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class MunicipalTopologyError(ValueError):
    """Unsafe, incomplete, or unsupported municipal topology input."""


@dataclass(frozen=True, slots=True)
class MunicipalProfile:
    name: str
    dataset_name: str
    columns: tuple[str, ...]
    default_city_code: str
    city_name: str
    official_page_url: str
    official_download_url: str


PROFILES = {
    "chuncheon": MunicipalProfile(
        name="chuncheon",
        dataset_name="강원특별자치도 춘천시_버스정류장 노선정보_20260326",
        columns=CHUNCHEON_COLUMNS,
        default_city_code="32010",
        city_name="강원특별자치도 춘천시",
        official_page_url="https://www.data.go.kr/data/15060075/fileData.do",
        official_download_url=(
            "https://www.data.go.kr/cmm/cmm/fileDownload.do?"
            "atchFileId=FILE_000000003616614&fileDetailSn=1&insertDataPrcus=N"
        ),
    )
}


def _bounded_text(value: Any, field: str, *, required: bool = False, maximum: int = MAX_CELL_CHARS) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise MunicipalTopologyError(f"{field} is required")
    if len(text) > maximum:
        raise MunicipalTopologyError(f"{field} exceeds {maximum} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise MunicipalTopologyError(f"{field} contains control characters")
    return text


def _identifier(value: Any, field: str) -> str:
    text = _bounded_text(value, field, required=True, maximum=96)
    if not _IDENTIFIER.fullmatch(text):
        raise MunicipalTopologyError(f"{field} has an invalid identifier")
    return text


def _iso_date(value: Any, field: str) -> str:
    text = _bounded_text(value, field, required=True, maximum=10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise MunicipalTopologyError(f"{field} must be YYYY-MM-DD") from exc
    return parsed.isoformat()


def _coordinate(value: Any, field: str, minimum: float, maximum: float) -> float:
    text = _bounded_text(value, field, required=True, maximum=32)
    try:
        number = float(text)
    except ValueError as exc:
        raise MunicipalTopologyError(f"{field} is not numeric") from exc
    if not minimum <= number <= maximum:
        raise MunicipalTopologyError(f"{field} is outside the Republic of Korea bounds")
    return number


def _decode_csv(data: bytes, expected_columns: tuple[str, ...]) -> tuple[str, str]:
    decoded_headers: list[tuple[str, tuple[str, ...]]] = []
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            header = next(csv.reader(io.StringIO(text, newline="")))
        except StopIteration as exc:
            raise MunicipalTopologyError("CSV is empty") from exc
        decoded_headers.append((encoding, tuple(header)))
        if tuple(header) == expected_columns:
            return text, encoding
    if decoded_headers:
        actual = ",".join(decoded_headers[0][1])[:240]
        raise MunicipalTopologyError(f"CSV header does not match the selected profile: {actual}")
    raise MunicipalTopologyError("CSV must be encoded as UTF-8 or CP949")


def _read_rows(
    data: bytes,
    *,
    profile: MunicipalProfile,
    max_rows: int,
    max_routes: int,
    max_route_stops: int,
) -> tuple[list[dict[str, Any]], str]:
    text, encoding = _decode_csv(data, profile.columns)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    routes: dict[str, dict[str, Any]] = {}
    row_count = 0
    for row_number, row in enumerate(reader, start=2):
        row_count += 1
        if row_count > max_rows:
            raise CatalogLimitError(f"CSV exceeds the {max_rows} row limit")
        if None in row or tuple(row.keys()) != profile.columns:
            raise MunicipalTopologyError(f"row {row_number} does not match the exact schema")
        route_no = _bounded_text(row["노선번호"], f"row {row_number} 노선번호", required=True, maximum=80)
        route_id = _identifier(row["노선"], f"row {row_number} 노선")
        node_id = _identifier(row["정류장"], f"row {row_number} 정류장")
        node_name = _bounded_text(row["정류장명"], f"row {row_number} 정류장명", required=True, maximum=160)
        route_date = _iso_date(row["데이터기준일"], f"row {row_number} 데이터기준일")
        try:
            node_order = int(_bounded_text(row["정류장순서"], f"row {row_number} 정류장순서", required=True, maximum=12))
        except ValueError as exc:
            raise MunicipalTopologyError(f"row {row_number} 정류장순서 is not an integer") from exc
        if node_order < 1:
            raise MunicipalTopologyError(f"row {row_number} 정류장순서 must be positive")
        longitude = _coordinate(row["경도"], f"row {row_number} 경도", 124.0, 132.0)
        latitude = _coordinate(row["위도"], f"row {row_number} 위도", 33.0, 39.5)

        route = routes.setdefault(
            route_id,
            {"route_numbers": set(), "route_date": route_date, "stops": []},
        )
        route["route_numbers"].add(route_no)
        if len(route["route_numbers"]) > 20:
            raise MunicipalTopologyError(f"route {route_id} has too many route labels")
        if route["route_date"] != route_date:
            raise MunicipalTopologyError(f"route {route_id} has conflicting data dates")
        route["stops"].append(
            {
                "node_id": node_id,
                "node_name": node_name,
                "node_order": node_order,
                "latitude": latitude,
                "longitude": longitude,
                "direction": "",
            }
        )
        if len(route["stops"]) > max_route_stops:
            raise CatalogLimitError(
                f"route {route_id} exceeds the {max_route_stops} stop limit"
            )
        if len(routes) > max_routes:
            raise CatalogLimitError(f"CSV exceeds the {max_routes} route limit")

    if row_count == 0:
        raise MunicipalTopologyError("CSV contains no route-stop rows")
    normalized: list[dict[str, Any]] = []
    for route_id in sorted(routes):
        route = routes[route_id]
        stops = sorted(route["stops"], key=lambda item: item["node_order"])
        orders = [item["node_order"] for item in stops]
        if len(stops) < 2:
            raise MunicipalTopologyError(f"route {route_id} must contain at least two stops")
        if len(orders) != len(set(orders)):
            raise MunicipalTopologyError(f"route {route_id} contains duplicate stop order values")
        if orders != list(range(1, len(orders) + 1)):
            raise MunicipalTopologyError(
                f"route {route_id} stop order must be complete and contiguous from 1"
            )
        normalized.append(
            {
                "route_id": route_id,
                "route_numbers": sorted(route["route_numbers"]),
                "route_date": route["route_date"],
                "stops": stops,
            }
        )
    return normalized, encoding


def _provenance(
    *,
    profile: MunicipalProfile,
    source_url: str,
    download_url: str,
    source_date: str,
    file_sha256: str,
    route_numbers: list[str],
    route_date: str,
) -> str:
    value = json.dumps(
        {
            "dataset": profile.dataset_name,
            "download": download_url,
            "file_sha256": file_sha256,
            "kind": "OFFICIAL_MUNICIPAL_ROUTE_STOP_CSV",
            "page": source_url,
            "route_date": route_date,
            "route_numbers": route_numbers,
            "source_date": source_date,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(value) > 512:
        raise MunicipalTopologyError("source provenance exceeds the catalog bound")
    return value


def import_municipal_topology_csv(
    *,
    catalog: NetworkCatalog,
    data: bytes,
    profile_name: str,
    city_code: str | None = None,
    source_url: str | None = None,
    download_url: str | None = None,
    source_date: str,
    expected_sha256: str | None = None,
    max_csv_bytes: int = DEFAULT_MAX_CSV_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_routes: int = DEFAULT_MAX_ROUTES,
    max_route_stops: int = DEFAULT_MAX_ROUTE_STOPS,
) -> dict[str, Any]:
    if profile_name not in PROFILES:
        raise MunicipalTopologyError(f"unsupported profile: {profile_name}")
    profile = PROFILES[profile_name]
    if not 1 <= len(data) <= max_csv_bytes:
        raise CatalogLimitError(f"CSV must contain 1..{max_csv_bytes} bytes")
    dataset_date = _iso_date(source_date, "source_date")
    city = _identifier(city_code or profile.default_city_code, "city_code")
    page = _bounded_text(source_url or profile.official_page_url, "source_url", required=True, maximum=2048)
    download = _bounded_text(download_url or profile.official_download_url, "download_url", required=True, maximum=2048)
    if not page.startswith("https://") or not download.startswith("https://"):
        raise MunicipalTopologyError("official source URLs must use HTTPS")
    digest = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None:
        expected = _bounded_text(expected_sha256, "expected_sha256", required=True, maximum=64)
        if not _SHA256.fullmatch(expected):
            raise MunicipalTopologyError("expected_sha256 must be 64 hexadecimal characters")
        if digest != expected.lower():
            raise MunicipalTopologyError("CSV SHA-256 does not match expected_sha256")

    routes, encoding = _read_rows(
        data,
        profile=profile,
        max_rows=max_rows,
        max_routes=max_routes,
        max_route_stops=max_route_stops,
    )
    sequences = [
        {
            "city_code": city,
            "route_id": route["route_id"],
            "ordered_stops": route["stops"],
            "source": _provenance(
                profile=profile,
                source_url=page,
                download_url=download,
                source_date=dataset_date,
                file_sha256=digest,
                route_numbers=route["route_numbers"],
                route_date=route["route_date"],
            ),
            "captured_at": f"{route['route_date']}T00:00:00Z",
        }
        for route in routes
    ]
    batch = catalog.hydrate_route_sequences_batch(sequences)
    unique_stops = {
        stop["node_id"] for route in routes for stop in route["stops"]
    }
    route_dates = sorted({route["route_date"] for route in routes})
    return {
        "ok": True,
        "profile": profile.name,
        "dataset": profile.dataset_name,
        "city_code": city,
        "city_name": profile.city_name,
        "encoding": encoding,
        "file_sha256": digest,
        "source_date": dataset_date,
        "route_data_dates": route_dates,
        "row_count": sum(len(route["stops"]) for route in routes),
        "route_count": len(routes),
        "route_label_conflicts": sum(
            1 for route in routes if len(route["route_numbers"]) > 1
        ),
        "unique_stop_count": len(unique_stops),
        "created": batch["created"],
        "activated": batch["activated"],
        "unchanged": len(routes) - batch["activated"],
        "revision": batch["revision"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import a bounded official municipal route-stop CSV into a catalog copy"
    )
    parser.add_argument("--catalog-db", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    parser.add_argument("--city-code")
    parser.add_argument("--source-url")
    parser.add_argument("--download-url")
    parser.add_argument("--source-date", required=True)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--max-csv-bytes", type=int, default=DEFAULT_MAX_CSV_BYTES)
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    parser.add_argument("--max-routes", type=int, default=DEFAULT_MAX_ROUTES)
    parser.add_argument("--max-route-stops", type=int, default=DEFAULT_MAX_ROUTE_STOPS)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        data = args.csv.read_bytes()
        result = import_municipal_topology_csv(
            catalog=NetworkCatalog(args.catalog_db),
            data=data,
            profile_name=args.profile,
            city_code=args.city_code,
            source_url=args.source_url,
            download_url=args.download_url,
            source_date=args.source_date,
            expected_sha256=args.expected_sha256,
            max_csv_bytes=args.max_csv_bytes,
            max_rows=args.max_rows,
            max_routes=args.max_routes,
            max_route_stops=args.max_route_stops,
        )
    except (OSError, CatalogError, MunicipalTopologyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "CHUNCHEON_COLUMNS",
    "MunicipalTopologyError",
    "PROFILES",
    "import_municipal_topology_csv",
]
