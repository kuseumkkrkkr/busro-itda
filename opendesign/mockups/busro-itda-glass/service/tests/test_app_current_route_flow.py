from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from app import BusroService  # noqa: E402
from config import Settings  # noqa: E402


FIXED_NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


class CurrentRouteFlowCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.service = BusroService(
            Settings(
                fixture_mode=True,
                db_path=root / "service.sqlite3",
                network_catalog_path=root / "catalog.sqlite3",
                fixture_path=SERVICE_DIR / "fixtures" / "tago_arrivals.json",
                fixture_delays_path=SERVICE_DIR / "fixtures" / "delay_samples.json",
            ),
            clock=lambda: FIXED_NOW,
        )
        self.snapshot = SimpleNamespace(version="current-tago-v1", routes=())
        self.structural = {
            "criterion": "minimum_transfers",
            "status": "DATA_GAP",
            "reasons": ["VERIFIED_TIMETABLE_REQUIRED"],
            "success_probability": None,
            "probability_basis": None,
            "probability_scope": None,
            "reliability": {
                "status": "DATA_GAP",
                "success_probability": None,
                "historical_gtfs_prior": {
                    "role": "model_weight_only",
                    "projection_allowed": False,
                },
            },
            "estimated_minutes": None,
            "transfers": 0,
            "walking_m": 0,
            "steps": [
                {
                    "kind": "ride",
                    "route_id": "TAGO-CURRENT-1",
                    "from": {
                        "city_code": "12",
                        "node_id": "CURRENT-O",
                        "node_name": "현재 출발",
                    },
                    "to": {
                        "city_code": "12",
                        "node_id": "CURRENT-D",
                        "node_name": "현재 도착",
                    },
                }
            ],
            "evidence": {"topology": "all_active_hydrated_route_sequences"},
            "coverage": {
                "structural": 1.0,
                "total_routes": 1,
                "service_routes": 0,
                "schedule_routes": 0,
                "passage_routes": 0,
            },
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _request(self) -> dict[str, object]:
        return {
            "from_stop_id": "CURRENT-O",
            "to_stop_id": "CURRENT-D",
            "from_city_code": "12",
            "to_city_code": "12",
            "service_date": "2026-08-31",
            "departure_time": "09:30",
            "preference": "low_transfer",
            "max_alternatives": 3,
        }

    def _generate(self, schedule_result: dict[str, object] | None = None):
        planned = {
            "status": "DATA_GAP",
            "reason": "EVIDENCE_INCOMPLETE",
            "graph": {
                "algorithm": "directed_dijkstra",
                "nodes": 2,
                "edges": 1,
                "coverage": {},
            },
            "alternatives": [self.structural],
        }
        with (
            patch.object(
                self.service.network_catalog,
                "planning_snapshot",
                return_value=self.snapshot,
            ),
            patch.object(
                self.service.network_catalog,
                "plan_gtfs_schedule",
                return_value=schedule_result,
            ) as schedule_plan,
            patch.object(
                self.service.network_catalog,
                "topology_coverage",
                return_value={
                    "targets": 1,
                    "complete": 1,
                    "hydrated_active_sequences": 1,
                    "coverage_ratio": 1.0,
                },
            ),
            patch.object(self.service.journey_planner, "build_graph"),
            patch.object(
                self.service.journey_planner, "plan", return_value=planned
            ) as structural_plan,
        ):
            response = self.service.generate_journeys(self._request())
        return response, schedule_plan, structural_plan

    def test_sources_accepts_origin_status_and_keeps_status_alias(self) -> None:
        current = self.service.sources({"origin_status": "VERIFIED_PRIOR_ONLY"})
        legacy = self.service.sources({"status": "VERIFIED_PRIOR_ONLY"})

        self.assertEqual(current["sources"], legacy["sources"])
        self.assertEqual([item["id"] for item in current["sources"]], ["ktdb-gtfs-2024"])
        self.assertEqual(
            current["sources"][0]["origin_status"], "VERIFIED_PRIOR_ONLY"
        )
        self.assertNotIn("status", current["sources"][0])

    def test_prior_only_gtfs_cannot_return_ready_or_current_times(self) -> None:
        ready_historical = {
            "status": "READY",
            "reason": None,
            "schedule": {"status": "READY", "departure_time": "09:35"},
            "graph": {"algorithm": "bounded_time_dependent_dijkstra"},
            "alternatives": [
                {
                    "status": "READY",
                    "departure_time": "09:35:00",
                    "arrival_time": "10:05:00",
                    "steps": [],
                }
            ],
        }

        response, schedule_plan, structural_plan = self._generate(ready_historical)

        schedule_plan.assert_not_called()
        structural_plan.assert_called_once()
        self.assertEqual(response["schedule"]["status"], "DATA_GAP")
        self.assertEqual(
            response["schedule"]["reason"], "HISTORICAL_GTFS_PRIOR_ONLY"
        )
        self.assertEqual(
            response["schedule"]["origin_status"], "VERIFIED_PRIOR_ONLY"
        )
        self.assertFalse(response["schedule"]["projection_allowed"])
        self.assertEqual(
            response["schedule"]["basis"], "HISTORICAL_GTFS_PRIOR_ONLY"
        )
        self.assertEqual(response["count"], 1)
        self.assertEqual(response["candidates"][0]["route_ids"], ["TAGO-CURRENT-1"])
        self.assertIsNone(response["candidates"][0]["success_probability"])
        self.assertFalse(
            response["candidates"][0]["reliability"]["historical_gtfs_prior"][
                "projection_allowed"
            ]
        )
        self.assertIsNone(response["candidates"][0]["estimated_minutes"])
        self.assertNotIn("scheduled", response["candidates"][0])
        self.assertNotIn("departure_time", response["candidates"][0])
        self.assertNotIn("arrival_time", response["candidates"][0])
        self.assertNotIn("departure_time", response["candidates"][0]["steps"][0])
        self.assertEqual(response["graph"]["algorithm"], "directed_dijkstra")

    def test_discovered_schedule_cannot_project_even_if_flag_is_true(self) -> None:
        self.service._source_origins["ktdb-gtfs-2024"] = {
            **self.service._source_origins["ktdb-gtfs-2024"],
            "origin_status": "VERIFIED_SCHEDULE_ORIGIN",
            "ingestion_status": "DISCOVERED_ONLY",
            "projection_allowed": True,
        }
        ready_if_called = {
            "status": "READY",
            "schedule": {"status": "READY", "departure_time": "09:35"},
            "alternatives": [{"status": "READY", "steps": []}],
        }

        response, schedule_plan, structural_plan = self._generate(ready_if_called)

        schedule_plan.assert_not_called()
        structural_plan.assert_called_once()
        self.assertEqual(response["schedule"]["status"], "DATA_GAP")
        self.assertFalse(response["schedule"]["projection_allowed"])
        self.assertNotIn("departure_time", response["candidates"][0])

    def test_current_schedule_gap_keeps_structural_candidates_primary(self) -> None:
        self.service._source_origins["ktdb-gtfs-2024"] = {
            **self.service._source_origins["ktdb-gtfs-2024"],
            "origin_status": "VERIFIED_SCHEDULE_ORIGIN",
            "ingestion_status": "ACTIVE",
            "projection_allowed": True,
        }
        schedule_gap = {
            "status": "DATA_GAP",
            "reason": "STOP_NOT_IN_ACTIVE_GTFS_FEED",
            "schedule": {
                "status": "DATA_GAP",
                "reason": "STOP_NOT_IN_ACTIVE_GTFS_FEED",
                "service_date": "2026-08-31",
                "departure_time": "09:30",
            },
            "graph": {
                "algorithm": "bounded_time_dependent_dijkstra",
                "search_complete": True,
            },
            "alternatives": [],
        }

        response, schedule_plan, structural_plan = self._generate(schedule_gap)

        schedule_plan.assert_called_once()
        structural_plan.assert_called_once()
        self.assertEqual(response["schedule_status"], "DATA_GAP")
        self.assertEqual(
            response["schedule_reason"], "STOP_NOT_IN_ACTIVE_GTFS_FEED"
        )
        self.assertEqual(response["count"], 1)
        self.assertEqual(response["candidates"], response["alternatives"])
        self.assertEqual(response["candidates"][0]["route_ids"], ["TAGO-CURRENT-1"])
        self.assertEqual(response["scheduled_alternatives"], [])
        self.assertEqual(response["static_alternatives"], [])
        self.assertEqual(response["preference_applied"], "low_transfer")
        self.assertFalse(response["preference_ignored"])
        self.assertEqual(response["graph"]["algorithm"], "directed_dijkstra")


if __name__ == "__main__":
    unittest.main()
