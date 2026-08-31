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

from network_audit import (  # noqa: E402
    AuditOptions,
    audit_database,
    main,
    open_catalog_read_only,
)


SCHEMA = """
CREATE TABLE catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE catalog_stops(
  source_id TEXT NOT NULL,city_code TEXT NOT NULL,node_id TEXT NOT NULL,
  node_name TEXT NOT NULL,latitude REAL NOT NULL,longitude REAL NOT NULL,
  collected_date TEXT NOT NULL,mobile_short_no TEXT NOT NULL,
  city_name TEXT NOT NULL,managing_city_name TEXT NOT NULL,
  PRIMARY KEY(source_id,city_code,node_id)
);
CREATE TABLE catalog_routes(
  source_id TEXT NOT NULL,city_code TEXT NOT NULL,route_id TEXT NOT NULL,
  route_no TEXT NOT NULL,start_node_id TEXT NOT NULL,end_node_id TEXT NOT NULL,
  start_stop_name TEXT NOT NULL,end_stop_name TEXT NOT NULL,
  municipality_name TEXT NOT NULL,
  PRIMARY KEY(source_id,city_code,route_id)
);
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
CREATE TABLE topology_targets(
  provider TEXT NOT NULL,city_code TEXT NOT NULL,route_id TEXT NOT NULL,
  route_no TEXT NOT NULL,discovery_source TEXT NOT NULL,discovered_at TEXT NOT NULL,
  PRIMARY KEY(provider,city_code,route_id)
);
CREATE TABLE topology_progress(
  provider TEXT NOT NULL,city_code TEXT NOT NULL,route_id TEXT NOT NULL,
  status TEXT NOT NULL,next_page INTEGER NOT NULL,total_count INTEGER,
  pages_fetched INTEGER NOT NULL,attempts INTEGER NOT NULL,requests_used INTEGER NOT NULL,
  staged_count INTEGER NOT NULL,content_sha256 TEXT,sequence_id TEXT,error_code TEXT,
  error_message TEXT,last_run_id TEXT,updated_at TEXT NOT NULL,completed_at TEXT,
  PRIMARY KEY(provider,city_code,route_id)
);
CREATE TABLE topology_pages(
  provider TEXT NOT NULL,city_code TEXT NOT NULL,route_id TEXT NOT NULL,
  page_no INTEGER NOT NULL,item_count INTEGER NOT NULL,total_count INTEGER NOT NULL,
  payload_sha256 TEXT NOT NULL,items_json TEXT NOT NULL,fetched_at TEXT NOT NULL,
  PRIMARY KEY(provider,city_code,route_id,page_no)
);
CREATE TABLE topology_runs(
  run_id TEXT PRIMARY KEY,provider TEXT NOT NULL,target_source TEXT NOT NULL,
  status TEXT NOT NULL,request_budget INTEGER NOT NULL,requests_used INTEGER NOT NULL,
  target_limit INTEGER,targets_processed INTEGER NOT NULL,succeeded INTEGER NOT NULL,
  unchanged INTEGER NOT NULL,failed INTEGER NOT NULL,deferred INTEGER NOT NULL,
  started_at TEXT NOT NULL,updated_at TEXT NOT NULL,finished_at TEXT
);
CREATE TABLE topology_discovered_cities(
  provider TEXT NOT NULL,city_code TEXT NOT NULL,city_name TEXT NOT NULL,
  discovered_at TEXT NOT NULL,PRIMARY KEY(provider,city_code)
);
CREATE TABLE topology_discovery_progress(
  provider TEXT NOT NULL,scope_key TEXT NOT NULL,status TEXT NOT NULL,
  next_page INTEGER NOT NULL,total_count INTEGER,requests_used INTEGER NOT NULL,
  error_code TEXT,error_message TEXT,updated_at TEXT NOT NULL,
  PRIMARY KEY(provider,scope_key)
);
"""


class NetworkAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "catalog.sqlite3"
        self._seed()

    def tearDown(self):
        self.temp.cleanup()

    def _seed(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(SCHEMA)
            connection.executemany(
                "INSERT INTO catalog_meta VALUES(?,?)",
                [
                    ("revision", "7"),
                    ("active_stops_source_id", "STOPS"),
                    ("active_routes_source_id", "ROUTES"),
                ],
            )
            connection.executemany(
                "INSERT INTO catalog_stops VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    ("STOPS", "25", "N1", "A", 36.30, 127.30, "2026-08-31", "", "대전", "대전"),
                    ("STOPS", "25", "N2", "B", 36.31, 127.31, "2026-08-31", "", "대전", "대전"),
                    ("STOPS", "25", "N3", "C", 36.32, 127.32, "2026-08-31", "", "대전", "대전"),
                    ("STOPS", "25", "NX", "D", 36.33, 127.33, "2026-08-31", "", "대전", "대전"),
                    ("STOPS", "12", "S1", "E", 36.50, 127.20, "2026-08-31", "", "세종", "세종"),
                    ("STOPS", "12", "S2", "F", 36.51, 127.21, "2026-08-31", "", "세종", "세종"),
                ],
            )
            connection.executemany(
                "INSERT INTO catalog_routes VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    ("ROUTES", "25", "R1", "101", "", "", "", "", "대전"),
                    ("ROUTES", "25", "R2", "102", "", "", "", "", "대전"),
                    ("ROUTES", "25", "R_MISSING", "103", "", "", "", "", "대전"),
                    ("ROUTES", "12", "R3", "991", "", "", "", "", "세종"),
                ],
            )
            connection.executemany(
                "INSERT INTO route_sequence_versions VALUES(?,?,?,?,?,?,?,?)",
                [
                    ("SEQ1", "25", "R1", "TEST", "2026", "a" * 64, 2, "2026"),
                    ("SEQ2", "25", "R2", "TEST", "2026", "b" * 64, 3, "2026"),
                    ("SEQX", "25", "RX", "TEST", "2026", "c" * 64, 1, "2026"),
                ],
            )
            connection.executemany(
                "INSERT INTO route_sequence_stops VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    ("SEQ1", 1, "N1", "A", 36.3000, 127.3000, "", 1, 1),
                    ("SEQ1", 2, "N2", "B", 36.3005, 127.3005, "", 1, 1),
                    ("SEQ2", 1, "N2", "B", 36.3005, 127.3005, "", 1, 1),
                    ("SEQ2", -1, "N4", "G", None, None, "", 1, 1),
                    ("SEQX", 1, "X1", "H", 91.0, 127.0, "", 1, 1),
                ],
            )
            connection.executemany(
                "INSERT INTO active_route_sequences VALUES(?,?,?)",
                [("25", "R1", "SEQ1"), ("25", "R2", "SEQ2"), ("25", "RX", "SEQX")],
            )
            connection.executemany(
                "INSERT INTO topology_targets VALUES(?,?,?,?,?,?)",
                [
                    ("TAGO", "25", "R1", "101", "TEST", "2026"),
                    ("TAGO", "25", "R2", "102", "TEST", "2026"),
                    ("TAGO", "12", "R3", "991", "TEST", "2026"),
                ],
            )
            connection.executemany(
                "INSERT INTO topology_progress VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    ("TAGO", "25", "R1", "COMPLETE", 1, 2, 1, 1, 1, 2, None, "SEQ1", None, None, "RUN", "2026", "2026"),
                    ("TAGO", "25", "R2", "FAILED", 1, 3, 1, 2, 2, 2, None, None, "BAD_DATA", "must-not-leak-secret-key", "RUN", "2026", None),
                ],
            )
            connection.execute(
                "INSERT INTO topology_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("RUN", "TAGO", "tago", "PARTIAL", 20, 3, None, 2, 1, 0, 1, 0, "2026", "2026", "2026"),
            )
            connection.executemany(
                "INSERT INTO topology_discovered_cities VALUES(?,?,?,?)",
                [("TAGO", "25", "대전", "2026"), ("TAGO", "12", "세종", "2026")],
            )
            connection.execute(
                "INSERT INTO topology_discovery_progress VALUES(?,?,?,?,?,?,?,?,?)",
                ("TAGO", "cities", "COMPLETE", 2, 2, 1, None, "do-not-print", "2026"),
            )
            connection.commit()

    def test_reports_exact_coverage_integrity_and_topology_without_writes(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        report = audit_database(self.path, AuditOptions(sample_limit=1, city_limit=1))
        after = hashlib.sha256(self.path.read_bytes()).hexdigest()

        self.assertEqual(before, after)
        self.assertEqual(report["active_catalog"]["static_routes"], 4)
        self.assertEqual(report["active_catalog"]["static_stops"], 6)
        self.assertEqual(report["active_graph"]["route_sequences"], 3)
        self.assertEqual(report["active_graph"]["route_stop_rows"], 5)
        self.assertEqual(report["active_graph"]["unique_stops"], 4)
        self.assertEqual(report["exact_route_overlap"]["exact_overlap"], 2)
        self.assertEqual(
            report["exact_route_overlap"]["catalog_exact_graph_coverage_ratio"], 0.5
        )
        self.assertEqual(report["exact_stop_overlap"]["exact_overlap"], 2)
        self.assertEqual(
            report["exact_stop_overlap"]["catalog_exact_graph_inclusion_ratio"],
            0.333333,
        )
        self.assertEqual(report["sequence_integrity"]["anomalous_sequences"], 2)
        self.assertEqual(report["sequence_integrity"]["fewer_than_two_sequences"], 1)
        self.assertEqual(report["sequence_integrity"]["declared_count_mismatch_sequences"], 1)
        self.assertEqual(report["sequence_integrity"]["order_issue_sequences"], 1)
        self.assertEqual(report["sequence_integrity"]["negative_order_rows"], 1)
        self.assertEqual(report["sequence_integrity"]["missing_coordinate_rows"], 1)
        self.assertEqual(report["sequence_integrity"]["out_of_range_coordinate_rows"], 1)
        self.assertEqual(len(report["sequence_integrity"]["anomaly_sample"]), 1)
        self.assertTrue(report["topology"]["available"])
        self.assertEqual(report["topology"]["targets"], 3)
        self.assertEqual(report["topology"]["targets_without_progress"], 1)
        self.assertEqual(report["topology"]["incomplete_targets"], 2)
        self.assertEqual(report["topology"]["retryable_failures"], 1)
        self.assertEqual(report["topology"]["retryable_provider_failures"], 1)
        self.assertEqual(report["topology"]["terminal_unusable_targets"], 0)
        self.assertEqual(report["topology"]["scan_remaining_targets"], 2)
        self.assertFalse(report["topology"]["scan_complete"])
        self.assertFalse(report["topology"]["nationwide_topology_complete"])
        self.assertTrue(report["topology"]["failed_staging_integrity"]["available"])
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("must-not-leak-secret-key", serialized)
        self.assertNotIn("do-not-print", serialized)
        self.assertEqual(report["city_coverage"]["route_coverage"]["lowest"][0]["city_code"], "12")
        self.assertEqual(report["audit_status"], "ISSUES_FOUND")

    def test_distinguishes_finished_scan_from_complete_topology(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(
                "UPDATE topology_progress SET attempts=3,total_count=1,staged_count=1,"
                "error_code='INVALID_ROUTE_TOPOLOGY' WHERE route_id='R2'"
            )
            connection.execute(
                "INSERT INTO topology_pages VALUES(?,?,?,?,?,?,?,?,?)",
                ("TAGO", "25", "R2", 1, 1, 1, "x" * 64, "[]", "2026"),
            )
            connection.execute(
                "INSERT INTO topology_progress VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("TAGO", "12", "R3", "COMPLETE", 1, 2, 1, 1, 1, 2,
                 None, "SEQ3", None, None, "RUN", "2026", "2026"),
            )
            connection.commit()

        topology = audit_database(self.path)["topology"]
        self.assertEqual(topology["terminal_unusable_targets"], 1)
        self.assertEqual(topology["retryable_failures"], 0)
        self.assertEqual(topology["scan_remaining_targets"], 0)
        self.assertTrue(topology["scan_complete"])
        self.assertFalse(topology["nationwide_topology_complete"])
        self.assertEqual(
            topology["failed_staging_integrity"]["mixed_page_corruption_targets"],
            0,
        )

    def test_detects_mixed_page_retry_corruption(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executemany(
                "INSERT INTO topology_pages VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    ("TAGO", "25", "R2", 1, 0, 0, "a" * 64, "[]", "2026"),
                    ("TAGO", "25", "R2", 2, 2, 3, "b" * 64, "[]", "2026"),
                ],
            )
            connection.commit()

        staging = audit_database(self.path)["topology"]["failed_staging_integrity"]
        self.assertEqual(staging["inconsistent_page_total_targets"], 1)
        self.assertEqual(staging["page1_zero_later_positive_targets"], 1)
        self.assertEqual(staging["mixed_page_corruption_targets"], 1)

    def test_optional_300m_components_are_bounded_and_computed(self):
        report = audit_database(
            self.path,
            AuditOptions(
                sample_limit=2,
                city_limit=2,
                components_300m=True,
                max_component_stops=10,
            ),
        )
        components = report["components_300m"]
        self.assertTrue(components["computed"])
        self.assertEqual(components["graph_unique_stops"], 4)
        self.assertEqual(components["component_count"], 2)
        self.assertEqual(components["largest_component_stops"], 3)

        skipped = audit_database(
            self.path,
            AuditOptions(components_300m=True, max_component_stops=3),
        )["components_300m"]
        self.assertFalse(skipped["computed"])
        self.assertEqual(skipped["reason"], "STOP_LIMIT_EXCEEDED")

    def test_read_only_connection_rejects_schema_writes(self):
        with open_catalog_read_only(self.path) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden(value TEXT)")

    def test_cli_writes_json_and_can_fail_on_findings(self):
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
        self.assertEqual(exit_code, 2)
        self.assertTrue(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
