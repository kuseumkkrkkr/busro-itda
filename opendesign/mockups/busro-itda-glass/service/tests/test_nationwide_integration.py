from __future__ import annotations

from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.parse import quote


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from app import BusroService  # noqa: E402
from config import Settings  # noqa: E402
from network_catalog import CatalogValidationError, NetworkCatalog  # noqa: E402
from server import BusroHTTPServer, Handler  # noqa: E402


FIXED_NOW = datetime(2026, 8, 31, 3, 0, tzinfo=timezone.utc)
STOP_CSV = """정류장번호,정류장명,위도,경도,정보수집일,모바일단축번호,도시코드,도시명,관리도시명
DJB_FIXTURE_STOP_001,샘플기점,36.3504,127.3845,2025-10-31,90001,25,대전광역시,대전BIS
DJB_FIXTURE_STOP_002,샘플환승점,36.3541,127.3901,2025-10-31,90002,25,대전광역시,대전BIS
DJB_FIXTURE_STOP_003,샘플종점,36.3582,127.3972,2025-10-31,90003,25,대전광역시,대전BIS
""".encode("utf-8")
ROUTE_CSV = """노선 아이디,노선명,기점노드 아이디,종점노드 아이디,기점정류장,종점정류장,지자체코드,지자체명
DJB_FIXTURE_001,샘플1,DJB_FIXTURE_STOP_001,DJB_FIXTURE_STOP_003,샘플기점,샘플종점,25,대전광역시
""".encode("utf-8")


class NationwideIntegrationCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        settings = Settings(
            fixture_mode=True,
            db_path=root / "runtime.sqlite3",
            network_catalog_path=root / "catalog.sqlite3",
            fixture_path=SERVICE_DIR / "fixtures" / "tago_arrivals.json",
            position_fixture_path=SERVICE_DIR / "fixtures" / "tago_positions.json",
            catalog_fixture_path=SERVICE_DIR / "fixtures" / "tago_catalog.json",
            fixture_delays_path=SERVICE_DIR / "fixtures" / "delay_samples.json",
        )
        self.service = BusroService(settings, clock=lambda: FIXED_NOW)
        self.service.network_catalog.import_stops_csv(
            STOP_CSV,
            source_url="https://www.data.go.kr/data/15067528/fileData.do",
            source_date="2025-10-31",
        )
        self.service.network_catalog.import_routes_csv(
            ROUTE_CSV,
            source_url="https://www.data.go.kr/data/15105964/fileData.do",
            source_date="2026-07-16",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_route_hydration_is_idempotent_and_generator_never_invents_probability(self) -> None:
        first = self.service.hydrate_network_route(
            {"city_code": "25", "route_id": "DJB_FIXTURE_001"}
        )
        second = self.service.hydrate_network_route(
            {"city_code": "25", "route_id": "DJB_FIXTURE_001"}
        )
        self.assertEqual(first["sequence"]["sequence_id"], second["sequence"]["sequence_id"])
        self.assertFalse(second["sequence"]["created"])
        self.assertFalse(second["sequence"]["activated"])
        self.assertEqual(first["sequence"]["revision"], second["sequence"]["revision"])

        result = self.service.generate_journeys(
            {
                "from_stop_id": "DJB_FIXTURE_STOP_001",
                "to_stop_id": "DJB_FIXTURE_STOP_003",
                "preference": "diverse",
                "max_alternatives": 5,
            }
        )
        self.assertGreaterEqual(result["count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["route_ids"], ["DJB_FIXTURE_001"])
        self.assertEqual(candidate["status"], "DATA_GAP")
        self.assertIsNone(candidate["success_probability"])
        self.assertIn("VERIFIED_TIMETABLE_REQUIRED", candidate["reasons"])

    def test_live_observations_never_substitute_for_verified_timetable(self) -> None:
        route_id = "LIVE_ROUTE_1"
        self.service.network_catalog.hydrate_route_sequence(
            city_code="25",
            route_id=route_id,
            ordered_stops=[
                {"node_id": "LIVE_O", "node_name": "실제 기점", "node_order": 1,
                 "latitude": 36.35, "longitude": 127.38},
                {"node_id": "LIVE_D", "node_name": "실제 종점", "node_order": 2,
                 "latitude": 36.36, "longitude": 127.39},
            ],
            source="TAGO:getRouteAcctoThrghSttnList",
            captured_at="2026-08-31T03:00:00Z",
        )

        def persist_position(index: int) -> None:
            captured = FIXED_NOW + timedelta(minutes=index)
            marker = f"live-evidence-{index:02d}"
            self.service.store.create_position_snapshot(
                snapshot_id=f"pos_{marker}",
                idempotency_key=marker,
                request_hash=f"request-{marker}",
                payload_hash=f"payload-{marker}",
                source="TAGO_POSITION",
                city_code="25",
                route_id=route_id,
                captured_at=captured.isoformat().replace("+00:00", "Z"),
                service_date="2026-08-31",
                upstream={"result_code": "00"},
                positions=[{
                    "vehicle_no": "TEST-LIVE-BUS",
                    "node_id": f"OBS_{index + 1}",
                    "node_name": f"관측 {index + 1}",
                    "node_order": index + 1,
                    "latitude": 36.35 + index * 0.001,
                    "longitude": 127.38 + index * 0.001,
                }],
                maximum_gap_seconds=180,
            )

        request = {
            "from_stop_id": "LIVE_O",
            "to_stop_id": "LIVE_D",
            "max_alternatives": 1,
        }
        # Eight polls yield only seven transition outcomes: still insufficient.
        for index in range(8):
            persist_position(index)
        insufficient = self.service.generate_journeys(request)["candidates"][0]
        self.assertEqual(insufficient["status"], "DATA_GAP")
        self.assertIn("VERIFIED_TIMETABLE_REQUIRED", insufficient["reasons"])
        self.assertIn("PASSAGE_HISTORY_REQUIRED", insufficient["reasons"])
        self.assertIsNone(insufficient["success_probability"])
        self.assertEqual(insufficient["coverage"]["service_routes"], 0)
        self.assertEqual(insufficient["coverage"]["observed_service_routes"], 1)
        self.assertEqual(insufficient["coverage"]["passage_routes"], 0)

        # The eighth persisted PASSAGE outcome crosses the history threshold,
        # but observations still cannot verify a timetable or transfer window.
        persist_position(8)
        observed = self.service.generate_journeys(request)["candidates"][0]
        self.assertEqual(observed["status"], "DATA_GAP")
        self.assertEqual(observed["reasons"], ["VERIFIED_TIMETABLE_REQUIRED"])
        self.assertIsNone(observed["success_probability"])
        self.assertIsNone(observed["probability_basis"])
        self.assertIsNone(observed["probability_scope"])
        self.assertEqual(
            observed["evidence"]["service_routes"][route_id]["basis"],
            "persisted_live_tago_observations",
        )
        self.assertFalse(observed["evidence"]["service_routes"][route_id]["verified"])
        self.assertEqual(
            observed["evidence"]["passage_routes"][route_id]["sample_count"], 8
        )

    def test_invalid_official_rows_are_only_excluded_in_explicit_quarantine_mode(self) -> None:
        payload = STOP_CSV + (
            "DJB_BAD,좌표오류,127.1,36.1,2025-10-31,99999,25,대전광역시,대전BIS\n"
        ).encode("utf-8")
        strict = NetworkCatalog(Path(self.temp.name) / "strict.sqlite3")
        with self.assertRaises(CatalogValidationError):
            strict.import_stops_csv(
                payload,
                source_url="https://www.data.go.kr/data/15067528/fileData.do",
                source_date="2025-10-31",
            )
        quarantined = NetworkCatalog(Path(self.temp.name) / "quarantine.sqlite3")
        imported = quarantined.import_stops_csv(
            payload,
            source_url="https://www.data.go.kr/data/15067528/fileData.do",
            source_date="2025-10-31",
            quarantine_invalid_rows=True,
        )
        self.assertEqual(imported["quality"]["rejected_row_count"], 1)
        self.assertEqual(imported["quality"]["coordinates_corrected"], 0)
        self.assertEqual(len(quarantined.search_stops("", limit=10)), 3)
        self.assertEqual(quarantined.provenance()[0]["quality"]["rejected_row_count"], 1)

    def test_fifty_concurrent_journey_reads_are_deterministic(self) -> None:
        self.service.hydrate_network_route({"city_code": "25", "route_id": "DJB_FIXTURE_001"})
        barrier = threading.Barrier(50)

        def generate(_index: int):
            barrier.wait(timeout=5)
            return self.service.generate_journeys(
                {
                    "from_stop_id": "DJB_FIXTURE_STOP_001",
                    "to_stop_id": "DJB_FIXTURE_STOP_003",
                    "preference": "diverse",
                    "max_alternatives": 5,
                }
            )

        with ThreadPoolExecutor(max_workers=50) as executor:
            results = list(executor.map(generate, range(50)))
        candidate_ids = {result["candidates"][0]["id"] for result in results}
        self.assertEqual(len(candidate_ids), 1)
        self.assertTrue(all(result["candidates"][0]["success_probability"] is None for result in results))


class NationwideHTTPCase(NationwideIntegrationCase):
    def setUp(self) -> None:
        super().setUp()
        self.service.hydrate_network_route({"city_code": "25", "route_id": "DJB_FIXTURE_001"})
        self.server = BusroHTTPServer(("127.0.0.1", 0), Handler, service=self.service)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, method: str, path: str, body: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, dict(response.headers), raw)
        connection.close()
        return result

    def test_single_origin_web_network_search_generator_and_source_registry(self) -> None:
        status, headers, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<div id=\"root\">", raw)
        self.assertIn(b"sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=", raw)
        self.assertIn("Content-Security-Policy", headers)
        self.assertIn("https://tile.openstreetmap.org", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "strict-origin-when-cross-origin")

        status, _, raw = self.request("GET", f"/api/network/stops?q={quote('샘플')}&limit=5")
        stops = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(stops["count"], 3)

        status, _, raw = self.request(
            "POST",
            "/api/journeys/generate",
            {
                "from_stop_id": "DJB_FIXTURE_STOP_001",
                "to_stop_id": "DJB_FIXTURE_STOP_003",
                "preference": "diverse",
                "max_alternatives": 5,
            },
        )
        generated = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(generated["count"], 1)
        self.assertIsNone(generated["candidates"][0]["success_probability"])

        status, _, raw = self.request("GET", "/api/sources?limit=3")
        sources = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(sources["count"], 3)
        self.assertEqual(sources["priority_order"][0]["id"], "TAGO")

        status, _, _ = self.request("GET", "/%2e%2e/server.py")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
