from __future__ import annotations

import io
from pathlib import Path
import sys
import unittest


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from municipal_source_discovery import (  # noqa: E402
    DataGoKrMunicipalDiscovery,
    DiscoveryError,
    parse_search_html,
)


class _Response:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size: int) -> bytes:
        return self.body[:size]


class _Opener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.urls: list[str] = []

    def open(self, request, timeout: float):
        self.urls.append(request.full_url)
        return _Response(self.body)


class MunicipalDiscoveryCase(unittest.TestCase):
    def test_parser_extracts_allowlisted_dataset_links_and_deduplicates(self) -> None:
        html = """
        <a href="/data/15092750/openapi.do"><span>부산광역시_부산 버스 정보시스템</span></a>
        <a href="/data/15092750/openapi.do">duplicate</a>
        <a href="https://evil.example/data/9/openapi.do">ignore</a>
        <a href="/data/15080662/openapi.do">경기도 버스 노선 조회</a>
        <a href="/data/15080662/openapi.do?x=1">query is not identity-bound</a>
        """
        rows = parse_search_html(html, query="버스", page=1)
        self.assertEqual([row.public_data_pk for row in rows], ["15092750", "15080662"])
        self.assertEqual(rows[0].detail_url, "https://www.data.go.kr/data/15092750/openapi.do")
        self.assertEqual(rows[0].dataset_kind, "openapi")

    def test_client_is_bounded_and_keeps_query_out_of_result_identity(self) -> None:
        opener = _Opener('<a href="/data/15092750/openapi.do">부산 버스</a>'.encode("utf-8"))
        client = DataGoKrMunicipalDiscovery(opener=opener)
        rows = client.search("부산 버스", page=1, per_page=20)
        self.assertEqual(len(rows), 1)
        self.assertIn("dType=API", opener.urls[0])
        self.assertIn("%EB%B6%80%EC%82%B0", opener.urls[0])
        with self.assertRaises(DiscoveryError):
            client.search("x" * 81)


if __name__ == "__main__":
    unittest.main()
