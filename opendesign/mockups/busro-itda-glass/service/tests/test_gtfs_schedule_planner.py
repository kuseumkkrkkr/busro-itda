from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import csv
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SERVICE_DIR = Path(__file__).resolve().parents[1]
TEST_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVICE_DIR))
sys.path.insert(0, str(TEST_DIR))

from app import AppError, BusroService  # noqa: E402
from config import Settings  # noqa: E402
from gtfs_ingest import import_gtfs_zip  # noqa: E402
from network_catalog import NetworkCatalog  # noqa: E402
from test_gtfs_ingest import base_tables, csv_text, write_zip  # noqa: E402


FIXED_NOW = datetime(2026, 8, 31, tzinfo=timezone.utc)


def transfer_tables() -> dict[str, bytes]:
    stops = (
        {"stop_id": "O", "stop_name": "출발", "stop_lat": "37.50", "stop_lon": "127.00"},
        {"stop_id": "X", "stop_name": "환승", "stop_lat": "37.51", "stop_lon": "127.01"},
        {"stop_id": "D", "stop_name": "도착", "stop_lat": "37.52", "stop_lon": "127.02"},
    )
    return {
        "stops.txt": csv_text(
            ("stop_id", "stop_name", "stop_lat", "stop_lon"), stops
        ).encode("utf-8"),
        "routes.txt": csv_text(
            ("route_id", "route_short_name", "route_long_name", "route_type"),
            (
                {"route_id": "R1", "route_short_name": "1", "route_long_name": "서부", "route_type": "3"},
                {"route_id": "R2", "route_short_name": "2", "route_long_name": "동부", "route_type": "3"},
            ),
        ).encode("utf-8"),
        "calendar.txt": csv_text(
            (
                "service_id", "monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday", "start_date", "end_date",
            ),
            (
                {
                    "service_id": "WK", "monday": "1", "tuesday": "1",
                    "wednesday": "1", "thursday": "1", "friday": "1",
                    "saturday": "0", "sunday": "0", "start_date": "20260801",
                    "end_date": "20260930",
                },
            ),
        ).encode("utf-8"),
        "trips.txt": csv_text(
            ("route_id", "service_id", "trip_id", "direction_id", "trip_headsign"),
            (
                {"route_id": "R1", "service_id": "WK", "trip_id": "T1", "direction_id": "0", "trip_headsign": "환승"},
                {"route_id": "R2", "service_id": "WK", "trip_id": "T2", "direction_id": "0", "trip_headsign": "도착"},
            ),
        ).encode("utf-8"),
        "stop_times.txt": csv_text(
            (
                "trip_id", "arrival_time", "departure_time", "stop_id",
                "stop_sequence", "pickup_type", "drop_off_type",
            ),
            (
                {"trip_id": "T1", "arrival_time": "08:00:00", "departure_time": "08:00:00", "stop_id": "O", "stop_sequence": "1", "pickup_type": "0", "drop_off_type": "0"},
                {"trip_id": "T1", "arrival_time": "08:20:00", "departure_time": "08:20:00", "stop_id": "X", "stop_sequence": "2", "pickup_type": "0", "drop_off_type": "0"},
                {"trip_id": "T2", "arrival_time": "08:30:00", "departure_time": "08:30:00", "stop_id": "X", "stop_sequence": "1", "pickup_type": "0", "drop_off_type": "0"},
                {"trip_id": "T2", "arrival_time": "09:00:00", "departure_time": "09:00:00", "stop_id": "D", "stop_sequence": "2", "pickup_type": "0", "drop_off_type": "0"},
            ),
        ).encode("utf-8"),
    }


def many_trip_tables() -> dict[str, bytes]:
    tables = transfer_tables()
    trips = []
    stop_times = []
    for index in range(105):
        trip_id = f"T{index:03d}"
        departure = 8 * 60 + index
        arrival = departure + 20
        trips.append(
            {
                "route_id": "R1", "service_id": "WK", "trip_id": trip_id,
                "direction_id": "0", "trip_headsign": "도착",
            }
        )
        stop_times.extend(
            (
                {
                    "trip_id": trip_id,
                    "arrival_time": f"{departure // 60:02d}:{departure % 60:02d}:00",
                    "departure_time": f"{departure // 60:02d}:{departure % 60:02d}:00",
                    "stop_id": "O", "stop_sequence": "1", "pickup_type": "0", "drop_off_type": "0",
                },
                {
                    "trip_id": trip_id,
                    "arrival_time": f"{arrival // 60:02d}:{arrival % 60:02d}:00",
                    "departure_time": f"{arrival // 60:02d}:{arrival % 60:02d}:00",
                    "stop_id": "D", "stop_sequence": "2", "pickup_type": "0", "drop_off_type": "0",
                },
            )
        )
    tables["trips.txt"] = csv_text(
        ("route_id", "service_id", "trip_id", "direction_id", "trip_headsign"),
        trips,
    ).encode("utf-8")
    tables["stop_times.txt"] = csv_text(
        (
            "trip_id", "arrival_time", "departure_time", "stop_id",
            "stop_sequence", "pickup_type", "drop_off_type",
        ),
        stop_times,
    ).encode("utf-8")
    return tables


class GtfsSchedulePlannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog = NetworkCatalog(self.root / "catalog.sqlite3", clock=lambda: FIXED_NOW)

    def tearDown(self):
        self.temporary.cleanup()

    def ingest(self, tables: dict[str, bytes]) -> None:
        path = self.root / "official.zip"
        digest = write_zip(path, tables)
        import_gtfs_zip(
            self.catalog,
            zip_path=path,
            expected_sha256=digest,
            source_url="https://example.go.kr/official/gtfs.zip",
            source_date="2026-08-31",
            provider="KTDB",
        )

    def node(self, name: str) -> str:
        with self.catalog.connect() as connection:
            return str(
                connection.execute(
                    "SELECT node_id FROM gtfs_stops WHERE stop_name=?", (name,)
                ).fetchone()[0]
            )

    def test_civil_midnight_considers_previous_service_day_24_hour_trip(self):
        self.ingest(base_tables())
        result = self.catalog.plan_gtfs_schedule(
            provider="KTDB",
            schedule_source_id="ktdb-gtfs-2024",
            origin_node_id=self.node("중간"),
            destination_node_id=self.node("도착"),
            service_date="2026-09-01",
            departure_time="00:00",
        )
        self.assertEqual(result["status"], "READY")
        candidate = result["alternatives"][0]
        self.assertEqual(candidate["gtfs_service_dates"], ["2026-08-31"])
        self.assertEqual(candidate["departure_datetime"], "2026-09-01T00:06:00+09:00")
        self.assertEqual(candidate["arrival_datetime"], "2026-09-01T00:20:00+09:00")
        self.assertEqual(result["graph"]["algorithm"], "bounded_time_dependent_dijkstra")

    def test_transfer_has_exact_paired_replay_metadata(self):
        self.ingest(transfer_tables())
        result = self.catalog.plan_gtfs_schedule(
            provider="KTDB",
            schedule_source_id="ktdb-gtfs-2024",
            origin_node_id=self.node("출발"),
            destination_node_id=self.node("도착"),
            service_date="2026-08-31",
            departure_time="07:55",
        )
        candidate = result["alternatives"][0]
        self.assertEqual(candidate["transfers"], 1)
        self.assertTrue(candidate["replay_ready"])
        self.assertEqual(len(candidate["replay_legs"]), 1)
        leg = candidate["replay_legs"][0]
        self.assertEqual(leg["scheduled_arrival"], "08:20:00")
        self.assertEqual(leg["next_departure"], "08:30:00")
        self.assertEqual(leg["minimum_transfer_minutes"], 5)
        self.assertEqual(leg["time_evidence_source"], "ktdb-gtfs-2024")
        self.assertTrue(leg["time_evidence_trip_id"].startswith("GTFS:KTDB:T"))
        self.assertTrue(leg["next_time_evidence_trip_id"].startswith("GTFS:KTDB:T"))
        self.assertTrue(leg["time_evidence_feed_id"].startswith("gtfs_"))
        self.assertEqual(
            leg["time_evidence_feed_id"], leg["next_time_evidence_feed_id"]
        )
        self.assertEqual(leg["node_id"], leg["next_node_id"])
        self.assertEqual(leg["gtfs_service_date"], "2026-08-31")
        self.assertEqual(leg["next_gtfs_service_date"], "2026-08-31")

    def test_normal_terminal_pickup_dropoff_restrictions_keep_trip_routable(self):
        tables = transfer_tables()
        reader = csv.DictReader(
            io.StringIO(tables["stop_times.txt"].decode("utf-8"))
        )
        rows = list(reader)
        rows[0]["drop_off_type"] = "1"
        rows[1]["pickup_type"] = "1"
        rows[2]["drop_off_type"] = "1"
        rows[3]["pickup_type"] = "1"
        tables["stop_times.txt"] = csv_text(
            tuple(reader.fieldnames or ()), rows
        ).encode("utf-8")
        self.ingest(tables)
        result = self.catalog.plan_gtfs_schedule(
            provider="KTDB",
            schedule_source_id="ktdb-gtfs-2024",
            origin_node_id=self.node("출발"),
            destination_node_id=self.node("도착"),
            service_date="2026-08-31",
            departure_time="07:55",
        )
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["alternatives"][0]["transfers"], 1)

    def test_cross_service_day_transfer_is_not_falsely_replay_ready(self):
        tables = transfer_tables()
        stop_times = tables["stop_times.txt"].decode("utf-8")
        for before, after in (
            ("08:00:00", "24:00:00"),
            ("08:20:00", "24:10:00"),
            ("08:30:00", "00:20:00"),
            ("09:00:00", "00:50:00"),
        ):
            stop_times = stop_times.replace(before, after)
        tables["stop_times.txt"] = stop_times.encode("utf-8")
        self.ingest(tables)
        result = self.catalog.plan_gtfs_schedule(
            provider="KTDB",
            schedule_source_id="ktdb-gtfs-2024",
            origin_node_id=self.node("출발"),
            destination_node_id=self.node("도착"),
            service_date="2026-09-01",
            departure_time="00:00",
        )
        candidate = result["alternatives"][0]
        self.assertEqual(candidate["gtfs_service_dates"], ["2026-08-31", "2026-09-01"])
        self.assertFalse(candidate["replay_ready"])
        self.assertEqual(candidate["replay_legs"], [])
        self.assertEqual(
            candidate["replay_data_gaps"][0]["reason"],
            "CROSS_SERVICE_DAY_REPLAY_UNSUPPORTED",
        )

    def test_exact_replay_lookup_is_not_limited_to_first_hundred_trips(self):
        self.ingest(many_trip_tables())
        with self.catalog.connect() as connection:
            trip = connection.execute(
                "SELECT trip_namespace_id FROM gtfs_trips WHERE raw_trip_id='T104'"
            ).fetchone()[0]
            route = connection.execute(
                "SELECT graph_route_id FROM gtfs_patterns"
            ).fetchone()[0]
        record = self.catalog.gtfs_exact_stop_time_record(
            provider="KTDB",
            service_date="2026-08-31",
            graph_route_id=route,
            node_id=self.node("도착"),
            node_order=2,
            trip_namespace_id=trip,
        )
        self.assertFalse(record["data_gap"])
        self.assertEqual(record["trip_id"], trip)
        self.assertEqual(record["arrival_time"], "10:04:00")
        mismatch = self.catalog.gtfs_exact_stop_time_record(
            provider="KTDB",
            service_date="2026-08-31",
            graph_route_id=route,
            node_id=self.node("도착"),
            node_order=2,
            trip_namespace_id=trip,
            expected_feed_id="gtfs_000000000000000000000000",
        )
        self.assertEqual(mismatch["reason"], "ACTIVE_GTFS_FEED_VERSION_MISMATCH")

    def test_departure_cap_never_claims_complete_earliest_arrival(self):
        self.ingest(many_trip_tables())
        with patch("network_catalog.MAX_GTFS_SCHEDULE_DEPARTURES_PER_STOP", 2):
            result = self.catalog.plan_gtfs_schedule(
                provider="KTDB",
                schedule_source_id="ktdb-gtfs-2024",
                origin_node_id=self.node("출발"),
                destination_node_id=self.node("도착"),
                service_date="2026-08-31",
                departure_time="08:00",
            )
        self.assertEqual(result["status"], "DATA_GAP")
        self.assertEqual(
            result["schedule"]["detail_reason"], "SCHEDULE_SEARCH_BOUND_REACHED"
        )
        self.assertTrue(result["graph"]["boarding_limit_hit"])

    def test_tentative_destination_at_expansion_cap_is_not_ready(self):
        self.ingest(base_tables())
        with patch("network_catalog.MAX_GTFS_SCHEDULE_EXPANSIONS", 1):
            result = self.catalog.plan_gtfs_schedule(
                provider="KTDB",
                schedule_source_id="ktdb-gtfs-2024",
                origin_node_id=self.node("출발"),
                destination_node_id=self.node("도착"),
                service_date="2026-08-31",
                departure_time="23:40",
            )
        self.assertEqual(result["status"], "DATA_GAP")
        self.assertEqual(
            result["schedule"]["detail_reason"], "SEARCH_EXPANSION_LIMIT_REACHED"
        )

    def test_calendar_date_removal_overrides_weekday_service(self):
        tables = transfer_tables()
        tables["calendar_dates.txt"] = csv_text(
            ("service_id", "date", "exception_type"),
            ({"service_id": "WK", "date": "20260831", "exception_type": "2"},),
        ).encode("utf-8")
        self.ingest(tables)
        result = self.catalog.plan_gtfs_schedule(
            provider="KTDB",
            schedule_source_id="ktdb-gtfs-2024",
            origin_node_id=self.node("출발"),
            destination_node_id=self.node("도착"),
            service_date="2026-08-31",
            departure_time="07:55",
        )
        self.assertEqual(result["reason"], "SCHEDULE_DATA_GAP")
        self.assertEqual(
            result["schedule"]["detail_reason"],
            "NO_OPERATING_GTFS_PATH_AT_REQUESTED_TIME",
        )

    def test_missing_feed_and_non_operating_time_are_schedule_data_gaps(self):
        missing = self.catalog.plan_gtfs_schedule(
            provider="KTDB",
            schedule_source_id="ktdb-gtfs-2024",
            origin_node_id="GTFS:KTDB:S00000000000000000000",
            destination_node_id="GTFS:KTDB:S11111111111111111111",
            service_date="2026-08-31",
            departure_time="08:00",
        )
        self.assertEqual(missing["reason"], "SCHEDULE_DATA_GAP")
        self.assertEqual(missing["schedule"]["detail_reason"], "ACTIVE_GTFS_FEED_REQUIRED")

        self.ingest(transfer_tables())
        weekend = self.catalog.plan_gtfs_schedule(
            provider="KTDB",
            schedule_source_id="ktdb-gtfs-2024",
            origin_node_id=self.node("출발"),
            destination_node_id=self.node("도착"),
            service_date="2026-08-30",
            departure_time="07:55",
        )
        self.assertEqual(weekend["reason"], "SCHEDULE_DATA_GAP")
        self.assertEqual(
            weekend["schedule"]["detail_reason"],
            "NO_OPERATING_GTFS_PATH_AT_REQUESTED_TIME",
        )

    def test_app_returns_scheduled_candidates_and_separate_static_fallback(self):
        settings = Settings(
            fixture_mode=True,
            db_path=self.root / "runtime.sqlite3",
            network_catalog_path=self.root / "app-catalog.sqlite3",
            fixture_path=SERVICE_DIR / "fixtures" / "tago_arrivals.json",
            position_fixture_path=SERVICE_DIR / "fixtures" / "tago_positions.json",
            catalog_fixture_path=SERVICE_DIR / "fixtures" / "tago_catalog.json",
            fixture_delays_path=SERVICE_DIR / "fixtures" / "delay_samples.json",
        )
        service = BusroService(settings, clock=lambda: FIXED_NOW)
        path = self.root / "app-official.zip"
        digest = write_zip(path, transfer_tables())
        import_gtfs_zip(
            service.network_catalog,
            zip_path=path,
            expected_sha256=digest,
            source_url="https://example.go.kr/official/gtfs.zip",
            source_date="2026-08-31",
            provider="KTDB",
        )
        with service.network_catalog.connect() as connection:
            nodes = {
                row["stop_name"]: row["node_id"]
                for row in connection.execute("SELECT stop_name,node_id FROM gtfs_stops")
            }
        response = service.generate_journeys(
            {
                "from_stop_id": nodes["출발"],
                "to_stop_id": nodes["도착"],
                "service_date": "2026-08-31",
                "departure_time": "07:55",
                "max_alternatives": 3,
            }
        )
        self.assertEqual(response["status"], "READY")
        self.assertTrue(response["schedule"]["feed_id"].startswith("gtfs_"))
        self.assertNotIn("source_url", response["schedule"])
        self.assertEqual(response["schedule"]["timezone"], "Asia/Seoul")
        self.assertEqual(response["preference_applied"], "earliest_arrival")
        self.assertTrue(response["preference_ignored"])
        self.assertTrue(response["candidates"][0]["scheduled"])
        self.assertTrue(
            response["candidates"][0]["arrival_datetime"].endswith("+09:00")
        )
        self.assertEqual(
            response["candidates"][0]["transfer_model"],
            "EXACT_STOP_SERVER_5_MIN",
        )
        self.assertEqual(len(response["candidates"][0]["replay_legs"]), 1)
        self.assertEqual(response["static_alternatives"], [])
        self.assertEqual(
            response["static_alternatives_notice"],
            "NOT_COMPUTED_SCHEDULE_READY",
        )

        with patch.object(
            service.network_catalog,
            "plan_gtfs_schedule",
            return_value={
                "status": "DATA_GAP",
                "reason": "SCHEDULE_BUSY",
                "schedule": {"status": "DATA_GAP", "reason": "SCHEDULE_BUSY"},
                "graph": {"search_complete": False},
                "alternatives": [],
            },
        ):
            with self.assertRaises(AppError) as busy:
                service.generate_journeys(
                    {
                        "from_stop_id": nodes["출발"],
                        "to_stop_id": nodes["도착"],
                        "service_date": "2026-08-31",
                        "departure_time": "07:56",
                    }
                )
        self.assertEqual(busy.exception.status, 429)
        self.assertEqual(busy.exception.details["retry_after_seconds"], 1)

    def test_same_schedule_request_is_singleflight_cached_and_deadline_bounded(self):
        self.ingest(transfer_tables())
        request = {
            "provider": "KTDB",
            "schedule_source_id": "ktdb-gtfs-2024",
            "origin_node_id": self.node("출발"),
            "destination_node_id": self.node("도착"),
            "service_date": "2026-08-31",
            "departure_time": "07:55",
        }
        with patch.object(
            self.catalog,
            "_plan_gtfs_schedule_uncached",
            wraps=self.catalog._plan_gtfs_schedule_uncached,
        ) as uncached:
            with ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(lambda _index: self.catalog.plan_gtfs_schedule(**request), range(24)))
        self.assertEqual(uncached.call_count, 1)
        self.assertTrue(all(item["status"] == "READY" for item in results))
        results[0]["status"] = "MUTATED"
        self.assertEqual(self.catalog.plan_gtfs_schedule(**request)["status"], "READY")

        deadline_request = {**request, "departure_time": "07:56"}
        with patch("network_catalog.GTFS_SCHEDULE_WALL_CLOCK_SECONDS", 0.0):
            deadline = self.catalog.plan_gtfs_schedule(**deadline_request)
        self.assertEqual(deadline["status"], "DATA_GAP")
        self.assertEqual(
            deadline["schedule"]["detail_reason"], "SCHEDULE_DEADLINE_REACHED"
        )
        self.assertFalse(deadline["graph"]["search_complete"])


if __name__ == "__main__":
    unittest.main()
