from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
from xml.sax.saxutils import escape
import sys
import tempfile
import unittest
from zipfile import ZIP_DEFLATED, ZipFile


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from network_catalog import CatalogLimitError, NetworkCatalog  # noqa: E402
from seoul_topology_ingest import (  # noqa: E402
    SEOUL_COLUMNS,
    SEOUL_PROFILE,
    SeoulTopologyError,
    import_seoul_topology_xlsx,
    parse_seoul_topology_xlsx,
)


FIXED_NOW = datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc)


def _xlsx(rows, header=SEOUL_COLUMNS) -> bytes:
    all_rows = [header, *rows]
    sheet_rows = []
    for row_number, row in enumerate(all_rows, start=1):
        cells = []
        for index, value in enumerate(row, start=1):
            column = chr(ord("A") + index - 1)
            reference = f"{column}{row_number}"
            if row_number > 1 and index in {1, 3, 4, 7, 8}:
                cells.append(f'<c r="{reference}"><v>{escape(str(value))}</v></c>')
            else:
                cells.append(
                    f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
                )
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:H{len(all_rows)}"/><sheetData>{"".join(sheet_rows)}</sheetData>'
        '</worksheet>'
    )
    output = io.BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>',
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>',
        )
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


def _row(**overrides):
    values = {
        "ROUTE_ID": "121900014",
        "노선명": "서초22",
        "순번": "2",
        "NODE_ID": "121000081",
        "ARS_ID": "02157",
        "정류소명": "정류소A",
        "X좌표": "127.015601",
        "Y좌표": "37.484755",
    }
    values.update(overrides)
    return tuple(values[column] for column in SEOUL_COLUMNS)


def _profile(data: bytes, *, rows: int, routes: int, stops: int, gaps: int):
    return replace(
        SEOUL_PROFILE,
        expected_sha256=hashlib.sha256(data).hexdigest(),
        expected_file_bytes=len(data),
        expected_rows=rows,
        expected_routes=routes,
        expected_unique_stops=stops,
        expected_non_contiguous_routes=gaps,
    )


class SeoulTopologyIngestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = NetworkCatalog(
            Path(self.temp.name) / "catalog.sqlite3", clock=lambda: FIXED_NOW
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def gap_rows():
        return [
            _row(),
            _row(
                **{
                    "순번": "4",
                    "NODE_ID": "121000082",
                    "ARS_ID": "02158",
                    "정류소명": "정류소B",
                    "X좌표": "127.016601",
                    "Y좌표": "37.485755",
                }
            ),
        ]

    def test_import_preserves_numeric_identifiers_and_published_order_gap(self):
        data = _xlsx(self.gap_rows())
        profile = _profile(data, rows=2, routes=1, stops=2, gaps=1)
        result = import_seoul_topology_xlsx(
            catalog=self.catalog, data=data, profile=profile
        )

        self.assertEqual(result["route_count"], 1)
        self.assertEqual(result["non_contiguous_route_count"], 1)
        self.assertEqual(result["activated"], 1)
        with self.catalog.connect() as connection:
            active = connection.execute(
                "SELECT route_id,sequence_id FROM active_route_sequences"
            ).fetchone()
            self.assertEqual(active["route_id"], "121900014")
            rows = connection.execute(
                "SELECT node_order,node_id FROM route_sequence_stops "
                "WHERE sequence_id=? ORDER BY node_order",
                (active["sequence_id"],),
            ).fetchall()
            self.assertEqual(
                [(row["node_order"], row["node_id"]) for row in rows],
                [(2, "121000081"), (4, "121000082")],
            )

    def test_same_snapshot_is_idempotent_and_keeps_provenance(self):
        data = _xlsx(self.gap_rows())
        profile = _profile(data, rows=2, routes=1, stops=2, gaps=1)
        first = import_seoul_topology_xlsx(
            catalog=self.catalog, data=data, profile=profile
        )
        second = import_seoul_topology_xlsx(
            catalog=self.catalog, data=data, profile=profile
        )
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["activated"], 0)
        source = self.catalog.planning_snapshot().route_sequences[0].source
        self.assertIn('"kind":"OFFICIAL_SEOUL_ROUTE_STOP_XLSX"', source)
        self.assertIn('"source_date":"2026-08-04"', source)
        self.assertIn(hashlib.sha256(data).hexdigest().upper(), source)

    def test_schema_hash_cardinality_and_bounds_are_strict(self):
        data = _xlsx(self.gap_rows())
        profile = _profile(data, rows=2, routes=1, stops=2, gaps=1)
        with self.assertRaises(SeoulTopologyError):
            parse_seoul_topology_xlsx(data, profile=replace(profile, expected_sha256="0" * 64))
        with self.assertRaises(SeoulTopologyError):
            parse_seoul_topology_xlsx(data, profile=replace(profile, expected_rows=3))
        with self.assertRaises(CatalogLimitError):
            parse_seoul_topology_xlsx(
                data, profile=profile, max_xlsx_bytes=len(data) - 1
            )

        wrong = _xlsx(self.gap_rows(), header=(*SEOUL_COLUMNS[:-1], "위도"))
        with self.assertRaises(SeoulTopologyError):
            parse_seoul_topology_xlsx(
                wrong,
                profile=_profile(wrong, rows=2, routes=1, stops=2, gaps=1),
            )

    def test_duplicate_or_decreasing_order_and_bad_coordinate_are_rejected(self):
        duplicate_rows = self.gap_rows()
        duplicate_rows[1] = _row(
            **{
                "순번": "2",
                "NODE_ID": "121000082",
                "ARS_ID": "02158",
                "정류소명": "정류소B",
            }
        )
        duplicate = _xlsx(duplicate_rows)
        with self.assertRaisesRegex(SeoulTopologyError, "unique and increasing"):
            parse_seoul_topology_xlsx(
                duplicate,
                profile=_profile(duplicate, rows=2, routes=1, stops=2, gaps=1),
            )

        invalid_rows = self.gap_rows()
        invalid_rows[1] = _row(
            **{
                "순번": "4",
                "NODE_ID": "121000082",
                "ARS_ID": "02158",
                "정류소명": "정류소B",
                "Y좌표": "51.0",
            }
        )
        invalid = _xlsx(invalid_rows)
        with self.assertRaisesRegex(SeoulTopologyError, "outside"):
            parse_seoul_topology_xlsx(
                invalid,
                profile=_profile(invalid, rows=2, routes=1, stops=2, gaps=1),
            )


if __name__ == "__main__":
    unittest.main()
