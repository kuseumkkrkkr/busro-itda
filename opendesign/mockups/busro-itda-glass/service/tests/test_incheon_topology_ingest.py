from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import unittest


SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

from incheon_topology_ingest import (  # noqa: E402
    INCHEON_COLUMNS,
    IncheonTopologyError,
    import_incheon_topology,
    prepare_incheon_topology,
)
from network_catalog import NetworkCatalog, STOP_COLUMNS  # noqa: E402


FIXED_NOW = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def csv_bytes(columns, rows, encoding="cp949"):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode(encoding)


def route_stop(**overrides):
    row = {
        "기준일자": "2025-12-31",
        "회사명": "(주)마니버스",
        "회사아이디": "168016",
        "노선번호": "1000",
        "노선아이디": "165000145",
        "순번": "1",
        "정류소명": "경남아너스빌",
        "정류소번호": "42099",
        "아이에스씨 아이디": "2800727",
        "정류소구간거리": "0",
        "정류소간누적거리": "0",
        "주요경유지여부": "N",
        "상_하행": "상행",
    }
    row.update(overrides)
    return row


def stop_catalog_row(**overrides):
    row = {
        "정류장번호": "ICB168000099",
        "정류장명": "경남아너스빌",
        "위도": "37.499133",
        "경도": "126.669479",
        "정보수집일": "2025-10-31",
        "모바일단축번호": "42099",
        "도시코드": "23",
        "도시명": "인천광역시",
        "관리도시명": "인천BIS",
    }
    row.update(overrides)
    return row


def simple_files(route_rows=None, stop_rows=None):
    routes = route_rows or [
        route_stop(),
        route_stop(
            **{
                "순번": "2",
                "정류소명": "거북시장",
                "정류소번호": "42136",
                "아이에스씨 아이디": "2800691",
                "정류소구간거리": "563",
                "정류소간누적거리": "563",
                "상_하행": "하행",
            }
        ),
    ]
    stops = stop_rows or [
        stop_catalog_row(),
        stop_catalog_row(
            **{
                "정류장번호": "ICB168000136",
                "정류장명": "거북시장",
                "위도": "37.502418",
                "경도": "126.672167",
                "모바일단축번호": "42136",
            }
        ),
    ]
    return csv_bytes(INCHEON_COLUMNS, routes), csv_bytes(STOP_COLUMNS, stops)


def prepare(route_rows=None, stop_rows=None, **overrides):
    route_data, stop_data = simple_files(route_rows, stop_rows)
    options = {
        "route_data": route_data,
        "stop_data": stop_data,
        "source_date": "2025-12-31",
        "expected_route_sha256": hashlib.sha256(route_data).hexdigest(),
        "expected_stop_sha256": hashlib.sha256(stop_data).hexdigest(),
    }
    options.update(overrides)
    return prepare_incheon_topology(**options)


class IncheonTopologyIngestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = NetworkCatalog(
            Path(self.temp.name) / "catalog.sqlite3", clock=lambda: FIXED_NOW
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_published_ids_and_directions_are_preserved_without_tago_prefix(self):
        prepared = prepare()
        self.assertEqual(prepared.summary["route_encoding"], "cp949")
        self.assertEqual(prepared.summary["candidate_route_count"], 1)
        candidate = prepared.candidates[0]
        self.assertEqual(candidate["city_code"], "23")
        self.assertEqual(candidate["route_id"], "165000145")
        self.assertEqual(
            [stop["node_id"] for stop in candidate["ordered_stops"]],
            ["2800727", "2800691"],
        )
        self.assertEqual(
            [stop["direction"] for stop in candidate["ordered_stops"]],
            ["상행", "하행"],
        )
        self.assertNotIn("ICB", candidate["route_id"])
        self.assertEqual(
            prepared.summary["identifier_policy"],
            "PUBLISHED_IDS_ONLY_NO_TAGO_PREFIX_INFERENCE",
        )

    def test_joint_operation_duplicates_are_removed_and_source_order_gaps_are_kept(self):
        rows = [
            route_stop(),
            route_stop(**{"회사아이디": "166011", "회사명": "미래교통"}),
            route_stop(
                **{
                    "순번": "3",
                    "정류소명": "거북시장",
                    "정류소번호": "42136",
                    "아이에스씨 아이디": "2800691",
                }
            ),
            route_stop(
                **{
                    "회사아이디": "166011",
                    "회사명": "미래교통",
                    "순번": "3",
                    "정류소명": "거북시장",
                    "정류소번호": "42136",
                    "아이에스씨 아이디": "2800691",
                }
            ),
        ]
        prepared = prepare(route_rows=rows)
        self.assertEqual(prepared.summary["route_source_row_count"], 4)
        self.assertEqual(prepared.summary["candidate_stop_count"], 2)
        self.assertEqual(prepared.summary["deduplicated_source_rows"], 2)
        self.assertEqual(prepared.summary["multi_company_routes"], 1)
        self.assertEqual(prepared.summary["source_order_gap_routes"], 1)
        self.assertEqual(
            [stop["node_order"] for stop in prepared.candidates[0]["ordered_stops"]],
            [1, 3],
        )

    def test_coordinate_join_is_exact_and_unresolved_coordinates_remain_absent(self):
        stops = [
            stop_catalog_row(
                **{
                    "정류장번호": "ICB168000099",
                    "정류장명": "다른 정류장",
                }
            ),
            stop_catalog_row(
                **{
                    "정류장번호": "ICB232000099",
                    "정류장명": "경남아너스빌",
                    "위도": "37.500000",
                    "경도": "126.670000",
                }
            ),
            stop_catalog_row(
                **{
                    "정류장번호": "ICB168000136",
                    "정류장명": "거북시장(구명칭)",
                    "위도": "37.502418",
                    "경도": "126.672167",
                    "모바일단축번호": "42136",
                }
            ),
            stop_catalog_row(
                **{
                    "정류장번호": "ICB232000136",
                    "정류장명": "다른 곳",
                    "위도": "37.600000",
                    "경도": "126.800000",
                    "모바일단축번호": "42136",
                }
            ),
        ]
        prepared = prepare(stop_rows=stops)
        first, second = prepared.candidates[0]["ordered_stops"]
        self.assertEqual((first["latitude"], first["longitude"]), (37.5, 126.67))
        self.assertNotIn("latitude", second)
        self.assertEqual(prepared.summary["coordinates_resolved"], 1)
        self.assertEqual(prepared.summary["coordinates_unresolved"], 1)

    def test_conflicting_topology_and_invalid_direction_are_rejected(self):
        conflict = [
            route_stop(),
            route_stop(**{"정류소명": "충돌"}),
            route_stop(**{"순번": "2"}),
        ]
        with self.assertRaises(IncheonTopologyError):
            prepare(route_rows=conflict)

        invalid_direction = [route_stop(), route_stop(**{"순번": "2", "상_하행": "왕복"})]
        with self.assertRaises(IncheonTopologyError):
            prepare(route_rows=invalid_direction)

    def test_hash_header_and_selected_coordinate_are_strict(self):
        route_data, stop_data = simple_files()
        with self.assertRaises(IncheonTopologyError):
            prepare_incheon_topology(
                route_data=route_data,
                stop_data=stop_data,
                source_date="2025-12-31",
                expected_route_sha256="0" * 64,
            )
        wrong_header = csv_bytes(INCHEON_COLUMNS[:-1], [])
        with self.assertRaises(IncheonTopologyError):
            prepare_incheon_topology(
                route_data=wrong_header,
                stop_data=stop_data,
                source_date="2025-12-31",
            )
        bad_stops = [
            stop_catalog_row(**{"위도": "51.0"}),
            stop_catalog_row(
                **{
                    "정류장번호": "ICB168000136",
                    "정류장명": "거북시장",
                    "모바일단축번호": "42136",
                }
            ),
        ]
        with self.assertRaises(IncheonTopologyError):
            prepare(stop_rows=bad_stops)

    def test_hard_single_point_coordinate_spike_is_suppressed(self):
        routes = [
            route_stop(),
            route_stop(
                **{
                    "순번": "2",
                    "정류소명": "멀리 잘못 결합된 정류장",
                    "정류소번호": "99998",
                    "아이에스씨 아이디": "2800998",
                }
            ),
            route_stop(
                **{
                    "순번": "3",
                    "정류소명": "인근 정류장",
                    "정류소번호": "99999",
                    "아이에스씨 아이디": "2800999",
                }
            ),
        ]
        stops = [
            stop_catalog_row(),
            stop_catalog_row(
                **{
                    "정류장번호": "ICB2800998",
                    "정류장명": "멀리 잘못 결합된 정류장",
                    "모바일단축번호": "99998",
                    "위도": "35.100000",
                    "경도": "129.000000",
                }
            ),
            stop_catalog_row(
                **{
                    "정류장번호": "ICB2800999",
                    "정류장명": "인근 정류장",
                    "모바일단축번호": "99999",
                    "위도": "37.500000",
                    "경도": "126.670000",
                }
            ),
        ]
        prepared = prepare(route_rows=routes, stop_rows=stops)
        middle = prepared.candidates[0]["ordered_stops"][1]
        self.assertNotIn("latitude", middle)
        self.assertEqual(prepared.summary["coordinate_spikes_suppressed"], 1)
        self.assertEqual(
            prepared.summary["coordinate_match_counts"]["SUPPRESSED_ROUTE_SPIKE"], 1
        )

    def test_import_uses_preserve_newer_and_reports_skipped_older(self):
        self.catalog.hydrate_route_sequence(
            city_code="23",
            route_id="165000145",
            ordered_stops=[
                {
                    "node_id": "NEW1",
                    "node_name": "신규1",
                    "node_order": 1,
                    "latitude": 37.5,
                    "longitude": 126.6,
                },
                {
                    "node_id": "NEW2",
                    "node_name": "신규2",
                    "node_order": 2,
                    "latitude": 37.51,
                    "longitude": 126.61,
                },
            ],
            source="newer",
            captured_at="2026-01-01T00:00:00Z",
        )
        route_data, stop_data = simple_files()
        result = import_incheon_topology(
            catalog=self.catalog,
            route_data=route_data,
            stop_data=stop_data,
            source_date="2025-12-31",
        )
        self.assertEqual(result["activation_policy"], "preserve_newer")
        self.assertEqual(result["activated"], 0)
        self.assertEqual(result["skipped_older"], 1)
        active = self.catalog.active_route_sequence_info(
            city_code="23", route_id="165000145"
        )
        self.assertEqual(active["captured_at"], "2026-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
