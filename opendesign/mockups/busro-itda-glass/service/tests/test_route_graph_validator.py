from __future__ import annotations

from contextlib import closing, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from route_graph_validator import (  # noqa: E402
    RouteGraphValidationError,
    ValidationOptions,
    main,
    open_catalog_read_only,
    validate_database,
)


SCHEMA = """
CREATE TABLE route_sequence_versions(
  sequence_id TEXT PRIMARY KEY,city_code TEXT NOT NULL,route_id TEXT NOT NULL,
  source TEXT NOT NULL,captured_at TEXT NOT NULL,sha256 TEXT NOT NULL,
  stop_count INTEGER NOT NULL,imported_at TEXT NOT NULL
);
CREATE TABLE route_sequence_stops(
  sequence_id TEXT NOT NULL,node_order INTEGER NOT NULL,node_id TEXT NOT NULL,
  node_name TEXT NOT NULL,latitude REAL,longitude REAL,direction TEXT NOT NULL,
  can_board INTEGER NOT NULL,can_alight INTEGER NOT NULL,
  PRIMARY KEY(sequence_id,node_order)
);
CREATE TABLE active_route_sequences(
  city_code TEXT NOT NULL,route_id TEXT NOT NULL,sequence_id TEXT NOT NULL,
  PRIMARY KEY(city_code,route_id)
);
"""


def _version(sequence_id: str, city: str, route: str, stop_count: int):
    return (sequence_id, city, route, "TEST", "2026", sequence_id * 8, stop_count, "2026")


def _stop(
    sequence_id: str,
    order: int,
    node_id: str,
    latitude: float | None,
    longitude: float | None,
    *,
    board: int = 1,
    alight: int = 1,
    direction: str = "",
):
    return (
        sequence_id,
        order,
        node_id,
        node_id,
        latitude,
        longitude,
        direction,
        board,
        alight,
    )


class RouteGraphValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "catalog.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(SCHEMA)
            connection.executemany(
                "INSERT INTO route_sequence_versions VALUES(?,?,?,?,?,?,?,?)",
                [
                    _version("S1", "25", "R1", 2),
                    _version("S2", "25", "R2", 2),
                    _version("S3", "30", "R3", 2),
                ],
            )
            connection.executemany(
                "INSERT INTO route_sequence_stops VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    _stop("S1", 1, "N1", 36.0000, 127.0000),
                    _stop("S1", 2, "N2", 36.0100, 127.0000),
                    _stop("S2", 1, "N2", 36.0100, 127.0000),
                    _stop("S2", 2, "N3", 36.0200, 127.0000),
                    _stop("S3", 1, "P1", 36.0210, 127.0000),
                    _stop("S3", 2, "P2", 36.0300, 127.0000),
                ],
            )
            connection.executemany(
                "INSERT INTO active_route_sequences VALUES(?,?,?)",
                [("25", "R1", "S1"), ("25", "R2", "S2"), ("30", "R3", "S3")],
            )
            connection.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_traverses_all_routes_and_builds_exact_and_300m_route_components(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        report = validate_database(self.path, ValidationOptions(sample_limit=3))
        after = hashlib.sha256(self.path.read_bytes()).hexdigest()

        self.assertEqual(before, after)
        self.assertTrue(report["validation_scope"]["all_active_routes_traversed"])
        self.assertFalse(
            report["validation_scope"]["planner_route_stop_state_graph_materialized"]
        )
        sequences = report["sequence_validation"]
        self.assertEqual(sequences["active_routes"], 3)
        self.assertEqual(sequences["route_stop_rows_traversed"], 6)
        self.assertEqual(sequences["forward_order_edges"], 3)
        self.assertEqual(sequences["forward_reachable_od_pairs"], 3)
        self.assertEqual(sequences["routes_without_forward_od_pair"], 0)

        transfers = report["transfer_connectivity"]
        self.assertEqual(transfers["unique_exact_stops"], 5)
        self.assertEqual(transfers["exact_shared_stop_groups"], 1)
        self.assertEqual(transfers["exact_route_pair_incidences"], 1)
        self.assertEqual(transfers["exact_components"]["component_count"], 2)
        self.assertEqual(transfers["exact_components"]["largest_component_routes"], 2)
        self.assertEqual(
            transfers["components_with_proximity"]["component_count"], 1
        )
        self.assertEqual(
            transfers["components_with_proximity"]["largest_component_routes"], 3
        )
        self.assertGreaterEqual(transfers["proximity_stop_pairs_within_radius"], 1)
        self.assertGreaterEqual(transfers["cross_city_proximity_stop_pairs"], 1)
        self.assertEqual(report["audit_status"], "PASS")

    def test_reports_order_duplicate_coordinate_and_forward_reachability_anomalies(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executemany(
                "INSERT INTO route_sequence_versions VALUES(?,?,?,?,?,?,?,?)",
                [
                    _version("S4", "40", "R4", 4),
                    _version("S5", "40", "R5", 2),
                    _version("S6", "40", "R6", 2),
                ],
            )
            connection.executemany(
                "INSERT INTO route_sequence_stops VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    _stop("S4", -1, "Q", 36.0, 127.0, board=0, alight=0),
                    _stop("S4", 1, "R", None, 127.0, board=0, alight=0),
                    _stop("S4", 3, "Q", 91.0, 127.0, board=0, alight=0),
                    _stop("S5", 1, "Z", 36.0, 127.0),
                    _stop("S5", 2, "Z5", 36.01, 127.0),
                    _stop("S6", 1, "Z", 36.1, 127.0),
                    _stop("S6", 2, "Z6", 36.11, 127.0),
                ],
            )
            connection.executemany(
                "INSERT INTO active_route_sequences VALUES(?,?,?)",
                [("40", "R4", "S4"), ("40", "R5", "S5"), ("40", "R6", "S6")],
            )
            connection.commit()

        report = validate_database(self.path)
        sequences = report["sequence_validation"]
        self.assertEqual(sequences["declared_count_mismatch_routes"], 1)
        self.assertEqual(sequences["routes_with_order_gaps"], 1)
        self.assertEqual(sequences["negative_order_rows"], 1)
        self.assertEqual(sequences["routes_with_repeated_nodes"], 1)
        self.assertEqual(sequences["routes_without_forward_od_pair"], 1)
        self.assertEqual(sequences["partial_coordinate_rows"], 1)
        self.assertEqual(sequences["out_of_range_coordinate_rows"], 1)
        self.assertEqual(
            report["transfer_connectivity"]["coordinate_conflict_stop_groups"],
            1,
        )
        self.assertEqual(report["audit_status"], "ISSUES_FOUND")
        self.assertTrue(report["sequence_validation"]["anomaly_sample"])

    def test_direction_boundary_is_reported_without_claiming_an_order_failure(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "INSERT INTO route_sequence_versions VALUES(?,?,?,?,?,?,?,?)",
                _version("S4", "40", "R4", 2),
            )
            connection.executemany(
                "INSERT INTO route_sequence_stops VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    _stop("S4", 1001, "D1", 36.0, 127.0, direction="0"),
                    _stop("S4", 2001, "D2", 36.06, 127.0, direction="1"),
                ],
            )
            connection.execute(
                "INSERT INTO active_route_sequences VALUES(?,?,?)",
                ("40", "R4", "S4"),
            )
            connection.commit()

        report = validate_database(self.path)
        sequences = report["sequence_validation"]
        self.assertEqual(sequences["routes_with_order_gaps"], 1)
        self.assertEqual(sequences["routes_with_same_direction_order_gaps"], 0)
        self.assertEqual(
            sequences["routes_with_direction_boundary_order_gaps"], 1
        )
        self.assertEqual(sequences["cross_direction_od_pairs_in_linear_chain"], 1)
        self.assertEqual(sequences["routes_with_only_cross_direction_od_pairs"], 1)
        self.assertEqual(sequences["routes_with_direction_boundary_over_300m"], 1)
        self.assertEqual(sequences["direction_boundaries_over_5km"], 1)
        self.assertEqual(
            sequences["direction_transition_counts"], {"0->1": 1}
        )
        self.assertEqual(
            len(sequences["direction_boundary_over_5km_sample"]), 1
        )
        self.assertEqual(report["audit_status"], "PASS")

    def test_sparse_same_direction_orders_are_warning_not_hard_failure(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "INSERT INTO route_sequence_versions VALUES(?,?,?,?,?,?,?,?)",
                _version("S4", "40", "R4", 2),
            )
            connection.executemany(
                "INSERT INTO route_sequence_stops VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    _stop("S4", 1, "G1", 36.0, 127.0, direction="0"),
                    _stop("S4", 6, "G2", 36.01, 127.01, direction="0"),
                ],
            )
            connection.execute(
                "INSERT INTO active_route_sequences VALUES(?,?,?)",
                ("40", "R4", "S4"),
            )
            connection.commit()

        report = validate_database(self.path)

        self.assertEqual(
            report["sequence_validation"]["routes_with_same_direction_order_gaps"],
            1,
        )
        self.assertNotIn(
            "routes_with_same_direction_order_gaps", report["hard_findings"]
        )
        self.assertEqual(
            report["warnings"]["routes_with_same_direction_order_gaps"], 1
        )
        self.assertEqual(report["audit_status"], "PASS")

    def test_same_direction_single_point_spike_is_a_hard_finding(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "INSERT INTO route_sequence_versions VALUES(?,?,?,?,?,?,?,?)",
                _version("S4", "40", "R4", 3),
            )
            connection.executemany(
                "INSERT INTO route_sequence_stops VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    _stop("S4", 1, "A", 36.000, 127.0, direction="0"),
                    _stop("S4", 2, "B", 36.300, 127.0, direction="0"),
                    _stop("S4", 3, "C", 36.001, 127.0, direction="0"),
                ],
            )
            connection.execute(
                "INSERT INTO active_route_sequences VALUES(?,?,?)",
                ("40", "R4", "S4"),
            )
            connection.commit()

        report = validate_database(self.path)
        sequences = report["sequence_validation"]
        self.assertEqual(sequences["single_point_route_spikes"], 1)
        self.assertEqual(sequences["routes_with_single_point_route_spikes"], 1)
        self.assertEqual(
            report["hard_findings"]["routes_with_single_point_route_spikes"], 1
        )
        self.assertEqual(len(sequences["single_point_spike_sample"]), 1)
        self.assertEqual(report["audit_status"], "ISSUES_FOUND")

    def test_legitimate_same_direction_long_run_is_only_a_distance_warning(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "INSERT INTO route_sequence_versions VALUES(?,?,?,?,?,?,?,?)",
                _version("S4", "40", "R4", 3),
            )
            connection.executemany(
                "INSERT INTO route_sequence_stops VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    _stop("S4", 1, "A", 36.000, 127.0, direction="0"),
                    _stop("S4", 2, "B", 36.300, 127.0, direction="0"),
                    _stop("S4", 3, "C", 36.600, 127.0, direction="0"),
                ],
            )
            connection.execute(
                "INSERT INTO active_route_sequences VALUES(?,?,?)",
                ("40", "R4", "S4"),
            )
            connection.commit()

        report = validate_database(self.path)
        sequences = report["sequence_validation"]
        self.assertEqual(sequences["segments_over_20km"], 2)
        self.assertEqual(sequences["single_point_route_spikes"], 0)
        self.assertEqual(sequences["routes_with_single_point_route_spikes"], 0)
        self.assertEqual(report["audit_status"], "PASS")

    def test_limits_fail_explicitly_and_read_only_connection_rejects_writes(self):
        with self.assertRaisesRegex(RouteGraphValidationError, "active route limit"):
            validate_database(
                self.path,
                ValidationOptions(max_active_routes=2),
            )
        with open_catalog_read_only(self.path) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden(value TEXT)")

    def test_cli_emits_concise_json(self):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "--catalog-db",
                    str(self.path),
                    "--sample-limit",
                    "1",
                    "--fail-on-anomaly",
                ]
            )
        self.assertEqual(exit_code, 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report["ok"])
        self.assertLess(len(output.getvalue()), 10_000)


if __name__ == "__main__":
    unittest.main()
