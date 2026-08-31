"""Strict, resumable collector for MOLIT route-specific stop data.

The official response has no coordinates. Rows are therefore staged in a
separate SQLite database and are never activation candidates until every
``STTN_ID`` resolves exactly against the active official ``catalog_stops``
source in a read-only :class:`NetworkCatalog` database. Unresolved or
ambiguous identifiers are quarantined; names are never used as a fuzzy join.

The CLI defaults to validation-only. Live access requires an explicit
``--probe`` or ``--collect`` mode and a key read from stdin/getpass. Service
keys are never accepted in argv, persisted, logged, or returned in summaries.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import getpass
import hashlib
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

from network_catalog import MAX_SEQUENCE_STOPS as MAX_NETWORK_CATALOG_SEQUENCE_STOPS


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


class MolitLimitError(MolitIngestError):
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


@dataclass(frozen=True, slots=True)
class MolitRequest:
    opr_ymd: str
    rte_id: str
    ctpv_cd: str
    sgg_cd: str
    page_no: int = 1
    num_of_rows: int = MAX_PAGE_SIZE

    def __post_init__(self) -> None:
        object.__setattr__(self, "opr_ymd", _operation_date(self.opr_ymd))
        object.__setattr__(
            self, "rte_id", _request_identifier(self.rte_id, "RTE_ID", 96)
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
        return {
            "pageNo": self.page_no,
            "numOfRows": self.num_of_rows,
            "OPR_YMD": self.opr_ymd,
            "RTE_ID": self.rte_id,
            "CTPV_CD": self.ctpv_cd,
            "SGG_CD": self.sgg_cd,
            "dataType": "JSON",
        }


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
class MolitPage:
    request: MolitRequest
    total_count: int
    rows: tuple[RouteStopRow, ...]


def _response_body(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    response = payload.get("response")
    if not isinstance(response, Mapping):
        raise MolitProtocolError("response object is missing")
    header = response.get("header")
    body = response.get("body")
    if not isinstance(header, Mapping) or not isinstance(body, Mapping):
        raise MolitProtocolError("response header/body is missing")
    code = _text(header.get("resultCode"), "resultCode", 64)
    if code not in _RESULT_OK:
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
    total_count = _bounded_int(body.get("totalCount"), "totalCount", 0, MAX_TOTAL_ROWS)
    page_no = _bounded_int(body.get("pageNo"), "pageNo", 1, MAX_PAGES)
    num_rows = _bounded_int(body.get("numOfRows"), "numOfRows", 1, MAX_PAGE_SIZE)
    if page_no != request.page_no or num_rows != request.num_of_rows:
        raise MolitProtocolError("official API pagination does not match the request")

    items = body.get("items")
    raw_items: Any = [] if items in (None, "") else items
    if isinstance(raw_items, Mapping):
        raw_items = raw_items.get("item", [])
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
    route_names: tuple[str, str] | None = None
    for index, item in enumerate(raw_items):
        if not isinstance(item, Mapping):
            raise MolitProtocolError(f"items[{index}] must be an object")
        missing = [field for field in RESPONSE_FIELDS if field not in item]
        if missing:
            raise MolitProtocolError(f"items[{index}] is missing {missing[0]}")
        sequence = _bounded_int(item["STTN_SEQ"], "STTN_SEQ", 1, 2_147_483_647)
        if previous_sequence is not None and sequence <= previous_sequence:
            raise MolitProtocolError("STTN_SEQ must be strictly increasing within a page")
        previous_sequence = sequence
        row = RouteStopRow(
            opr_ymd=_text(item["OPR_YMD"], "OPR_YMD", 8),
            rte_id=_text(item["RTE_ID"], "RTE_ID", 96),
            rte_no=_text(item["RTE_NO"], "RTE_NO", 100),
            rte_nm=_text(item["RTE_NM"], "RTE_NM", 150),
            sttn_seq=sequence,
            sttn_id=_text(item["STTN_ID"], "STTN_ID", 96),
            sttn_nm=_text(item["STTN_NM"], "STTN_NM", 150),
            ctpv_cd=_text(item["CTPV_CD"], "CTPV_CD", 2),
            sgg_cd=_text(item["SGG_CD"], "SGG_CD", 5),
            emd_cd=_text(item["EMD_CD"], "EMD_CD", 10, required=False),
            ctpv_nm=_text(item["CTPV_NM"], "CTPV_NM", 40),
            sgg_nm=_text(item["SGG_NM"], "SGG_NM", 40),
            emd_nm=_text(item["EMD_NM"], "EMD_NM", 40, required=False),
            trfc_mns_se_cd=_text(item["TRFC_MNS_SE_CD"], "TRFC_MNS_SE_CD", 1),
        )
        if row.opr_ymd != request.opr_ymd or row.rte_id != request.rte_id:
            raise MolitProtocolError("response row does not match the requested target")
        if not re.fullmatch(r"[0-9]{2}", row.ctpv_cd) or not re.fullmatch(
            r"[0-9]{5}", row.sgg_cd
        ):
            raise MolitProtocolError("response row has invalid regional codes")
        current_names = (row.rte_no, row.rte_nm)
        if route_names is None:
            route_names = current_names
        elif current_names != route_names:
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
        self._opener = opener or build_opener(_RejectRedirects(), ProxyHandler({}))
        self._sleep = sleeper
        self._monotonic = monotonic
        self._rate_lock = threading.Lock()
        self._last_request_at: float | None = None

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
            self._throttle()
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
            except HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt >= self.retries:
                    raise MolitProtocolError(
                        f"official API returned HTTP status {exc.code}"
                    ) from exc
            except URLError as exc:
                if attempt >= self.retries:
                    raise MolitProtocolError("official API request failed") from exc
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
                "SELECT r.sttn_seq,r.sttn_nm,m.node_id,m.node_name,m.latitude,m.longitude,"
                "m.source_id "
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
            "source": f"{PROVIDER}:{ENDPOINT}:stops={next(iter(source_ids))}",
            "captured_at": self._now(),
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
        description="Validate or stage MOLIT route-specific stop pages"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_const", const="validate", dest="mode")
    mode.add_argument("--probe", action="store_const", const="probe", dest="mode")
    mode.add_argument("--collect", action="store_const", const="collect", dest="mode")
    parser.set_defaults(mode="validate")
    parser.add_argument("--opr-ymd", required=True)
    parser.add_argument("--rte-id", required=True)
    parser.add_argument("--ctpv-cd", required=True)
    parser.add_argument("--sgg-cd", required=True)
    parser.add_argument("--page-size", type=int, default=MAX_PAGE_SIZE)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--max-total-rows", type=int, default=100_000)
    parser.add_argument("--requests-per-second", type=float, default=2.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--stage-db", type=Path)
    parser.add_argument("--catalog-db", type=Path)
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
        request = MolitRequest(
            opr_ymd=args.opr_ymd,
            rte_id=args.rte_id,
            ctpv_cd=args.ctpv_cd,
            sgg_cd=args.sgg_cd,
            num_of_rows=args.page_size,
        )
        max_pages = _bounded_int(args.max_pages, "max_pages", 1, MAX_PAGES)
        max_total_rows = _bounded_int(
            args.max_total_rows, "max_total_rows", 2, MAX_TOTAL_ROWS
        )
        _bounded_float(args.requests_per_second, "requests_per_second", 0.1, MAX_RPS)
        _bounded_int(args.retries, "retries", 0, MAX_RETRIES)
        _bounded_float(args.timeout_seconds, "timeout_seconds", 0.5, MAX_TIMEOUT_SECONDS)
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
        stage = MolitRouteStopStage(args.stage_db, max_total_rows=max_total_rows)
        result = ResumableMolitCollector(
            client, stage, max_pages=max_pages
        ).collect(request)
        if args.catalog_db is not None and result["status"] == "STAGED":
            result = {
                **result,
                "resolution": stage.resolve_against_catalog(
                    request.target_key, args.catalog_db
                ),
            }
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
    "MolitIngestError",
    "MolitLimitError",
    "MolitPage",
    "MolitProtocolError",
    "MolitRequest",
    "MolitRouteStopClient",
    "MolitRouteStopStage",
    "MolitValidationError",
    "ResumableMolitCollector",
    "RouteStopRow",
    "build_parser",
    "main",
    "parse_page",
]
