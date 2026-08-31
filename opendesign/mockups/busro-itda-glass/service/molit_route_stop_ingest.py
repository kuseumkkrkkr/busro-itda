"""Strict, resumable collector for MOLIT route-stop data.

The official response has no coordinates. Rows are therefore staged in a
separate SQLite database and are never activation candidates until every
``STTN_ID`` resolves exactly against the active official ``catalog_stops``
source in a read-only :class:`NetworkCatalog` database. Unresolved or
ambiguous identifiers are quarantined; names are never used as a fuzzy join.

Route-specific requests and ``rte_id``-omitted regional batches use separate
staging tables.  A regional page may contain several routes, so uniqueness is
checked on ``(RTE_ID, STTN_SEQ)`` and route metadata is checked across every
page before any activation candidate is exposed.

The CLI defaults to validation-only. Live access requires an explicit
``--probe`` or ``--collect`` mode and a key read from stdin/getpass. Service
keys are never accepted in argv, persisted, logged, or returned in summaries.
"""

from __future__ import annotations

import argparse
import csv
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import getpass
import hashlib
import io
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile

from network_catalog import (
    MAX_SEQUENCE_BATCH as MAX_NETWORK_CATALOG_SEQUENCE_BATCH,
    MAX_SEQUENCE_STOPS as MAX_NETWORK_CATALOG_SEQUENCE_STOPS,
    CatalogError,
    NetworkCatalog,
)


ENDPOINT = (
    "https://apis.data.go.kr/1613000/BusRoutespecificStopInformation/"
    "getBusRoutespecificStopInformation"
)
PROVIDER = "MOLIT_BUS_ROUTE_SPECIFIC_STOP"
MAX_PAGE_SIZE = 1_000
MAX_PAGES = 1_000
MAX_TOTAL_ROWS = 1_000_000
MAX_RESPONSE_BYTES = 8_000_000
MAX_RETRIES = 5
MAX_TIMEOUT_SECONDS = 30.0
MAX_RPS = 30.0
MAX_LEGAL_DONG_BYTES = 32_000_000
MAX_REQUEST_BUDGET = 100_000
RESPONSE_FIELDS = (
    "OPR_YMD",
    "RTE_ID",
    "RTE_NO",
    "RTE_NM",
    "STTN_SEQ",
    "STTN_ID",
    "STTN_NM",
    "CTPV_CD",
    "SGG_CD",
    "EMD_CD",
    "CTPV_NM",
    "SGG_NM",
    "EMD_NM",
    "TRFC_MNS_SE_CD",
)
_IDENTIFIER = re.compile(r"^[0-9A-Za-z가-힣_.:-]+$")
_RESULT_OK = frozenset({"0", "00", "NORMAL_SERVICE"})


class MolitIngestError(RuntimeError):
    """Base error for the independent MOLIT collector."""


class MolitValidationError(MolitIngestError):
    pass


class MolitProtocolError(MolitIngestError):
    pass


class MolitFatalUpstreamError(MolitProtocolError):
    pass


class MolitTransientUpstreamError(MolitProtocolError):
    """The gateway reached the provider but returned a retryable envelope."""


class MolitQuotaError(MolitFatalUpstreamError):
    pass


class MolitAuthenticationError(MolitFatalUpstreamError):
    pass


class MolitLimitError(MolitIngestError):
    pass


class MolitRequestBudgetExhausted(MolitLimitError):
    pass


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _bounded_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MolitValidationError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise MolitValidationError(f"{name} must be {minimum}..{maximum}")
    return parsed


def _bounded_float(value: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise MolitValidationError(f"{name} must be numeric") from exc
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise MolitValidationError(f"{name} must be {minimum}..{maximum}")
    return parsed


def _text(value: Any, name: str, maximum: int, *, required: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise MolitProtocolError(f"{name} is required")
    if len(text) > maximum:
        raise MolitProtocolError(f"{name} exceeds {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise MolitProtocolError(f"{name} contains control characters")
    return text


def _request_identifier(value: Any, name: str, maximum: int) -> str:
    text = "" if value is None else str(value)
    if not 1 <= len(text) <= maximum or not _IDENTIFIER.fullmatch(text):
        raise MolitValidationError(f"{name} is invalid")
    return text


def _operation_date(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[0-9]{8}", text):
        raise MolitValidationError("OPR_YMD must use YYYYMMDD")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise MolitValidationError("OPR_YMD is not a calendar date") from exc
    return text


def _operation_timestamp(value: Any) -> str:
    operation_date = _operation_date(value)
    return datetime.strptime(operation_date, "%Y%m%d").replace(tzinfo=UTC).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _bounded_provenance(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) > 512:
        raise MolitLimitError("route provenance exceeds NetworkCatalog limit")
    return encoded


def _route_provenance(
    *,
    opr_ymd: str,
    route_no: str,
    route_name: str,
    stops_source_id: str,
) -> str:
    return _bounded_provenance(
        {
            "opr_ymd": _operation_date(opr_ymd),
            "op": "MOLIT_15142031",
            "route_name": route_name,
            "route_no": route_no,
            "stops_source_hash": hashlib.sha256(
                stops_source_id.encode("utf-8")
            ).hexdigest()[:16],
        }
    )


def _casefold_mapping(value: Any, context: str) -> dict[str, Any]:
    """Normalize only letter casing; never guess aliases or separators."""

    if not isinstance(value, Mapping):
        raise MolitProtocolError(f"{context} must be an object")
    normalized: dict[str, Any] = {}
    original_names: dict[str, str] = {}
    for raw_name, item in value.items():
        if not isinstance(raw_name, str):
            raise MolitProtocolError(f"{context} field names must be strings")
        name = raw_name.casefold()
        if name in normalized:
            raise MolitProtocolError(
                f"{context} has ambiguous casing for {original_names[name]}"
            )
        normalized[name] = item
        original_names[name] = raw_name
    return normalized


def _required_field(normalized: Mapping[str, Any], name: str, context: str) -> Any:
    key = name.casefold()
    if key not in normalized:
        raise MolitProtocolError(f"{context} is missing {name}")
    return normalized[key]


@dataclass(frozen=True, slots=True)
class MolitRequest:
    opr_ymd: str
    rte_id: str | None
    ctpv_cd: str
    sgg_cd: str
    page_no: int = 1
    num_of_rows: int = MAX_PAGE_SIZE

    def __post_init__(self) -> None:
        object.__setattr__(self, "opr_ymd", _operation_date(self.opr_ymd))
        route = None if self.rte_id is None else self.rte_id
        object.__setattr__(
            self,
            "rte_id",
            None if route is None else _request_identifier(route, "RTE_ID", 96),
        )
        ctpv = str(self.ctpv_cd or "")
        sgg = str(self.sgg_cd or "")
        if not re.fullmatch(r"[0-9]{2}", ctpv):
            raise MolitValidationError("CTPV_CD must contain two digits")
        if not re.fullmatch(r"[0-9]{5}", sgg):
            raise MolitValidationError("SGG_CD must contain five digits")
        object.__setattr__(self, "ctpv_cd", ctpv)
        object.__setattr__(self, "sgg_cd", sgg)
        object.__setattr__(
            self,
            "page_no",
            _bounded_int(self.page_no, "pageNo", 1, MAX_PAGES),
        )
        object.__setattr__(
            self,
            "num_of_rows",
            _bounded_int(self.num_of_rows, "numOfRows", 1, MAX_PAGE_SIZE),
        )

    @property
    def target_key(self) -> str:
        if self.rte_id is None:
            canonical = json.dumps(
                [self.opr_ymd, self.ctpv_cd, self.sgg_cd],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            return "molit_region_" + hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest()[:24]
        canonical = json.dumps(
            [self.opr_ymd, self.rte_id, self.ctpv_cd, self.sgg_cd],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return "molit_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    def next_page(self, page_no: int) -> "MolitRequest":
        return MolitRequest(
            opr_ymd=self.opr_ymd,
            rte_id=self.rte_id,
            ctpv_cd=self.ctpv_cd,
            sgg_cd=self.sgg_cd,
            page_no=page_no,
            num_of_rows=self.num_of_rows,
        )

    def public_parameters(self) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "pageNo": self.page_no,
            "numOfRows": self.num_of_rows,
            "opr_ymd": self.opr_ymd,
            "ctpv_cd": self.ctpv_cd,
            "sgg_cd": self.sgg_cd,
            "dataType": "JSON",
        }
        if self.rte_id is not None:
            parameters["rte_id"] = self.rte_id
        return parameters

    @property
    def is_region_batch(self) -> bool:
        return self.rte_id is None


@dataclass(frozen=True, slots=True)
class RouteStopRow:
    opr_ymd: str
    rte_id: str
    rte_no: str
    rte_nm: str
    sttn_seq: int
    sttn_id: str
    sttn_nm: str
    ctpv_cd: str
    sgg_cd: str
    emd_cd: str
    ctpv_nm: str
    sgg_nm: str
    emd_nm: str
    trfc_mns_se_cd: str


@dataclass(frozen=True, slots=True)
class MolitRegionCode:
    legal_dong_code: str
    ctpv_cd: str
    sgg_cd: str
    name: str


def _read_bounded_legal_dong_file(path: Path) -> bytes:
    if not path.is_file():
        raise MolitValidationError("legal-dong code file does not exist")
    if path.suffix.casefold() == ".zip":
        try:
            with zipfile.ZipFile(path) as archive:
                entries = [entry for entry in archive.infolist() if not entry.is_dir()]
                if len(entries) != 1:
                    raise MolitValidationError(
                        "legal-dong ZIP must contain exactly one data file"
                    )
                entry = entries[0]
                if entry.flag_bits & 0x1:
                    raise MolitValidationError("encrypted legal-dong ZIP is not supported")
                if entry.file_size > MAX_LEGAL_DONG_BYTES:
                    raise MolitLimitError("legal-dong data exceeds byte limit")
                with archive.open(entry, "r") as source:
                    payload = source.read(MAX_LEGAL_DONG_BYTES + 1)
        except (OSError, zipfile.BadZipFile) as exc:
            raise MolitValidationError("legal-dong ZIP is invalid") from exc
    else:
        try:
            with path.open("rb") as source:
                payload = source.read(MAX_LEGAL_DONG_BYTES + 1)
        except OSError as exc:
            raise MolitValidationError("legal-dong code file could not be read") from exc
    if len(payload) > MAX_LEGAL_DONG_BYTES:
        raise MolitLimitError("legal-dong data exceeds byte limit")
    return payload


def load_active_sgg_codes(path: str | Path) -> tuple[MolitRegionCode, ...]:
    """Load current SGG rows from the official legal-dong ZIP/TSV.

    The official rule used here is structural and deterministic: a current
    10-digit row whose last five digits are zero is an SGG-level row; a row
    whose final eight digits are zero is a province-level row and is excluded.
    This retains single-tier Sejong (36110) without a name-based exception.
    """

    payload = _read_bounded_legal_dong_file(Path(path).resolve())
    decoded: str | None = None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            decoded = payload.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if decoded is None or "\x00" in decoded:
        raise MolitValidationError("legal-dong data is not valid UTF-8 or CP949 TSV")
    reader = csv.reader(io.StringIO(decoded), delimiter="\t")
    try:
        header = next(reader)
    except StopIteration as exc:
        raise MolitValidationError("legal-dong TSV is empty") from exc
    if header != ["법정동코드", "법정동명", "폐지여부"]:
        raise MolitValidationError("legal-dong TSV header does not match the official schema")
    seen_codes: set[str] = set()
    regions: list[MolitRegionCode] = []
    for line_number, columns in enumerate(reader, start=2):
        if not columns or all(not str(column).strip() for column in columns):
            continue
        if len(columns) != 3:
            raise MolitValidationError(
                f"legal-dong TSV row {line_number} must contain three columns"
            )
        code, name, status = (str(column).strip() for column in columns)
        if not re.fullmatch(r"[0-9]{10}", code):
            raise MolitValidationError(
                f"legal-dong TSV row {line_number} has an invalid code"
            )
        if code in seen_codes:
            raise MolitValidationError(f"legal-dong TSV repeats code {code}")
        seen_codes.add(code)
        _text(name, "법정동명", 200)
        if status not in {"존재", "폐지"}:
            raise MolitValidationError(
                f"legal-dong TSV row {line_number} has an unknown status"
            )
        if (
            status == "존재"
            and code.endswith("00000")
            and code[2:] != "00000000"
        ):
            regions.append(
                MolitRegionCode(
                    legal_dong_code=code,
                    ctpv_cd=code[:2],
                    sgg_cd=code[:5],
                    name=name,
                )
            )
    if not regions:
        raise MolitValidationError("legal-dong TSV contains no current SGG rows")
    regions.sort(key=lambda region: region.legal_dong_code)
    return tuple(regions)


def build_region_batch_requests(
    path: str | Path,
    *,
    opr_ymd: str,
    page_size: int = MAX_PAGE_SIZE,
) -> tuple[MolitRequest, ...]:
    return tuple(
        MolitRequest(
            opr_ymd=opr_ymd,
            rte_id=None,
            ctpv_cd=region.ctpv_cd,
            sgg_cd=region.sgg_cd,
            num_of_rows=page_size,
        )
        for region in load_active_sgg_codes(path)
    )


def legal_dong_data_sha256(path: str | Path) -> str:
    """Hash the bounded decoded-data payload (ZIP member or direct TSV)."""

    return hashlib.sha256(
        _read_bounded_legal_dong_file(Path(path).resolve())
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class MolitPage:
    request: MolitRequest
    total_count: int
    rows: tuple[RouteStopRow, ...]


def _response_body(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    root = _casefold_mapping(payload, "official API response")
    # The data.go.kr gateway uses a separate envelope when the provider cannot
    # route/process a request.  Preserve the upstream code instead of reducing
    # it to the unhelpful "missing response" protocol error.
    service_response = root.get("openapi_serviceresponse")
    if isinstance(service_response, Mapping):
        service_root = _casefold_mapping(
            service_response, "OpenAPI_ServiceResponse"
        )
        message_header = service_root.get("cmmmsgheader")
        if isinstance(message_header, Mapping):
            error_root = _casefold_mapping(message_header, "cmmMsgHeader")
            reason_code = _text(
                error_root.get("returnreasoncode"),
                "returnReasonCode",
                16,
                required=False,
            )
            error_message = _text(
                error_root.get("errormsg") or error_root.get("errMsg")
                or error_root.get("returnauthmsg") or error_root.get("returnAuthMsg"),
                "errMsg",
                160,
                required=False,
            )
            if reason_code in {"04", "05"}:
                raise MolitTransientUpstreamError(
                    "official API gateway returned "
                    f"{reason_code or 'unknown'}{(': ' + error_message) if error_message else ''}"
                )
            raise MolitProtocolError(
                "official API gateway returned "
                f"{reason_code or 'unknown'}{(': ' + error_message) if error_message else ''}"
            )
    response = _casefold_mapping(
        _required_field(root, "response", "official API response"), "response"
    )
    header = _casefold_mapping(
        _required_field(response, "header", "response"), "response.header"
    )
    body = _casefold_mapping(
        _required_field(response, "body", "response"), "response.body"
    )
    code = _text(
        _required_field(header, "resultCode", "response.header"), "resultCode", 64
    )
    if code not in _RESULT_OK:
        if code == "22":
            raise MolitQuotaError(f"official API returned result code {code}")
        if code in {"12", "20", "30", "31", "32"}:
            raise MolitAuthenticationError(
                f"official API returned result code {code}"
            )
        raise MolitProtocolError(f"official API returned result code {code}")
    return header, body


def parse_page(payload: bytes | str | Mapping[str, Any], request: MolitRequest) -> MolitPage:
    """Parse and cross-check one official JSON page without guessing fields."""

    if isinstance(payload, bytes):
        if len(payload) > MAX_RESPONSE_BYTES:
            raise MolitLimitError("official API response exceeds byte limit")
        try:
            decoded: Any = json.loads(payload.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MolitProtocolError("official API did not return valid UTF-8 JSON") from exc
    elif isinstance(payload, str):
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MolitProtocolError("official API did not return valid JSON") from exc
    else:
        decoded = payload
    if not isinstance(decoded, Mapping):
        raise MolitProtocolError("official API response must be an object")
    _header, body = _response_body(decoded)
    total_count = _bounded_int(
        _required_field(body, "totalCount", "response.body"),
        "totalCount",
        0,
        MAX_TOTAL_ROWS,
    )
    page_no = _bounded_int(
        _required_field(body, "pageNo", "response.body"), "pageNo", 1, MAX_PAGES
    )
    num_rows = _bounded_int(
        _required_field(body, "numOfRows", "response.body"),
        "numOfRows",
        1,
        MAX_PAGE_SIZE,
    )
    if page_no != request.page_no or num_rows != request.num_of_rows:
        raise MolitProtocolError("official API pagination does not match the request")

    items = _required_field(body, "items", "response.body")
    raw_items: Any = [] if items in (None, "") else items
    if isinstance(raw_items, Mapping):
        normalized_items = _casefold_mapping(raw_items, "response.body.items")
        raw_items = (
            []
            if not normalized_items
            else _required_field(normalized_items, "item", "response.body.items")
        )
    if raw_items in (None, ""):
        raw_items = []
    if isinstance(raw_items, Mapping):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raise MolitProtocolError("response items.item must be a list or object")
    if len(raw_items) > request.num_of_rows or len(raw_items) > total_count:
        raise MolitProtocolError("response row count exceeds declared bounds")

    rows: list[RouteStopRow] = []
    previous_sequence: int | None = None
    route_names: dict[str, tuple[str, str]] = {}
    page_pairs: set[tuple[str, int]] = set()
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            raise MolitProtocolError(f"items[{index}] must be an object")
        normalized_item = _casefold_mapping(item, f"items[{index}]")
        values = {
            field: _required_field(normalized_item, field, f"items[{index}]")
            for field in RESPONSE_FIELDS
        }
        sequence = _bounded_int(
            values["STTN_SEQ"], "STTN_SEQ", 1, 2_147_483_647
        )
        if (
            not request.is_region_batch
            and previous_sequence is not None
            and sequence <= previous_sequence
        ):
            raise MolitProtocolError("STTN_SEQ must be strictly increasing within a page")
        previous_sequence = sequence
        row = RouteStopRow(
            opr_ymd=_text(values["OPR_YMD"], "OPR_YMD", 8),
            rte_id=_text(values["RTE_ID"], "RTE_ID", 96),
            rte_no=_text(values["RTE_NO"], "RTE_NO", 100),
            rte_nm=_text(values["RTE_NM"], "RTE_NM", 150),
            sttn_seq=sequence,
            sttn_id=_text(values["STTN_ID"], "STTN_ID", 96),
            sttn_nm=_text(values["STTN_NM"], "STTN_NM", 150),
            ctpv_cd=_text(values["CTPV_CD"], "CTPV_CD", 2),
            sgg_cd=_text(values["SGG_CD"], "SGG_CD", 5),
            emd_cd=_text(values["EMD_CD"], "EMD_CD", 10, required=False),
            ctpv_nm=_text(values["CTPV_NM"], "CTPV_NM", 40),
            sgg_nm=_text(values["SGG_NM"], "SGG_NM", 40),
            emd_nm=_text(values["EMD_NM"], "EMD_NM", 40, required=False),
            trfc_mns_se_cd=_text(
                values["TRFC_MNS_SE_CD"], "TRFC_MNS_SE_CD", 1
            ),
        )
        if row.opr_ymd != request.opr_ymd or (
            request.rte_id is not None and row.rte_id != request.rte_id
        ):
            raise MolitProtocolError("response row does not match the requested target")
        if not re.fullmatch(r"[0-9]{2}", row.ctpv_cd) or not re.fullmatch(
            r"[0-9]{5}", row.sgg_cd
        ):
            raise MolitProtocolError("response row has invalid regional codes")
        pair = (row.rte_id, row.sttn_seq)
        if pair in page_pairs:
            raise MolitProtocolError("duplicate (RTE_ID, STTN_SEQ) within a page")
        page_pairs.add(pair)
        current_names = (row.rte_no, row.rte_nm)
        prior_names = route_names.setdefault(row.rte_id, current_names)
        if current_names != prior_names:
            raise MolitProtocolError("route number/name changed within a page")
        if row.trfc_mns_se_cd != "B":
            raise MolitProtocolError("response row is not a bus transport record")
        rows.append(row)
    if total_count > 0 and not rows:
        raise MolitProtocolError("non-empty response declared no rows for this page")
    return MolitPage(request=request, total_count=total_count, rows=tuple(rows))


class MolitRouteStopClient:
    """Bounded HTTPS client with no redirects, proxies, or secret logging."""

    def __init__(
        self,
        service_key: str,
        *,
        requests_per_second: float = 2.0,
        retries: int = 2,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        request_budget: int | None = None,
        opener: Any | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        key = str(service_key or "")
        if "%" in key:
            if re.search(r"%(?![0-9A-Fa-f]{2})", key):
                raise MolitValidationError("service key is invalid")
            try:
                key = unquote(key, errors="strict")
            except UnicodeError as exc:
                raise MolitValidationError("service key is invalid") from exc
        if not 1 <= len(key) <= 512 or any(
            character.isspace() or ord(character) < 32 for character in key
        ):
            raise MolitValidationError("service key is invalid")
        self._service_key = key
        self.requests_per_second = _bounded_float(
            requests_per_second, "requests_per_second", 0.1, MAX_RPS
        )
        self.retries = _bounded_int(retries, "retries", 0, MAX_RETRIES)
        self.timeout_seconds = _bounded_float(
            timeout_seconds, "timeout_seconds", 0.5, MAX_TIMEOUT_SECONDS
        )
        self.max_response_bytes = _bounded_int(
            max_response_bytes, "max_response_bytes", 1_024, MAX_RESPONSE_BYTES
        )
        self.request_budget = (
            None
            if request_budget is None
            else _bounded_int(
                request_budget, "request_budget", 1, MAX_REQUEST_BUDGET
            )
        )
        self._opener = opener or build_opener(_RejectRedirects(), ProxyHandler({}))
        self._sleep = sleeper
        self._monotonic = monotonic
        self._rate_lock = threading.Lock()
        self._last_request_at: float | None = None
        self._request_lock = threading.Lock()
        self._requests_attempted = 0

    @property
    def requests_attempted(self) -> int:
        with self._request_lock:
            return self._requests_attempted

    def _consume_request_budget(self) -> None:
        with self._request_lock:
            if (
                self.request_budget is not None
                and self._requests_attempted >= self.request_budget
            ):
                raise MolitRequestBudgetExhausted("official API request budget exhausted")
            self._requests_attempted += 1

    def _throttle(self) -> None:
        with self._rate_lock:
            now = self._monotonic()
            minimum_interval = 1.0 / self.requests_per_second
            if self._last_request_at is not None:
                remaining = minimum_interval - (now - self._last_request_at)
                if remaining > 0:
                    self._sleep(remaining)
                    now = self._monotonic()
            self._last_request_at = now

    def fetch_page(self, request: MolitRequest) -> MolitPage:
        parameters = {"serviceKey": self._service_key, **request.public_parameters()}
        url = ENDPOINT + "?" + urlencode(parameters)
        http_request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "busro-itda-molit-route-stop-ingest/1.0",
            },
        )
        for attempt in range(self.retries + 1):
            self._consume_request_budget()
            self._throttle()
            terminal_error: MolitIngestError | None = None
            try:
                with self._opener.open(
                    http_request, timeout=self.timeout_seconds
                ) as response:
                    status = int(getattr(response, "status", 200))
                    if status != 200:
                        raise MolitProtocolError(
                            f"official API returned HTTP status {status}"
                        )
                    payload = response.read(self.max_response_bytes + 1)
                    if len(payload) > self.max_response_bytes:
                        raise MolitLimitError(
                            "official API response exceeds configured byte limit"
                        )
                    return parse_page(payload, request)
            except MolitTransientUpstreamError as exc:
                if attempt >= self.retries:
                    terminal_error = exc
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt >= self.retries:
                    message = f"official API returned HTTP status {exc.code}"
                    terminal_error = (
                        MolitQuotaError(message)
                        if exc.code == 429
                        else MolitAuthenticationError(message)
                        if exc.code in {401, 403}
                        else MolitProtocolError(message)
                    )
            except URLError:
                if attempt >= self.retries:
                    terminal_error = MolitProtocolError(
                        "official API request failed"
                    )
            if terminal_error is not None:
                raise terminal_error
            self._sleep(min(2.0, 0.25 * (2**attempt)))
        raise AssertionError("unreachable")


class MolitRouteStopStage:
    """Independent resumable staging DB; it never mutates NetworkCatalog."""

    def __init__(self, path: str | Path, *, max_total_rows: int = MAX_TOTAL_ROWS) -> None:
        self.path = Path(path).resolve()
        self.max_total_rows = _bounded_int(
            max_total_rows, "max_total_rows", 2, MAX_TOTAL_ROWS
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS molit_targets (
                    target_key TEXT PRIMARY KEY,
                    opr_ymd TEXT NOT NULL,
                    rte_id TEXT NOT NULL,
                    ctpv_cd TEXT NOT NULL,
                    sgg_cd TEXT NOT NULL,
                    page_size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    next_page INTEGER NOT NULL,
                    total_count INTEGER,
                    staged_rows INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS molit_pages (
                    target_key TEXT NOT NULL REFERENCES molit_targets(target_key) ON DELETE CASCADE,
                    page_no INTEGER NOT NULL,
                    total_count INTEGER NOT NULL,
                    row_count INTEGER NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    PRIMARY KEY(target_key,page_no)
                );
                CREATE TABLE IF NOT EXISTS molit_rows (
                    target_key TEXT NOT NULL REFERENCES molit_targets(target_key) ON DELETE CASCADE,
                    page_no INTEGER NOT NULL,
                    row_index INTEGER NOT NULL,
                    opr_ymd TEXT NOT NULL,
                    rte_id TEXT NOT NULL,
                    rte_no TEXT NOT NULL,
                    rte_nm TEXT NOT NULL,
                    sttn_seq INTEGER NOT NULL,
                    sttn_id TEXT NOT NULL,
                    sttn_nm TEXT NOT NULL,
                    ctpv_cd TEXT NOT NULL,
                    sgg_cd TEXT NOT NULL,
                    emd_cd TEXT NOT NULL,
                    ctpv_nm TEXT NOT NULL,
                    sgg_nm TEXT NOT NULL,
                    emd_nm TEXT NOT NULL,
                    trfc_mns_se_cd TEXT NOT NULL,
                    PRIMARY KEY(target_key,page_no,row_index),
                    UNIQUE(target_key,sttn_seq)
                );
                CREATE TABLE IF NOT EXISTS molit_resolved_stops (
                    target_key TEXT NOT NULL REFERENCES molit_targets(target_key) ON DELETE CASCADE,
                    sttn_seq INTEGER NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    source_id TEXT NOT NULL,
                    PRIMARY KEY(target_key,sttn_seq)
                );
                CREATE TABLE IF NOT EXISTS molit_quarantine (
                    target_key TEXT NOT NULL REFERENCES molit_targets(target_key) ON DELETE CASCADE,
                    sttn_seq INTEGER NOT NULL,
                    sttn_id TEXT NOT NULL,
                    sttn_nm TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY(target_key,sttn_seq)
                );
                """
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    def ensure_target(self, request: MolitRequest) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM molit_targets WHERE target_key=?", (request.target_key,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO molit_targets VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        request.target_key,
                        request.opr_ymd,
                        request.rte_id,
                        request.ctpv_cd,
                        request.sgg_cd,
                        request.num_of_rows,
                        "PENDING",
                        1,
                        None,
                        0,
                        self._now(),
                    ),
                )
                connection.commit()
            elif int(row["page_size"]) != request.num_of_rows:
                raise MolitValidationError(
                    "resumed target must keep the original numOfRows"
                )
        return self.target_state(request.target_key)

    def target_state(self, target_key: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM molit_targets WHERE target_key=?", (target_key,)
            ).fetchone()
        if row is None:
            raise MolitValidationError("staging target does not exist")
        return dict(row)

    @staticmethod
    def _page_digest(page: MolitPage) -> str:
        canonical = json.dumps(
            [page.total_count, [asdict(row) for row in page.rows]],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def stage_page(self, page: MolitPage) -> dict[str, Any]:
        if any(
            row.opr_ymd != page.request.opr_ymd
            or row.rte_id != page.request.rte_id
            or row.trfc_mns_se_cd != "B"
            for row in page.rows
        ):
            raise MolitProtocolError("staged row does not match the requested bus target")
        sequences = [row.sttn_seq for row in page.rows]
        if sequences != sorted(set(sequences)):
            raise MolitProtocolError("staged STTN_SEQ values must be strictly increasing")
        route_names = {(row.rte_no, row.rte_nm) for row in page.rows}
        if len(route_names) > 1:
            raise MolitProtocolError("route number/name changed within a page")
        self.ensure_target(page.request.next_page(1))
        digest = self._page_digest(page)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                "SELECT * FROM molit_targets WHERE target_key=?",
                (page.request.target_key,),
            ).fetchone()
            existing = connection.execute(
                "SELECT payload_sha256 FROM molit_pages WHERE target_key=? AND page_no=?",
                (page.request.target_key, page.request.page_no),
            ).fetchone()
            if existing is not None:
                if existing["payload_sha256"] != digest:
                    raise MolitProtocolError("staged page changed across resume")
                connection.rollback()
                return self.target_state(page.request.target_key)
            if int(target["next_page"]) != page.request.page_no:
                raise MolitValidationError("page staging must be contiguous")
            prior_total = target["total_count"]
            if prior_total is not None and int(prior_total) != page.total_count:
                raise MolitProtocolError("totalCount changed across pages")
            staged_rows = int(target["staged_rows"]) + len(page.rows)
            if staged_rows > page.total_count or staged_rows > self.max_total_rows:
                raise MolitLimitError("staged rows exceed declared or configured total")
            if staged_rows < page.total_count and len(page.rows) < page.request.num_of_rows:
                raise MolitProtocolError("short page cannot precede remaining rows")
            previous_sequence = connection.execute(
                "SELECT MAX(sttn_seq) FROM molit_rows WHERE target_key=?",
                (page.request.target_key,),
            ).fetchone()[0]
            if (
                previous_sequence is not None
                and page.rows
                and page.rows[0].sttn_seq <= int(previous_sequence)
            ):
                raise MolitProtocolError(
                    "STTN_SEQ must be strictly increasing across pages"
                )
            existing_route_name = connection.execute(
                "SELECT rte_no,rte_nm FROM molit_rows WHERE target_key=? "
                "ORDER BY sttn_seq LIMIT 1",
                (page.request.target_key,),
            ).fetchone()
            if (
                existing_route_name is not None
                and route_names
                and next(iter(route_names))
                != (existing_route_name["rte_no"], existing_route_name["rte_nm"])
            ):
                raise MolitProtocolError("route number/name changed across pages")
            connection.execute(
                "INSERT INTO molit_pages VALUES(?,?,?,?,?)",
                (
                    page.request.target_key,
                    page.request.page_no,
                    page.total_count,
                    len(page.rows),
                    digest,
                ),
            )
            connection.executemany(
                "INSERT INTO molit_rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        page.request.target_key,
                        page.request.page_no,
                        index,
                        row.opr_ymd,
                        row.rte_id,
                        row.rte_no,
                        row.rte_nm,
                        row.sttn_seq,
                        row.sttn_id,
                        row.sttn_nm,
                        row.ctpv_cd,
                        row.sgg_cd,
                        row.emd_cd,
                        row.ctpv_nm,
                        row.sgg_nm,
                        row.emd_nm,
                        row.trfc_mns_se_cd,
                    )
                    for index, row in enumerate(page.rows)
                ],
            )
            status = (
                "EMPTY"
                if page.total_count == 0
                else "STAGED"
                if staged_rows == page.total_count
                else "STAGING"
            )
            connection.execute(
                "UPDATE molit_targets SET status=?,next_page=?,total_count=?,"
                "staged_rows=?,updated_at=? WHERE target_key=?",
                (
                    status,
                    page.request.page_no + 1,
                    page.total_count,
                    staged_rows,
                    self._now(),
                    page.request.target_key,
                ),
            )
            connection.commit()
        return self.target_state(page.request.target_key)

    def next_request(self, target_key: str) -> MolitRequest | None:
        state = self.target_state(target_key)
        if state["status"] in {"STAGED", "READY_FOR_ACTIVATION", "QUARANTINED", "EMPTY"}:
            return None
        return MolitRequest(
            opr_ymd=state["opr_ymd"],
            rte_id=state["rte_id"],
            ctpv_cd=state["ctpv_cd"],
            sgg_cd=state["sgg_cd"],
            page_no=state["next_page"],
            num_of_rows=state["page_size"],
        )

    @staticmethod
    def _catalog_connection(path: Path) -> sqlite3.Connection:
        if not path.is_file():
            raise MolitValidationError("NetworkCatalog database does not exist")
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        required = {"catalog_meta", "catalog_stops"}
        present = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
                tuple(sorted(required)),
            )
        }
        if present != required:
            connection.close()
            raise MolitValidationError("NetworkCatalog stop schema is incomplete")
        return connection

    def resolve_against_catalog(
        self, target_key: str, catalog_path: str | Path
    ) -> dict[str, Any]:
        state = self.target_state(target_key)
        if state["status"] not in {"STAGED", "QUARANTINED", "READY_FOR_ACTIVATION"}:
            raise MolitValidationError("target must be fully staged before resolution")
        catalog = Path(catalog_path).resolve()
        if catalog == self.path:
            raise MolitValidationError("staging and NetworkCatalog paths must differ")
        with closing(self._catalog_connection(catalog)) as official, closing(
            self.connect()
        ) as stage:
            source = official.execute(
                "SELECT value FROM catalog_meta WHERE key='active_stops_source_id'"
            ).fetchone()
            if source is None or not str(source[0]).strip():
                raise MolitValidationError("NetworkCatalog has no active official stops source")
            rows = stage.execute(
                "SELECT * FROM molit_rows WHERE target_key=? ORDER BY sttn_seq",
                (target_key,),
            ).fetchall()
            if len(rows) != int(state["total_count"]) or len(rows) < 2:
                raise MolitValidationError("staged route is incomplete or too short")
            if len(rows) > MAX_NETWORK_CATALOG_SEQUENCE_STOPS:
                raise MolitLimitError(
                    "staged route exceeds the NetworkCatalog sequence-stop limit"
                )
            stage.execute("BEGIN IMMEDIATE")
            stage.execute("DELETE FROM molit_resolved_stops WHERE target_key=?", (target_key,))
            stage.execute("DELETE FROM molit_quarantine WHERE target_key=?", (target_key,))
            resolved: list[tuple[Any, ...]] = []
            quarantined: list[tuple[Any, ...]] = []
            for row in rows:
                area_matches = official.execute(
                    "SELECT city_code,node_id,node_name,latitude,longitude "
                    "FROM catalog_stops WHERE source_id=? AND node_id=? "
                    "AND city_code IN (?,?) ORDER BY city_code,node_id LIMIT 2",
                    (source[0], row["sttn_id"], row["sgg_cd"], row["ctpv_cd"]),
                ).fetchall()
                candidates = area_matches
                if not candidates:
                    candidates = official.execute(
                        "SELECT city_code,node_id,node_name,latitude,longitude "
                        "FROM catalog_stops WHERE source_id=? AND node_id=? "
                        "ORDER BY city_code,node_id LIMIT 2",
                        (source[0], row["sttn_id"]),
                    ).fetchall()
                if len(candidates) != 1:
                    reason = (
                        "STOP_ID_NOT_FOUND_IN_ACTIVE_OFFICIAL_CATALOG"
                        if not candidates
                        else "STOP_ID_AMBIGUOUS_IN_ACTIVE_OFFICIAL_CATALOG"
                    )
                    quarantined.append(
                        (target_key, row["sttn_seq"], row["sttn_id"], row["sttn_nm"], reason)
                    )
                    continue
                match = candidates[0]
                resolved.append(
                    (
                        target_key,
                        row["sttn_seq"],
                        match["node_id"],
                        match["node_name"],
                        match["city_code"],
                        match["latitude"],
                        match["longitude"],
                        source[0],
                    )
                )
            stage.executemany(
                "INSERT INTO molit_resolved_stops VALUES(?,?,?,?,?,?,?,?)", resolved
            )
            stage.executemany(
                "INSERT INTO molit_quarantine VALUES(?,?,?,?,?)", quarantined
            )
            status = "QUARANTINED" if quarantined else "READY_FOR_ACTIVATION"
            stage.execute(
                "UPDATE molit_targets SET status=?,updated_at=? WHERE target_key=?",
                (status, self._now(), target_key),
            )
            stage.commit()
        return {
            "target_key": target_key,
            "status": status,
            "resolved_rows": len(resolved),
            "quarantined_rows": len(quarantined),
        }

    def activation_candidate(self, target_key: str) -> dict[str, Any]:
        """Return a NetworkCatalog-compatible payload without activating it."""

        state = self.target_state(target_key)
        if state["status"] != "READY_FOR_ACTIVATION":
            raise MolitValidationError("target is not ready for activation")
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT r.sttn_seq,r.sttn_nm,r.rte_no,r.rte_nm,m.node_id,m.node_name,"
                "m.latitude,m.longitude,m.source_id "
                "FROM molit_rows r JOIN molit_resolved_stops m "
                "ON m.target_key=r.target_key AND m.sttn_seq=r.sttn_seq "
                "WHERE r.target_key=? ORDER BY r.sttn_seq",
                (target_key,),
            ).fetchall()
        if len(rows) != int(state["total_count"]):
            raise MolitValidationError("resolved activation candidate is incomplete")
        source_ids = {str(row["source_id"]) for row in rows}
        if len(source_ids) != 1:
            raise MolitValidationError("resolved stops do not share one official source")
        return {
            "city_code": state["sgg_cd"],
            "route_id": state["rte_id"],
            "source": _route_provenance(
                opr_ymd=state["opr_ymd"],
                route_no=str(rows[0]["rte_no"]),
                route_name=str(rows[0]["rte_nm"]),
                stops_source_id=next(iter(source_ids)),
            ),
            "captured_at": _operation_timestamp(state["opr_ymd"]),
            "ordered_stops": [
                {
                    "node_order": int(row["sttn_seq"]),
                    "node_id": row["node_id"],
                    "node_name": row["node_name"],
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                    "direction": "",
                    "can_board": True,
                    "can_alight": True,
                }
                for row in rows
            ],
        }


class MolitRegionBatchStage(MolitRouteStopStage):
    """Separate resumable staging for an ``rte_id``-omitted regional query."""

    _TERMINAL_STATUSES = frozenset(
        {
            "EMPTY",
            "STAGED",
            "READY_FOR_ACTIVATION",
            "PARTIALLY_QUARANTINED",
            "QUARANTINED",
        }
    )

    def __init__(self, path: str | Path, *, max_total_rows: int = MAX_TOTAL_ROWS) -> None:
        super().__init__(path, max_total_rows=max_total_rows)
        self._catalog_stop_cache_lock = threading.Lock()
        self._catalog_stop_cache_key: tuple[Path, str] | None = None
        self._catalog_stops_by_id: dict[
            str, tuple[tuple[str, str, str, float, float], ...]
        ] = {}
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS molit_region_targets (
                    target_key TEXT PRIMARY KEY,
                    opr_ymd TEXT NOT NULL,
                    ctpv_cd TEXT NOT NULL,
                    sgg_cd TEXT NOT NULL,
                    page_size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    next_page INTEGER NOT NULL,
                    total_count INTEGER,
                    staged_rows INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS molit_region_pages (
                    target_key TEXT NOT NULL REFERENCES molit_region_targets(target_key)
                        ON DELETE CASCADE,
                    page_no INTEGER NOT NULL,
                    total_count INTEGER NOT NULL,
                    row_count INTEGER NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    PRIMARY KEY(target_key,page_no)
                );
                CREATE TABLE IF NOT EXISTS molit_region_routes (
                    target_key TEXT NOT NULL REFERENCES molit_region_targets(target_key)
                        ON DELETE CASCADE,
                    rte_id TEXT NOT NULL,
                    rte_no TEXT NOT NULL,
                    rte_nm TEXT NOT NULL,
                    status TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    resolved_rows INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(target_key,rte_id)
                );
                CREATE TABLE IF NOT EXISTS molit_region_rows (
                    target_key TEXT NOT NULL REFERENCES molit_region_targets(target_key)
                        ON DELETE CASCADE,
                    page_no INTEGER NOT NULL,
                    row_index INTEGER NOT NULL,
                    opr_ymd TEXT NOT NULL,
                    rte_id TEXT NOT NULL,
                    rte_no TEXT NOT NULL,
                    rte_nm TEXT NOT NULL,
                    sttn_seq INTEGER NOT NULL,
                    sttn_id TEXT NOT NULL,
                    sttn_nm TEXT NOT NULL,
                    ctpv_cd TEXT NOT NULL,
                    sgg_cd TEXT NOT NULL,
                    emd_cd TEXT NOT NULL,
                    ctpv_nm TEXT NOT NULL,
                    sgg_nm TEXT NOT NULL,
                    emd_nm TEXT NOT NULL,
                    trfc_mns_se_cd TEXT NOT NULL,
                    PRIMARY KEY(target_key,page_no,row_index),
                    UNIQUE(target_key,rte_id,sttn_seq)
                );
                CREATE TABLE IF NOT EXISTS molit_region_resolved_stops (
                    target_key TEXT NOT NULL REFERENCES molit_region_targets(target_key)
                        ON DELETE CASCADE,
                    rte_id TEXT NOT NULL,
                    sttn_seq INTEGER NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    source_id TEXT NOT NULL,
                    PRIMARY KEY(target_key,rte_id,sttn_seq)
                );
                CREATE TABLE IF NOT EXISTS molit_region_route_quarantine (
                    target_key TEXT NOT NULL REFERENCES molit_region_targets(target_key)
                        ON DELETE CASCADE,
                    rte_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    unresolved_rows INTEGER NOT NULL,
                    PRIMARY KEY(target_key,rte_id)
                );
                CREATE TABLE IF NOT EXISTS molit_region_stop_quarantine (
                    target_key TEXT NOT NULL REFERENCES molit_region_targets(target_key)
                        ON DELETE CASCADE,
                    rte_id TEXT NOT NULL,
                    sttn_seq INTEGER NOT NULL,
                    sttn_id TEXT NOT NULL,
                    sttn_nm TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY(target_key,rte_id,sttn_seq)
                );
                CREATE INDEX IF NOT EXISTS idx_molit_region_rows_route
                    ON molit_region_rows(target_key,rte_id,sttn_seq);
                CREATE TABLE IF NOT EXISTS molit_nationwide_runs (
                    run_key TEXT PRIMARY KEY,
                    opr_ymd TEXT NOT NULL,
                    legal_source_sha256 TEXT NOT NULL,
                    page_size INTEGER NOT NULL,
                    total_regions INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    network_requests INTEGER NOT NULL,
                    ready_candidates INTEGER NOT NULL,
                    conflict_routes INTEGER NOT NULL,
                    activated INTEGER NOT NULL,
                    skipped_older INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS molit_nationwide_regions (
                    run_key TEXT NOT NULL REFERENCES molit_nationwide_runs(run_key)
                        ON DELETE CASCADE,
                    region_index INTEGER NOT NULL,
                    legal_dong_code TEXT NOT NULL,
                    ctpv_cd TEXT NOT NULL,
                    sgg_cd TEXT NOT NULL,
                    region_name TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    network_requests INTEGER NOT NULL,
                    error_code TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(run_key,region_index),
                    UNIQUE(run_key,target_key)
                );
                CREATE TABLE IF NOT EXISTS molit_nationwide_routes (
                    run_key TEXT NOT NULL REFERENCES molit_nationwide_runs(run_key)
                        ON DELETE CASCADE,
                    rte_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    owner_target_key TEXT NOT NULL,
                    owner_city_code TEXT NOT NULL,
                    owner_basis TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL,
                    sequence_sha256 TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    PRIMARY KEY(run_key,rte_id)
                );
                """
            )

    @staticmethod
    def _require_batch_request(request: MolitRequest) -> None:
        if not request.is_region_batch:
            raise MolitValidationError("regional batch request must omit RTE_ID")

    def ensure_target(self, request: MolitRequest) -> dict[str, Any]:
        self._require_batch_request(request)
        first_page = request.next_page(1)
        with closing(self.connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO molit_region_targets "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    first_page.target_key,
                    first_page.opr_ymd,
                    first_page.ctpv_cd,
                    first_page.sgg_cd,
                    first_page.num_of_rows,
                    "PENDING",
                    1,
                    None,
                    0,
                    self._now(),
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM molit_region_targets WHERE target_key=?",
                (first_page.target_key,),
            ).fetchone()
            if int(row["page_size"]) != first_page.num_of_rows:
                raise MolitValidationError(
                    "resumed regional target must keep the original numOfRows"
                )
        return self.target_state(first_page.target_key)

    def target_state(self, target_key: str) -> dict[str, Any]:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM molit_region_targets WHERE target_key=?", (target_key,)
            ).fetchone()
        if row is None:
            raise MolitValidationError("regional staging target does not exist")
        return dict(row)

    def stage_page(self, page: MolitPage) -> dict[str, Any]:
        self._require_batch_request(page.request)
        if any(
            row.opr_ymd != page.request.opr_ymd or row.trfc_mns_se_cd != "B"
            for row in page.rows
        ):
            raise MolitProtocolError(
                "staged row does not match the requested regional bus target"
            )
        pairs = [(row.rte_id, row.sttn_seq) for row in page.rows]
        if any(not row.rte_id or row.sttn_seq < 1 for row in page.rows):
            raise MolitProtocolError("regional rows require RTE_ID and positive STTN_SEQ")
        if len(pairs) != len(set(pairs)):
            raise MolitProtocolError("duplicate (RTE_ID, STTN_SEQ) within a page")
        page_metadata: dict[str, tuple[str, str]] = {}
        for row in page.rows:
            metadata = (row.rte_no, row.rte_nm)
            existing = page_metadata.setdefault(row.rte_id, metadata)
            if existing != metadata:
                raise MolitProtocolError("route number/name changed within a page")

        self.ensure_target(page.request.next_page(1))
        digest = self._page_digest(page)
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                "SELECT * FROM molit_region_targets WHERE target_key=?",
                (page.request.target_key,),
            ).fetchone()
            existing_page = connection.execute(
                "SELECT payload_sha256 FROM molit_region_pages "
                "WHERE target_key=? AND page_no=?",
                (page.request.target_key, page.request.page_no),
            ).fetchone()
            if existing_page is not None:
                if existing_page["payload_sha256"] != digest:
                    raise MolitProtocolError("staged regional page changed across resume")
                connection.rollback()
                return self.target_state(page.request.target_key)
            if int(target["next_page"]) != page.request.page_no:
                raise MolitValidationError("regional page staging must be contiguous")
            prior_total = target["total_count"]
            if prior_total is not None and int(prior_total) != page.total_count:
                raise MolitProtocolError("totalCount changed across regional pages")
            staged_rows = int(target["staged_rows"]) + len(page.rows)
            if (
                page.total_count > self.max_total_rows
                or staged_rows > page.total_count
                or staged_rows > self.max_total_rows
            ):
                raise MolitLimitError(
                    "regional staged rows exceed declared or configured total"
                )
            expected_pages = max(1, math.ceil(page.total_count / page.request.num_of_rows))
            if page.request.page_no > expected_pages:
                raise MolitProtocolError("regional response exceeds expected page count")
            if staged_rows < page.total_count and len(page.rows) != page.request.num_of_rows:
                raise MolitProtocolError(
                    "short regional page cannot precede remaining rows"
                )
            if staged_rows == page.total_count and page.request.page_no != expected_pages:
                raise MolitProtocolError("regional pagination completed on the wrong page")

            for rte_id, metadata in page_metadata.items():
                existing_route = connection.execute(
                    "SELECT rte_no,rte_nm FROM molit_region_routes "
                    "WHERE target_key=? AND rte_id=?",
                    (page.request.target_key, rte_id),
                ).fetchone()
                if existing_route is not None and metadata != (
                    existing_route["rte_no"],
                    existing_route["rte_nm"],
                ):
                    raise MolitProtocolError(
                        f"route metadata changed across pages for RTE_ID {rte_id}"
                    )
            duplicate = next(
                (
                    pair
                    for pair in pairs
                    if connection.execute(
                        "SELECT 1 FROM molit_region_rows "
                        "WHERE target_key=? AND rte_id=? AND sttn_seq=?",
                        (page.request.target_key, pair[0], pair[1]),
                    ).fetchone()
                    is not None
                ),
                None,
            )
            if duplicate is not None:
                raise MolitProtocolError(
                    "duplicate (RTE_ID, STTN_SEQ) across regional pages"
                )

            connection.execute(
                "INSERT INTO molit_region_pages VALUES(?,?,?,?,?)",
                (
                    page.request.target_key,
                    page.request.page_no,
                    page.total_count,
                    len(page.rows),
                    digest,
                ),
            )
            connection.executemany(
                "INSERT INTO molit_region_rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        page.request.target_key,
                        page.request.page_no,
                        index,
                        row.opr_ymd,
                        row.rte_id,
                        row.rte_no,
                        row.rte_nm,
                        row.sttn_seq,
                        row.sttn_id,
                        row.sttn_nm,
                        row.ctpv_cd,
                        row.sgg_cd,
                        row.emd_cd,
                        row.ctpv_nm,
                        row.sgg_nm,
                        row.emd_nm,
                        row.trfc_mns_se_cd,
                    )
                    for index, row in enumerate(page.rows)
                ],
            )
            for rte_id, metadata in page_metadata.items():
                added = sum(1 for row in page.rows if row.rte_id == rte_id)
                connection.execute(
                    "INSERT INTO molit_region_routes "
                    "VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(target_key,rte_id) DO UPDATE SET "
                    "row_count=row_count+excluded.row_count,updated_at=excluded.updated_at",
                    (
                        page.request.target_key,
                        rte_id,
                        metadata[0],
                        metadata[1],
                        "STAGING",
                        added,
                        0,
                        self._now(),
                    ),
                )
            status = (
                "EMPTY"
                if page.total_count == 0
                else "STAGED"
                if staged_rows == page.total_count
                else "STAGING"
            )
            if status == "STAGED":
                actual_rows = connection.execute(
                    "SELECT COUNT(*) FROM molit_region_rows WHERE target_key=?",
                    (page.request.target_key,),
                ).fetchone()[0]
                if int(actual_rows) != page.total_count:
                    raise MolitProtocolError(
                        "regional pagination is incomplete after the final page"
                    )
                connection.execute(
                    "UPDATE molit_region_routes SET status='STAGED',updated_at=? "
                    "WHERE target_key=?",
                    (self._now(), page.request.target_key),
                )
            connection.execute(
                "UPDATE molit_region_targets SET status=?,next_page=?,total_count=?,"
                "staged_rows=?,updated_at=? WHERE target_key=?",
                (
                    status,
                    page.request.page_no + 1,
                    page.total_count,
                    staged_rows,
                    self._now(),
                    page.request.target_key,
                ),
            )
            connection.commit()
        return self.target_state(page.request.target_key)

    def next_request(self, target_key: str) -> MolitRequest | None:
        state = self.target_state(target_key)
        if state["status"] in self._TERMINAL_STATUSES:
            return None
        return MolitRequest(
            opr_ymd=state["opr_ymd"],
            rte_id=None,
            ctpv_cd=state["ctpv_cd"],
            sgg_cd=state["sgg_cd"],
            page_no=state["next_page"],
            num_of_rows=state["page_size"],
        )

    def _exact_catalog_stop_index(
        self,
        official: sqlite3.Connection,
        catalog_path: Path,
        source_id: str,
    ) -> dict[str, tuple[tuple[str, str, str, float, float], ...]]:
        cache_key = (catalog_path, source_id)
        with self._catalog_stop_cache_lock:
            if self._catalog_stop_cache_key == cache_key:
                return self._catalog_stops_by_id
            mutable: dict[str, list[tuple[str, str, str, float, float]]] = {}
            for row in official.execute(
                "SELECT city_code,node_id,node_name,latitude,longitude "
                "FROM catalog_stops WHERE source_id=? ORDER BY node_id,city_code",
                (source_id,),
            ):
                mutable.setdefault(str(row["node_id"]), []).append(
                    (
                        str(row["city_code"]),
                        str(row["node_id"]),
                        str(row["node_name"]),
                        float(row["latitude"]),
                        float(row["longitude"]),
                    )
                )
            self._catalog_stops_by_id = {
                node_id: tuple(matches) for node_id, matches in mutable.items()
            }
            self._catalog_stop_cache_key = cache_key
            return self._catalog_stops_by_id

    def resolve_against_catalog(
        self, target_key: str, catalog_path: str | Path
    ) -> dict[str, Any]:
        state = self.target_state(target_key)
        if state["status"] not in {
            "STAGED",
            "READY_FOR_ACTIVATION",
            "PARTIALLY_QUARANTINED",
            "QUARANTINED",
        }:
            raise MolitValidationError(
                "regional target must be fully staged before resolution"
            )
        catalog = Path(catalog_path).resolve()
        if catalog == self.path:
            raise MolitValidationError("staging and NetworkCatalog paths must differ")
        with closing(self._catalog_connection(catalog)) as official, closing(
            self.connect()
        ) as stage:
            source = official.execute(
                "SELECT value FROM catalog_meta WHERE key='active_stops_source_id'"
            ).fetchone()
            if source is None or not str(source[0]).strip():
                raise MolitValidationError(
                    "NetworkCatalog has no active official stops source"
                )
            source_id = str(source[0])
            stops_by_id = self._exact_catalog_stop_index(
                official, catalog, source_id
            )
            routes = stage.execute(
                "SELECT * FROM molit_region_routes WHERE target_key=? ORDER BY rte_id",
                (target_key,),
            ).fetchall()
            if not routes:
                raise MolitValidationError("regional target contains no routes")
            stage.execute("BEGIN IMMEDIATE")
            stage.execute(
                "DELETE FROM molit_region_resolved_stops WHERE target_key=?",
                (target_key,),
            )
            stage.execute(
                "DELETE FROM molit_region_route_quarantine WHERE target_key=?",
                (target_key,),
            )
            stage.execute(
                "DELETE FROM molit_region_stop_quarantine WHERE target_key=?",
                (target_key,),
            )
            ready_routes = 0
            quarantined_routes = 0
            resolved_rows = 0
            quarantined_rows = 0
            for route in routes:
                rows = stage.execute(
                    "SELECT * FROM molit_region_rows "
                    "WHERE target_key=? AND rte_id=? ORDER BY sttn_seq",
                    (target_key, route["rte_id"]),
                ).fetchall()
                if len(rows) != int(route["row_count"]):
                    raise MolitProtocolError(
                        f"regional route row count is inconsistent for RTE_ID {route['rte_id']}"
                    )
                route_reason: str | None = None
                if len(rows) < 2:
                    route_reason = "ROUTE_HAS_FEWER_THAN_TWO_STOPS"
                elif len(rows) > MAX_NETWORK_CATALOG_SEQUENCE_STOPS:
                    route_reason = "ROUTE_EXCEEDS_NETWORK_CATALOG_LIMIT"
                route_resolved: list[tuple[Any, ...]] = []
                route_quarantine: list[tuple[Any, ...]] = []
                if route_reason is None:
                    for row in rows:
                        all_matches = stops_by_id.get(str(row["sttn_id"]), ())
                        area_matches = tuple(
                            match
                            for match in all_matches
                            if match[0] in {row["sgg_cd"], row["ctpv_cd"]}
                        )
                        candidates = area_matches or all_matches
                        if len(candidates) != 1:
                            reason = (
                                "STOP_ID_NOT_FOUND_IN_ACTIVE_OFFICIAL_CATALOG"
                                if not candidates
                                else "STOP_ID_AMBIGUOUS_IN_ACTIVE_OFFICIAL_CATALOG"
                            )
                            route_quarantine.append(
                                (
                                    target_key,
                                    row["rte_id"],
                                    row["sttn_seq"],
                                    row["sttn_id"],
                                    row["sttn_nm"],
                                    reason,
                                )
                            )
                            continue
                        match = candidates[0]
                        route_resolved.append(
                            (
                                target_key,
                                row["rte_id"],
                                row["sttn_seq"],
                                match[1],
                                match[2],
                                match[0],
                                match[3],
                                match[4],
                                source_id,
                            )
                        )
                stage.executemany(
                    "INSERT INTO molit_region_resolved_stops VALUES(?,?,?,?,?,?,?,?,?)",
                    route_resolved,
                )
                stage.executemany(
                    "INSERT INTO molit_region_stop_quarantine VALUES(?,?,?,?,?,?)",
                    route_quarantine,
                )
                resolved_rows += len(route_resolved)
                quarantined_rows += len(route_quarantine)
                if route_reason is None and route_quarantine:
                    route_reason = "UNRESOLVED_OR_AMBIGUOUS_STOP_ID"
                if route_reason is not None:
                    unresolved = len(rows) if not route_quarantine else len(route_quarantine)
                    stage.execute(
                        "INSERT INTO molit_region_route_quarantine VALUES(?,?,?,?)",
                        (target_key, route["rte_id"], route_reason, unresolved),
                    )
                    route_status = "QUARANTINED"
                    quarantined_routes += 1
                else:
                    route_status = "READY_FOR_ACTIVATION"
                    ready_routes += 1
                stage.execute(
                    "UPDATE molit_region_routes SET status=?,resolved_rows=?,updated_at=? "
                    "WHERE target_key=? AND rte_id=?",
                    (
                        route_status,
                        len(route_resolved),
                        self._now(),
                        target_key,
                        route["rte_id"],
                    ),
                )
            status = (
                "READY_FOR_ACTIVATION"
                if ready_routes and not quarantined_routes
                else "PARTIALLY_QUARANTINED"
                if ready_routes
                else "QUARANTINED"
            )
            stage.execute(
                "UPDATE molit_region_targets SET status=?,updated_at=? WHERE target_key=?",
                (status, self._now(), target_key),
            )
            stage.commit()
        return {
            "target_key": target_key,
            "status": status,
            "routes": len(routes),
            "ready_routes": ready_routes,
            "quarantined_routes": quarantined_routes,
            "resolved_rows": resolved_rows,
            "quarantined_rows": quarantined_rows,
        }

    def activation_candidate(self, target_key: str, rte_id: str) -> dict[str, Any]:
        state = self.target_state(target_key)
        with closing(self.connect()) as connection:
            route = connection.execute(
                "SELECT * FROM molit_region_routes "
                "WHERE target_key=? AND rte_id=?",
                (target_key, rte_id),
            ).fetchone()
            if route is None or route["status"] != "READY_FOR_ACTIVATION":
                raise MolitValidationError("regional route is not ready for activation")
            rows = connection.execute(
                "SELECT r.sttn_seq,m.node_id,m.node_name,m.latitude,m.longitude,m.source_id "
                "FROM molit_region_rows r JOIN molit_region_resolved_stops m "
                "ON m.target_key=r.target_key AND m.rte_id=r.rte_id "
                "AND m.sttn_seq=r.sttn_seq "
                "WHERE r.target_key=? AND r.rte_id=? ORDER BY r.sttn_seq",
                (target_key, rte_id),
            ).fetchall()
        if len(rows) != int(route["row_count"]) or len(rows) < 2:
            raise MolitValidationError("resolved regional route is incomplete")
        source_ids = {str(row["source_id"]) for row in rows}
        if len(source_ids) != 1:
            raise MolitValidationError("resolved stops do not share one official source")
        return {
            "city_code": state["sgg_cd"],
            "route_id": rte_id,
            "source": _route_provenance(
                opr_ymd=state["opr_ymd"],
                route_no=str(route["rte_no"]),
                route_name=str(route["rte_nm"]),
                stops_source_id=next(iter(source_ids)),
            ),
            "captured_at": _operation_timestamp(state["opr_ymd"]),
            "ordered_stops": [
                {
                    "node_order": int(row["sttn_seq"]),
                    "node_id": row["node_id"],
                    "node_name": row["node_name"],
                    "latitude": float(row["latitude"]),
                    "longitude": float(row["longitude"]),
                    "direction": "",
                    "can_board": True,
                    "can_alight": True,
                }
                for row in rows
            ],
        }

    def activation_candidates(self, target_key: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            route_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT rte_id FROM molit_region_routes "
                    "WHERE target_key=? AND status='READY_FOR_ACTIVATION' ORDER BY rte_id",
                    (target_key,),
                )
            ]
        return [self.activation_candidate(target_key, rte_id) for rte_id in route_ids]


class ResumableMolitCollector:
    def __init__(
        self,
        client: MolitRouteStopClient,
        stage: MolitRouteStopStage,
        *,
        max_pages: int = 100,
    ) -> None:
        self.client = client
        self.stage = stage
        self.max_pages = _bounded_int(max_pages, "max_pages", 1, MAX_PAGES)

    def collect(self, request: MolitRequest) -> dict[str, Any]:
        state = self.stage.ensure_target(request.next_page(1))
        fetched = 0
        while state["status"] in {"PENDING", "STAGING"}:
            if fetched >= self.max_pages:
                raise MolitLimitError("collection exceeds configured page budget")
            next_request = self.stage.next_request(request.target_key)
            if next_request is None:
                break
            page = self.client.fetch_page(next_request)
            state = self.stage.stage_page(page)
            fetched += 1
        return {**state, "pages_fetched_this_run": fetched}


class ResumableMolitRegionCollector:
    def __init__(
        self,
        client: MolitRouteStopClient,
        stage: MolitRegionBatchStage,
        *,
        max_pages: int = 100,
    ) -> None:
        self.client = client
        self.stage = stage
        self.max_pages = _bounded_int(max_pages, "max_pages", 1, MAX_PAGES)

    def collect(self, request: MolitRequest) -> dict[str, Any]:
        if not request.is_region_batch:
            raise MolitValidationError("regional collector request must omit RTE_ID")
        state = self.stage.ensure_target(request.next_page(1))
        fetched = 0
        while state["status"] in {"PENDING", "STAGING"}:
            if fetched >= self.max_pages:
                raise MolitLimitError("regional collection exceeds configured page budget")
            next_request = self.stage.next_request(request.target_key)
            if next_request is None:
                break
            page = self.client.fetch_page(next_request)
            state = self.stage.stage_page(page)
            fetched += 1
        return {**state, "pages_fetched_this_run": fetched}


def activate_candidates_preserving_newer(
    catalog: Any, candidates: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Activate valid candidates while isolating a bad route by bisection."""

    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    def submit(batch: list[Mapping[str, Any]]) -> None:
        if not batch:
            return
        try:
            outcome = catalog.hydrate_route_sequences_batch(
                batch, activation_policy="preserve_newer"
            )
        except CatalogError as exc:
            if len(batch) == 1:
                failures.append(
                    {
                        "city_code": str(batch[0]["city_code"]),
                        "route_id": str(batch[0]["route_id"]),
                        "error_code": type(exc).__name__,
                    }
                )
                return
            midpoint = len(batch) // 2
            submit(batch[:midpoint])
            submit(batch[midpoint:])
            return
        sequences = outcome.get("sequences")
        if not isinstance(sequences, list) or len(sequences) != len(batch):
            raise MolitProtocolError("NetworkCatalog returned an invalid activation result")
        results.extend(dict(sequence) for sequence in sequences)

    pending = list(candidates)
    for offset in range(0, len(pending), MAX_NETWORK_CATALOG_SEQUENCE_BATCH):
        submit(pending[offset : offset + MAX_NETWORK_CATALOG_SEQUENCE_BATCH])
    revisions = [int(result["revision"]) for result in results if "revision" in result]
    return {
        "route_count": len(pending),
        "processed": len(results),
        "created": sum(bool(result.get("created")) for result in results),
        "activated": sum(bool(result.get("activated")) for result in results),
        "skipped_older": sum(bool(result.get("skipped_older")) for result in results),
        "failed": len(failures),
        "activation_policy": "preserve_newer",
        "revision": max(revisions) if revisions else None,
        "sequences": results,
        "failures": failures,
    }


class NationwideMolitRegionCollector:
    """Resumable, budgeted nationwide orchestration over official SGG rows."""

    _COLLECTED_REGION_STATUSES = frozenset(
        {
            "EMPTY",
            "STAGED",
            "READY_FOR_ACTIVATION",
            "PARTIALLY_QUARANTINED",
            "QUARANTINED",
        }
    )

    def __init__(
        self,
        client: MolitRouteStopClient,
        stage: MolitRegionBatchStage,
        *,
        request_budget: int,
        max_pages_per_region: int = 100,
    ) -> None:
        self.client = client
        self.stage = stage
        self.request_budget = _bounded_int(
            request_budget, "request_budget", 1, MAX_REQUEST_BUDGET
        )
        if isinstance(client, MolitRouteStopClient):
            exact_limit = client.requests_attempted + self.request_budget
            if client.request_budget is None or client.request_budget > exact_limit:
                client.request_budget = exact_limit
        self.max_pages_per_region = _bounded_int(
            max_pages_per_region, "max_pages", 1, MAX_PAGES
        )

    @staticmethod
    def _run_key(
        *,
        opr_ymd: str,
        legal_source_sha256: str,
        page_size: int,
        regions: Sequence[MolitRegionCode],
    ) -> str:
        canonical = json.dumps(
            [
                _operation_date(opr_ymd),
                legal_source_sha256,
                page_size,
                [region.legal_dong_code for region in regions],
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return "molit_nationwide_" + hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest()[:24]

    def _ensure_run(
        self,
        *,
        regions: Sequence[MolitRegionCode],
        requests: Sequence[MolitRequest],
        opr_ymd: str,
        legal_source_sha256: str,
        page_size: int,
    ) -> str:
        if len(regions) != len(requests) or not regions:
            raise MolitValidationError("nationwide regions and requests are inconsistent")
        run_key = self._run_key(
            opr_ymd=opr_ymd,
            legal_source_sha256=legal_source_sha256,
            page_size=page_size,
            regions=regions,
        )
        now = self.stage._now()
        with closing(self.stage.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO molit_nationwide_runs "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_key,
                    _operation_date(opr_ymd),
                    legal_source_sha256,
                    page_size,
                    len(regions),
                    "PENDING",
                    0,
                    0,
                    0,
                    0,
                    0,
                    now,
                ),
            )
            for index, (region, request) in enumerate(
                zip(regions, requests, strict=True)
            ):
                if not request.is_region_batch:
                    raise MolitValidationError(
                        "nationwide requests must omit RTE_ID"
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO molit_nationwide_regions "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_key,
                        index,
                        region.legal_dong_code,
                        region.ctpv_cd,
                        region.sgg_cd,
                        region.name,
                        request.target_key,
                        "PENDING",
                        0,
                        0,
                        "",
                        "",
                        now,
                    ),
                )
            run = connection.execute(
                "SELECT * FROM molit_nationwide_runs WHERE run_key=?", (run_key,)
            ).fetchone()
            registered = connection.execute(
                "SELECT COUNT(*) FROM molit_nationwide_regions WHERE run_key=?",
                (run_key,),
            ).fetchone()[0]
            if (
                int(run["total_regions"]) != len(regions)
                or int(run["page_size"]) != page_size
                or int(registered) != len(regions)
            ):
                raise MolitValidationError("nationwide resume metadata is inconsistent")
            connection.commit()
        return run_key

    def _client_requests(self) -> int | None:
        value = getattr(self.client, "requests_attempted", None)
        return int(value) if isinstance(value, int) and value >= 0 else None

    def _add_network_requests(self, run_key: str, region_index: int, count: int) -> None:
        if count <= 0:
            return
        with closing(self.stage.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE molit_nationwide_runs SET network_requests=network_requests+?,"
                "updated_at=? WHERE run_key=?",
                (count, self.stage._now(), run_key),
            )
            connection.execute(
                "UPDATE molit_nationwide_regions "
                "SET network_requests=network_requests+?,updated_at=? "
                "WHERE run_key=? AND region_index=?",
                (count, self.stage._now(), run_key, region_index),
            )
            connection.commit()

    def _mark_region(
        self,
        run_key: str,
        region_index: int,
        status: str,
        *,
        start_attempt: bool = False,
        error: MolitIngestError | None = None,
    ) -> None:
        with closing(self.stage.connect()) as connection:
            connection.execute(
                "UPDATE molit_nationwide_regions SET status=?,attempts=attempts+?,"
                "error_code=?,error_message=?,updated_at=? "
                "WHERE run_key=? AND region_index=?",
                (
                    status,
                    int(start_attempt),
                    "" if error is None else type(error).__name__,
                    "",
                    self.stage._now(),
                    run_key,
                    region_index,
                ),
            )
            connection.commit()

    def _set_run_status(self, run_key: str, status: str) -> None:
        with closing(self.stage.connect()) as connection:
            connection.execute(
                "UPDATE molit_nationwide_runs SET status=?,updated_at=? WHERE run_key=?",
                (status, self.stage._now(), run_key),
            )
            connection.commit()

    @staticmethod
    def _route_digest(rows: Sequence[sqlite3.Row], route: sqlite3.Row) -> str:
        canonical = json.dumps(
            [
                route["rte_no"],
                route["rte_nm"],
                [
                    [
                        row["sttn_seq"],
                        row["sttn_id"],
                        row["sttn_nm"],
                        row["ctpv_cd"],
                        row["sgg_cd"],
                        row["emd_cd"],
                        row["ctpv_nm"],
                        row["sgg_nm"],
                        row["emd_nm"],
                        row["trfc_mns_se_cd"],
                    ]
                    for row in rows
                ],
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _reconcile_routes(
        self,
        run_key: str,
        *,
        catalog_resolved: bool,
        catalog_path: str | Path | None,
    ) -> dict[str, int]:
        catalog_route_owners: dict[str, set[str]] = {}
        if catalog_path is not None:
            with closing(
                self.stage._catalog_connection(Path(catalog_path).resolve())
            ) as official:
                has_routes = official.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='catalog_routes'"
                ).fetchone()
                active_source = official.execute(
                    "SELECT value FROM catalog_meta "
                    "WHERE key='active_routes_source_id'"
                ).fetchone()
                if has_routes is not None and active_source is not None:
                    for row in official.execute(
                        "SELECT city_code,route_id FROM catalog_routes "
                        "WHERE source_id=? ORDER BY city_code,route_id",
                        (active_source[0],),
                    ):
                        catalog_route_owners.setdefault(str(row["route_id"]), set()).add(
                            str(row["city_code"])
                        )
        with closing(self.stage.connect()) as connection:
            route_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT rr.rte_id FROM molit_region_routes rr "
                    "JOIN molit_nationwide_regions nr ON nr.target_key=rr.target_key "
                    "WHERE nr.run_key=? ORDER BY rr.rte_id",
                    (run_key,),
                )
            ]
            reconciled: list[tuple[Any, ...]] = []
            for rte_id in route_ids:
                occurrences = connection.execute(
                    "SELECT nr.region_index,nr.legal_dong_code,nr.sgg_cd,nr.target_key,"
                    "rr.rte_no,rr.rte_nm,rr.status AS route_status,"
                    "rr.row_count,rr.resolved_rows "
                    "FROM molit_nationwide_regions nr "
                    "JOIN molit_region_routes rr ON rr.target_key=nr.target_key "
                    "WHERE nr.run_key=? AND rr.rte_id=? ORDER BY nr.region_index",
                    (run_key, rte_id),
                ).fetchall()
                evidence: list[dict[str, Any]] = []
                digests: set[str] = set()
                resolved_city_codes: set[str] = set()
                resolved_namespace_complete = True
                for occurrence in occurrences:
                    rows = connection.execute(
                        "SELECT * FROM molit_region_rows "
                        "WHERE target_key=? AND rte_id=? ORDER BY sttn_seq",
                        (occurrence["target_key"], rte_id),
                    ).fetchall()
                    resolved_cities = connection.execute(
                        "SELECT city_code,COUNT(*) AS row_count "
                        "FROM molit_region_resolved_stops "
                        "WHERE target_key=? AND rte_id=? GROUP BY city_code "
                        "ORDER BY city_code",
                        (occurrence["target_key"], rte_id),
                    ).fetchall()
                    resolved_count = sum(
                        int(resolved["row_count"]) for resolved in resolved_cities
                    )
                    occurrence_city_codes = {
                        str(resolved["city_code"]) for resolved in resolved_cities
                    }
                    resolved_city_codes.update(occurrence_city_codes)
                    if (
                        occurrence["route_status"] != "READY_FOR_ACTIVATION"
                        or int(occurrence["resolved_rows"])
                        != int(occurrence["row_count"])
                        or resolved_count != int(occurrence["row_count"])
                    ):
                        resolved_namespace_complete = False
                    digest = self._route_digest(rows, occurrence)
                    digests.add(digest)
                    evidence.append(
                        {
                            "region_index": int(occurrence["region_index"]),
                            "legal_dong_code": occurrence["legal_dong_code"],
                            "sgg_cd": occurrence["sgg_cd"],
                            "target_key": occurrence["target_key"],
                            "sequence_sha256": digest,
                            "row_count": len(rows),
                            "route_status": occurrence["route_status"],
                            "resolved_catalog_city_codes": sorted(
                                occurrence_city_codes
                            ),
                        }
                    )
                owner = occurrences[0]
                exact_owner_codes = catalog_route_owners.get(rte_id, set())
                owner_city_code = owner["sgg_cd"]
                owner_basis = "lowest_query_sgg_no_exact_catalog_route"
                if len(exact_owner_codes) == 1:
                    owner_city_code = next(iter(exact_owner_codes))
                    owner_basis = "active_catalog_routes_exact_route_id"
                elif resolved_namespace_complete and len(resolved_city_codes) == 1:
                    owner_city_code = next(iter(resolved_city_codes))
                    owner_basis = "resolved_stop_namespace_exact"
                if len(exact_owner_codes) > 1:
                    status = "CONFLICT"
                    error_code = "CATALOG_ROUTE_ID_AMBIGUOUS"
                    sequence_sha256 = ""
                    owner_basis = "active_catalog_routes_ambiguous_route_id"
                elif len(digests) != 1:
                    status = "CONFLICT"
                    error_code = "CROSS_REGION_SEQUENCE_CONFLICT"
                    sequence_sha256 = ""
                elif not catalog_resolved:
                    status = "UNRESOLVED"
                    error_code = "CATALOG_RESOLUTION_NOT_RUN"
                    sequence_sha256 = next(iter(digests))
                elif any(
                    occurrence["route_status"] != "READY_FOR_ACTIVATION"
                    for occurrence in occurrences
                ):
                    status = "QUARANTINED"
                    error_code = "REGIONAL_ROUTE_NOT_READY"
                    sequence_sha256 = next(iter(digests))
                elif owner_basis == "lowest_query_sgg_no_exact_catalog_route":
                    status = "NAMESPACE_FALLBACK"
                    error_code = "OWNER_NAMESPACE_UNRESOLVED"
                    sequence_sha256 = next(iter(digests))
                else:
                    status = "CANDIDATE"
                    error_code = ""
                    sequence_sha256 = next(iter(digests))
                reconciled.append(
                    (
                        run_key,
                        rte_id,
                        status,
                        owner["target_key"],
                        owner_city_code,
                        owner_basis,
                        len(occurrences),
                        sequence_sha256,
                        json.dumps(
                            {
                                "owner_basis": owner_basis,
                                "owner_city_code": owner_city_code,
                                "owner_query_sgg": owner["sgg_cd"],
                                "exact_catalog_city_codes": sorted(exact_owner_codes),
                                "occurrences": evidence,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        error_code,
                    )
                )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM molit_nationwide_routes WHERE run_key=?", (run_key,)
            )
            connection.executemany(
                "INSERT INTO molit_nationwide_routes VALUES(?,?,?,?,?,?,?,?,?,?)",
                reconciled,
            )
            connection.commit()
        return {
            "routes": len(reconciled),
            "ready_candidates": sum(row[2] == "CANDIDATE" for row in reconciled),
            "conflict_routes": sum(row[2] == "CONFLICT" for row in reconciled),
            "quarantined_routes": sum(row[2] == "QUARANTINED" for row in reconciled),
            "fallback_owner_routes": sum(
                row[5] == "lowest_query_sgg_no_exact_catalog_route"
                for row in reconciled
            ),
            "deduplicated_occurrences": sum(max(0, int(row[6]) - 1) for row in reconciled),
        }

    def activation_candidates(self, run_key: str) -> list[dict[str, Any]]:
        with closing(self.stage.connect()) as connection:
            routes = connection.execute(
                "SELECT * FROM molit_nationwide_routes "
                "WHERE run_key=? AND status='CANDIDATE' "
                "ORDER BY owner_city_code,rte_id",
                (run_key,),
            ).fetchall()
        candidates: list[dict[str, Any]] = []
        for route in routes:
            candidate = self.stage.activation_candidate(
                route["owner_target_key"], route["rte_id"]
            )
            candidate["city_code"] = route["owner_city_code"]
            provenance = json.loads(candidate["source"])
            provenance.update(
                {
                    "occurrences": int(route["occurrence_count"]),
                    "owner_basis": {
                        "active_catalog_routes_exact_route_id": "catalog_exact",
                        "resolved_stop_namespace_exact": "resolved_stops_exact",
                    }.get(route["owner_basis"], "query_sgg"),
                    "owner_city": route["owner_city_code"],
                    "reconcile": route["sequence_sha256"][:16],
                }
            )
            candidate["source"] = _bounded_provenance(provenance)
            candidates.append(candidate)
        return candidates

    def _record_activation(
        self, run_key: str, activation: Mapping[str, Any]
    ) -> None:
        with closing(self.stage.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for result in activation["sequences"]:
                status = (
                    "SKIPPED_OLDER"
                    if result.get("skipped_older")
                    else "ACTIVATED"
                    if result.get("activated")
                    else "UNCHANGED"
                )
                connection.execute(
                    "UPDATE molit_nationwide_routes SET status=?,error_code='' "
                    "WHERE run_key=? AND rte_id=?",
                    (status, run_key, result["route_id"]),
                )
            for failure in activation["failures"]:
                connection.execute(
                    "UPDATE molit_nationwide_routes SET status='ACTIVATION_FAILED',"
                    "error_code=? WHERE run_key=? AND rte_id=?",
                    (failure["error_code"], run_key, failure["route_id"]),
                )
            connection.execute(
                "UPDATE molit_nationwide_runs SET activated=?,skipped_older=?,updated_at=? "
                "WHERE run_key=?",
                (
                    activation["activated"],
                    activation["skipped_older"],
                    self.stage._now(),
                    run_key,
                ),
            )
            connection.commit()

    def _summary(self, run_key: str, *, requests_this_run: int) -> dict[str, Any]:
        with closing(self.stage.connect()) as connection:
            run = dict(
                connection.execute(
                    "SELECT * FROM molit_nationwide_runs WHERE run_key=?", (run_key,)
                ).fetchone()
            )
            regions = [
                dict(row)
                for row in connection.execute(
                    "SELECT region_index,legal_dong_code,ctpv_cd,sgg_cd,region_name,"
                    "target_key,status,attempts,network_requests,error_code,error_message "
                    "FROM molit_nationwide_regions WHERE run_key=? ORDER BY region_index",
                    (run_key,),
                )
            ]
            route_counts = {
                str(row["status"]): int(row["count"])
                for row in connection.execute(
                    "SELECT status,COUNT(*) AS count FROM molit_nationwide_routes "
                    "WHERE run_key=? GROUP BY status ORDER BY status",
                    (run_key,),
                )
            }
            fallback_owner_routes = int(
                connection.execute(
                    "SELECT COUNT(*) FROM molit_nationwide_routes "
                    "WHERE run_key=? AND owner_basis=?",
                    (run_key, "lowest_query_sgg_no_exact_catalog_route"),
                ).fetchone()[0]
            )
            empty_regions = sum(region["status"] == "EMPTY" for region in regions)
        region_counts: dict[str, int] = {}
        for region in regions:
            region_counts[region["status"]] = region_counts.get(region["status"], 0) + 1
        warnings: list[str] = []
        if fallback_owner_routes:
            warnings.append(
                "OWNER_NAMESPACE_FALLBACK_BLOCKED_FROM_ACTIVATION"
            )
        if empty_regions:
            warnings.append(
                "EMPTY_REGIONS_REQUIRE_EXACT_STATIC_NAMESPACE_CROSSCHECK"
            )
        return {
            **run,
            "requests_this_run": requests_this_run,
            "region_status_counts": region_counts,
            "route_status_counts": route_counts,
            "fallback_owner_routes": fallback_owner_routes,
            "empty_regions": empty_regions,
            "completeness_warnings": warnings,
            "regions": regions,
        }

    def collect(
        self,
        regions: Sequence[MolitRegionCode],
        *,
        opr_ymd: str,
        page_size: int,
        legal_source_sha256: str,
        catalog_path: str | Path | None = None,
        activate: bool = False,
        catalog_factory: Callable[[Path], Any] = NetworkCatalog,
    ) -> dict[str, Any]:
        page_size = _bounded_int(page_size, "numOfRows", 1, MAX_PAGE_SIZE)
        if activate and catalog_path is None:
            raise MolitValidationError("nationwide activation requires a catalog database")
        requests = tuple(
            MolitRequest(
                opr_ymd=opr_ymd,
                rte_id=None,
                ctpv_cd=region.ctpv_cd,
                sgg_cd=region.sgg_cd,
                num_of_rows=page_size,
            )
            for region in regions
        )
        run_key = self._ensure_run(
            regions=regions,
            requests=requests,
            opr_ymd=opr_ymd,
            legal_source_sha256=legal_source_sha256,
            page_size=page_size,
        )
        requests_this_run = 0
        with closing(self.stage.connect()) as connection:
            pending = connection.execute(
                "SELECT * FROM molit_nationwide_regions WHERE run_key=? "
                "AND status NOT IN ('EMPTY','STAGED','READY_FOR_ACTIVATION',"
                "'PARTIALLY_QUARANTINED','QUARANTINED') ORDER BY region_index",
                (run_key,),
            ).fetchall()
        budget_exhausted = False
        systemic_status: str | None = None
        for region_state in pending:
            if requests_this_run >= self.request_budget:
                budget_exhausted = True
                break
            index = int(region_state["region_index"])
            request = requests[index]
            self._mark_region(run_key, index, "RUNNING", start_attempt=True)
            state = self.stage.ensure_target(request)
            pages = 0
            region_failed = False
            while state["status"] in {"PENDING", "STAGING"}:
                if pages >= self.max_pages_per_region:
                    error = MolitLimitError(
                        "regional collection exceeds configured page budget"
                    )
                    self._mark_region(run_key, index, "FAILED", error=error)
                    region_failed = True
                    break
                if requests_this_run >= self.request_budget:
                    budget_exhausted = True
                    break
                next_request = self.stage.next_request(request.target_key)
                if next_request is None:
                    break
                before = self._client_requests()
                try:
                    page = self.client.fetch_page(next_request)
                except MolitRequestBudgetExhausted:
                    after = self._client_requests()
                    used = 0 if before is not None and after is not None else 0
                    if before is not None and after is not None:
                        used = max(0, after - before)
                    self._add_network_requests(run_key, index, used)
                    requests_this_run += used
                    budget_exhausted = True
                    break
                except MolitFatalUpstreamError as exc:
                    after = self._client_requests()
                    used = (
                        max(1, after - before)
                        if before is not None and after is not None
                        else 1
                    )
                    self._add_network_requests(run_key, index, used)
                    requests_this_run += used
                    self._mark_region(
                        run_key, index, "PAUSED_SYSTEMIC", error=exc
                    )
                    systemic_status = (
                        "UPSTREAM_QUOTA_EXHAUSTED"
                        if isinstance(exc, MolitQuotaError)
                        else "UPSTREAM_AUTH_BLOCKED"
                    )
                    break
                except MolitIngestError as exc:
                    after = self._client_requests()
                    used = (
                        max(1, after - before)
                        if before is not None and after is not None
                        else 1
                    )
                    self._add_network_requests(run_key, index, used)
                    requests_this_run += used
                    self._mark_region(run_key, index, "FAILED", error=exc)
                    region_failed = True
                    break
                after = self._client_requests()
                used = (
                    max(1, after - before)
                    if before is not None and after is not None
                    else 1
                )
                self._add_network_requests(run_key, index, used)
                requests_this_run += used
                try:
                    state = self.stage.stage_page(page)
                except MolitIngestError as exc:
                    self._mark_region(run_key, index, "FAILED", error=exc)
                    region_failed = True
                    break
                pages += 1
            if systemic_status is not None:
                break
            if budget_exhausted:
                self._mark_region(run_key, index, state["status"])
                break
            if not region_failed:
                self._mark_region(run_key, index, state["status"])

        if systemic_status is not None:
            self._set_run_status(run_key, systemic_status)
            return self._summary(run_key, requests_this_run=requests_this_run)

        with closing(self.stage.connect()) as connection:
            unfinished = connection.execute(
                "SELECT COUNT(*) FROM molit_nationwide_regions WHERE run_key=? "
                "AND status NOT IN ('EMPTY','STAGED','READY_FOR_ACTIVATION',"
                "'PARTIALLY_QUARANTINED','QUARANTINED')",
                (run_key,),
            ).fetchone()[0]
            failures = connection.execute(
                "SELECT COUNT(*) FROM molit_nationwide_regions "
                "WHERE run_key=? AND status='FAILED'",
                (run_key,),
            ).fetchone()[0]
        if unfinished:
            status = "BUDGET_EXHAUSTED" if budget_exhausted else "PARTIAL_FAILURE"
            self._set_run_status(run_key, status)
            return self._summary(run_key, requests_this_run=requests_this_run)
        if failures:
            self._set_run_status(run_key, "PARTIAL_FAILURE")
            return self._summary(run_key, requests_this_run=requests_this_run)

        resolution_failed = False
        if catalog_path is not None:
            with closing(self.stage.connect()) as connection:
                collected_regions = connection.execute(
                    "SELECT region_index,target_key,status FROM molit_nationwide_regions "
                    "WHERE run_key=? AND status!='EMPTY' ORDER BY region_index",
                    (run_key,),
                ).fetchall()
            for collected in collected_regions:
                try:
                    resolution = self.stage.resolve_against_catalog(
                        collected["target_key"], catalog_path
                    )
                except MolitIngestError as exc:
                    self._mark_region(
                        run_key,
                        int(collected["region_index"]),
                        "RESOLUTION_FAILED",
                        error=exc,
                    )
                    resolution_failed = True
                    continue
                self._mark_region(
                    run_key,
                    int(collected["region_index"]),
                    resolution["status"],
                )

        reconciliation = self._reconcile_routes(
            run_key,
            catalog_resolved=catalog_path is not None and not resolution_failed,
            catalog_path=catalog_path,
        )
        with closing(self.stage.connect()) as connection:
            connection.execute(
                "UPDATE molit_nationwide_runs SET ready_candidates=?,conflict_routes=?,"
                "updated_at=? WHERE run_key=?",
                (
                    reconciliation["ready_candidates"],
                    reconciliation["conflict_routes"],
                    self.stage._now(),
                    run_key,
                ),
            )
            connection.commit()
        if resolution_failed:
            self._set_run_status(run_key, "RESOLUTION_PARTIAL_FAILURE")
            return self._summary(run_key, requests_this_run=requests_this_run)
        if catalog_path is None:
            self._set_run_status(run_key, "COLLECTED_UNRESOLVED")
            return self._summary(run_key, requests_this_run=requests_this_run)
        if not activate:
            self._set_run_status(
                run_key,
                "READY_FOR_ACTIVATION_WITH_ISSUES"
                if reconciliation["conflict_routes"]
                or reconciliation["quarantined_routes"]
                or reconciliation["fallback_owner_routes"]
                or any(
                    region["status"] == "EMPTY"
                    for region in self._summary(
                        run_key, requests_this_run=requests_this_run
                    )["regions"]
                )
                else "READY_FOR_ACTIVATION",
            )
            return self._summary(run_key, requests_this_run=requests_this_run)

        candidates = self.activation_candidates(run_key)
        catalog = catalog_factory(Path(catalog_path).resolve())
        activation = activate_candidates_preserving_newer(catalog, candidates)
        self._record_activation(run_key, activation)
        final_status = (
            "ACTIVATION_PARTIAL_FAILURE"
            if activation["failed"]
            else "INCOMPLETE_WITH_QUARANTINE"
            if reconciliation["conflict_routes"]
            or reconciliation["quarantined_routes"]
            else "INCOMPLETE_WITH_NAMESPACE_FALLBACK"
            if reconciliation["fallback_owner_routes"]
            else "INCOMPLETE_WITH_EMPTY_REGIONS"
            if self._summary(
                run_key, requests_this_run=requests_this_run
            )["empty_regions"]
            else "COMPLETE"
        )
        self._set_run_status(run_key, final_status)
        return {
            **self._summary(run_key, requests_this_run=requests_this_run),
            "activation": {
                key: value
                for key, value in activation.items()
                if key not in {"sequences", "failures"}
            },
        }


def _service_key_from_stdin() -> str:
    key = (
        getpass.getpass("Official API service key: ")
        if sys.stdin.isatty()
        else sys.stdin.readline().rstrip("\r\n")
    )
    if not key:
        raise MolitValidationError("service key was not provided")
    return key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or stage MOLIT route-specific or regional stop pages"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_const", const="validate", dest="mode")
    mode.add_argument("--probe", action="store_const", const="probe", dest="mode")
    mode.add_argument("--collect", action="store_const", const="collect", dest="mode")
    parser.set_defaults(mode="validate")
    parser.add_argument("--opr-ymd", required=True)
    parser.add_argument("--rte-id")
    parser.add_argument("--ctpv-cd")
    parser.add_argument("--sgg-cd")
    parser.add_argument(
        "--legal-dong-codes",
        type=Path,
        help="validate-only nationwide SGG enumeration from official ZIP/TSV",
    )
    parser.add_argument("--page-size", type=int, default=MAX_PAGE_SIZE)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--max-total-rows", type=int, default=100_000)
    parser.add_argument("--request-budget", type=int, default=1_000)
    parser.add_argument("--requests-per-second", type=float, default=2.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--stage-db", type=Path)
    parser.add_argument("--catalog-db", type=Path)
    parser.add_argument(
        "--activate",
        action="store_true",
        help="activate reconciled nationwide candidates with preserve_newer",
    )
    parser.add_argument("--service-key-stdin", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[..., MolitRouteStopClient] = MolitRouteStopClient,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        max_pages = _bounded_int(args.max_pages, "max_pages", 1, MAX_PAGES)
        max_total_rows = _bounded_int(
            args.max_total_rows, "max_total_rows", 2, MAX_TOTAL_ROWS
        )
        _bounded_float(args.requests_per_second, "requests_per_second", 0.1, MAX_RPS)
        _bounded_int(args.retries, "retries", 0, MAX_RETRIES)
        _bounded_float(args.timeout_seconds, "timeout_seconds", 0.5, MAX_TIMEOUT_SECONDS)
        page_size = _bounded_int(args.page_size, "numOfRows", 1, MAX_PAGE_SIZE)
        request_budget = _bounded_int(
            args.request_budget, "request_budget", 1, MAX_REQUEST_BUDGET
        )
        if args.legal_dong_codes is not None:
            if args.mode == "probe":
                raise MolitValidationError(
                    "legal-dong nationwide mode supports validate-only or collect"
                )
            if args.rte_id is not None or args.ctpv_cd is not None or args.sgg_cd is not None:
                raise MolitValidationError(
                    "legal-dong enumeration cannot be combined with one target"
                )
            regions = load_active_sgg_codes(args.legal_dong_codes)
            requests = build_region_batch_requests(
                args.legal_dong_codes,
                opr_ymd=args.opr_ymd,
                page_size=page_size,
            )
            legal_hash = legal_dong_data_sha256(args.legal_dong_codes)
            if args.mode == "validate":
                if args.stage_db is not None or args.catalog_db is not None:
                    raise MolitValidationError(
                        "validate-only mode does not open staging or catalog databases"
                    )
                if args.activate:
                    raise MolitValidationError("validate-only mode cannot activate routes")
                print(
                    json.dumps(
                        {
                            "status": "VALID",
                            "target_mode": "nationwide_region_enumeration",
                            "network_called": False,
                            "database_written": False,
                            "endpoint": ENDPOINT,
                            "legal_data_sha256": legal_hash,
                            "region_count": len(regions),
                            "enumeration_policy": "current_sgg_only_no_legacy_fallback",
                            "legacy_codes_included": False,
                            "regions": [
                                {
                                    **asdict(region),
                                    "target_key": request.target_key,
                                    "parameters": request.public_parameters(),
                                }
                                for region, request in zip(
                                    regions, requests, strict=True
                                )
                            ],
                            "bounds": {
                                "max_pages": max_pages,
                                "max_total_rows": max_total_rows,
                                "request_budget": request_budget,
                                "requests_per_second": args.requests_per_second,
                                "retries": args.retries,
                                "timeout_seconds": args.timeout_seconds,
                            },
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0
            if not args.service_key_stdin:
                raise MolitValidationError(
                    "live modes require --service-key-stdin; keys are not accepted in argv"
                )
            if args.stage_db is None:
                raise MolitValidationError(
                    "nationwide collect mode requires --stage-db"
                )
            if (
                args.catalog_db is not None
                and args.catalog_db.resolve() == args.stage_db.resolve()
            ):
                raise MolitValidationError(
                    "staging and NetworkCatalog paths must differ"
                )
            if args.activate and args.catalog_db is None:
                raise MolitValidationError(
                    "nationwide activation requires --catalog-db"
                )
            key = _service_key_from_stdin()
            client = client_factory(
                key,
                requests_per_second=args.requests_per_second,
                retries=args.retries,
                timeout_seconds=args.timeout_seconds,
                request_budget=request_budget,
            )
            stage = MolitRegionBatchStage(
                args.stage_db, max_total_rows=max_total_rows
            )
            result = NationwideMolitRegionCollector(
                client,
                stage,
                request_budget=request_budget,
                max_pages_per_region=max_pages,
            ).collect(
                regions,
                opr_ymd=args.opr_ymd,
                page_size=page_size,
                legal_source_sha256=legal_hash,
                catalog_path=args.catalog_db,
                activate=args.activate,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if args.activate:
            raise MolitValidationError(
                "--activate is supported only for nationwide legal-dong collect"
            )
        if args.ctpv_cd is None or args.sgg_cd is None:
            raise MolitValidationError(
                "one target requires --ctpv-cd and --sgg-cd"
            )
        request = MolitRequest(
            opr_ymd=args.opr_ymd,
            rte_id=args.rte_id,
            ctpv_cd=args.ctpv_cd,
            sgg_cd=args.sgg_cd,
            num_of_rows=page_size,
        )
        if args.mode == "validate":
            if args.stage_db is not None or args.catalog_db is not None:
                raise MolitValidationError(
                    "validate-only mode does not open staging or catalog databases"
                )
            print(
                json.dumps(
                    {
                        "status": "VALID",
                        "network_called": False,
                        "database_written": False,
                        "endpoint": ENDPOINT,
                        "target_mode": (
                            "region_batch"
                            if request.is_region_batch
                            else "route_specific"
                        ),
                        "parameters": request.public_parameters(),
                        "bounds": {
                            "max_pages": max_pages,
                            "max_total_rows": max_total_rows,
                            "requests_per_second": args.requests_per_second,
                            "retries": args.retries,
                            "timeout_seconds": args.timeout_seconds,
                        },
                        "coordinate_policy": "exact_active_official_stop_id_or_quarantine",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if not args.service_key_stdin:
            raise MolitValidationError(
                "live modes require --service-key-stdin; keys are not accepted in argv"
            )
        key = _service_key_from_stdin()
        client = client_factory(
            key,
            requests_per_second=args.requests_per_second,
            retries=args.retries,
            timeout_seconds=args.timeout_seconds,
        )
        if args.mode == "probe":
            page = client.fetch_page(request)
            print(
                json.dumps(
                    {
                        "status": "PROBE_OK",
                        "network_called": True,
                        "database_written": False,
                        "page_no": request.page_no,
                        "rows": len(page.rows),
                        "routes": len({row.rte_id for row in page.rows}),
                        "total_count": page.total_count,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.stage_db is None:
            raise MolitValidationError("collect mode requires --stage-db")
        if args.catalog_db is not None and args.catalog_db.resolve() == args.stage_db.resolve():
            raise MolitValidationError("staging and NetworkCatalog paths must differ")
        if request.is_region_batch:
            stage: MolitRouteStopStage | MolitRegionBatchStage = MolitRegionBatchStage(
                args.stage_db, max_total_rows=max_total_rows
            )
            result = ResumableMolitRegionCollector(
                client, stage, max_pages=max_pages
            ).collect(request)
        else:
            stage = MolitRouteStopStage(args.stage_db, max_total_rows=max_total_rows)
            result = ResumableMolitCollector(
                client, stage, max_pages=max_pages
            ).collect(request)
        if args.catalog_db is not None and result["status"] == "STAGED":
            resolution = stage.resolve_against_catalog(
                request.target_key, args.catalog_db
            )
            result = {**result, "resolution": resolution}
            if request.is_region_batch:
                result["activation_candidates"] = len(
                    stage.activation_candidates(request.target_key)
                )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except MolitIngestError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ENDPOINT",
    "MAX_PAGE_SIZE",
    "MAX_REQUEST_BUDGET",
    "MolitIngestError",
    "MolitAuthenticationError",
    "MolitFatalUpstreamError",
    "MolitLimitError",
    "MolitPage",
    "MolitProtocolError",
    "MolitQuotaError",
    "MolitRequestBudgetExhausted",
    "MolitRegionBatchStage",
    "MolitRegionCode",
    "MolitRequest",
    "MolitRouteStopClient",
    "MolitRouteStopStage",
    "MolitTransientUpstreamError",
    "MolitValidationError",
    "NationwideMolitRegionCollector",
    "ResumableMolitCollector",
    "ResumableMolitRegionCollector",
    "RouteStopRow",
    "activate_candidates_preserving_newer",
    "build_region_batch_requests",
    "build_parser",
    "load_active_sgg_codes",
    "legal_dong_data_sha256",
    "main",
    "parse_page",
]
