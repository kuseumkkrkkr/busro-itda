from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from journey_research import load_research_cases, verify_research_cases  # noqa: E402
from network_catalog import NetworkCatalog  # noqa: E402


class JourneyResearchCase(unittest.TestCase):
    def test_curated_catalog_is_bounded_and_uses_300m_policy(self) -> None:
        cases = load_research_cases()
        self.assertEqual(len(cases), 9)
        self.assertTrue(all(case["source_url"].startswith("https://") for case in cases))

    def test_report_distinguishes_hydrated_labels_from_plans_and_walk_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = NetworkCatalog(Path(directory) / "catalog.sqlite3")
            with catalog.connect() as connection:
                connection.executemany(
                    "INSERT INTO route_sequence_versions VALUES(?,?,?,?,?,?,?,?)",
                    [
                        ("seq-991", "12", "R991", "test", "2026-09-01", "a" * 64, 2, "2026-09-01"),
                        ("seq-b1", "25", "RB1", "test", "2026-09-01", "b" * 64, 2, "2026-09-01"),
                    ],
                )
                connection.executemany(
                    "INSERT INTO topology_targets VALUES(?,?,?,?,?,?)",
                    [
                        ("TAGO", "12", "R991", "991", "test", "2026-09-01"),
                        ("TAGO", "25", "RB1", "B1", "test", "2026-09-01"),
                    ],
                )
                connection.executemany(
                    "INSERT INTO active_route_sequences VALUES(?,?,?)",
                    [("12", "R991", "seq-991"), ("25", "RB1", "seq-b1")],
                )
                connection.commit()
            cases = (
                {
                    "id": "observed",
                    "title": "Observed",
                    "region": "Test",
                    "evidence_level": "observed_report",
                    "from_hint": "A",
                    "to_hint": "B",
                    "route_labels": ["991", "B1"],
                    "notes": "test",
                    "source_title": "test",
                    "source_url": "https://example.com/test",
                },
                {
                    "id": "planned",
                    "title": "Planned",
                    "region": "Test",
                    "evidence_level": "planned_report",
                    "from_hint": "A",
                    "to_hint": "B",
                    "route_labels": ["991"],
                    "notes": "test",
                    "source_title": "test",
                    "source_url": "https://example.com/test",
                },
                {
                    "id": "walk",
                    "title": "Walk",
                    "region": "Test",
                    "evidence_level": "observed_walk_gap",
                    "from_hint": "A",
                    "to_hint": "B",
                    "route_labels": ["991"],
                    "walk_gap_m": 301,
                    "notes": "test",
                    "source_title": "test",
                    "source_url": "https://example.com/test",
                },
            )
            report = verify_research_cases(catalog, cases)
            self.assertEqual(report[0]["verification"], "ROUTE_LABELS_HYDRATED")
            self.assertEqual(report[1]["verification"], "PLANNED_NEEDS_LIVE_VERIFY")
            self.assertEqual(report[2]["verification"], "WALK_GAP_OVER_300M")


if __name__ == "__main__":
    unittest.main()
