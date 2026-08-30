from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import tracemalloc
import unittest
from unittest import mock
import zipfile


SERVICE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_DIR))

import gtfs_ingest as gtfs_module  # noqa: E402
from gtfs_ingest import GtfsImportError, GtfsImportLimits, import_gtfs_zip  # noqa: E402
from network_catalog import CatalogLimitError, NetworkCatalog  # noqa: E402


def csv_text(columns, rows) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def base_tables() -> dict[str, bytes]:
    stop_a = "정류장 A/원본"
    stop_b = "정류장 B/원본"
    stop_c = "정류장 C/원본"
    return {
        "stops.txt": csv_text(
            ("stop_id", "stop_name", "stop_lat", "stop_lon"),
            (
                {"stop_id": stop_a, "stop_name": "출발", "stop_lat": "37.50", "stop_lon": "127.00"},
                {"stop_id": stop_b, "stop_name": "중간", "stop_lat": "37.51", "stop_lon": "127.01"},
                {"stop_id": stop_c, "stop_name": "도착", "stop_lat": "37.52", "stop_lon": "127.02"},
            ),
        ).encode("utf-8"),
        "routes.txt": csv_text(
            ("route_id", "route_short_name", "route_long_name", "route_type"),
            (
                {"route_id": "원본 노선/1", "route_short_name": "1", "route_long_name": "왕복", "route_type": "3"},
                {"route_id": "철도/제외", "route_short_name": "R", "route_long_name": "버스 아님", "route_type": "2"},
            ),
        ).encode("utf-8"),
        "calendar.txt": csv_text(
            (
                "service_id", "monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday", "start_date", "end_date",
            ),
            (
                {
                    "service_id": "평일 서비스", "monday": "1", "tuesday": "1",
                    "wednesday": "1", "thursday": "1", "friday": "1",
                    "saturday": "0", "sunday": "0", "start_date": "20260801",
                    "end_date": "20260930",
                },
            ),
        ).encode("utf-8"),
        "calendar_dates.txt": csv_text(
            ("service_id", "date", "exception_type"),
            ({"service_id": "평일 서비스", "date": "20260830", "exception_type": "1"},),
        ).encode("utf-8"),
        "trips.txt": csv_text(
            ("route_id", "service_id", "trip_id", "direction_id", "trip_headsign"),
            (
                {"route_id": "원본 노선/1", "service_id": "평일 서비스", "trip_id": "왕복/상행", "direction_id": "0", "trip_headsign": "도착 방면"},
                {"route_id": "원본 노선/1", "service_id": "평일 서비스", "trip_id": "왕복/하행", "direction_id": "1", "trip_headsign": "출발 방면"},
                {"route_id": "철도/제외", "service_id": "평일 서비스", "trip_id": "철도/trip", "direction_id": "0", "trip_headsign": "그래프 제외"},
            ),
        ).encode("utf-8"),
        "stop_times.txt": csv_text(
            (
                "trip_id", "arrival_time", "departure_time", "stop_id",
                "stop_sequence", "pickup_type", "drop_off_type",
            ),
            (
                {"trip_id": "왕복/상행", "arrival_time": "23:50:00", "departure_time": "23:50:00", "stop_id": stop_a, "stop_sequence": "1", "pickup_type": "0", "drop_off_type": "0"},
                {"trip_id": "왕복/상행", "arrival_time": "24:05:00", "departure_time": "24:06:00", "stop_id": stop_b, "stop_sequence": "2", "pickup_type": "0", "drop_off_type": "0"},
                {"trip_id": "왕복/상행", "arrival_time": "24:20:00", "departure_time": "24:20:00", "stop_id": stop_c, "stop_sequence": "3", "pickup_type": "0", "drop_off_type": "0"},
                {"trip_id": "왕복/하행", "arrival_time": "25:00:00", "departure_time": "25:00:00", "stop_id": stop_c, "stop_sequence": "1", "pickup_type": "0", "drop_off_type": "0"},
                {"trip_id": "왕복/하행", "arrival_time": "25:15:00", "departure_time": "25:16:00", "stop_id": stop_b, "stop_sequence": "2", "pickup_type": "0", "drop_off_type": "0"},
                {"trip_id": "왕복/하행", "arrival_time": "25:30:00", "departure_time": "25:30:00", "stop_id": stop_a, "stop_sequence": "3", "pickup_type": "0", "drop_off_type": "0"},
                {"trip_id": "철도/trip", "arrival_time": "08:00:00", "departure_time": "08:00:00", "stop_id": stop_a, "stop_sequence": "1", "pickup_type": "0", "drop_off_type": "0"},
                {"trip_id": "철도/trip", "arrival_time": "08:30:00", "departure_time": "08:30:00", "stop_id": stop_c, "stop_sequence": "2", "pickup_type": "0", "drop_off_type": "0"},
            ),
        ).encode("utf-8"),
    }


def write_zip(path: Path, tables: dict[str, bytes], *, extra: dict[str, bytes] | None = None) -> str:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in {**tables, **(extra or {})}.items():
            archive.writestr(name, content)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GtfsIngestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog = NetworkCatalog(self.root / "catalog.sqlite3")

    def tearDown(self):
        self.temporary.cleanup()

    def ingest(self, path: Path, digest: str, *, source_date: str = "2026-08-31", limits=None):
        return import_gtfs_zip(
            self.catalog,
            zip_path=path,
            expected_sha256=digest,
            source_url="https://example.go.kr/official/gtfs.zip",
            source_date=source_date,
            provider="KTDB",
            limits=limits,
        )

    def test_bidirectional_patterns_calendar_24_hour_times_and_aliases(self):
        path = self.root / "official.zip"
        digest = write_zip(path, base_tables())

        result = self.ingest(path, digest)

        self.assertTrue(result["created"])
        self.assertTrue(result["activated"])
        self.assertEqual(
            result["staging"], "DISK_BACKED_VALIDATED_THEN_ATOMICALLY_ACTIVATED"
        )
        self.assertFalse(list(self.root.glob("gtfs-stage-*")))
        self.assertEqual(result["counts"]["bus_patterns"], 2)
        self.assertFalse(result["eligible_for_success_rate"])
        snapshot = self.catalog.planning_snapshot()
        self.assertEqual(len(snapshot.route_sequences), 2)
        forward = next(
            sequence for sequence in snapshot.route_sequences
            if [stop.node_name for stop in sequence.stops] == ["출발", "중간", "도착"]
        )
        reverse = next(
            sequence for sequence in snapshot.route_sequences
            if [stop.node_name for stop in sequence.stops] == ["도착", "중간", "출발"]
        )
        self.assertNotEqual(forward.route_id, reverse.route_id)
        self.assertTrue(forward.route_id.startswith("GTFS:KTDB:"))
        self.assertTrue(all(stop.node_id.startswith("GTFS:KTDB:S") for stop in forward.stops))

        monday = self.catalog.gtfs_schedule_evidence(
            provider="KTDB", graph_route_id=forward.route_id, service_date="2026-08-31"
        )
        self.assertFalse(monday["eligible_for_success_rate"])
        self.assertIsNone(monday["success_probability"])
        self.assertTrue(monday["trips"][0]["calendar"]["operates_on_date"])
        self.assertIn(
            "24:20:00",
            [item["arrival_time"] for item in monday["trips"][0]["stop_times"]],
        )
        self.assertIn(
            24 * 3600 + 20 * 60,
            [item["arrival_seconds"] for item in monday["trips"][0]["stop_times"]],
        )
        added_sunday = self.catalog.gtfs_schedule_evidence(
            provider="KTDB", graph_route_id=forward.route_id, service_date="2026-08-30"
        )
        self.assertEqual(added_sunday["trips"][0]["calendar"]["exception_type"], 1)
        self.assertTrue(added_sunday["trips"][0]["calendar"]["operates_on_date"])

        with self.catalog.connect() as connection:
            alias = connection.execute(
                "SELECT raw_id,namespaced_id FROM gtfs_id_aliases "
                "WHERE entity_type='STOP' AND raw_id=?",
                ("정류장 A/원본",),
            ).fetchone()
            table_hashes = connection.execute(
                "SELECT file_name,sha256 FROM gtfs_feed_tables ORDER BY file_name"
            ).fetchall()
        self.assertEqual(alias["raw_id"], "정류장 A/원본")
        self.assertNotEqual(alias["raw_id"], alias["namespaced_id"])
        self.assertEqual(
            {row["file_name"] for row in table_hashes},
            set(base_tables()),
        )
        self.assertTrue(all(len(row["sha256"]) == 64 for row in table_hashes))

    def test_same_feed_reimport_is_idempotent(self):
        path = self.root / "official.zip"
        digest = write_zip(path, base_tables())
        first = self.ingest(path, digest)
        second = self.ingest(path, digest)

        self.assertFalse(second["created"])
        self.assertFalse(second["activated"])
        self.assertEqual(second["revision"], first["revision"])
        with self.catalog.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM gtfs_feed_versions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM gtfs_patterns").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM active_route_sequences").fetchone()[0], 2)

    def test_unsafe_zip_path_and_compression_bomb_are_rejected(self):
        unsafe_path = self.root / "unsafe.zip"
        unsafe_tables = base_tables()
        unsafe_tables.pop("stops.txt")
        unsafe_digest = write_zip(
            unsafe_path, unsafe_tables, extra={"../stops.txt": base_tables()["stops.txt"]}
        )
        with self.assertRaises(GtfsImportError):
            self.ingest(unsafe_path, unsafe_digest)

        bomb_path = self.root / "bomb.zip"
        bomb_digest = write_zip(
            bomb_path, base_tables(), extra={"padding.txt": b"A" * 100_000}
        )
        with self.assertRaises(CatalogLimitError):
            self.ingest(
                bomb_path,
                bomb_digest,
                limits=GtfsImportLimits(max_compression_ratio=5.0),
            )

        nested_path = self.root / "nested.zip"
        nested_digest = write_zip(
            nested_path,
            {f"공식자료/GTFS/{name}": content for name, content in base_tables().items()},
        )
        nested = self.ingest(nested_path, nested_digest)
        self.assertTrue(nested["created"])

    def test_invalid_feed_rolls_back_without_changing_active_feed(self):
        good_path = self.root / "good.zip"
        good_digest = write_zip(good_path, base_tables())
        good = self.ingest(good_path, good_digest)
        before = self.catalog.gtfs_feed_evidence(provider="KTDB")

        broken = base_tables()
        broken_text = broken["stop_times.txt"].decode("utf-8").replace(
            "정류장 C/원본", "없는 정류장", 1
        )
        broken["stop_times.txt"] = broken_text.encode("utf-8")
        bad_path = self.root / "bad.zip"
        bad_digest = write_zip(bad_path, broken)
        with self.assertRaises(GtfsImportError):
            self.ingest(bad_path, bad_digest, source_date="2026-09-01")

        after = self.catalog.gtfs_feed_evidence(provider="KTDB")
        self.assertEqual(after["feed"]["feed_id"], before["feed"]["feed_id"])
        with self.catalog.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM gtfs_feed_versions").fetchone()[0], 1)
            revision = int(connection.execute("SELECT value FROM catalog_meta WHERE key='revision'").fetchone()[0])
        self.assertEqual(revision, good["revision"])

    def test_explicit_non_monotonic_stop_times_are_rejected_before_activation(self):
        arrives_after_departure = base_tables()
        arrives_after_departure["stop_times.txt"] = (
            arrives_after_departure["stop_times.txt"]
            .decode("utf-8")
            .replace("24:20:00,24:20:00", "24:21:00,24:20:00", 1)
            .encode("utf-8")
        )
        first_path = self.root / "arrival-after-departure.zip"
        first_digest = write_zip(first_path, arrives_after_departure)
        with self.assertRaisesRegex(GtfsImportError, "arrives after departure"):
            self.ingest(first_path, first_digest)

        backwards = base_tables()
        backwards["stop_times.txt"] = (
            backwards["stop_times.txt"]
            .decode("utf-8")
            .replace("24:05:00,24:06:00", "23:40:00,23:41:00", 1)
            .encode("utf-8")
        )
        second_path = self.root / "backwards.zip"
        second_digest = write_zip(second_path, backwards)
        with self.assertRaisesRegex(GtfsImportError, "not time-monotonic"):
            self.ingest(second_path, second_digest)

        self.assertTrue(self.catalog.gtfs_feed_evidence(provider="KTDB")["data_gap"])

    def test_new_valid_feed_replaces_only_active_provider_patterns_atomically(self):
        first_path = self.root / "first.zip"
        first_digest = write_zip(first_path, base_tables())
        first = self.ingest(first_path, first_digest)
        self.assertEqual(len(self.catalog.planning_snapshot().route_sequences), 2)

        second_tables = base_tables()
        for file_name in ("trips.txt", "stop_times.txt"):
            reader = csv.DictReader(io.StringIO(second_tables[file_name].decode("utf-8")))
            rows = [row for row in reader if row["trip_id"] == "왕복/상행"]
            second_tables[file_name] = csv_text(tuple(reader.fieldnames or ()), rows).encode("utf-8")
        second_path = self.root / "second.zip"
        second_digest = write_zip(second_path, second_tables)
        second = self.ingest(second_path, second_digest, source_date="2026-09-01")

        self.assertTrue(second["created"])
        self.assertTrue(second["activated"])
        self.assertGreater(second["revision"], first["revision"])
        self.assertEqual(len(self.catalog.planning_snapshot().route_sequences), 1)
        with self.catalog.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM gtfs_feed_versions").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM active_route_sequences").fetchone()[0], 1)

    def test_sha_utf8_and_cell_limits_are_enforced_before_activation(self):
        path = self.root / "official.zip"
        digest = write_zip(path, base_tables())
        with self.assertRaises(GtfsImportError):
            self.ingest(path, "0" * 64)

        invalid_utf8 = base_tables()
        invalid_utf8["routes.txt"] = b"route_id,route_short_name,route_long_name,route_type\nR,\xff,x,3\n"
        utf8_path = self.root / "invalid-utf8.zip"
        utf8_digest = write_zip(utf8_path, invalid_utf8)
        with self.assertRaises(GtfsImportError):
            self.ingest(utf8_path, utf8_digest)

        long_cell = base_tables()
        long_cell["routes.txt"] = long_cell["routes.txt"].replace("왕복".encode(), ("가" * 100).encode())
        cell_path = self.root / "cell.zip"
        cell_digest = write_zip(cell_path, long_cell)
        with self.assertRaises(GtfsImportError):
            self.ingest(
                cell_path,
                cell_digest,
                limits=GtfsImportLimits(max_cell_chars=32),
            )

    def test_production_row_defaults_cover_ktdb_and_smaller_bounds_fail_safely(self):
        defaults = GtfsImportLimits()
        self.assertGreaterEqual(defaults.max_rows_per_table, 25_000_000)
        self.assertGreaterEqual(defaults.max_total_rows, 30_000_000)

        path = self.root / "bounded.zip"
        digest = write_zip(path, base_tables())
        with self.assertRaises(CatalogLimitError):
            self.ingest(
                path,
                digest,
                limits=GtfsImportLimits(max_rows_per_table=5),
            )
        self.assertFalse(self.catalog.gtfs_feed_evidence(provider="KTDB").get("feed"))
        self.assertFalse(list(self.root.glob("gtfs-stage-*")))

    def test_verified_descriptor_is_not_reopened_and_members_stream_once(self):
        path = self.root / "official.zip"
        digest = write_zip(path, base_tables())
        attacker_tables = base_tables()
        attacker_tables["stops.txt"] = attacker_tables["stops.txt"].replace(
            "출발".encode("utf-8"), "공격자".encode("utf-8")
        )
        attacker_path = self.root / "replacement.zip"
        write_zip(attacker_path, attacker_tables)
        real_zipfile = zipfile.ZipFile
        opened_members: list[str] = []
        constructor_inputs: list[object] = []

        class RaceGuardZipFile(real_zipfile):
            def __init__(self, file, *args, **kwargs):
                constructor_inputs.append(file)
                redirected = attacker_path if isinstance(file, (str, Path)) else file
                super().__init__(redirected, *args, **kwargs)

            def open(self, name, *args, **kwargs):
                member = name.filename if isinstance(name, zipfile.ZipInfo) else str(name)
                opened_members.append(member)
                return super().open(name, *args, **kwargs)

        with mock.patch.object(gtfs_module.zipfile, "ZipFile", RaceGuardZipFile):
            self.ingest(path, digest)

        self.assertEqual(len(constructor_inputs), 1)
        self.assertFalse(isinstance(constructor_inputs[0], (str, Path)))
        self.assertEqual(sorted(opened_members), sorted(base_tables()))
        with self.catalog.connect() as connection:
            names = {
                row[0] for row in connection.execute("SELECT stop_name FROM gtfs_stops")
            }
        self.assertIn("출발", names)
        self.assertNotIn("공격자", names)

    def test_total_rows_abort_inside_table_and_stage_disk_is_prebounded(self):
        path = self.root / "bounded-resources.zip"
        digest = write_zip(path, base_tables())
        with self.assertRaisesRegex(CatalogLimitError, "total rows"):
            self.ingest(
                path,
                digest,
                limits=GtfsImportLimits(max_total_rows=10, insert_batch_rows=1),
            )
        with self.assertRaisesRegex(CatalogLimitError, "page limit"):
            self.ingest(
                path,
                digest,
                limits=GtfsImportLimits(max_stage_bytes=16 * 1024),
            )
        with mock.patch.object(
            gtfs_module.shutil, "disk_usage", return_value=mock.Mock(free=0)
        ):
            with self.assertRaisesRegex(CatalogLimitError, "free space"):
                self.ingest(path, digest)
        self.assertFalse(self.catalog.gtfs_feed_evidence(provider="KTDB").get("feed"))
        self.assertFalse(list(self.root.glob("gtfs-stage-*")))

    def test_stop_level_boarding_restrictions_do_not_remove_bus_topology(self):
        tables = base_tables()
        reader = csv.DictReader(io.StringIO(tables["stop_times.txt"].decode("utf-8")))
        rows = list(reader)
        rows[0]["pickup_type"] = "1"
        tables["stop_times.txt"] = csv_text(
            tuple(reader.fieldnames or ()), rows
        ).encode("utf-8")
        path = self.root / "restricted.zip"
        digest = write_zip(path, tables)

        result = self.ingest(path, digest)

        self.assertEqual(result["counts"]["bus_patterns"], 2)
        sequences = self.catalog.planning_snapshot().route_sequences
        self.assertEqual(len(sequences), 2)
        with self.catalog.connect() as connection:
            trip = connection.execute(
                "SELECT pattern_id FROM gtfs_trips WHERE raw_trip_id='왕복/상행'"
            ).fetchone()
            restriction = connection.execute(
                "SELECT pickup_type FROM gtfs_stop_times "
                "WHERE raw_trip_id='왕복/상행' AND stop_sequence=1"
            ).fetchone()
        self.assertIsNotNone(trip["pattern_id"])
        self.assertEqual(restriction["pickup_type"], 1)

        rows[3]["drop_off_type"] = "2"
        tables["stop_times.txt"] = csv_text(
            tuple(reader.fieldnames or ()), rows
        ).encode("utf-8")
        all_restricted_path = self.root / "all-restricted.zip"
        all_restricted_digest = write_zip(all_restricted_path, tables)
        all_restricted = self.ingest(
            all_restricted_path, all_restricted_digest, source_date="2026-09-01"
        )
        self.assertEqual(all_restricted["counts"]["bus_patterns"], 2)
        self.assertEqual(len(self.catalog.planning_snapshot().route_sequences), 2)

    def test_single_trip_stop_limit_aborts_before_unbounded_list_growth(self):
        tables = base_tables()
        tables["trips.txt"] = csv_text(
            ("route_id", "service_id", "trip_id", "direction_id", "trip_headsign"),
            ({
                "route_id": "원본 노선/1", "service_id": "평일 서비스",
                "trip_id": "LONG", "direction_id": "0", "trip_headsign": "도착 방면",
            },),
        ).encode("utf-8")
        tables["stop_times.txt"] = csv_text(
            (
                "trip_id", "arrival_time", "departure_time", "stop_id",
                "stop_sequence", "pickup_type", "drop_off_type",
            ),
            (
                {
                    "trip_id": "LONG", "arrival_time": "08:00:00",
                    "departure_time": "08:00:00",
                    "stop_id": "정류장 A/원본" if index % 2 else "정류장 B/원본",
                    "stop_sequence": str(index), "pickup_type": "0", "drop_off_type": "0",
                }
                for index in range(1, gtfs_module.MAX_PATTERN_STOPS + 2)
            ),
        ).encode("utf-8")
        path = self.root / "too-long-trip.zip"
        digest = write_zip(path, tables)

        with self.assertRaisesRegex(CatalogLimitError, "exceeds 10000 stops"):
            self.ingest(path, digest)
        self.assertFalse(self.catalog.gtfs_feed_evidence(provider="KTDB").get("feed"))
        self.assertFalse(list(self.root.glob("gtfs-stage-*")))

    def test_stop_times_stream_to_disk_without_rows_sized_python_memory(self):
        tables = base_tables()
        trip_count = 10_000
        tables["trips.txt"] = csv_text(
            ("route_id", "service_id", "trip_id", "direction_id", "trip_headsign"),
            (
                {
                    "route_id": "원본 노선/1", "service_id": "평일 서비스",
                    "trip_id": f"T{index:05d}", "direction_id": "0",
                    "trip_headsign": "도착 방면",
                }
                for index in range(trip_count)
            ),
        ).encode("utf-8")
        tables["stop_times.txt"] = csv_text(
            (
                "trip_id", "arrival_time", "departure_time", "stop_id",
                "stop_sequence", "pickup_type", "drop_off_type",
            ),
            (
                {
                    "trip_id": f"T{trip:05d}", "arrival_time": time,
                    "departure_time": time, "stop_id": stop_id,
                    "stop_sequence": str(sequence), "pickup_type": "0", "drop_off_type": "0",
                }
                for trip in range(trip_count)
                for sequence, stop_id, time in (
                    (1, "정류장 A/원본", "24:00:00"),
                    (2, "정류장 C/원본", "24:20:00"),
                )
            ),
        ).encode("utf-8")
        path = self.root / "streaming.zip"
        digest = write_zip(path, tables)

        tracemalloc.start()
        try:
            result = self.ingest(path, digest)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result["counts"]["stop_times"], trip_count * 2)
        self.assertLess(peak, 64 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
