from __future__ import annotations

from contextlib import closing, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from molit_route_stop_ingest import (  # noqa: E402
    MAX_PAGE_SIZE,
    MolitLimitError,
    MolitPage,
    MolitProtocolError,
    MolitQuotaError,
    MolitRegionBatchStage,
    MolitRegionCode,
    MolitRequest,
    MolitRequestBudgetExhausted,
    MolitRouteStopClient,
    MolitRouteStopStage,
    MolitTransientUpstreamError,
    MolitValidationError,
    NationwideMolitRegionCollector,
    ResumableMolitCollector,
    ResumableMolitRegionCollector,
    RouteStopRow,
    activate_candidates_preserving_newer,
    build_region_batch_requests,
    build_parser,
    load_active_sgg_codes,
    main,
    parse_page,
)


def raw_row(
    sequence: int,
    stop_id: str,
    stop_name: str | None = None,
    *,
    rte_id: str = "11110001",
    rte_no: str = "100",
    rte_nm: str = "100번",
) -> dict[str, object]:
    return {
        "OPR_YMD": "20260831",
        "RTE_ID": rte_id,
        "RTE_NO": rte_no,
        "RTE_NM": rte_nm,
        "STTN_SEQ": sequence,
        "STTN_ID": stop_id,
        "STTN_NM": stop_name or stop_id,
        "CTPV_CD": "11",
        "SGG_CD": "11140",
        "EMD_CD": "1114015000",
        "CTPV_NM": "서울특별시",
        "SGG_NM": "중구",
        "EMD_NM": "을지로동",
        "TRFC_MNS_SE_CD": "B",
    }


def payload(
    rows: list[dict[str, object]],
    *,
    page_no: int = 1,
    page_size: int = 2,
    total_count: int | None = None,
    result_code: str = "00",
) -> bytes:
    return json.dumps(
        {
            "response": {
                "header": {"resultCode": result_code, "resultMsg": "NORMAL SERVICE."},
                "body": {
                    "items": {"item": rows} if rows else "",
                    "numOfRows": page_size,
                    "pageNo": page_no,
                    "totalCount": len(rows) if total_count is None else total_count,
                },
            }
        },
        ensure_ascii=False,
    ).encode("utf-8")


def request(page_no: int = 1, page_size: int = 2) -> MolitRequest:
    return MolitRequest(
        opr_ymd="20260831",
        rte_id="11110001",
        ctpv_cd="11",
        sgg_cd="11140",
        page_no=page_no,
        num_of_rows=page_size,
    )


def batch_request(page_no: int = 1, page_size: int = 3) -> MolitRequest:
    return MolitRequest(
        opr_ymd="20260831",
        rte_id=None,
        ctpv_cd="11",
        sgg_cd="11140",
        page_no=page_no,
        num_of_rows=page_size,
    )


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.body = body
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class FakeOpener:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def open(self, req, timeout: float):  # noqa: ANN001
        self.urls.append(req.full_url)
        return self.responses.pop(0)


class MolitParserCase(unittest.TestCase):
    def test_request_validates_calendar_codes_and_official_page_limit(self) -> None:
        self.assertEqual(request(page_size=MAX_PAGE_SIZE).num_of_rows, 1000)
        with self.assertRaisesRegex(MolitValidationError, "calendar date"):
            MolitRequest("20260230", "R1", "11", "11140")
        with self.assertRaisesRegex(MolitValidationError, "numOfRows"):
            request(page_size=1001)
        with self.assertRaisesRegex(MolitValidationError, "CTPV_CD"):
            MolitRequest("20260831", "R1", "1", "11140")
        with self.assertRaisesRegex(MolitValidationError, "RTE_ID"):
            MolitRequest("20260831", "", "11", "11140")

    def test_parser_accepts_official_json_and_single_item_object(self) -> None:
        decoded = json.loads(payload([raw_row(1, "STOP_1")]))
        decoded["response"]["body"]["items"]["item"] = raw_row(1, "STOP_1")
        page = parse_page(decoded, request())

        self.assertEqual(page.total_count, 1)
        self.assertEqual(page.rows[0].sttn_id, "STOP_1")
        self.assertEqual(page.rows[0].trfc_mns_se_cd, "B")

    def test_parser_rejects_target_mismatch_missing_fields_and_bad_order(self) -> None:
        mismatch = raw_row(1, "STOP_1")
        mismatch["RTE_ID"] = "OTHER"
        with self.assertRaisesRegex(MolitProtocolError, "requested target"):
            parse_page(payload([mismatch]), request())

        missing = raw_row(1, "STOP_1")
        del missing["STTN_ID"]
        with self.assertRaisesRegex(MolitProtocolError, "missing STTN_ID"):
            parse_page(payload([missing]), request())

        with self.assertRaisesRegex(MolitProtocolError, "strictly increasing"):
            parse_page(
                payload([raw_row(2, "STOP_2"), raw_row(1, "STOP_1")]),
                request(),
            )

    def test_parser_rejects_error_envelope_and_non_bus_rows(self) -> None:
        with self.assertRaisesRegex(MolitProtocolError, "result code 30"):
            parse_page(payload([], total_count=0, result_code="30"), request())
        wrong_mode = raw_row(1, "STOP_1")
        wrong_mode["TRFC_MNS_SE_CD"] = "S"
        with self.assertRaisesRegex(MolitProtocolError, "not a bus"):
            parse_page(payload([wrong_mode]), request())
        with self.assertRaises(MolitQuotaError):
            parse_page(payload([], total_count=0, result_code="22"), request())

        gateway_error = {
            "OpenAPI_ServiceResponse": {
                "cmmMsgHeader": {
                    "errMsg": "HTTP_ERROR",
                    "returnAuthMsg": "HTTP 에러",
                    "returnReasonCode": "04",
                }
            }
        }
        with self.assertRaisesRegex(MolitTransientUpstreamError, "04"):
            parse_page(gateway_error, request())

    def test_cross_boundary_stop_region_is_preserved_not_forced_to_query_region(self) -> None:
        cross_boundary = raw_row(1, "STOP_1")
        cross_boundary["CTPV_CD"] = "26"
        cross_boundary["SGG_CD"] = "26110"

        page = parse_page(payload([cross_boundary]), request())

        self.assertEqual(page.rows[0].ctpv_cd, "26")
        self.assertEqual(page.rows[0].sgg_cd, "26110")

    def test_parser_accepts_official_field_casing_without_guessing_aliases(self) -> None:
        decoded = json.loads(payload([raw_row(1, "STOP_1")]))
        response = decoded.pop("response")
        decoded["RESPONSE"] = response
        response["HEADER"] = response.pop("header")
        response["BODY"] = response.pop("body")
        response["HEADER"]["RESULTCODE"] = response["HEADER"].pop("resultCode")
        body = response["BODY"]
        body["TOTALCOUNT"] = body.pop("totalCount")
        body["PAGENO"] = body.pop("pageNo")
        body["NUMOFROWS"] = body.pop("numOfRows")
        body["ITEMS"] = body.pop("items")
        body["ITEMS"]["ITEM"] = {
            key.lower(): value for key, value in body["ITEMS"].pop("item")[0].items()
        }

        parsed = parse_page(decoded, request())

        self.assertEqual(parsed.rows[0].rte_id, "11110001")

        ambiguous = raw_row(1, "STOP_1")
        ambiguous["rte_id"] = ambiguous["RTE_ID"]
        with self.assertRaisesRegex(MolitProtocolError, "ambiguous casing"):
            parse_page(payload([ambiguous]), request())

        guessed = {key.lower(): value for key, value in raw_row(1, "STOP_1").items()}
        guessed["sttn-seq"] = guessed.pop("sttn_seq")
        with self.assertRaisesRegex(MolitProtocolError, "missing STTN_SEQ"):
            parse_page(payload([guessed]), request())

    def test_region_page_groups_routes_and_rejects_duplicate_route_sequence(self) -> None:
        rows = [
            raw_row(2, "A2", rte_id="A", rte_no="10", rte_nm="10번"),
            raw_row(1, "B1", rte_id="B", rte_no="20", rte_nm="20번"),
            raw_row(1, "A1", rte_id="A", rte_no="10", rte_nm="10번"),
        ]

        parsed = parse_page(payload(rows, page_size=3), batch_request())

        self.assertEqual({row.rte_id for row in parsed.rows}, {"A", "B"})
        duplicate = rows + [
            raw_row(1, "OTHER", rte_id="A", rte_no="10", rte_nm="10번")
        ]
        with self.assertRaisesRegex(MolitProtocolError, "duplicate \(RTE_ID, STTN_SEQ\)"):
            parse_page(
                payload(duplicate, page_size=4, total_count=4),
                batch_request(page_size=4),
            )

        changed = rows + [
            raw_row(3, "A3", rte_id="A", rte_no="CHANGED", rte_nm="10번")
        ]
        with self.assertRaisesRegex(MolitProtocolError, "number/name changed"):
            parse_page(
                payload(changed, page_size=4, total_count=4),
                batch_request(page_size=4),
            )


class MolitClientCase(unittest.TestCase):
    def test_client_uses_fixed_https_endpoint_and_bounded_encoded_key(self) -> None:
        opener = FakeOpener([FakeResponse(payload([raw_row(1, "STOP_1")]))])
        client = MolitRouteStopClient(
            "abc+/=",
            opener=opener,
            requests_per_second=30,
            retries=0,
            sleeper=lambda _seconds: None,
        )

        page = client.fetch_page(request())

        self.assertEqual(len(page.rows), 1)
        self.assertTrue(opener.urls[0].startswith("https://apis.data.go.kr/1613000/"))
        self.assertIn("serviceKey=abc%2B%2F%3D", opener.urls[0])
        self.assertIn("dataType=JSON", opener.urls[0])

    def test_client_stops_reading_after_response_byte_limit(self) -> None:
        opener = FakeOpener([FakeResponse(b"x" * 1025)])
        client = MolitRouteStopClient(
            "secret",
            opener=opener,
            max_response_bytes=1024,
            retries=0,
        )
        with self.assertRaisesRegex(MolitLimitError, "byte limit"):
            client.fetch_page(request())

    def test_encoded_service_key_is_not_double_encoded(self) -> None:
        opener = FakeOpener([FakeResponse(payload([raw_row(1, "STOP_1")]))])
        client = MolitRouteStopClient(
            "abc%2B%2F%3D",
            opener=opener,
            retries=0,
        )

        client.fetch_page(request())

        self.assertIn("serviceKey=abc%2B%2F%3D", opener.urls[0])
        self.assertNotIn("%252B", opener.urls[0])

    def test_region_request_uses_documented_lowercase_parameters_and_omits_rte_id(self) -> None:
        rows = [raw_row(1, "A1", rte_id="A", rte_no="10", rte_nm="10번")]
        opener = FakeOpener(
            [FakeResponse(payload(rows, page_size=3, total_count=1))]
        )
        client = MolitRouteStopClient("secret", opener=opener, retries=0)

        client.fetch_page(batch_request())

        url = opener.urls[0]
        self.assertIn("opr_ymd=20260831", url)
        self.assertIn("ctpv_cd=11", url)
        self.assertIn("sgg_cd=11140", url)
        self.assertNotIn("rte_id=", url)
        self.assertNotIn("RTE_ID=", url)

    def test_http_failure_does_not_chain_a_url_containing_the_service_key(self) -> None:
        class ErrorOpener:
            def open(self, req, timeout: float):  # noqa: ANN001
                raise HTTPError(req.full_url, 500, "failure", None, None)

        client = MolitRouteStopClient(
            "private-secret", opener=ErrorOpener(), retries=0
        )

        with self.assertRaises(MolitProtocolError) as caught:
            client.fetch_page(request())

        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("private-secret", str(caught.exception))

    def test_request_budget_counts_physical_retry_attempts(self) -> None:
        class ErrorOpener:
            def open(self, req, timeout: float):  # noqa: ANN001
                raise HTTPError(req.full_url, 500, "failure", None, None)

        client = MolitRouteStopClient(
            "secret",
            opener=ErrorOpener(),
            retries=2,
            request_budget=2,
            sleeper=lambda _seconds: None,
        )

        with self.assertRaisesRegex(
            MolitRequestBudgetExhausted, "request budget exhausted"
        ):
            client.fetch_page(request())

        self.assertEqual(client.requests_attempted, 2)

    def test_cli_has_no_service_key_value_argument_and_validate_is_side_effect_free(self) -> None:
        options = {option for action in build_parser()._actions for option in action.option_strings}
        self.assertNotIn("--service-key", options)
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(
                [
                    "--validate-only",
                    "--opr-ymd",
                    "20260831",
                    "--rte-id",
                    "11110001",
                    "--ctpv-cd",
                    "11",
                    "--sgg-cd",
                    "11140",
                ]
            )
        summary = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertFalse(summary["network_called"])
        self.assertFalse(summary["database_written"])
        self.assertNotIn("serviceKey", summary["parameters"])

    def test_probe_reads_key_from_stdin_and_returns_only_bounded_summary(self) -> None:
        seen_keys: list[str] = []

        class Client:
            def fetch_page(self, requested: MolitRequest) -> MolitPage:
                return parse_page(
                    payload(
                        [raw_row(1, "STOP_1")],
                        page_size=MAX_PAGE_SIZE,
                        total_count=1,
                    ),
                    requested,
                )

        def factory(key: str, **_kwargs: object) -> Client:
            seen_keys.append(key)
            return Client()

        output = io.StringIO()
        with patch("sys.stdin", io.StringIO("probe-secret\n")), redirect_stdout(output):
            status = main(
                [
                    "--probe",
                    "--service-key-stdin",
                    "--opr-ymd",
                    "20260831",
                    "--rte-id",
                    "11110001",
                    "--ctpv-cd",
                    "11",
                    "--sgg-cd",
                    "11140",
                ],
                client_factory=factory,
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(seen_keys, ["probe-secret"])
        self.assertEqual(summary["status"], "PROBE_OK")
        self.assertFalse(summary["database_written"])
        self.assertNotIn("probe-secret", output.getvalue())


class MolitStageCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.stage_path = Path(self.temp.name) / "molit-stage.sqlite3"
        self.catalog_path = Path(self.temp.name) / "catalog.sqlite3"
        self.stage = MolitRouteStopStage(self.stage_path, max_total_rows=100)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def page(page_no: int, rows: list[dict[str, object]], total: int = 3) -> MolitPage:
        return parse_page(
            payload(rows, page_no=page_no, page_size=2, total_count=total),
            request(page_no),
        )

    def create_catalog(self, stops: list[tuple[str, str, str, float, float]]) -> None:
        connection = sqlite3.connect(self.catalog_path)
        try:
            connection.executescript(
                """
                CREATE TABLE catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO catalog_meta VALUES('active_stops_source_id','official-source');
                CREATE TABLE catalog_stops(
                    source_id TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO catalog_stops VALUES('official-source',?,?,?,?,?)", stops
            )
            connection.commit()
        finally:
            connection.close()

    def test_staging_is_contiguous_idempotent_and_resumable(self) -> None:
        first = self.page(1, [raw_row(1, "S1"), raw_row(2, "S2")])
        second = self.page(2, [raw_row(3, "S3")])

        state = self.stage.stage_page(first)
        repeated = self.stage.stage_page(first)
        resumed = self.stage.next_request(first.request.target_key)
        complete = self.stage.stage_page(second)

        self.assertEqual(state["status"], "STAGING")
        self.assertEqual(repeated["staged_rows"], 2)
        self.assertEqual(resumed.page_no, 2)
        self.assertEqual(complete["status"], "STAGED")
        self.assertIsNone(self.stage.next_request(first.request.target_key))

        changed = MolitPage(
            request=first.request,
            total_count=first.total_count,
            rows=(
                replace(first.rows[0], sttn_nm="changed"),
                first.rows[1],
            ),
        )
        with self.assertRaisesRegex(MolitProtocolError, "changed across resume"):
            self.stage.stage_page(changed)

    def test_collector_resumes_from_next_unstaged_page(self) -> None:
        first = self.page(1, [raw_row(1, "S1"), raw_row(2, "S2")])
        second = self.page(2, [raw_row(3, "S3")])
        self.stage.stage_page(first)

        class Client:
            calls: list[int] = []

            def fetch_page(inner_self, requested: MolitRequest) -> MolitPage:
                inner_self.calls.append(requested.page_no)
                return second

        client = Client()
        result = ResumableMolitCollector(client, self.stage, max_pages=1).collect(request())

        self.assertEqual(client.calls, [2])
        self.assertEqual(result["status"], "STAGED")
        self.assertEqual(result["pages_fetched_this_run"], 1)

    def test_cross_page_stop_sequence_must_remain_strictly_increasing(self) -> None:
        first = self.page(1, [raw_row(3, "S3"), raw_row(4, "S4")])
        backwards = self.page(2, [raw_row(2, "S2")])
        self.stage.stage_page(first)

        with self.assertRaisesRegex(MolitProtocolError, "across pages"):
            self.stage.stage_page(backwards)

    def test_resolution_requires_exact_official_ids_and_quarantines_name_only_match(self) -> None:
        staged = self.page(
            1,
            [raw_row(1, "S1", "Exact"), raw_row(2, "MISSING", "Same Name")],
            total=2,
        )
        self.stage.stage_page(staged)
        self.create_catalog(
            [
                ("11140", "S1", "Exact", 37.56, 126.99),
                ("11140", "OTHER", "Same Name", 37.57, 127.00),
            ]
        )

        result = self.stage.resolve_against_catalog(
            staged.request.target_key, self.catalog_path
        )

        self.assertEqual(result["status"], "QUARANTINED")
        self.assertEqual(result["resolved_rows"], 1)
        self.assertEqual(result["quarantined_rows"], 1)
        with self.assertRaisesRegex(MolitValidationError, "not ready"):
            self.stage.activation_candidate(staged.request.target_key)
        with closing(sqlite3.connect(self.stage_path)) as connection:
            reason = connection.execute(
                "SELECT reason FROM molit_quarantine"
            ).fetchone()[0]
        self.assertEqual(reason, "STOP_ID_NOT_FOUND_IN_ACTIVE_OFFICIAL_CATALOG")

    def test_ready_candidate_copies_catalog_coordinates_without_synthesis(self) -> None:
        staged = self.page(
            1,
            [raw_row(1, "S1"), raw_row(2, "S2")],
            total=2,
        )
        self.stage.stage_page(staged)
        self.create_catalog(
            [
                ("11140", "S1", "Official One", 37.5001, 126.9001),
                ("11140", "S2", "Official Two", 37.5002, 126.9002),
            ]
        )

        resolved = self.stage.resolve_against_catalog(
            staged.request.target_key, self.catalog_path
        )
        candidate = self.stage.activation_candidate(staged.request.target_key)

        self.assertEqual(resolved["status"], "READY_FOR_ACTIVATION")
        self.assertEqual(candidate["captured_at"], "2026-08-31T00:00:00Z")
        self.assertEqual(candidate["city_code"], "11140")
        self.assertEqual(candidate["route_id"], "11110001")
        self.assertEqual(
            [stop["latitude"] for stop in candidate["ordered_stops"]],
            [37.5001, 37.5002],
        )
        self.assertEqual(
            [stop["node_name"] for stop in candidate["ordered_stops"]],
            ["Official One", "Official Two"],
        )


class MolitRegionStageCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.stage_path = Path(self.temp.name) / "molit-region-stage.sqlite3"
        self.catalog_path = Path(self.temp.name) / "catalog.sqlite3"
        self.stage = MolitRegionBatchStage(self.stage_path, max_total_rows=100)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def page(
        page_no: int,
        rows: list[dict[str, object]],
        *,
        total: int,
        page_size: int = 3,
    ) -> MolitPage:
        return parse_page(
            payload(
                rows,
                page_no=page_no,
                page_size=page_size,
                total_count=total,
            ),
            batch_request(page_no, page_size),
        )

    def create_catalog(self, stops: list[tuple[str, str, str, float, float]]) -> None:
        connection = sqlite3.connect(self.catalog_path)
        try:
            connection.executescript(
                """
                CREATE TABLE catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO catalog_meta VALUES('active_stops_source_id','official-source');
                CREATE TABLE catalog_stops(
                    source_id TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO catalog_stops VALUES('official-source',?,?,?,?,?)", stops
            )
            connection.commit()
        finally:
            connection.close()

    def test_regional_staging_groups_split_routes_and_resumes(self) -> None:
        first = self.page(
            1,
            [
                raw_row(1, "A1", rte_id="A", rte_no="10", rte_nm="10번"),
                raw_row(1, "B1", rte_id="B", rte_no="20", rte_nm="20번"),
                raw_row(2, "A2", rte_id="A", rte_no="10", rte_nm="10번"),
            ],
            total=5,
        )
        second = self.page(
            2,
            [
                raw_row(2, "B2", rte_id="B", rte_no="20", rte_nm="20번"),
                raw_row(3, "B3", rte_id="B", rte_no="20", rte_nm="20번"),
            ],
            total=5,
        )

        state = self.stage.stage_page(first)
        resumed = self.stage.next_request(first.request.target_key)

        class Client:
            calls: list[int] = []

            def fetch_page(inner_self, requested: MolitRequest) -> MolitPage:
                inner_self.calls.append(requested.page_no)
                return second

        client = Client()
        result = ResumableMolitRegionCollector(
            client, self.stage, max_pages=1
        ).collect(first.request)

        self.assertEqual(state["status"], "STAGING")
        self.assertEqual(resumed.page_no, 2)
        self.assertEqual(client.calls, [2])
        self.assertEqual(result["status"], "STAGED")
        with closing(sqlite3.connect(self.stage_path)) as connection:
            grouped = connection.execute(
                "SELECT rte_id,row_count,status FROM molit_region_routes ORDER BY rte_id"
            ).fetchall()
        self.assertEqual(grouped, [("A", 2, "STAGED"), ("B", 3, "STAGED")])

    def test_regional_staging_rejects_cross_page_duplicates_and_metadata_changes(self) -> None:
        first = self.page(
            1,
            [
                raw_row(1, "A1", rte_id="A", rte_no="10", rte_nm="10번"),
                raw_row(1, "B1", rte_id="B", rte_no="20", rte_nm="20번"),
            ],
            total=3,
            page_size=2,
        )
        self.stage.stage_page(first)
        duplicate = self.page(
            2,
            [raw_row(1, "A1-again", rte_id="A", rte_no="10", rte_nm="10번")],
            total=3,
            page_size=2,
        )
        with self.assertRaisesRegex(MolitProtocolError, "across regional pages"):
            self.stage.stage_page(duplicate)

        other_stage = MolitRegionBatchStage(
            Path(self.temp.name) / "metadata.sqlite3", max_total_rows=100
        )
        other_stage.stage_page(first)
        changed = self.page(
            2,
            [raw_row(2, "A2", rte_id="A", rte_no="11", rte_nm="11번")],
            total=3,
            page_size=2,
        )
        with self.assertRaisesRegex(MolitProtocolError, "metadata changed"):
            other_stage.stage_page(changed)

    def test_regional_resolution_quarantines_whole_unresolved_route(self) -> None:
        staged = self.page(
            1,
            [
                raw_row(1, "A1", rte_id="A", rte_no="10", rte_nm="10번"),
                raw_row(2, "A2", rte_id="A", rte_no="10", rte_nm="10번"),
                raw_row(1, "B1", "Same Name", rte_id="B", rte_no="20", rte_nm="20번"),
                raw_row(2, "MISSING", "Same Name", rte_id="B", rte_no="20", rte_nm="20번"),
            ],
            total=4,
            page_size=4,
        )
        self.stage.stage_page(staged)
        self.create_catalog(
            [
                ("11140", "A1", "Official A1", 37.50, 126.90),
                ("11140", "A2", "Official A2", 37.51, 126.91),
                ("11140", "B1", "Official B1", 37.52, 126.92),
                ("11140", "OTHER", "Same Name", 37.53, 126.93),
            ]
        )

        result = self.stage.resolve_against_catalog(
            staged.request.target_key, self.catalog_path
        )
        candidates = self.stage.activation_candidates(staged.request.target_key)

        self.assertEqual(result["status"], "PARTIALLY_QUARANTINED")
        self.assertEqual(result["ready_routes"], 1)
        self.assertEqual(result["quarantined_routes"], 1)
        self.assertEqual([candidate["route_id"] for candidate in candidates], ["A"])
        self.assertEqual(
            [stop["node_id"] for stop in candidates[0]["ordered_stops"]],
            ["A1", "A2"],
        )
        with self.assertRaisesRegex(MolitValidationError, "not ready"):
            self.stage.activation_candidate(staged.request.target_key, "B")
        with closing(sqlite3.connect(self.stage_path)) as connection:
            quarantine = connection.execute(
                "SELECT reason,unresolved_rows FROM molit_region_route_quarantine "
                "WHERE rte_id='B'"
            ).fetchone()
        self.assertEqual(quarantine, ("UNRESOLVED_OR_AMBIGUOUS_STOP_ID", 1))


class NationwideMolitRegionCollectorCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.stage_path = Path(self.temp.name) / "nationwide-stage.sqlite3"
        self.catalog_path = Path(self.temp.name) / "catalog.sqlite3"
        self.stage = MolitRegionBatchStage(self.stage_path, max_total_rows=100)
        self.regions = (
            MolitRegionCode("1111000000", "11", "11110", "서울특별시 종로구"),
            MolitRegionCode("1114000000", "11", "11140", "서울특별시 중구"),
            MolitRegionCode("2611000000", "26", "26110", "부산광역시 중구"),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def create_catalog(self, stop_ids: list[str], *, city_code: str = "11140") -> None:
        connection = sqlite3.connect(self.catalog_path)
        try:
            connection.executescript(
                """
                CREATE TABLE catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT INTO catalog_meta VALUES('active_stops_source_id','official-source');
                CREATE TABLE catalog_stops(
                    source_id TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO catalog_stops VALUES('official-source',?,?,?,?,?)",
                [
                    (
                        city_code,
                        stop_id,
                        f"Official {stop_id}",
                        37.5 + index / 1000,
                        126.9,
                    )
                    for index, stop_id in enumerate(stop_ids)
                ],
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def scenario_client(
        rows_by_sgg: dict[str, list[dict[str, object]]],
        *,
        failing: set[str] | None = None,
    ):
        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def fetch_page(inner_self, requested: MolitRequest) -> MolitPage:
                inner_self.calls.append(requested.sgg_cd)
                if requested.sgg_cd in (failing or set()):
                    raise MolitProtocolError("synthetic regional failure")
                rows = rows_by_sgg[requested.sgg_cd]
                return parse_page(
                    payload(
                        rows,
                        page_no=requested.page_no,
                        page_size=requested.num_of_rows,
                        total_count=len(rows),
                    ),
                    requested,
                )

        return Client()

    def test_request_budget_pauses_and_resume_skips_staged_regions(self) -> None:
        rows = {
            region.sgg_cd: [
                raw_row(
                    1,
                    f"{region.sgg_cd}-1",
                    rte_id=f"R-{region.sgg_cd}",
                    rte_no=region.sgg_cd,
                    rte_nm=region.name,
                ),
                raw_row(
                    2,
                    f"{region.sgg_cd}-2",
                    rte_id=f"R-{region.sgg_cd}",
                    rte_no=region.sgg_cd,
                    rte_nm=region.name,
                ),
            ]
            for region in self.regions[:2]
        }
        first_client = self.scenario_client(rows)
        first = NationwideMolitRegionCollector(
            first_client, self.stage, request_budget=1
        ).collect(
            self.regions[:2],
            opr_ymd="20260831",
            page_size=10,
            legal_source_sha256="a" * 64,
        )

        self.assertEqual(first["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(first["requests_this_run"], 1)
        self.assertEqual(first_client.calls, ["11110"])

        second_client = self.scenario_client(rows)
        second = NationwideMolitRegionCollector(
            second_client, self.stage, request_budget=2
        ).collect(
            self.regions[:2],
            opr_ymd="20260831",
            page_size=10,
            legal_source_sha256="a" * 64,
        )

        self.assertEqual(second["status"], "COLLECTED_UNRESOLVED")
        self.assertEqual(second_client.calls, ["11140"])
        self.assertEqual(second["region_status_counts"], {"STAGED": 2})
        self.assertEqual(second["route_status_counts"], {"UNRESOLVED": 2})

    def test_region_failure_is_isolated_and_only_failed_region_is_retried(self) -> None:
        rows = {
            region.sgg_cd: [
                raw_row(
                    1,
                    f"{region.sgg_cd}-1",
                    rte_id=f"R-{region.sgg_cd}",
                    rte_no=region.sgg_cd,
                    rte_nm=region.name,
                ),
                raw_row(
                    2,
                    f"{region.sgg_cd}-2",
                    rte_id=f"R-{region.sgg_cd}",
                    rte_no=region.sgg_cd,
                    rte_nm=region.name,
                ),
            ]
            for region in self.regions
        }
        failing_client = self.scenario_client(rows, failing={"11140"})
        first = NationwideMolitRegionCollector(
            failing_client, self.stage, request_budget=10
        ).collect(
            self.regions,
            opr_ymd="20260831",
            page_size=10,
            legal_source_sha256="b" * 64,
        )

        self.assertEqual(first["status"], "PARTIAL_FAILURE")
        self.assertEqual(
            first["region_status_counts"], {"STAGED": 2, "FAILED": 1}
        )
        failed_region = next(
            region for region in first["regions"] if region["status"] == "FAILED"
        )
        self.assertEqual(failed_region["error_code"], "MolitProtocolError")
        self.assertEqual(failed_region["error_message"], "")
        retry_client = self.scenario_client(rows)
        retry = NationwideMolitRegionCollector(
            retry_client, self.stage, request_budget=10
        ).collect(
            self.regions,
            opr_ymd="20260831",
            page_size=10,
            legal_source_sha256="b" * 64,
        )

        self.assertEqual(retry["status"], "COLLECTED_UNRESOLVED")
        self.assertEqual(retry_client.calls, ["11140"])
        self.assertEqual(retry["region_status_counts"], {"STAGED": 3})

    def test_systemic_quota_error_stops_before_the_next_region(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def fetch_page(inner_self, requested: MolitRequest) -> MolitPage:
                inner_self.calls.append(requested.sgg_cd)
                raise MolitQuotaError("synthetic quota exhaustion")

        client = Client()
        result = NationwideMolitRegionCollector(
            client, self.stage, request_budget=10
        ).collect(
            self.regions,
            opr_ymd="20260831",
            page_size=10,
            legal_source_sha256="q" * 64,
        )

        self.assertEqual(result["status"], "UPSTREAM_QUOTA_EXHAUSTED")
        self.assertEqual(client.calls, ["11110"])
        self.assertEqual(result["requests_this_run"], 1)
        self.assertEqual(
            result["region_status_counts"], {"PAUSED_SYSTEMIC": 1, "PENDING": 2}
        )

    def test_unique_resolved_stop_namespace_becomes_activation_owner(self) -> None:
        rows = {
            "11110": [
                raw_row(1, "S1", rte_id="LOCAL", rte_no="01", rte_nm="01번"),
                raw_row(2, "S2", rte_id="LOCAL", rte_no="01", rte_nm="01번"),
            ]
        }
        self.create_catalog(["S1", "S2"], city_code="11")
        client = self.scenario_client(rows)

        class Catalog:
            def __init__(self) -> None:
                self.candidates: list[dict[str, object]] = []

            def hydrate_route_sequences_batch(
                inner_self,
                sequences: list[dict[str, object]],
                *,
                activation_policy: str,
            ) -> dict[str, object]:
                self.assertEqual(activation_policy, "preserve_newer")
                inner_self.candidates.extend(sequences)
                return {
                    "sequences": [
                        {
                            "route_id": sequence["route_id"],
                            "revision": 1,
                            "created": True,
                            "activated": True,
                            "skipped_older": False,
                        }
                        for sequence in sequences
                    ]
                }

        catalog = Catalog()
        result = NationwideMolitRegionCollector(
            client, self.stage, request_budget=10
        ).collect(
            self.regions[:1],
            opr_ymd="20260831",
            page_size=10,
            legal_source_sha256="s" * 64,
            catalog_path=self.catalog_path,
            activate=True,
            catalog_factory=lambda _path: catalog,
        )

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["ready_candidates"], 1)
        self.assertEqual(result["fallback_owner_routes"], 0)
        self.assertEqual(len(catalog.candidates), 1)
        candidate = catalog.candidates[0]
        self.assertEqual(candidate["city_code"], "11")
        self.assertEqual(
            json.loads(candidate["source"])["owner_basis"],
            "resolved_stops_exact",
        )
        with closing(sqlite3.connect(self.stage_path)) as connection:
            owner = connection.execute(
                "SELECT status,owner_city_code,owner_basis "
                "FROM molit_nationwide_routes WHERE rte_id='LOCAL'"
            ).fetchone()
        self.assertEqual(owner, ("ACTIVATED", "11", "resolved_stop_namespace_exact"))

    def test_cross_region_dedup_conflict_and_preserve_newer_activation(self) -> None:
        rows = {
            "11110": [
                raw_row(1, "A1", rte_id="DUP", rte_no="10", rte_nm="10번"),
                raw_row(2, "A2", rte_id="DUP", rte_no="10", rte_nm="10번"),
                raw_row(1, "C1", rte_id="CONFLICT", rte_no="20", rte_nm="20번"),
                raw_row(2, "C2", rte_id="CONFLICT", rte_no="20", rte_nm="20번"),
            ],
            "11140": [
                raw_row(1, "A1", rte_id="DUP", rte_no="10", rte_nm="10번"),
                raw_row(2, "A2", rte_id="DUP", rte_no="10", rte_nm="10번"),
                raw_row(1, "C1", rte_id="CONFLICT", rte_no="20", rte_nm="20번"),
                raw_row(2, "C3", rte_id="CONFLICT", rte_no="20", rte_nm="20번"),
            ],
            "26110": [
                raw_row(1, "U1", rte_id="UNIQUE", rte_no="30", rte_nm="30번"),
                raw_row(2, "U2", rte_id="UNIQUE", rte_no="30", rte_nm="30번"),
            ],
        }
        self.create_catalog(["A1", "A2", "C1", "C2", "C3", "U1", "U2"])
        with closing(sqlite3.connect(self.catalog_path)) as connection:
            connection.executescript(
                """
                INSERT INTO catalog_meta VALUES('active_routes_source_id','routes-source');
                CREATE TABLE catalog_routes(
                    source_id TEXT NOT NULL,
                    city_code TEXT NOT NULL,
                    route_id TEXT NOT NULL
                );
                INSERT INTO catalog_routes VALUES('routes-source','11','DUP');
                INSERT INTO catalog_routes VALUES('routes-source','21','UNIQUE');
                """
            )
        client = self.scenario_client(rows)

        class Catalog:
            def __init__(self) -> None:
                self.calls: list[tuple[list[dict[str, object]], str]] = []

            def hydrate_route_sequences_batch(
                inner_self,
                sequences: list[dict[str, object]],
                *,
                activation_policy: str,
            ) -> dict[str, object]:
                inner_self.calls.append((sequences, activation_policy))
                results = []
                for sequence in sequences:
                    skipped = sequence["route_id"] == "DUP"
                    results.append(
                        {
                            "route_id": sequence["route_id"],
                            "revision": 7,
                            "created": True,
                            "activated": not skipped,
                            "skipped_older": skipped,
                        }
                    )
                return {"sequences": results}

        catalog = Catalog()
        result = NationwideMolitRegionCollector(
            client, self.stage, request_budget=10
        ).collect(
            self.regions,
            opr_ymd="20260831",
            page_size=10,
            legal_source_sha256="c" * 64,
            catalog_path=self.catalog_path,
            activate=True,
            catalog_factory=lambda _path: catalog,
        )

        self.assertEqual(result["status"], "INCOMPLETE_WITH_QUARANTINE")
        self.assertEqual(result["conflict_routes"], 1)
        self.assertEqual(result["ready_candidates"], 2)
        self.assertEqual(result["activation"]["activated"], 1)
        self.assertEqual(result["activation"]["skipped_older"], 1)
        self.assertEqual(len(catalog.calls), 1)
        activated_candidates, policy = catalog.calls[0]
        self.assertEqual(policy, "preserve_newer")
        self.assertEqual(
            {candidate["route_id"] for candidate in activated_candidates},
            {"DUP", "UNIQUE"},
        )
        duplicate = next(
            candidate
            for candidate in activated_candidates
            if candidate["route_id"] == "DUP"
        )
        self.assertEqual(duplicate["city_code"], "11")
        self.assertEqual(duplicate["captured_at"], "2026-08-31T00:00:00Z")
        provenance = json.loads(duplicate["source"])
        self.assertEqual(provenance["route_no"], "10")
        self.assertEqual(provenance["route_name"], "10번")
        self.assertEqual(provenance["owner_city"], "11")
        self.assertEqual(provenance["owner_basis"], "catalog_exact")
        self.assertEqual(provenance["occurrences"], 2)
        with closing(sqlite3.connect(self.stage_path)) as connection:
            conflict = connection.execute(
                "SELECT status,error_code,occurrence_count,evidence_json "
                "FROM molit_nationwide_routes WHERE rte_id='CONFLICT'"
            ).fetchone()
        self.assertEqual(conflict[:3], ("CONFLICT", "CROSS_REGION_SEQUENCE_CONFLICT", 2))
        evidence = json.loads(conflict[3])
        self.assertEqual(
            len(
                {
                    row["sequence_sha256"]
                    for row in evidence["occurrences"]
                }
            ),
            2,
        )

    def test_activation_helper_bisects_catalog_validation_failures(self) -> None:
        candidates = [
            {"city_code": "11", "route_id": "GOOD", "ordered_stops": [1, 2]},
            {"city_code": "11", "route_id": "BAD", "ordered_stops": [1, 2]},
            {"city_code": "11", "route_id": "GOOD2", "ordered_stops": [1, 2]},
        ]

        class Catalog:
            def hydrate_route_sequences_batch(
                self,
                sequences: list[dict[str, object]],
                *,
                activation_policy: str,
            ) -> dict[str, object]:
                from network_catalog import CatalogValidationError

                if any(sequence["route_id"] == "BAD" for sequence in sequences):
                    raise CatalogValidationError("bad route")
                return {
                    "sequences": [
                        {
                            "route_id": sequence["route_id"],
                            "revision": 1,
                            "created": True,
                            "activated": True,
                            "skipped_older": False,
                        }
                        for sequence in sequences
                    ]
                }

        result = activate_candidates_preserving_newer(Catalog(), candidates)

        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failures"][0]["route_id"], "BAD")
        self.assertEqual(result["activation_policy"], "preserve_newer")


class LegalDongEnumerationCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.tsv = Path(self.temp.name) / "legal.tsv"
        self.zip = Path(self.temp.name) / "legal.zip"
        rows = [
            "법정동코드\t법정동명\t폐지여부",
            "1100000000\t서울특별시\t존재",
            "1111000000\t서울특별시 종로구\t존재",
            "1111010100\t서울특별시 종로구 청운동\t존재",
            "2611000000\t부산광역시 중구\t폐지",
            "3611000000\t세종특별자치시\t존재",
            "5000000000\t제주특별자치도\t존재",
            "5011000000\t제주특별자치도 제주시\t존재",
        ]
        self.tsv.write_bytes(("\n".join(rows) + "\n").encode("cp949"))
        with zipfile.ZipFile(self.zip, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("법정동코드 전체자료.txt", self.tsv.read_bytes())

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_current_sgg_enumeration_excludes_provinces_and_keeps_sejong(self) -> None:
        direct = load_active_sgg_codes(self.tsv)
        zipped = load_active_sgg_codes(self.zip)

        self.assertEqual(direct, zipped)
        self.assertEqual(
            [region.sgg_cd for region in direct], ["11110", "36110", "50110"]
        )
        sejong = next(region for region in direct if region.sgg_cd == "36110")
        self.assertEqual(sejong.name, "세종특별자치시")
        requests = build_region_batch_requests(
            self.zip, opr_ymd="20260831", page_size=1000
        )
        self.assertTrue(all(requested.is_region_batch for requested in requests))
        self.assertTrue(
            all("rte_id" not in requested.public_parameters() for requested in requests)
        )

    def test_validate_only_can_enumerate_regions_without_network_or_database(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(
                [
                    "--validate-only",
                    "--opr-ymd",
                    "20260831",
                    "--legal-dong-codes",
                    str(self.zip),
                ]
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(summary["region_count"], 3)
        self.assertFalse(summary["network_called"])
        self.assertFalse(summary["database_written"])
        self.assertFalse(summary["legacy_codes_included"])
        self.assertEqual(
            [region["sgg_cd"] for region in summary["regions"]],
            ["11110", "36110", "50110"],
        )

    def test_nationwide_collect_cli_is_explicit_resumable_and_does_not_persist_key(self) -> None:
        seen: dict[str, object] = {}

        class Client:
            def fetch_page(self, requested: MolitRequest) -> MolitPage:
                return parse_page(
                    payload(
                        [],
                        page_no=requested.page_no,
                        page_size=requested.num_of_rows,
                        total_count=0,
                    ),
                    requested,
                )

        def factory(key: str, **kwargs: object) -> Client:
            seen["key"] = key
            seen["request_budget"] = kwargs["request_budget"]
            return Client()

        stage_path = Path(self.temp.name) / "nationwide.sqlite3"
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO("nationwide-secret\n")), redirect_stdout(
            output
        ):
            status = main(
                [
                    "--collect",
                    "--service-key-stdin",
                    "--opr-ymd",
                    "20260831",
                    "--legal-dong-codes",
                    str(self.zip),
                    "--stage-db",
                    str(stage_path),
                    "--request-budget",
                    "10",
                ],
                client_factory=factory,
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(summary["status"], "COLLECTED_UNRESOLVED")
        self.assertEqual(summary["region_status_counts"], {"EMPTY": 3})
        self.assertEqual(seen, {"key": "nationwide-secret", "request_budget": 10})
        self.assertNotIn("nationwide-secret", output.getvalue())
        self.assertNotIn(b"nationwide-secret", stage_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
