from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from network_catalog import CatalogValidationError, NetworkCatalog  # noqa: E402
from route_topology_anomalies import (  # noqa: E402
    SINGLE_POINT_ROUTE_SPIKE_ERROR_CODE,
    single_point_route_spike,
)


FIXED_NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


def _point(order: int, latitude: float, *, direction: str = "0") -> dict:
    return {
        "node_order": order,
        "latitude": latitude,
        "longitude": 127.0,
        "direction": direction,
    }


def _sequence_rows(*, spike: bool) -> list[dict]:
    middle_latitude = 36.30 if spike else 36.01
    return [
        {
            "node_id": "NODE_A",
            "node_name": "A",
            **_point(1, 36.00),
        },
        {
            "node_id": "NODE_B",
            "node_name": "B",
            **_point(2, middle_latitude),
        },
        {
            "node_id": "NODE_C",
            "node_name": "C",
            **_point(3, 36.001),
        },
    ]


class SinglePointRouteSpikeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = NetworkCatalog(
            Path(self.temp.name) / "catalog.sqlite3", clock=lambda: FIXED_NOW
        )
        self.catalog.upsert_topology_targets(
            provider="TAGO",
            routes=[
                {"city_code": "25", "route_id": "ROUTE_A", "route_no": "100"}
            ],
            discovery_source="TEST",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _insert_raw_active(self, sequence_id: str = "seq_bad") -> None:
        rows = _sequence_rows(spike=True)
        with self.catalog.connect() as connection:
            connection.execute(
                "INSERT INTO route_sequence_versions VALUES(?,?,?,?,?,?,?,?)",
                (
                    sequence_id,
                    "25",
                    "ROUTE_A",
                    "ORIGINAL_PROVENANCE",
                    "2026-08-30T00:00:00Z",
                    "a" * 64,
                    len(rows),
                    "2026-08-31T00:00:00Z",
                ),
            )
            connection.executemany(
                "INSERT INTO route_sequence_stops("
                "sequence_id,node_order,node_id,node_name,latitude,longitude,"
                "direction,can_board,can_alight) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        sequence_id,
                        row["node_order"],
                        row["node_id"],
                        row["node_name"],
                        row["latitude"],
                        row["longitude"],
                        row["direction"],
                        1,
                        1,
                    )
                    for row in rows
                ],
            )
            connection.execute(
                "INSERT INTO active_route_sequences VALUES(?,?,?)",
                ("25", "ROUTE_A", sequence_id),
            )
            connection.commit()

    @staticmethod
    def _spike_evidence():
        rows = _sequence_rows(spike=True)
        evidence = single_point_route_spike(*rows)
        assert evidence is not None
        return evidence

    def test_normal_long_distance_run_is_not_a_single_point_spike(self):
        self.assertIsNone(
            single_point_route_spike(
                _point(1, 36.0),
                _point(2, 36.3),
                _point(3, 36.6),
            )
        )
        self.assertIsNotNone(
            single_point_route_spike(
                _point(1, 36.0),
                _point(2, 36.3),
                _point(3, 36.001),
            )
        )
        self.assertIsNone(
            single_point_route_spike(
                _point(1, 36.0, direction=""),
                _point(2, 36.3, direction=""),
                _point(3, 36.001, direction=""),
            )
        )
        self.assertIsNone(
            single_point_route_spike(
                _point(1, 36.0, direction="0"),
                _point(2, 36.3, direction="1"),
                _point(3, 36.001, direction="0"),
            )
        )

    def test_quarantine_removes_only_active_pointer_and_invalidates_snapshot(self):
        self._insert_raw_active()
        before_revision = self.catalog.snapshot().revision
        self.assertEqual(len(self.catalog.planning_snapshot().route_sequences), 1)

        result = self.catalog.quarantine_topology_route_spike(
            provider="TAGO",
            city_code="25",
            route_id="ROUTE_A",
            expected_sequence_id="seq_bad",
            evidence=self._spike_evidence(),
        )

        self.assertTrue(result["pointer_removed"])
        self.assertTrue(result["expected_sequence_id_matched"])
        self.assertEqual(result["revision"], before_revision + 1)
        self.assertEqual(len(self.catalog.planning_snapshot().route_sequences), 0)
        with self.catalog.connect() as connection:
            active_count = connection.execute(
                "SELECT COUNT(*) FROM active_route_sequences"
            ).fetchone()[0]
            version = connection.execute(
                "SELECT source,sha256,stop_count FROM route_sequence_versions "
                "WHERE sequence_id='seq_bad'"
            ).fetchone()
            stop_count = connection.execute(
                "SELECT COUNT(*) FROM route_sequence_stops "
                "WHERE sequence_id='seq_bad'"
            ).fetchone()[0]
            progress = connection.execute(
                "SELECT status,error_code,error_message FROM topology_progress "
                "WHERE provider='TAGO' AND city_code='25' AND route_id='ROUTE_A'"
            ).fetchone()
        self.assertEqual(active_count, 0)
        self.assertEqual(tuple(version), ("ORIGINAL_PROVENANCE", "a" * 64, 3))
        self.assertEqual(stop_count, 3)
        self.assertEqual(progress["status"], "FAILED")
        self.assertEqual(progress["error_code"], SINGLE_POINT_ROUTE_SPIKE_ERROR_CODE)
        self.assertLessEqual(len(progress["error_message"]), 240)
        self.assertNotIn("ROUTE_A", progress["error_message"])
        self.assertNotIn("NODE_B", progress["error_message"])

    def test_expected_sequence_guard_never_removes_a_new_pointer(self):
        self._insert_raw_active("seq_current")
        before_revision = self.catalog.snapshot().revision

        result = self.catalog.quarantine_topology_route_spike(
            provider="TAGO",
            city_code="25",
            route_id="ROUTE_A",
            expected_sequence_id="seq_stale",
            evidence=self._spike_evidence(),
        )

        self.assertFalse(result["pointer_removed"])
        self.assertFalse(result["expected_sequence_id_matched"])
        self.assertEqual(result["revision"], before_revision)
        with self.catalog.connect() as connection:
            active = connection.execute(
                "SELECT sequence_id FROM active_route_sequences "
                "WHERE city_code='25' AND route_id='ROUTE_A'"
            ).fetchone()[0]
        self.assertEqual(active, "seq_current")

    def test_corrected_sequence_can_reactivate_after_pointer_quarantine(self):
        self._insert_raw_active()
        quarantine = self.catalog.quarantine_topology_route_spike(
            provider="TAGO",
            city_code="25",
            route_id="ROUTE_A",
            expected_sequence_id="seq_bad",
            evidence=self._spike_evidence(),
        )
        with self.catalog.connect() as connection:
            connection.execute(
                "UPDATE topology_progress SET attempts=3 "
                "WHERE provider='TAGO' AND city_code='25' AND route_id='ROUTE_A'"
            )
            connection.commit()
        self.catalog.create_topology_run(
            run_id="run_corrected",
            provider="TAGO",
            target_source="DISCOVERY",
            request_budget=10,
            target_limit=1,
        )
        self.assertIsNone(
            self.catalog.claim_topology_target(
                provider="TAGO", run_id="run_corrected"
            )
        )
        claimed = self.catalog.claim_specific_topology_target(
            provider="TAGO",
            run_id="run_corrected",
            city_code="25",
            route_id="ROUTE_A",
        )
        self.assertIsNotNone(claimed)

        corrected = self.catalog.hydrate_route_sequence(
            city_code="25",
            route_id="ROUTE_A",
            ordered_stops=_sequence_rows(spike=False),
            source="CORRECTED_SOURCE",
            captured_at="2026-08-31T01:00:00Z",
        )
        self.catalog.finish_topology_target(
            provider="TAGO",
            city_code="25",
            route_id="ROUTE_A",
            unchanged=False,
            content_sha256=corrected["sha256"],
            sequence_id=corrected["sequence_id"],
        )

        self.assertTrue(corrected["activated"])
        self.assertEqual(corrected["revision"], quarantine["revision"] + 1)
        with self.catalog.connect() as connection:
            active = connection.execute(
                "SELECT sequence_id FROM active_route_sequences "
                "WHERE city_code='25' AND route_id='ROUTE_A'"
            ).fetchone()[0]
            preserved = connection.execute(
                "SELECT COUNT(*) FROM route_sequence_versions "
                "WHERE sequence_id='seq_bad'"
            ).fetchone()[0]
            progress = connection.execute(
                "SELECT status,error_code FROM topology_progress "
                "WHERE provider='TAGO' AND city_code='25' AND route_id='ROUTE_A'"
            ).fetchone()
        self.assertEqual(active, corrected["sequence_id"])
        self.assertEqual(preserved, 1)
        self.assertEqual(tuple(progress), ("COMPLETE", None))

    def test_direct_hydration_rejects_spike_without_persisting_a_version(self):
        with self.assertRaisesRegex(
            CatalogValidationError, "same-direction single-point route spike"
        ):
            self.catalog.hydrate_route_sequence(
                city_code="25",
                route_id="ROUTE_A",
                ordered_stops=_sequence_rows(spike=True),
                source="BAD_SOURCE",
                captured_at="2026-08-31T01:00:00Z",
            )
        with self.catalog.connect() as connection:
            versions = connection.execute(
                "SELECT COUNT(*) FROM route_sequence_versions"
            ).fetchone()[0]
        self.assertEqual(versions, 0)


if __name__ == "__main__":
    unittest.main()
