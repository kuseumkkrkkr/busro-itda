from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from osm import MAX_STOPS, OSMError, fetch_bus_relation, resolve_route_geometry  # noqa: E402


STOPS = [
    {"node_id": "A", "latitude": 36.601, "longitude": 127.298},
    {"node_id": "B", "latitude": 36.585, "longitude": 127.305},
    {"node_id": "C", "latitude": 36.565, "longitude": 127.315},
]


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        return self.payload[:limit]


class OSMCase(unittest.TestCase):
    @patch("osm.urlopen")
    def test_bus_relation_geometry_has_truthful_precision(self, mocked) -> None:
        mocked.return_value = FakeResponse(
            {
                "elements": [
                    {
                        "type": "relation",
                        "id": 10732231,
                        "tags": {"name": "세종 시내버스 601", "ref": "601", "network": "세종특별자치시 시내버스"},
                        "members": [
                            {
                                "type": "way",
                                "geometry": [
                                    {"lat": 36.601, "lon": 127.298},
                                    {"lat": 36.585, "lon": 127.305},
                                    {"lat": 36.565, "lon": 127.315},
                                ],
                            }
                        ],
                    }
                ]
            }
        )
        result = fetch_bus_relation(route_ref="601", stops=STOPS)
        self.assertIsNotNone(result)
        self.assertEqual(result["geometry_source"], "osm_bus_relation")
        self.assertEqual(result["geometry"]["type"], "MultiLineString")
        self.assertFalse(result["verified_operator_shape"])
        self.assertEqual(result["relation"]["id"], "10732231")
        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "https://overpass-api.de/api/interpreter")
        self.assertNotIn("serviceKey", request.data.decode("utf-8"))
        self.assertIn("out body geom", parse_qs(request.data.decode("utf-8"))["data"][0])

    @patch("osm.urlopen")
    def test_empty_relation_falls_back_to_labelled_road_estimate(self, mocked) -> None:
        mocked.side_effect = [
            FakeResponse({"elements": []}),
            FakeResponse(
                {
                    "code": "Ok",
                    "routes": [
                        {
                            "distance": 4200.5,
                            "duration": 790.2,
                            "geometry": {
                                "type": "LineString",
                                "coordinates": [[127.298, 36.601], [127.305, 36.585], [127.315, 36.565]],
                            },
                        }
                    ],
                }
            ),
        ]
        result = resolve_route_geometry(route_ref="601", stops=STOPS)
        self.assertEqual(result["geometry_source"], "osm_road_route_estimate")
        self.assertEqual(result["precision"], "ordered_stops_road_estimate")
        self.assertIn("estimate", result["data_gap"])
        self.assertFalse(result["verified_operator_shape"])

    def test_route_ref_injection_is_rejected(self) -> None:
        with self.assertRaises(OSMError) as raised:
            fetch_bus_relation(route_ref='601"];out;node["x"="', stops=STOPS)
        self.assertEqual(raised.exception.code, "INVALID_ROUTE_REF")
        self.assertEqual(raised.exception.status, 400)

    def test_outside_korea_and_excessive_stops_are_rejected(self) -> None:
        with self.assertRaises(OSMError) as outside:
            fetch_bus_relation(
                route_ref="601",
                stops=[{"latitude": 52.5, "longitude": 13.4}, {"latitude": 52.6, "longitude": 13.5}],
            )
        self.assertEqual(outside.exception.code, "STOP_OUTSIDE_KOREA")
        excessive = [
            {"latitude": 36.5 + index * 0.00001, "longitude": 127.2}
            for index in range(MAX_STOPS + 1)
        ]
        with self.assertRaises(OSMError) as too_many:
            fetch_bus_relation(route_ref="601", stops=excessive)
        self.assertEqual(too_many.exception.code, "TOO_MANY_STOPS")


if __name__ == "__main__":
    unittest.main()
