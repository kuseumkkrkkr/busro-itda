"""Strict importer for Incheon's official route-stop topology CSV.

The route file deliberately stays in its municipal identifier namespace.  It
never manufactures TAGO ``ICB`` identifiers.  Coordinates are optional and
are joined from the official nationwide stop catalog only through published
fields (mobile stop number and, when needed, exact stop name).
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping

from network_catalog import CatalogError, CatalogLimitError, NetworkCatalog, STOP_COLUMNS
from route_topology_anomalies import single_point_route_spike


INCHEON_COLUMNS = (
    "기준일자",
    "회사명",
    "회사아이디",
    "노선번호",
    "노선아이디",
    "순번",
    "정류소명",
    "정류소번호",
    "아이에스씨 아이디",
    "정류소구간거리",
    "정류소간누적거리",
    "주요경유지여부",
    "상_하행",
)
OFFICIAL_PAGE_URL = "https://www.data.go.kr/data/15048265/fileData.do"
OFFICIAL_DOWNLOAD_URL = (
    "https://www.data.go.kr/cmm/cmm/fileDownload.do?"
    "atchFileId=FILE_000000003646050&fileDetailSn=1&insertDataPrcus=N"
)
STOP_CATALOG_PAGE_URL = "https://www.data.go.kr/data/15067528/fileData.do"
DEFAULT_CITY_CODE = "23"
DEFAULT_MAX_ROUTE_CSV_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_STOP_CSV_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_ROUTE_ROWS = 100_000
DEFAULT_MAX_STOP_ROWS = 300_000
DEFAULT_MAX_ROUTES = 2_000
MAX_CELL_CHARS = 512
_IDENTIFIER = re.compile(r"^[0-9A-Za-z_.:-]{1,96}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_DIRECTIONS = frozenset({"상행", "하행", "순환", ""})
_MAIN_STOP_FLAGS = frozenset({"Y", "N", ""})


class IncheonTopologyError(ValueError):
    """Unsafe, inconsistent, or unsupported official input."""


@dataclass(frozen=True, slots=True)
class PreparedIncheonTopology:
    candidates: tuple[dict[str, Any], ...]
    summary: dict[str, Any]


def _bounded_text(
    value: Any,
    field: str,
    *,
    required: bool = False,
    maximum: int = MAX_CELL_CHARS,
) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise IncheonTopologyError(f"{field} is required")
    if len(text) > maximum:
        raise IncheonTopologyError(f"{field} exceeds {maximum} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise IncheonTopologyError(f"{field} contains control characters")
    return text


def _identifier(value: Any, field: str) -> str:
    text = _bounded_text(value, field, required=True, maximum=96)
    if not _IDENTIFIER.fullmatch(text):
        raise IncheonTopologyError(f"{field} has an invalid identifier")
    return text


def _iso_date(value: Any, field: str) -> str:
    text = _bounded_text(value, field, required=True, maximum=10)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise IncheonTopologyError(f"{field} must be YYYY-MM-DD") from exc


def _positive_int(value: Any, field: str) -> int:
    text = _bounded_text(value, field, required=True, maximum=12)
    try:
        number = int(text)
    except ValueError as exc:
        raise IncheonTopologyError(f"{field} is not an integer") from exc
    if number < 1:
        raise IncheonTopologyError(f"{field} must be positive")
    return number


def _optional_nonnegative_int(value: Any, field: str) -> int | None:
    text = _bounded_text(value, field, maximum=16)
    if not text:
        return None
    try:
        number = int(text)
    except ValueError as exc:
        raise IncheonTopologyError(f"{field} is not an integer") from exc
    if number < 0:
        raise IncheonTopologyError(f"{field} must not be negative")
    return number


def _decode_csv(
    data: bytes,
    expected_columns: tuple[str, ...],
    label: str,
) -> tuple[str, str]:
    decoded_headers: list[tuple[str, tuple[str, ...]]] = []
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        try:
            header = tuple(next(csv.reader(io.StringIO(text, newline=""))))
        except StopIteration as exc:
            raise IncheonTopologyError(f"{label} CSV is empty") from exc
        decoded_headers.append((encoding, header))
        if header == expected_columns:
            return text, encoding
    if decoded_headers:
        actual = ",".join(decoded_headers[0][1])[:240]
        raise IncheonTopologyError(f"{label} CSV header mismatch: {actual}")
    raise IncheonTopologyError(f"{label} CSV must be UTF-8 or CP949")


def _verified_digest(data: bytes, expected: str | None, label: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    if expected is None:
        return digest
    value = _bounded_text(expected, f"expected_{label}_sha256", required=True, maximum=64)
    if not _SHA256.fullmatch(value):
        raise IncheonTopologyError(
            f"expected_{label}_sha256 must be 64 hexadecimal characters"
        )
    if digest != value.lower():
        raise IncheonTopologyError(f"{label} CSV SHA-256 does not match")
    return digest


def _coordinate(value: Any, field: str, minimum: float, maximum: float) -> float:
    text = _bounded_text(value, field, required=True, maximum=32)
    try:
        number = float(text)
    except ValueError as exc:
        raise IncheonTopologyError(f"{field} is not numeric") from exc
    if not minimum <= number <= maximum:
        raise IncheonTopologyError(f"{field} is outside Republic of Korea bounds")
    return number


def _read_stop_catalog(
    data: bytes,
    *,
    max_rows: int,
) -> tuple[
    dict[str, list[dict[str, str]]],
    dict[str, list[dict[str, str]]],
    dict[str, Any],
]:
    text, encoding = _decode_csv(data, STOP_COLUMNS, "stop catalog")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    incheon_bis: dict[str, list[dict[str, str]]] = defaultdict(list)
    nationwide: dict[str, list[dict[str, str]]] = defaultdict(list)
    source_dates: Counter[str] = Counter()
    row_count = 0
    for row_number, row in enumerate(reader, start=2):
        row_count += 1
        if row_count > max_rows:
            raise CatalogLimitError(f"stop catalog exceeds the {max_rows} row limit")
        if None in row or tuple(row.keys()) != STOP_COLUMNS:
            raise IncheonTopologyError(
                f"stop catalog row {row_number} does not match the exact schema"
            )
        source_date = _bounded_text(
            row["정보수집일"], f"stop catalog row {row_number} 정보수집일", maximum=10
        )
        if source_date:
            source_dates[
                _iso_date(source_date, f"stop catalog row {row_number} 정보수집일")
            ] += 1
        mobile = _bounded_text(
            row["모바일단축번호"],
            f"stop catalog row {row_number} 모바일단축번호",
            maximum=32,
        )
        if not mobile:
            continue
        record = {
            "node_id": _identifier(
                row["정류장번호"], f"stop catalog row {row_number} 정류장번호"
            ),
            "node_name": _bounded_text(
                row["정류장명"],
                f"stop catalog row {row_number} 정류장명",
                required=True,
                maximum=160,
            ),
            "latitude": _bounded_text(
                row["위도"], f"stop catalog row {row_number} 위도", maximum=32
            ),
            "longitude": _bounded_text(
                row["경도"], f"stop catalog row {row_number} 경도", maximum=32
            ),
            "management_city": _bounded_text(
                row["관리도시명"],
                f"stop catalog row {row_number} 관리도시명",
                maximum=80,
            ),
        }
        nationwide[mobile].append(record)
        if record["management_city"] == "인천BIS":
            incheon_bis[mobile].append(record)
    if row_count == 0:
        raise IncheonTopologyError("stop catalog contains no rows")
    return incheon_bis, nationwide, {
        "encoding": encoding,
        "row_count": row_count,
        "source_dates": dict(sorted(source_dates.items())),
    }


def _select_coordinate(
    *,
    stop_number: str,
    stop_name: str,
    incheon_bis: Mapping[str, list[dict[str, str]]],
    nationwide: Mapping[str, list[dict[str, str]]],
) -> tuple[tuple[float, float] | None, str]:
    local = incheon_bis.get(stop_number, [])
    local_exact = [item for item in local if item["node_name"] == stop_name]
    selected: dict[str, str] | None = None
    match_kind = "UNRESOLVED"
    if len(local_exact) == 1:
        selected = local_exact[0]
        match_kind = "INCHEON_BIS_MOBILE_AND_EXACT_NAME"
    else:
        global_exact = [
            item
            for item in nationwide.get(stop_number, [])
            if item["node_name"] == stop_name
        ]
        if len(global_exact) == 1:
            selected = global_exact[0]
            match_kind = "NATIONWIDE_MOBILE_AND_EXACT_NAME"
    if selected is None:
        return None, match_kind
    latitude = _coordinate(selected["latitude"], "matched stop latitude", 33.0, 39.5)
    longitude = _coordinate(selected["longitude"], "matched stop longitude", 124.0, 132.0)
    return (latitude, longitude), match_kind


def _provenance(
    *,
    route_numbers: list[str],
    source_date: str,
    route_sha256: str,
    stop_sha256: str,
    source_url: str,
    stop_source_url: str,
) -> str:
    value = json.dumps(
        {
            "dataset": "인천광역시_버스노선별 정류장 현황",
            "file_sha256": route_sha256,
            "kind": "OFFICIAL_MUNICIPAL_ROUTE_STOP_CSV",
            "page": source_url,
            "route_numbers": route_numbers,
            "source_date": source_date,
            "stop_catalog_sha256": stop_sha256,
            "stop_page": stop_source_url,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(value) > 512:
        raise IncheonTopologyError("source provenance exceeds the catalog bound")
    return value


def prepare_incheon_topology(
    *,
    route_data: bytes,
    stop_data: bytes,
    source_date: str,
    expected_route_sha256: str | None = None,
    expected_stop_sha256: str | None = None,
    city_code: str = DEFAULT_CITY_CODE,
    source_url: str = OFFICIAL_PAGE_URL,
    download_url: str = OFFICIAL_DOWNLOAD_URL,
    stop_source_url: str = STOP_CATALOG_PAGE_URL,
    max_route_csv_bytes: int = DEFAULT_MAX_ROUTE_CSV_BYTES,
    max_stop_csv_bytes: int = DEFAULT_MAX_STOP_CSV_BYTES,
    max_route_rows: int = DEFAULT_MAX_ROUTE_ROWS,
    max_stop_rows: int = DEFAULT_MAX_STOP_ROWS,
    max_routes: int = DEFAULT_MAX_ROUTES,
) -> PreparedIncheonTopology:
    if not 1 <= len(route_data) <= max_route_csv_bytes:
        raise CatalogLimitError(
            f"route CSV must contain 1..{max_route_csv_bytes} bytes"
        )
    if not 1 <= len(stop_data) <= max_stop_csv_bytes:
        raise CatalogLimitError(
            f"stop CSV must contain 1..{max_stop_csv_bytes} bytes"
        )
    dated = _iso_date(source_date, "source_date")
    city = _identifier(city_code, "city_code")
    page = _bounded_text(source_url, "source_url", required=True, maximum=2048)
    download = _bounded_text(
        download_url, "download_url", required=True, maximum=2048
    )
    stop_page = _bounded_text(
        stop_source_url, "stop_source_url", required=True, maximum=2048
    )
    if not all(value.startswith("https://") for value in (page, download, stop_page)):
        raise IncheonTopologyError("official source URLs must use HTTPS")
    route_digest = _verified_digest(route_data, expected_route_sha256, "route")
    stop_digest = _verified_digest(stop_data, expected_stop_sha256, "stop")
    stop_by_mobile, all_stops_by_mobile, stop_summary = _read_stop_catalog(
        stop_data,
        max_rows=max_stop_rows,
    )
    route_text, route_encoding = _decode_csv(route_data, INCHEON_COLUMNS, "route")
    reader = csv.DictReader(io.StringIO(route_text, newline=""))
    routes: dict[str, dict[str, Any]] = {}
    source_direction_counts: Counter[str] = Counter()
    duplicate_rows = 0
    row_count = 0
    for row_number, row in enumerate(reader, start=2):
        row_count += 1
        if row_count > max_route_rows:
            raise CatalogLimitError(f"route CSV exceeds the {max_route_rows} row limit")
        if None in row or tuple(row.keys()) != INCHEON_COLUMNS:
            raise IncheonTopologyError(
                f"route row {row_number} does not match the exact schema"
            )
        row_date = _iso_date(row["기준일자"], f"route row {row_number} 기준일자")
        if row_date != dated:
            raise IncheonTopologyError(
                f"route row {row_number} 기준일자 conflicts with source_date"
            )
        company_id = _identifier(
            row["회사아이디"], f"route row {row_number} 회사아이디"
        )
        company_name = _bounded_text(
            row["회사명"],
            f"route row {row_number} 회사명",
            required=True,
            maximum=160,
        )
        route_no = _bounded_text(
            row["노선번호"],
            f"route row {row_number} 노선번호",
            required=True,
            maximum=80,
        )
        route_id = _identifier(
            row["노선아이디"], f"route row {row_number} 노선아이디"
        )
        source_order = _positive_int(row["순번"], f"route row {row_number} 순번")
        node_name = _bounded_text(
            row["정류소명"],
            f"route row {row_number} 정류소명",
            required=True,
            maximum=160,
        )
        stop_number = _identifier(
            row["정류소번호"], f"route row {row_number} 정류소번호"
        )
        node_id = _identifier(
            row["아이에스씨 아이디"],
            f"route row {row_number} 아이에스씨 아이디",
        )
        segment_distance = _optional_nonnegative_int(
            row["정류소구간거리"], f"route row {row_number} 정류소구간거리"
        )
        cumulative_distance = _optional_nonnegative_int(
            row["정류소간누적거리"],
            f"route row {row_number} 정류소간누적거리",
        )
        main_stop = _bounded_text(
            row["주요경유지여부"],
            f"route row {row_number} 주요경유지여부",
            maximum=1,
        )
        if main_stop not in _MAIN_STOP_FLAGS:
            raise IncheonTopologyError(
                f"route row {row_number} 주요경유지여부 is invalid"
            )
        direction = _bounded_text(
            row["상_하행"], f"route row {row_number} 상_하행", maximum=2
        )
        if direction not in _DIRECTIONS:
            raise IncheonTopologyError(f"route row {row_number} 상_하행 is invalid")
        source_direction_counts[direction or "BLANK"] += 1
        coordinates, coordinate_match = _select_coordinate(
            stop_number=stop_number,
            stop_name=node_name,
            incheon_bis=stop_by_mobile,
            nationwide=all_stops_by_mobile,
        )
        topology = (
            node_id,
            node_name,
            stop_number,
            segment_distance,
            cumulative_distance,
            main_stop,
            direction,
        )
        route = routes.setdefault(
            route_id,
            {
                "route_numbers": set(),
                "companies": set(),
                "stops": {},
            },
        )
        route["route_numbers"].add(route_no)
        route["companies"].add((company_id, company_name))
        existing = route["stops"].get(source_order)
        if existing is not None:
            if existing["topology"] != topology:
                raise IncheonTopologyError(
                    f"route {route_id} order {source_order} has conflicting topology"
                )
            duplicate_rows += 1
            continue
        stop: dict[str, Any] = {
            "node_id": node_id,
            "node_name": node_name,
            "node_order": source_order,
            "direction": direction,
        }
        if coordinates is not None:
            stop["latitude"], stop["longitude"] = coordinates
        route["stops"][source_order] = {
            "candidate": stop,
            "coordinate_match": coordinate_match,
            "topology": topology,
        }
        if len(routes) > max_routes:
            raise CatalogLimitError(f"route CSV exceeds the {max_routes} route limit")
    if row_count == 0:
        raise IncheonTopologyError("route CSV contains no rows")

    candidates: list[dict[str, Any]] = []
    coordinate_matches: Counter[str] = Counter()
    candidate_direction_counts: Counter[str] = Counter()
    unique_node_ids: set[str] = set()
    source_order_gap_routes = 0
    multi_company_routes = 0
    coordinate_spikes_suppressed = 0
    routes_with_suppressed_coordinate_spikes = 0
    for route_id in sorted(routes):
        route = routes[route_id]
        entries = [route["stops"][order] for order in sorted(route["stops"])]
        if len(entries) < 2:
            raise IncheonTopologyError(f"route {route_id} must contain at least two stops")
        orders = [entry["candidate"]["node_order"] for entry in entries]
        if orders != list(range(orders[0], orders[-1] + 1)):
            source_order_gap_routes += 1
        if len(route["companies"]) > 1:
            multi_company_routes += 1
        route_suppressed = 0
        while True:
            spike_index = next(
                (
                    index
                    for index in range(1, len(entries) - 1)
                    if single_point_route_spike(
                        entries[index - 1]["candidate"],
                        entries[index]["candidate"],
                        entries[index + 1]["candidate"],
                    )
                    is not None
                ),
                None,
            )
            if spike_index is None:
                break
            middle = entries[spike_index]
            middle["candidate"].pop("latitude", None)
            middle["candidate"].pop("longitude", None)
            middle["coordinate_match"] = "SUPPRESSED_ROUTE_SPIKE"
            route_suppressed += 1
        if route_suppressed:
            coordinate_spikes_suppressed += route_suppressed
            routes_with_suppressed_coordinate_spikes += 1
        for entry in entries:
            coordinate_matches[entry["coordinate_match"]] += 1
            direction = entry["candidate"]["direction"] or "BLANK"
            candidate_direction_counts[direction] += 1
            unique_node_ids.add(entry["candidate"]["node_id"])
        route_numbers = sorted(route["route_numbers"])
        candidates.append(
            {
                "city_code": city,
                "route_id": route_id,
                "ordered_stops": [entry["candidate"] for entry in entries],
                "source": _provenance(
                    route_numbers=route_numbers,
                    source_date=dated,
                    route_sha256=route_digest,
                    stop_sha256=stop_digest,
                    source_url=page,
                    stop_source_url=stop_page,
                ),
                "captured_at": f"{dated}T00:00:00Z",
            }
        )
    unresolved = (
        coordinate_matches["UNRESOLVED"]
        + coordinate_matches["SUPPRESSED_ROUTE_SPIKE"]
    )
    summary = {
        "ok": True,
        "mode": "VALIDATED",
        "dataset": "인천광역시_버스노선별 정류장 현황_20251231",
        "city_code": city,
        "source_date": dated,
        "source_url": page,
        "download_url": download,
        "route_encoding": route_encoding,
        "route_file_sha256": route_digest,
        "route_source_row_count": row_count,
        "candidate_route_count": len(candidates),
        "candidate_stop_count": sum(
            len(candidate["ordered_stops"]) for candidate in candidates
        ),
        "deduplicated_source_rows": duplicate_rows,
        "unique_published_stop_ids": len(unique_node_ids),
        "source_order_gap_routes": source_order_gap_routes,
        "multi_company_routes": multi_company_routes,
        "source_direction_counts": dict(sorted(source_direction_counts.items())),
        "candidate_direction_counts": dict(sorted(candidate_direction_counts.items())),
        "coordinate_match_counts": dict(sorted(coordinate_matches.items())),
        "coordinates_resolved": sum(coordinate_matches.values()) - unresolved,
        "coordinates_unresolved": unresolved,
        "coordinate_spikes_suppressed": coordinate_spikes_suppressed,
        "routes_with_suppressed_coordinate_spikes": routes_with_suppressed_coordinate_spikes,
        "stop_catalog_encoding": stop_summary["encoding"],
        "stop_catalog_file_sha256": stop_digest,
        "stop_catalog_row_count": stop_summary["row_count"],
        "stop_catalog_source_dates": stop_summary["source_dates"],
        "identifier_policy": "PUBLISHED_IDS_ONLY_NO_TAGO_PREFIX_INFERENCE",
    }
    return PreparedIncheonTopology(tuple(candidates), summary)


def import_incheon_topology(
    *,
    catalog: NetworkCatalog,
    route_data: bytes,
    stop_data: bytes,
    source_date: str,
    **options: Any,
) -> dict[str, Any]:
    prepared = prepare_incheon_topology(
        route_data=route_data,
        stop_data=stop_data,
        source_date=source_date,
        **options,
    )
    batch = catalog.hydrate_route_sequences_batch(
        prepared.candidates,
        activation_policy="preserve_newer",
    )
    return {
        **prepared.summary,
        "mode": "IMPORTED",
        "activation_policy": batch["activation_policy"],
        "created": batch["created"],
        "activated": batch["activated"],
        "skipped_older": batch["skipped_older"],
        "unchanged": len(prepared.candidates) - batch["activated"] - batch["skipped_older"],
        "revision": batch["revision"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or import the official Incheon route-stop topology CSV"
    )
    parser.add_argument("--route-csv", type=Path, required=True)
    parser.add_argument("--stop-csv", type=Path, required=True)
    parser.add_argument("--source-date", required=True)
    parser.add_argument("--expected-route-sha256")
    parser.add_argument("--expected-stop-sha256")
    parser.add_argument("--city-code", default=DEFAULT_CITY_CODE)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate-only", action="store_true")
    mode.add_argument("--catalog-db", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        route_data = args.route_csv.read_bytes()
        stop_data = args.stop_csv.read_bytes()
        options = {
            "route_data": route_data,
            "stop_data": stop_data,
            "source_date": args.source_date,
            "expected_route_sha256": args.expected_route_sha256,
            "expected_stop_sha256": args.expected_stop_sha256,
            "city_code": args.city_code,
        }
        if args.validate_only:
            result = prepare_incheon_topology(**options).summary
        else:
            result = import_incheon_topology(
                catalog=NetworkCatalog(args.catalog_db),
                **options,
            )
    except (OSError, CatalogError, IncheonTopologyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "INCHEON_COLUMNS",
    "IncheonTopologyError",
    "PreparedIncheonTopology",
    "import_incheon_topology",
    "prepare_incheon_topology",
]
