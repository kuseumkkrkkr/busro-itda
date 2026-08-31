from __future__ import annotations

from collections import deque
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
import zipfile


SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from municipal_source_fetch import (  # noqa: E402
    ATTACHMENT_METADATA_PATH,
    DOWNLOAD_PATH,
    GWANGJU_HOST,
    GWANGJU_NOTICE_DOWNLOAD_PATH,
    GWANGJU_NOTICE_VIEW_PATH,
    GWANGJU_ORIGIN,
    GwangjuNoticeFetcher,
    MAX_NOTICE_HTML_BYTES,
    MunicipalSourceFetchError,
    MunicipalSourceFetcher,
    OFFICIAL_HOST,
    PreviousFetch,
    parse_gwangju_notice_html,
)


PUBLIC_DATA_PK = "15055904"
DETAIL_PK = "uddi:50829ade-7956-4989-a2b8-b8f0b9b1b21a"
ATTACHMENT_ID = "FILE_000000000012345"
GWANGJU_NOTICE_ID = "1209"
GWANGJU_TITLE = "시내버스 시간표 변경 안내(2026.8.22.~)"
GWANGJU_FILENAME = "시내버스운행시간표(평일)-26.8.22.xlsx"


def _metadata(*, attachment_id: str = ATTACHMENT_ID) -> bytes:
    return json.dumps(
        {
            "status": True,
            "publicDataPk": PUBLIC_DATA_PK,
            "publicDataDetailPk": DETAIL_PK,
            "atchFileId": attachment_id,
            "fileDetailSn": "1",
            "dataSetFileDetailInfo": {"dataNm": "official schedule"},
        }
    ).encode("utf-8")


def _package(fmt: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if fmt == "XLSX":
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("xl/workbook.xml", "<workbook/>")
        elif fmt == "HWPX":
            archive.writestr("mimetype", "application/hwp+zip")
            archive.writestr("Contents/content.hpf", "<package/>")
        else:
            raise AssertionError(fmt)
    return output.getvalue()


def _gwangju_notice(
    attachments: tuple[tuple[str, str, int], ...],
    *,
    notice_id: str = GWANGJU_NOTICE_ID,
    title: str = GWANGJU_TITLE,
) -> bytes:
    links = "".join(
        f"<a class=\"txt_pt13\" href=javascript:fnFileDown('{file_index}')>"
        f"★{filename}</a> ({byte_count:,} byte)<br/>"
        for file_index, filename, byte_count in attachments
    )
    return (
        "<!doctype html><html><body>"
        f'<input type="hidden" name="B_IDX" value="{notice_id}" />'
        f"<td>{title}</td><td>{links}</td>"
        "</body></html>"
    ).encode("utf-8")


def _gwangju_notice_response(body: bytes) -> "FakeResponse":
    return FakeResponse(
        200,
        body,
        {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Length": str(len(body)),
        },
    )


class FakeSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
        *,
        on_read=None,
    ) -> None:
        self.status = status
        self._body = io.BytesIO(body)
        self._headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.on_read = on_read
        self.read_calls = 0
        self.closed = False

    def getheader(self, name: str):
        return self._headers.get(name.lower())

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        if self.on_read is not None:
            self.on_read()
        return self._body.read(size)

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, owner, response: FakeResponse) -> None:
        self.owner = owner
        self.response = response
        self.sock = FakeSocket()
        self.request_data = None
        self.closed = False

    def request(self, method, target, body=None, headers=None) -> None:
        self.request_data = (method, target, body, dict(headers or {}))
        self.owner.requests.append(self.request_data)

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class FakeHTTP:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = deque(responses)
        self.connections: list[tuple[str, int, float, FakeConnection]] = []
        self.requests: list[tuple] = []

    def __call__(self, host: str, port: int, timeout: float) -> FakeConnection:
        if not self.responses:
            raise AssertionError("unexpected HTTP connection")
        connection = FakeConnection(self, self.responses.popleft())
        self.connections.append((host, port, timeout, connection))
        return connection


def _response_for_file(data: bytes, media_type: str) -> FakeResponse:
    return FakeResponse(
        200,
        data,
        {
            "Content-Type": media_type,
            "Content-Length": str(len(data)),
            "ETag": '"official-v1"',
            "Last-Modified": "Mon, 10 Nov 2025 00:00:00 GMT",
        },
    )


class MunicipalSourceFetcherCase(unittest.TestCase):
    def _fetch(self, root: Path, fake: FakeHTTP, fmt: str, **kwargs):
        fetcher = MunicipalSourceFetcher(
            timeout_seconds=10,
            max_bytes=2 * 1024 * 1024,
            _connection_factory=fake,
            **kwargs,
        )
        return fetcher.fetch(
            public_data_pk=PUBLIC_DATA_PK,
            public_data_detail_pk=DETAIL_PK,
            expected_format=fmt,
            quarantine_root=root,
        )

    def test_xlsx_is_content_addressed_and_repeat_is_idempotent(self) -> None:
        data = _package("XLSX")
        fake = FakeHTTP(
            [
                FakeResponse(200, _metadata(), {"Content-Length": str(len(_metadata()))}),
                _response_for_file(
                    data,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
                FakeResponse(200, _metadata()),
                _response_for_file(data, "application/octet-stream"),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._fetch(root, fake, "XLSX")
            first_stat = first.path.stat()
            second = self._fetch(root, fake, "XLSX")

            digest = hashlib.sha256(data).hexdigest()
            self.assertEqual(first.status, "DOWNLOADED")
            self.assertEqual(second.status, "UNCHANGED")
            self.assertEqual(first.sha256, digest)
            self.assertEqual(first.path, root / "data-go-kr" / f"{digest}.xlsx")
            self.assertEqual(second.path, first.path)
            self.assertEqual(first.path.read_bytes(), data)
            self.assertEqual(first.path.stat().st_ino, first_stat.st_ino)

        metadata_request, download_request = fake.requests[:2]
        self.assertEqual(metadata_request[0:2], ("POST", ATTACHMENT_METADATA_PATH))
        form = parse_qs(metadata_request[2].decode("ascii"), keep_blank_values=True)
        self.assertEqual(form["publicDataPk"], [PUBLIC_DATA_PK])
        self.assertEqual(form["publicDataDetailPk"], [DETAIL_PK])
        self.assertEqual(form["atchFileId"], [""])
        self.assertEqual(form["publicDataTyCode"], ["PR0051"])
        self.assertEqual(download_request[0], "GET")
        self.assertTrue(download_request[1].startswith(DOWNLOAD_PATH + "?"))
        self.assertNotIn(PUBLIC_DATA_PK, download_request[1])
        self.assertTrue(all(host == OFFICIAL_HOST for host, _, _, _ in fake.connections))

    def test_install_uses_live_o_excl_path_without_hard_link_support(self) -> None:
        data = _package("XLSX")
        fake = FakeHTTP(
            [
                FakeResponse(200, _metadata()),
                _response_for_file(data, "application/octet-stream"),
            ]
        )
        real_open = os.open
        final_open_flags: list[int] = []

        def observe_open(path, flags, mode=0o777):
            if Path(path).suffix.casefold() == ".xlsx":
                final_open_flags.append(flags)
            return real_open(path, flags, mode)

        unsupported_link = OSError(1, "Incorrect function")
        with tempfile.TemporaryDirectory() as temporary, patch(
            "municipal_source_fetch.os.link", side_effect=unsupported_link
        ) as link_mock, patch(
            "municipal_source_fetch.os.open", side_effect=observe_open
        ):
            result = self._fetch(Path(temporary), fake, "XLSX")
            self.assertEqual(result.path.read_bytes(), data)

        link_mock.assert_not_called()
        self.assertEqual(len(final_open_flags), 1)
        required = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        self.assertEqual(final_open_flags[0] & required, required)

    def test_exclusive_install_failure_removes_created_file_but_not_existing_file(self) -> None:
        data = _package("XLSX")
        digest = hashlib.sha256(data).hexdigest()
        fetcher = MunicipalSourceFetcher()
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary)
            final_path = store / f"{digest}.xlsx"
            first_temp = store / ".first.part"
            first_temp.write_bytes(data)
            with patch(
                "municipal_source_fetch.os.fsync",
                side_effect=OSError("simulated write failure"),
            ), self.assertRaises(MunicipalSourceFetchError) as raised:
                fetcher._install(first_temp, final_path, digest, "XLSX")
            self.assertEqual(raised.exception.code, "QUARANTINE_INSTALL_FAILED")
            self.assertFalse(first_temp.exists())
            self.assertFalse(final_path.exists())

            existing = b"pre-existing content"
            final_path.write_bytes(existing)
            second_temp = store / ".second.part"
            second_temp.write_bytes(data)
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                fetcher._install(second_temp, final_path, digest, "XLSX")
            self.assertEqual(raised.exception.code, "IMMUTABLE_COLLISION")
            self.assertFalse(second_temp.exists())
            self.assertEqual(final_path.read_bytes(), existing)

    def test_conditional_304_returns_verified_existing_content(self) -> None:
        data = _package("XLSX")
        digest = hashlib.sha256(data).hexdigest()
        fake = FakeHTTP(
            [
                FakeResponse(200, _metadata()),
                FakeResponse(304, headers={"ETag": '"official-v1"'}),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "data-go-kr"
            store.mkdir()
            existing = store / f"{digest}.xlsx"
            existing.write_bytes(data)
            fetcher = MunicipalSourceFetcher(_connection_factory=fake)
            result = fetcher.fetch(
                public_data_pk=PUBLIC_DATA_PK,
                public_data_detail_pk=DETAIL_PK,
                expected_format="XLSX",
                quarantine_root=root,
                previous=PreviousFetch(
                    digest,
                    "XLSX",
                    etag='"official-v1"',
                    last_modified="Mon, 10 Nov 2025 00:00:00 GMT",
                ),
            )
            self.assertEqual(result.status, "NOT_MODIFIED")
            self.assertEqual(result.path, existing)
        headers = fake.requests[1][3]
        self.assertEqual(headers["If-None-Match"], '"official-v1"')
        self.assertEqual(
            headers["If-Modified-Since"], "Mon, 10 Nov 2025 00:00:00 GMT"
        )

    def test_supports_csv_and_hwpx_magic_with_compatible_mime(self) -> None:
        cases = (
            ("CSV", "노선,출발시간\n100,06:00\n".encode("cp949"), "text/csv", ".csv"),
            ("HWPX", _package("HWPX"), "application/hwp+zip", ".hwpx"),
        )
        for fmt, data, media_type, suffix in cases:
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as temporary:
                fake = FakeHTTP(
                    [FakeResponse(200, _metadata()), _response_for_file(data, media_type)]
                )
                result = self._fetch(Path(temporary), fake, fmt)
                self.assertEqual(result.format, fmt)
                self.assertEqual(result.path.suffix, suffix)
                self.assertEqual(result.path.read_bytes(), data)

    def test_rejects_untrusted_registry_and_attachment_identifiers_before_download(self) -> None:
        unopened = FakeHTTP([])
        fetcher = MunicipalSourceFetcher(_connection_factory=unopened)
        with tempfile.TemporaryDirectory() as temporary:
            calls = (
                {"public_data_pk": "https://evil.invalid"},
                {"public_data_detail_pk": "../../secret"},
                {"expected_format": "HTML"},
            )
            base = {
                "public_data_pk": PUBLIC_DATA_PK,
                "public_data_detail_pk": DETAIL_PK,
                "expected_format": "XLSX",
                "quarantine_root": temporary,
            }
            for override in calls:
                with self.subTest(override=override), self.assertRaises(
                    MunicipalSourceFetchError
                ):
                    fetcher.fetch(**{**base, **override})
        self.assertEqual(unopened.connections, [])

        malicious = FakeHTTP([FakeResponse(200, _metadata(attachment_id="../../etc/passwd"))])
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                self._fetch(Path(temporary), malicious, "XLSX")
        self.assertEqual(raised.exception.code, "INVALID_ATTACHMENT_ID")
        self.assertEqual(len(malicious.requests), 1)

    def test_redirect_host_and_scheme_are_revalidated_without_environment_proxy(self) -> None:
        locations = (
            "https://evil.invalid/file.xlsx",
            "http://www.data.go.kr/cmm/cmm/fileDownload.do",
            "https://user@www.data.go.kr/cmm/cmm/fileDownload.do",
        )
        for location in locations:
            with self.subTest(location=location), tempfile.TemporaryDirectory() as temporary:
                fake = FakeHTTP(
                    [
                        FakeResponse(200, _metadata()),
                        FakeResponse(302, headers={"Location": location}),
                    ]
                )
                with patch.dict(
                    os.environ,
                    {"HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""},
                ):
                    with self.assertRaises(MunicipalSourceFetchError) as raised:
                        self._fetch(Path(temporary), fake, "XLSX")
                self.assertEqual(raised.exception.code, "UNSAFE_REDIRECT")
                self.assertTrue(all(host == OFFICIAL_HOST for host, _, _, _ in fake.connections))
                self.assertEqual(len(fake.connections), 2)

        data = _package("XLSX")
        direct = FakeHTTP(
            [FakeResponse(200, _metadata()), _response_for_file(data, "application/octet-stream")]
        )
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}
        ), patch(
            "municipal_source_fetch.http.client.HTTPSConnection", side_effect=direct
        ):
            result = MunicipalSourceFetcher().fetch(
                public_data_pk=PUBLIC_DATA_PK,
                public_data_detail_pk=DETAIL_PK,
                expected_format="XLSX",
                quarantine_root=temporary,
            )
        self.assertEqual(result.status, "DOWNLOADED")
        self.assertTrue(all(host == OFFICIAL_HOST for host, _, _, _ in direct.connections))

    def test_same_host_redirect_is_followed_and_bounded(self) -> None:
        data = _package("XLSX")
        fake = FakeHTTP(
            [
                FakeResponse(200, _metadata()),
                FakeResponse(302, headers={"Location": "/download/official.xlsx?id=1"}),
                _response_for_file(data, "application/octet-stream"),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = self._fetch(Path(temporary), fake, "XLSX")
        self.assertEqual(result.status, "DOWNLOADED")
        self.assertEqual(fake.requests[2][1], "/download/official.xlsx?id=1")

    def test_total_deadline_and_byte_limit_leave_no_partial_file(self) -> None:
        class Clock:
            def __init__(self) -> None:
                self.value = 0.0

            def __call__(self) -> float:
                return self.value

        clock = Clock()
        data = _package("XLSX")

        def expire() -> None:
            clock.value = 2.0

        fake = FakeHTTP(
            [
                FakeResponse(200, _metadata()),
                FakeResponse(
                    200,
                    data,
                    {"Content-Type": "application/octet-stream"},
                    on_read=expire,
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fetcher = MunicipalSourceFetcher(
                timeout_seconds=1,
                max_bytes=1024 * 1024,
                _connection_factory=fake,
                _monotonic=clock,
            )
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                fetcher.fetch(
                    public_data_pk=PUBLIC_DATA_PK,
                    public_data_detail_pk=DETAIL_PK,
                    expected_format="XLSX",
                    quarantine_root=root,
                )
            self.assertEqual(raised.exception.code, "DEADLINE_EXCEEDED")
            self.assertEqual(list((root / "data-go-kr").glob("*")), [])

        oversized = FakeResponse(
            200,
            b"not read",
            {"Content-Type": "application/octet-stream", "Content-Length": "2049"},
        )
        fake = FakeHTTP([FakeResponse(200, _metadata()), oversized])
        with tempfile.TemporaryDirectory() as temporary:
            fetcher = MunicipalSourceFetcher(max_bytes=2048, _connection_factory=fake)
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                fetcher.fetch(
                    public_data_pk=PUBLIC_DATA_PK,
                    public_data_detail_pk=DETAIL_PK,
                    expected_format="XLSX",
                    quarantine_root=temporary,
                )
            self.assertEqual(raised.exception.code, "RESPONSE_TOO_LARGE")
            self.assertEqual(oversized.read_calls, 0)

        streamed = FakeResponse(
            200,
            b"x" * 2049,
            {"Content-Type": "application/octet-stream"},
        )
        fake = FakeHTTP([FakeResponse(200, _metadata()), streamed])
        with tempfile.TemporaryDirectory() as temporary:
            fetcher = MunicipalSourceFetcher(max_bytes=2048, _connection_factory=fake)
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                fetcher.fetch(
                    public_data_pk=PUBLIC_DATA_PK,
                    public_data_detail_pk=DETAIL_PK,
                    expected_format="XLSX",
                    quarantine_root=temporary,
                )
            self.assertEqual(raised.exception.code, "RESPONSE_TOO_LARGE")

    def test_mime_magic_mismatch_and_immutable_collision_fail_closed(self) -> None:
        html = b"<!doctype html>\n<html>,error</html>\n"
        fake = FakeHTTP([FakeResponse(200, _metadata()), _response_for_file(html, "text/html")])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                self._fetch(root, fake, "XLSX")
            self.assertEqual(raised.exception.code, "MAGIC_MISMATCH")
            self.assertEqual(list((root / "data-go-kr").glob("*")), [])

        data = _package("XLSX")
        fake = FakeHTTP([FakeResponse(200, _metadata()), _response_for_file(data, "text/html")])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                self._fetch(root, fake, "XLSX")
            self.assertEqual(raised.exception.code, "MIME_MISMATCH")
            self.assertEqual(list((root / "data-go-kr").glob("*")), [])

        digest = hashlib.sha256(data).hexdigest()
        fake = FakeHTTP(
            [FakeResponse(200, _metadata()), _response_for_file(data, "application/octet-stream")]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = root / "data-go-kr"
            store.mkdir()
            collision = store / f"{digest}.xlsx"
            collision.write_bytes(b"attacker")
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                self._fetch(root, fake, "XLSX")
            self.assertEqual(raised.exception.code, "IMMUTABLE_COLLISION")
            self.assertEqual(collision.read_bytes(), b"attacker")


class GwangjuNoticeFetcherCase(unittest.TestCase):
    def _fetch(self, root: Path, fake: FakeHTTP, **kwargs):
        fetcher = GwangjuNoticeFetcher(
            timeout_seconds=10,
            max_bytes=2 * 1024 * 1024,
            _connection_factory=fake,
            **kwargs,
        )
        return fetcher.fetch(
            notice_id=GWANGJU_NOTICE_ID,
            file_index="1",
            quarantine_root=root,
        )

    def test_parser_binds_live_fragment_filename_size_and_effective_date(self) -> None:
        html = _gwangju_notice(
            (
                ("1", GWANGJU_FILENAME, 951_576),
                ("2", "시내버스운행시간표(토요일)-26.8.22.xlsx", 445_902),
                ("3", "시내버스운행시간표(공휴일)-26.8.22.xlsx", 444_880),
            )
        ).decode("utf-8")
        metadata = parse_gwangju_notice_html(html, notice_id=1209)

        self.assertEqual(metadata.notice_id, GWANGJU_NOTICE_ID)
        self.assertEqual(metadata.title, GWANGJU_TITLE)
        self.assertEqual(metadata.effective_date, "2026-08-22")
        self.assertEqual(metadata.source_url, GWANGJU_ORIGIN + GWANGJU_NOTICE_VIEW_PATH)
        self.assertEqual(
            [(item.file_index, item.filename, item.byte_count) for item in metadata.attachments],
            [
                ("1", GWANGJU_FILENAME, 951_576),
                ("2", "시내버스운행시간표(토요일)-26.8.22.xlsx", 445_902),
                ("3", "시내버스운행시간표(공휴일)-26.8.22.xlsx", 444_880),
            ],
        )

    def test_fetch_is_fixed_post_content_addressed_and_idempotent(self) -> None:
        data = _package("XLSX")
        notice = _gwangju_notice((("1", GWANGJU_FILENAME, len(data)),))
        fake = FakeHTTP(
            [
                _gwangju_notice_response(notice),
                _response_for_file(data, "application/octet-stream"),
                _gwangju_notice_response(notice),
                _response_for_file(
                    data,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = self._fetch(root, fake)
            inode = first.path.stat().st_ino
            second = self._fetch(root, fake)

            digest = hashlib.sha256(data).hexdigest()
            self.assertEqual(first.status, "DOWNLOADED")
            self.assertEqual(second.status, "UNCHANGED")
            self.assertEqual(first.notice_title, GWANGJU_TITLE)
            self.assertEqual(first.effective_date, "2026-08-22")
            self.assertEqual(first.filename, GWANGJU_FILENAME)
            self.assertEqual(first.source_url, GWANGJU_ORIGIN + GWANGJU_NOTICE_VIEW_PATH)
            self.assertEqual(first.sha256, digest)
            self.assertEqual(first.path, root / "gwangju-bus" / f"{digest}.xlsx")
            self.assertEqual(second.path.stat().st_ino, inode)

        view_request, download_request = fake.requests[:2]
        self.assertEqual(view_request[0:2], ("POST", GWANGJU_NOTICE_VIEW_PATH))
        self.assertEqual(parse_qs(view_request[2].decode("ascii")), {"B_IDX": ["1209"]})
        self.assertEqual(
            download_request[0:2], ("POST", GWANGJU_NOTICE_DOWNLOAD_PATH)
        )
        self.assertEqual(
            parse_qs(download_request[2].decode("ascii")),
            {"B_IDX": ["1209"], "FILE_IDX": ["1"]},
        )
        self.assertTrue(all(host == GWANGJU_HOST for host, _, _, _ in fake.connections))

    def test_parser_fails_closed_on_unmatched_or_ambiguous_view_metadata(self) -> None:
        missing_size = (
            '<input type="hidden" name="B_IDX" value="1209" />'
            f"<td>{GWANGJU_TITLE}</td>"
            "<a href=javascript:fnFileDown('1')>★schedule.xlsx</a><br/>"
        )
        duplicate_index = _gwangju_notice(
            (("1", "weekday.xlsx", 100), ("1", "holiday.xlsx", 200))
        ).decode("utf-8")
        bad_filename = _gwangju_notice(
            (("1", "../schedule.xlsx", 100),)
        ).decode("utf-8")
        wrong_notice = _gwangju_notice(
            (("1", "schedule.xlsx", 100),), notice_id="1210"
        ).decode("utf-8")
        for html, code in (
            (missing_size, "INVALID_NOTICE_METADATA"),
            (duplicate_index, "INVALID_NOTICE_METADATA"),
            (bad_filename, "INVALID_NOTICE_METADATA"),
            (wrong_notice, "NOTICE_ID_MISMATCH"),
        ):
            with self.subTest(code=code), self.assertRaises(
                MunicipalSourceFetchError
            ) as raised:
                parse_gwangju_notice_html(html, notice_id=GWANGJU_NOTICE_ID)
            self.assertEqual(raised.exception.code, code)

    def test_numeric_html_and_attachment_size_limits_prevent_download(self) -> None:
        unopened = FakeHTTP([])
        fetcher = GwangjuNoticeFetcher(_connection_factory=unopened)
        with tempfile.TemporaryDirectory() as temporary:
            for notice_id, file_index in (
                (0, "1"),
                ("01", "1"),
                ("1209", 0),
                ("1209", "1/../../"),
                (2_147_483_648, "1"),
                ("1209", 10_000),
            ):
                with self.subTest(
                    notice_id=notice_id, file_index=file_index
                ), self.assertRaises(MunicipalSourceFetchError):
                    fetcher.fetch(
                        notice_id=notice_id,
                        file_index=file_index,
                        quarantine_root=temporary,
                    )
        self.assertEqual(unopened.connections, [])

        oversized_view = FakeResponse(
            200,
            b"not read",
            {
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": str(MAX_NOTICE_HTML_BYTES + 1),
            },
        )
        fake = FakeHTTP([oversized_view])
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                self._fetch(Path(temporary), fake)
        self.assertEqual(raised.exception.code, "RESPONSE_TOO_LARGE")
        self.assertEqual(oversized_view.read_calls, 0)

        notice = _gwangju_notice((("1", GWANGJU_FILENAME, 2049),))
        fake = FakeHTTP([_gwangju_notice_response(notice)])
        with tempfile.TemporaryDirectory() as temporary:
            fetcher = GwangjuNoticeFetcher(max_bytes=2048, _connection_factory=fake)
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                fetcher.fetch(
                    notice_id=GWANGJU_NOTICE_ID,
                    file_index="1",
                    quarantine_root=temporary,
                )
        self.assertEqual(raised.exception.code, "RESPONSE_TOO_LARGE")
        self.assertEqual(len(fake.requests), 1)

    def test_redirect_size_mime_and_magic_are_revalidated(self) -> None:
        data = _package("XLSX")
        notice = _gwangju_notice((("1", GWANGJU_FILENAME, len(data)),))
        fake = FakeHTTP(
            [
                _gwangju_notice_response(notice),
                FakeResponse(302, headers={"Location": "https://evil.invalid/file.xlsx"}),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}
        ):
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                self._fetch(Path(temporary), fake)
        self.assertEqual(raised.exception.code, "UNSAFE_REDIRECT")
        self.assertTrue(all(host == GWANGJU_HOST for host, _, _, _ in fake.connections))

        mismatch = FakeHTTP(
            [
                _gwangju_notice_response(notice),
                FakeResponse(
                    200,
                    data,
                    {
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(len(data) - 1),
                    },
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                self._fetch(Path(temporary), mismatch)
        self.assertEqual(raised.exception.code, "NOTICE_SIZE_MISMATCH")

        for body, media_type, code in (
            (b"<!doctype html><html>error</html>", "application/octet-stream", "MAGIC_MISMATCH"),
            (data, "text/html", "MIME_MISMATCH"),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                body_notice = _gwangju_notice((("1", GWANGJU_FILENAME, len(body)),))
                fake = FakeHTTP(
                    [
                        _gwangju_notice_response(body_notice),
                        _response_for_file(body, media_type),
                    ]
                )
                with self.assertRaises(MunicipalSourceFetchError) as raised:
                    self._fetch(Path(temporary), fake)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(list((Path(temporary) / "gwangju-bus").glob("*")), [])

    def test_total_deadline_covers_notice_and_download_and_removes_partial(self) -> None:
        class Clock:
            def __init__(self) -> None:
                self.value = 0.0

            def __call__(self) -> float:
                return self.value

        clock = Clock()
        data = _package("XLSX")
        notice = _gwangju_notice((("1", GWANGJU_FILENAME, len(data)),))

        def expire() -> None:
            clock.value = 2.0

        fake = FakeHTTP(
            [
                _gwangju_notice_response(notice),
                FakeResponse(
                    200,
                    data,
                    {"Content-Type": "application/octet-stream"},
                    on_read=expire,
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fetcher = GwangjuNoticeFetcher(
                timeout_seconds=1,
                max_bytes=1024 * 1024,
                _connection_factory=fake,
                _monotonic=clock,
            )
            with self.assertRaises(MunicipalSourceFetchError) as raised:
                fetcher.fetch(
                    notice_id=GWANGJU_NOTICE_ID,
                    file_index="1",
                    quarantine_root=root,
                )
            self.assertEqual(raised.exception.code, "DEADLINE_EXCEEDED")
            self.assertEqual(list((root / "gwangju-bus").glob("*")), [])


if __name__ == "__main__":
    unittest.main()
