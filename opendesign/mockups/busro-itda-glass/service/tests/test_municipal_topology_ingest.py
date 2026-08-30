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

from journey_planner import JourneyPlanner  # noqa: E402
from municipal_topology_ingest import (  # noqa: E402
    CHUNCHEON_COLUMNS,
    MunicipalTopologyError,
    import_municipal_topology_csv,
)
from network_catalog import (  # noqa: E402
    CatalogLimitError,
    NetworkCatalog,
    STOP_COLUMNS,
)


FIXED_NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


def csv_bytes(columns, rows, encoding="cp949"):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode(encoding)


def route_stop(**overrides):
    row = {
        "노선번호": "1",
        "노선": "250000100",
        "정류장순서": "1",
        "정류장": "265000793",
        "정류장명": "상공회의소",
        "경도": "127.75267",
        "위도": "37.89832",
        "데이터기준일": "2026-03-26",
    }
    row.update(overrides)
    return row


def import_rows(catalog, rows, **overrides):
    data = csv_bytes(CHUNCHEON_COLUMNS, rows)
    options = {
        "catalog": catalog,
        "data": data,
        "profile_name": "chuncheon",
        "source_date": "2026-03-26",
        "expected_sha256": hashlib.sha256(data).hexdigest(),
    }
    options.update(overrides)
    return import_municipal_topology_csv(**options)


class MunicipalTopologyIngestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = NetworkCatalog(
            Path(self.temp.name) / "catalog.sqlite3", clock=lambda: FIXED_NOW
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def simple_route():
        return [
            route_stop(),
            route_stop(
                **{
                    "정류장순서": "2",
                    "정류장": "250026779",
                    "정류장명": "장학해온채A",
                    "경도": "127.75391",
                    "위도": "37.89731",
                }
            ),
            route_stop(
                **{
                    "정류장순서": "3",
                    "정류장": "250026778",
                    "정류장명": "춘천역",
                    "경도": "127.71700",
                    "위도": "37.88400",
                }
            ),
        ]

    def test_cp949_official_ids_become_searchable_directed_topology(self):
        result = import_rows(self.catalog, self.simple_route())
        self.assertEqual(result["encoding"], "cp949")
        self.assertEqual(result["route_count"], 1)
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["activated"], 1)

        stop = self.catalog.search_stops("상공회의소", limit=10)[0]
        self.assertEqual(stop["node_id"], "265000793")
        self.assertEqual(stop["city_code"], "32010")
        self.assertEqual(stop["catalog_kind"], "HYDRATED_TOPOLOGY")
        self.assertTrue(stop["graph_ready"])
        self.assertEqual(stop["route_count"], 1)

        planner = JourneyPlanner()
        forward = planner.plan(
            self.catalog.planning_snapshot(),
            origin_node_id="265000793",
            destination_node_id="250026778",
            alternatives=1,
        )
        reverse = planner.plan(
            self.catalog.planning_snapshot(),
            origin_node_id="250026778",
            destination_node_id="265000793",
            alternatives=1,
        )
        self.assertEqual(forward["alternatives"][0]["route_ids"], ["250000100"])
        self.assertEqual(forward["graph"]["algorithm"], "directed_dijkstra")
        self.assertEqual(reverse["status"], "DATA_GAP")
        self.assertEqual(reverse["reason"], "NO_DIRECTED_PATH_IN_HYDRATED_GRAPH")

    def test_same_file_is_idempotent_and_keeps_one_active_version(self):
        first = import_rows(self.catalog, self.simple_route())
        second = import_rows(self.catalog, self.simple_route())
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["activated"], 0)
        self.assertEqual(second["unchanged"], 1)
        with self.catalog.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM route_sequence_versions").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM active_route_sequences").fetchone()[0],
                1,
            )

    def test_one_official_route_id_can_preserve_multiple_display_labels(self):
        rows = self.simple_route()
        rows[1] = {**rows[1], "노선번호": "1-별칭"}
        result = import_rows(self.catalog, rows)
        self.assertEqual(result["route_label_conflicts"], 1)
        source = self.catalog.planning_snapshot().route_sequences[0].source
        self.assertIn('"route_numbers":["1","1-별칭"]', source)

    def test_invalid_late_route_rolls_back_before_any_activation(self):
        rows = self.simple_route()[:2]
        rows.extend(
            [
                route_stop(
                    **{
                        "노선번호": "2",
                        "노선": "250000200",
                        "정류장순서": "1",
                        "정류장": "B1",
                    }
                ),
                route_stop(
                    **{
                        "노선번호": "2",
                        "노선": "250000200",
                        "정류장순서": "1",
                        "정류장": "B2",
                    }
                ),
            ]
        )
        with self.assertRaises(MunicipalTopologyError):
            import_rows(self.catalog, rows)
        self.assertEqual(self.catalog.planning_snapshot().route_sequences, ())

    def test_header_hash_coordinate_and_resource_bounds_are_strict(self):
        wrong_header = csv_bytes(CHUNCHEON_COLUMNS[:-1], [])
        with self.assertRaises(MunicipalTopologyError):
            import_municipal_topology_csv(
                catalog=self.catalog,
                data=wrong_header,
                profile_name="chuncheon",
                source_date="2026-03-26",
            )

        data = csv_bytes(CHUNCHEON_COLUMNS, self.simple_route())
        with self.assertRaises(MunicipalTopologyError):
            import_municipal_topology_csv(
                catalog=self.catalog,
                data=data,
                profile_name="chuncheon",
                source_date="2026-03-26",
                expected_sha256="0" * 64,
            )
        with self.assertRaises(CatalogLimitError):
            import_municipal_topology_csv(
                catalog=self.catalog,
                data=data,
                profile_name="chuncheon",
                source_date="2026-03-26",
                max_csv_bytes=len(data) - 1,
            )
        with self.assertRaises(CatalogLimitError):
            import_municipal_topology_csv(
                catalog=self.catalog,
                data=data,
                profile_name="chuncheon",
                source_date="2026-03-26",
                max_rows=2,
            )
        bad_coordinate = self.simple_route()
        bad_coordinate[1] = route_stop(
            **{
                "정류장순서": "2",
                "정류장": "BAD",
                "위도": "51.0",
            }
        )
        with self.assertRaises(MunicipalTopologyError):
            import_rows(self.catalog, bad_coordinate)

    def test_static_provider_prefix_is_never_guessed_or_joined(self):
        static_rows = [
            {
                "정류장번호": "CCB265000793",
                "정류장명": "상공회의소",
                "위도": "37.89832",
                "경도": "127.75267",
                "정보수집일": "2026-03-26",
                "모바일단축번호": "",
                "도시코드": "32010",
                "도시명": "강원특별자치도 춘천시",
                "관리도시명": "춘천BIS",
            }
        ]
        self.catalog.import_stops_csv(
            csv_bytes(STOP_COLUMNS, static_rows, "utf-8-sig"),
            source_url="https://www.data.go.kr/data/15067528/fileData.do",
            source_date="2026-03-26",
        )
        import_rows(self.catalog, self.simple_route())
        matches = self.catalog.search_stops("상공회의소", limit=10)
        self.assertEqual(
            {item["node_id"] for item in matches},
            {"265000793", "CCB265000793"},
        )
        municipal = next(item for item in matches if item["node_id"] == "265000793")
        static = next(item for item in matches if item["node_id"] == "CCB265000793")
        self.assertTrue(municipal["graph_ready"])
        self.assertFalse(static["graph_ready"])


if __name__ == "__main__":
    unittest.main()
