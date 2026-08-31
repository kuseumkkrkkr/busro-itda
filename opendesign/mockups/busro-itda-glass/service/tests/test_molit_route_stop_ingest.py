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


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from molit_route_stop_ingest import (  # noqa: E402
    MAX_PAGE_SIZE,
    MolitLimitError,
    MolitPage,
    MolitProtocolError,
    MolitRequest,
    MolitRouteStopClient,
    MolitRouteStopStage,
    MolitValidationError,
    ResumableMolitCollector,
    RouteStopRow,
    build_parser,
    main,
    parse_page,
)


def raw_row(sequence: int, stop_id: str, stop_name: str | None = None) -> dict[str, object]:
    return {
        "OPR_YMD": "20260831",
        "RTE_ID": "11110001",
        "RTE_NO": "100",
        "RTE_NM": "100번",
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

    def test_cross_boundary_stop_region_is_preserved_not_forced_to_query_region(self) -> None:
        cross_boundary = raw_row(1, "STOP_1")
        cross_boundary["CTPV_CD"] = "26"
        cross_boundary["SGG_CD"] = "26110"

        page = parse_page(payload([cross_boundary]), request())

        self.assertEqual(page.rows[0].ctpv_cd, "26")
        self.assertEqual(page.rows[0].sgg_cd, "26110")


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


if __name__ == "__main__":
    unittest.main()
