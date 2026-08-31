"""Strict offline importer for the official Seoul route-stop XLSX snapshot.

Validation is the default CLI mode.  SQLite is opened only when ``--apply``
and ``--catalog-db`` are both explicit, after the complete workbook has passed
schema, provenance, cardinality, order, identifier, and coordinate checks.
Published order gaps are retained exactly; no stop or order is synthesized.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys
from typing import Any, Iterable
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from network_catalog import CatalogError, CatalogLimitError, NetworkCatalog


SEOUL_COLUMNS = (
    "ROUTE_ID",
    "노선명",
    "순번",
    "NODE_ID",
    "ARS_ID",
    "정류소명",
    "X좌표",
    "Y좌표",
)
SEOUL_CITY_CODE = "11"
DEFAULT_MAX_XLSX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_MEMBERS = 128
DEFAULT_MAX_ROWS = 50_000
DEFAULT_MAX_ROUTES = 1_000
DEFAULT_MAX_ROUTE_STOPS = 500
DEFAULT_MAX_SHARED_STRINGS = 100_000
DEFAULT_MAX_SHARED_STRING_CHARS = 20_000_000
MAX_CELL_CHARS = 512

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_M = "{" + _MAIN_NS + "}"
_R = "{" + _REL_NS + "}"
_PR = "{" + _PACKAGE_REL_NS + "}"
_CELL_REF = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
_DIGITS = re.compile(r"^[0-9]+$")


class SeoulTopologyError(ValueError):
    """Unsafe, incomplete, altered, or unsupported Seoul workbook."""


@dataclass(frozen=True, slots=True)
class SeoulWorkbookProfile:
    dataset_name: str
    official_page_url: str
    source_date: str
    published_date: str
    expected_sha256: str
    expected_file_bytes: int
    expected_rows: int
    expected_routes: int
    expected_unique_stops: int
    expected_non_contiguous_routes: int
    sheet_name: str = "Data"


SEOUL_PROFILE = SeoulWorkbookProfile(
    dataset_name="서울시 버스노선별 정류장 정보",
    official_page_url="https://data.seoul.go.kr/dataList/OA-1095/F/1/datasetView.do",
    source_date="2026-08-04",
    published_date="2026-08-05",
    expected_sha256=(
        "97066F015032DF4635174E49B8CBE1E40C679AAD98C48BDCB4CEF15F54B4937D"
    ),
    expected_file_bytes=3_251_800,
    expected_rows=41_676,
    expected_routes=718,
    expected_unique_stops=12_898,
    expected_non_contiguous_routes=10,
)


@dataclass(frozen=True, slots=True)
class ParsedSeoulTopology:
    routes: tuple[dict[str, Any], ...]
    file_sha256: str
    row_count: int
    route_count: int
    unique_stop_count: int
    non_contiguous_route_count: int

    def summary(
        self, profile: SeoulWorkbookProfile = SEOUL_PROFILE
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "validate_only",
            "dataset": profile.dataset_name,
            "city_code": SEOUL_CITY_CODE,
            "source_date": profile.source_date,
            "published_date": profile.published_date,
            "file_sha256": self.file_sha256,
            "row_count": self.row_count,
            "route_count": self.route_count,
            "unique_stop_count": self.unique_stop_count,
            "non_contiguous_route_count": self.non_contiguous_route_count,
            "published_order_gaps_preserved": True,
        }


def _bounded_text(
    value: Any,
    field: str,
    *,
    maximum: int = MAX_CELL_CHARS,
    required: bool = True,
) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise SeoulTopologyError(f"{field} is required")
    if len(text) > maximum:
        raise SeoulTopologyError(f"{field} exceeds {maximum} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise SeoulTopologyError(f"{field} contains control characters")
    return text


def _iso_date(value: str, field: str) -> str:
    text = _bounded_text(value, field, maximum=10)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise SeoulTopologyError(f"{field} must be YYYY-MM-DD") from exc


def _digits(value: str, field: str, *, maximum: int = 20) -> str:
    text = _bounded_text(value, field, maximum=maximum)
    if not _DIGITS.fullmatch(text):
        raise SeoulTopologyError(f"{field} must be published integer digits")
    return text


def _positive_order(value: str, field: str) -> int:
    text = _digits(value, field, maximum=6)
    parsed = int(text)
    if parsed < 1:
        raise SeoulTopologyError(f"{field} must be positive")
    return parsed


def _coordinate(value: str, field: str, minimum: float, maximum: float) -> float:
    text = _bounded_text(value, field, maximum=32)
    try:
        parsed = float(text)
    except ValueError as exc:
        raise SeoulTopologyError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise SeoulTopologyError(f"{field} is outside Republic of Korea bounds")
    return parsed


def _safe_member_name(name: str) -> str:
    clean = name.replace("\\", "/")
    parts = PurePosixPath(clean).parts
    if (
        not clean
        or clean.startswith("/")
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
    ):
        raise SeoulTopologyError("XLSX contains an unsafe archive member")
    return clean


def _validate_archive(
    archive: ZipFile,
    *,
    max_members: int,
    max_uncompressed_bytes: int,
) -> None:
    infos = archive.infolist()
    if not 1 <= len(infos) <= max_members:
        raise CatalogLimitError(f"XLSX must contain 1..{max_members} archive members")
    total = 0
    seen: set[str] = set()
    for info in infos:
        name = _safe_member_name(info.filename)
        folded = name.casefold()
        if folded in seen:
            raise SeoulTopologyError("XLSX contains duplicate archive members")
        seen.add(folded)
        if info.flag_bits & 0x1:
            raise SeoulTopologyError("encrypted XLSX members are not supported")
        total += int(info.file_size)
        if total > max_uncompressed_bytes:
            raise CatalogLimitError(
                f"XLSX expands beyond {max_uncompressed_bytes} bytes"
            )


def _xml_root(archive: ZipFile, member: str) -> ET.Element:
    try:
        return ET.fromstring(archive.read(member))
    except KeyError as exc:
        raise SeoulTopologyError(f"XLSX is missing {member}") from exc
    except ET.ParseError as exc:
        raise SeoulTopologyError(f"XLSX contains invalid XML in {member}") from exc


def _sheet_member(archive: ZipFile, profile: SeoulWorkbookProfile) -> str:
    workbook = _xml_root(archive, "xl/workbook.xml")
    relationships = _xml_root(archive, "xl/_rels/workbook.xml.rels")
    relation_targets: dict[str, str] = {}
    for relation in relationships.findall(_PR + "Relationship"):
        if relation.attrib.get("TargetMode") == "External":
            raise SeoulTopologyError("external workbook relationships are forbidden")
        relation_targets[str(relation.attrib.get("Id") or "")] = str(
            relation.attrib.get("Target") or ""
        )
    sheets = workbook.find(_M + "sheets")
    rows = [] if sheets is None else list(sheets.findall(_M + "sheet"))
    if len(rows) != 1 or rows[0].attrib.get("name") != profile.sheet_name:
        raise SeoulTopologyError(
            f"XLSX must contain exactly one {profile.sheet_name!r} worksheet"
        )
    relationship_id = rows[0].attrib.get(_R + "id") or ""
    target = relation_targets.get(relationship_id)
    if not target:
        raise SeoulTopologyError("worksheet relationship is missing")
    if target.startswith("/"):
        member = target.lstrip("/")
    else:
        member = posixpath.normpath(posixpath.join("xl", target))
    member = _safe_member_name(member)
    if not member.startswith("xl/worksheets/") or member not in archive.namelist():
        raise SeoulTopologyError("worksheet relationship target is invalid")
    return member


def _shared_strings(
    archive: ZipFile,
    *,
    max_strings: int,
    max_chars: int,
) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml_root(archive, "xl/sharedStrings.xml")
    values: list[str] = []
    total_chars = 0
    for node in root.findall(_M + "si"):
        if len(values) >= max_strings:
            raise CatalogLimitError(f"XLSX exceeds {max_strings} shared strings")
        value = "".join(part.text or "" for part in node.iter(_M + "t"))
        total_chars += len(value)
        if total_chars > max_chars:
            raise CatalogLimitError(
                f"XLSX shared strings exceed {max_chars} characters"
            )
        values.append(value)
    return values


def _column_index(label: str) -> int:
    result = 0
    for char in label:
        result = result * 26 + ord(char) - ord("A") + 1
    return result


def _cell_value(cell: ET.Element, shared_strings: list[str], field: str) -> str:
    if cell.find(_M + "f") is not None:
        raise SeoulTopologyError(f"{field} formulas are forbidden")
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(part.text or "" for part in cell.iter(_M + "t"))
    value_node = cell.find(_M + "v")
    value = "" if value_node is None else value_node.text or ""
    if kind == "s":
        try:
            return shared_strings[int(value)]
        except (ValueError, IndexError) as exc:
            raise SeoulTopologyError(f"{field} has an invalid shared-string index") from exc
    if kind not in {None, "str"}:
        raise SeoulTopologyError(f"{field} uses an unsupported XLSX cell type")
    return value


def _worksheet_rows(
    archive: ZipFile,
    member: str,
    shared_strings: list[str],
    *,
    max_rows: int,
) -> Iterable[tuple[int, tuple[str, ...]]]:
    try:
        stream = archive.open(member)
    except KeyError as exc:
        raise SeoulTopologyError("worksheet XML is missing") from exc
    observed = 0
    try:
        for _, element in ET.iterparse(stream, events=("end",)):
            if element.tag != _M + "row":
                continue
            observed += 1
            if observed > max_rows + 1:
                raise CatalogLimitError(f"XLSX exceeds the {max_rows} data-row limit")
            try:
                row_number = int(element.attrib.get("r") or "")
            except ValueError as exc:
                raise SeoulTopologyError("worksheet row number is invalid") from exc
            if row_number != observed:
                raise SeoulTopologyError("worksheet rows must be sequential without hidden gaps")
            values: dict[int, str] = {}
            for cell in element.findall(_M + "c"):
                match = _CELL_REF.fullmatch(cell.attrib.get("r") or "")
                if match is None or int(match.group(2)) != row_number:
                    raise SeoulTopologyError(f"row {row_number} has an invalid cell reference")
                column = _column_index(match.group(1))
                if not 1 <= column <= len(SEOUL_COLUMNS) or column in values:
                    raise SeoulTopologyError(f"row {row_number} has an unexpected cell")
                values[column] = _cell_value(
                    cell, shared_strings, f"row {row_number} column {column}"
                )
            if set(values) != set(range(1, len(SEOUL_COLUMNS) + 1)):
                raise SeoulTopologyError(f"row {row_number} does not contain exactly 8 cells")
            yield row_number, tuple(values[index] for index in range(1, 9))
            element.clear()
    except ET.ParseError as exc:
        raise SeoulTopologyError("worksheet XML is invalid") from exc
    finally:
        stream.close()


def parse_seoul_topology_xlsx(
    data: bytes,
    *,
    profile: SeoulWorkbookProfile = SEOUL_PROFILE,
    max_xlsx_bytes: int = DEFAULT_MAX_XLSX_BYTES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_archive_members: int = DEFAULT_MAX_ARCHIVE_MEMBERS,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_routes: int = DEFAULT_MAX_ROUTES,
    max_route_stops: int = DEFAULT_MAX_ROUTE_STOPS,
) -> ParsedSeoulTopology:
    if not 1 <= len(data) <= max_xlsx_bytes:
        raise CatalogLimitError(f"XLSX must contain 1..{max_xlsx_bytes} bytes")
    if len(data) != profile.expected_file_bytes:
        raise SeoulTopologyError("XLSX byte size does not match the official snapshot")
    source_date = _iso_date(profile.source_date, "source_date")
    _iso_date(profile.published_date, "published_date")
    digest = hashlib.sha256(data).hexdigest().upper()
    if digest != profile.expected_sha256.upper():
        raise SeoulTopologyError("XLSX SHA-256 does not match the official snapshot")

    try:
        archive = ZipFile(io.BytesIO(data))
    except BadZipFile as exc:
        raise SeoulTopologyError("XLSX is not a valid ZIP workbook") from exc
    with archive:
        _validate_archive(
            archive,
            max_members=max_archive_members,
            max_uncompressed_bytes=max_uncompressed_bytes,
        )
        sheet_member = _sheet_member(archive, profile)
        strings = _shared_strings(
            archive,
            max_strings=DEFAULT_MAX_SHARED_STRINGS,
            max_chars=DEFAULT_MAX_SHARED_STRING_CHARS,
        )
        rows = _worksheet_rows(
            archive,
            sheet_member,
            strings,
            max_rows=max_rows,
        )
        try:
            _, header = next(iter(rows))
        except StopIteration as exc:
            raise SeoulTopologyError("XLSX worksheet is empty") from exc
        if header != SEOUL_COLUMNS:
            raise SeoulTopologyError("XLSX header does not match the exact Seoul schema")

        routes: dict[str, dict[str, Any]] = {}
        stops: dict[str, tuple[str, float, float]] = {}
        row_count = 0
        for row_number, values in rows:
            row_count += 1
            if row_count > max_rows:
                raise CatalogLimitError(f"XLSX exceeds the {max_rows} data-row limit")
            route_id = _digits(values[0], f"row {row_number} ROUTE_ID")
            route_name = _bounded_text(
                values[1], f"row {row_number} 노선명", maximum=80
            )
            node_order = _positive_order(values[2], f"row {row_number} 순번")
            node_id = _digits(values[3], f"row {row_number} NODE_ID")
            _digits(values[4], f"row {row_number} ARS_ID", maximum=10)
            node_name = _bounded_text(
                values[5], f"row {row_number} 정류소명", maximum=160
            )
            longitude = _coordinate(
                values[6], f"row {row_number} X좌표", 124.0, 132.0
            )
            latitude = _coordinate(
                values[7], f"row {row_number} Y좌표", 33.0, 39.5
            )

            existing_stop = stops.setdefault(
                node_id, (node_name, latitude, longitude)
            )
            if existing_stop != (node_name, latitude, longitude):
                raise SeoulTopologyError(
                    f"NODE_ID {node_id} has conflicting names or coordinates"
                )
            route = routes.setdefault(
                route_id,
                {
                    "route_name": route_name,
                    "last_order": None,
                    "stops": [],
                },
            )
            if route["route_name"] != route_name:
                raise SeoulTopologyError(f"ROUTE_ID {route_id} has conflicting names")
            previous_order = route["last_order"]
            if previous_order is not None and node_order <= previous_order:
                raise SeoulTopologyError(
                    f"ROUTE_ID {route_id} order must be unique and increasing"
                )
            route["last_order"] = node_order
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
                    f"ROUTE_ID {route_id} exceeds {max_route_stops} stops"
                )
            if len(routes) > max_routes:
                raise CatalogLimitError(f"XLSX exceeds the {max_routes} route limit")

    if row_count != profile.expected_rows:
        raise SeoulTopologyError("XLSX route-stop row count does not match the snapshot")
    if len(routes) != profile.expected_routes:
        raise SeoulTopologyError("XLSX route count does not match the snapshot")
    if len(stops) != profile.expected_unique_stops:
        raise SeoulTopologyError("XLSX unique-stop count does not match the snapshot")

    normalized: list[dict[str, Any]] = []
    non_contiguous = 0
    for route_id in sorted(routes):
        route = routes[route_id]
        route_stops = route["stops"]
        if len(route_stops) < 2:
            raise SeoulTopologyError(f"ROUTE_ID {route_id} must contain at least two stops")
        orders = [int(item["node_order"]) for item in route_stops]
        if orders != list(range(1, len(orders) + 1)):
            non_contiguous += 1
        normalized.append(
            {
                "route_id": route_id,
                "route_name": route["route_name"],
                "stops": route_stops,
            }
        )
    if non_contiguous != profile.expected_non_contiguous_routes:
        raise SeoulTopologyError(
            "XLSX non-contiguous route-order count does not match the snapshot"
        )
    return ParsedSeoulTopology(
        routes=tuple(normalized),
        file_sha256=digest,
        row_count=row_count,
        route_count=len(normalized),
        unique_stop_count=len(stops),
        non_contiguous_route_count=non_contiguous,
    )


def _route_provenance(
    profile: SeoulWorkbookProfile,
    *,
    file_sha256: str,
    route_name: str,
) -> str:
    value = json.dumps(
        {
            "dataset": profile.dataset_name,
            "file_sha256": file_sha256,
            "kind": "OFFICIAL_SEOUL_ROUTE_STOP_XLSX",
            "page": profile.official_page_url,
            "published_date": profile.published_date,
            "route_name": route_name,
            "source_date": profile.source_date,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(value) > 512:
        raise SeoulTopologyError("route provenance exceeds the catalog bound")
    return value


def import_seoul_topology_xlsx(
    *,
    catalog: NetworkCatalog,
    data: bytes,
    profile: SeoulWorkbookProfile = SEOUL_PROFILE,
    **limits: Any,
) -> dict[str, Any]:
    parsed = parse_seoul_topology_xlsx(data, profile=profile, **limits)
    sequences = [
        {
            "city_code": SEOUL_CITY_CODE,
            "route_id": route["route_id"],
            "ordered_stops": route["stops"],
            "source": _route_provenance(
                profile,
                file_sha256=parsed.file_sha256,
                route_name=route["route_name"],
            ),
            "captured_at": f"{profile.source_date}T00:00:00Z",
        }
        for route in parsed.routes
    ]
    batch = catalog.hydrate_route_sequences_batch(sequences)
    return {
        **parsed.summary(profile),
        "mode": "import",
        "created": batch["created"],
        "activated": batch["activated"],
        "unchanged": parsed.route_count - batch["activated"],
        "revision": batch["revision"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or explicitly import the official Seoul route-stop XLSX"
    )
    parser.add_argument("--xlsx", type=Path, required=True)
    parser.add_argument("--catalog-db", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="activate all validated routes atomically in --catalog-db",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if args.apply != (args.catalog_db is not None):
        print(
            json.dumps(
                {"ok": False, "error": "--apply and --catalog-db must be supplied together"},
                ensure_ascii=False,
            )
        )
        return 2
    try:
        data = args.xlsx.read_bytes()
        if args.apply:
            result = import_seoul_topology_xlsx(
                catalog=NetworkCatalog(args.catalog_db),
                data=data,
            )
        else:
            result = parse_seoul_topology_xlsx(data).summary()
    except (OSError, CatalogError, SeoulTopologyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "SEOUL_COLUMNS",
    "SEOUL_PROFILE",
    "SeoulTopologyError",
    "SeoulWorkbookProfile",
    "import_seoul_topology_xlsx",
    "parse_seoul_topology_xlsx",
]
