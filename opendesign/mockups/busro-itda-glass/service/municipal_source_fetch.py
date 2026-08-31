"""Bounded, quarantine-only fetches from allow-listed official file sources.

The caller supplies registry identifiers, never a URL.  Downloaded bytes are
sniffed and installed under their SHA-256 without parsing or activation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import errno
import hashlib
from html.parser import HTMLParser
import http.client
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urljoin, urlsplit
import zipfile


OFFICIAL_ORIGIN = "https://www.data.go.kr"
OFFICIAL_HOST = "www.data.go.kr"
ATTACHMENT_METADATA_PATH = "/tcs/dss/selectFileDataDownload.do"
DOWNLOAD_PATH = "/cmm/cmm/fileDownload.do"

GWANGJU_ORIGIN = "https://bus.gwangju.go.kr"
GWANGJU_HOST = "bus.gwangju.go.kr"
GWANGJU_NOTICE_VIEW_PATH = "/guide/notice/noticeView"
GWANGJU_NOTICE_DOWNLOAD_PATH = "/guide/notice/noticeFileDown"

DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MAX_CONFIGURED_BYTES = 256 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_NOTICE_HTML_BYTES = 512 * 1024
MAX_REDIRECTS = 3
READ_CHUNK_BYTES = 64 * 1024
MAX_ZIP_MEMBERS = 4096
MAX_HEADER_LENGTH = 1024

_PUBLIC_DATA_PK_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_DETAIL_PK_RE = re.compile(
    r"^uddi:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_ATTACHMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")
_FILE_DETAIL_SN_RE = re.compile(r"^[1-9][0-9]{0,9}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NOTICE_ID_RE = re.compile(r"^[1-9][0-9]{0,9}$")
_FILE_INDEX_RE = re.compile(r"^[1-9][0-9]{0,3}$")
_FILE_DOWN_RE = re.compile(
    r"^\s*(?:javascript\s*:\s*)?fnFileDown\(\s*(['\"])([1-9][0-9]{0,3})\1\s*\)\s*;?\s*$",
    re.IGNORECASE,
)
_NOTICE_SIZE_RE = re.compile(
    r"^\s*\(\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)\s*bytes?\s*\)\s*$",
    re.IGNORECASE,
)
_EFFECTIVE_DATE_RE = re.compile(
    r"^(?P<title>.{1,200}\(\s*(?P<year>[0-9]{4})\.(?P<month>[0-9]{1,2})\."
    r"(?P<day>[0-9]{1,2})\.?\s*~\s*\))$"
)

_EXTENSION = {"XLSX": ".xlsx", "CSV": ".csv", "HWPX": ".hwpx"}
_GENERIC_BINARY_MIMES = {
    "application/octet-stream",
    "binary/octet-stream",
    "application/download",
    "application/force-download",
}
_ALLOWED_MIMES = {
    "XLSX": _GENERIC_BINARY_MIMES
    | {
        "application/zip",
        "application/x-zip-compressed",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    },
    "CSV": _GENERIC_BINARY_MIMES
    | {
        "text/csv",
        "application/csv",
        "text/plain",
        "application/vnd.ms-excel",
    },
    "HWPX": _GENERIC_BINARY_MIMES
    | {
        "application/zip",
        "application/x-zip-compressed",
        "application/hwp+zip",
        "application/vnd.hancom.hwpx",
        "application/x-hwp",
    },
}


class MunicipalSourceFetchError(RuntimeError):
    """Stable, non-reflective failure from the quarantine fetch contract."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreviousFetch:
    """Previously quarantined content used for a conditional request."""

    sha256: str
    format: str
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class FetchResult:
    """Evidence returned after bytes are safely present in quarantine."""

    status: str
    public_data_pk: str
    public_data_detail_pk: str
    attachment_id: str
    file_detail_sn: str
    format: str
    media_type: str
    sha256: str
    byte_count: int
    path: Path
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class GwangjuNoticeAttachment:
    """One attachment identity bound to the official notice HTML."""

    file_index: str
    filename: str
    byte_count: int


@dataclass(frozen=True)
class GwangjuNoticeMetadata:
    """Bounded metadata parsed from one fixed-host notice view."""

    notice_id: str
    title: str
    effective_date: str
    source_url: str
    attachments: tuple[GwangjuNoticeAttachment, ...]


@dataclass(frozen=True)
class GwangjuFetchResult:
    """Evidence for one raw Gwangju notice attachment in quarantine."""

    status: str
    notice_id: str
    file_index: str
    notice_title: str
    effective_date: str
    source_url: str
    filename: str
    format: str
    media_type: str
    sha256: str
    byte_count: int
    path: Path
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class _Attachment:
    attachment_id: str
    file_detail_sn: str


@dataclass(frozen=True)
class _Download:
    status: int
    response: Any
    connection: Any


def _fail(code: str, message: str) -> MunicipalSourceFetchError:
    return MunicipalSourceFetchError(code, message)


def _safe_header(value: Any, name: str, *, maximum: int = MAX_HEADER_LENGTH) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise _fail("INVALID_RESPONSE_HEADER", f"Invalid {name} response header")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _fail("INVALID_RESPONSE_HEADER", f"Invalid {name} response header")
    return value


def _format(value: str) -> str:
    if not isinstance(value, str) or value.upper() not in _EXTENSION:
        raise _fail("INVALID_FORMAT", "Expected format must be XLSX, CSV, or HWPX")
    return value.upper()


def _public_data_pk(value: str) -> str:
    if not isinstance(value, str) or _PUBLIC_DATA_PK_RE.fullmatch(value) is None:
        raise _fail("INVALID_PUBLIC_DATA_PK", "publicDataPk is invalid")
    return value


def _detail_pk(value: str) -> str:
    if not isinstance(value, str) or _DETAIL_PK_RE.fullmatch(value) is None:
        raise _fail("INVALID_DETAIL_PK", "publicDataDetailPk is invalid")
    return value


def _sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise _fail("INVALID_PREVIOUS_FETCH", "Previous SHA-256 is invalid")
    return value


def _bounded_numeric_id(
    value: str | int,
    name: str,
    pattern: re.Pattern[str],
    *,
    maximum: int,
) -> str:
    if isinstance(value, bool):
        raise _fail(f"INVALID_{name}", f"{name} is invalid")
    if isinstance(value, int):
        normalized = str(value)
    elif isinstance(value, str):
        normalized = value
    else:
        raise _fail(f"INVALID_{name}", f"{name} is invalid")
    if pattern.fullmatch(normalized) is None or int(normalized) > maximum:
        raise _fail(f"INVALID_{name}", f"{name} is invalid")
    return normalized


def _conditional_header(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise _fail("INVALID_PREVIOUS_FETCH", f"Previous {name} is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _fail("INVALID_PREVIOUS_FETCH", f"Previous {name} is invalid")
    return value


def _fixed_host_url(value: str, host: str) -> str:
    """Validate every fixed or redirected URL against one exact HTTPS host."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise _fail("UNSAFE_REDIRECT", "Download redirect is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise _fail("UNSAFE_REDIRECT", "Download redirect is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != host
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith("/")
        or len(parsed.query) > 2048
        or any(ord(character) < 32 for character in parsed.path + parsed.query)
    ):
        raise _fail("UNSAFE_REDIRECT", "Download redirect left the official HTTPS host")
    return value


def _official_url(value: str) -> str:
    return _fixed_host_url(value, OFFICIAL_HOST)


def _response_header(response: Any, name: str) -> str | None:
    getter = getattr(response, "getheader", None)
    value = getter(name) if callable(getter) else None
    return _safe_header(value, name) if value is not None else None


def _content_length(response: Any, maximum: int) -> int | None:
    raw = _response_header(response, "Content-Length")
    if raw is None:
        return None
    if not raw.isascii() or not raw.isdigit():
        raise _fail("INVALID_CONTENT_LENGTH", "Content-Length is invalid")
    length = int(raw)
    if length > maximum:
        raise _fail("RESPONSE_TOO_LARGE", "Official response exceeds the byte limit")
    return length


def _content_type(response: Any) -> str:
    raw = _response_header(response, "Content-Type")
    if raw is None:
        raise _fail("MIME_MISMATCH", "Download Content-Type is missing")
    media_type = raw.split(";", 1)[0].strip().lower()
    if not media_type:
        raise _fail("MIME_MISMATCH", "Download Content-Type is invalid")
    return media_type


def _set_socket_timeout(connection: Any, timeout: float) -> None:
    sock = getattr(connection, "sock", None)
    setter = getattr(sock, "settimeout", None)
    if callable(setter):
        setter(timeout)


def _sniff_csv(path: Path) -> bool:
    with path.open("rb") as handle:
        sample = handle.read(64 * 1024)
    if not sample or b"\x00" in sample:
        return False
    stripped = sample.lstrip().lower()
    if stripped.startswith((b"<!doctype", b"<html", b"<script", b"{", b"[")):
        return False
    text: str | None = None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = sample.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return False
    if any(ord(character) < 32 and character not in "\t\r\n" for character in text):
        return False
    lines = [line for line in text.splitlines() if line.strip()]
    return len(lines) >= 2 and "," in lines[0] and "," in lines[1]


def _sniff_zip(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ZIP_MEMBERS:
                return None
            names = {info.filename for info in infos}
            if len(names) != len(infos):
                return None
            if "[Content_Types].xml" in names and "xl/workbook.xml" in names:
                return "XLSX"
            if "mimetype" not in names or "Contents/content.hpf" not in names:
                return None
            mimetype_info = archive.getinfo("mimetype")
            if mimetype_info.file_size > 64:
                return None
            if archive.read(mimetype_info).strip() == b"application/hwp+zip":
                return "HWPX"
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        return None
    return None


def _sniff(path: Path) -> str:
    with path.open("rb") as handle:
        signature = handle.read(4)
    if signature.startswith(b"PK\x03\x04"):
        detected = _sniff_zip(path)
        if detected is not None:
            return detected
    elif _sniff_csv(path):
        return "CSV"
    raise _fail("MAGIC_MISMATCH", "Downloaded bytes are not an allowed official file type")


def _hash_file(path: Path, maximum: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise _fail("RESPONSE_TOO_LARGE", "Quarantine content exceeds the byte limit")
            digest.update(chunk)
    return digest.hexdigest(), total


def _collapsed_text(value: str) -> str:
    return " ".join(value.split())


def _gwangju_filename(value: str) -> str:
    filename = _collapsed_text(value).lstrip("★").strip()
    if (
        not filename
        or len(filename) > 240
        or not filename.casefold().endswith(".xlsx")
        or filename in {".", ".."}
        or any(character in filename for character in "\\/<>:\"|?*")
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise _fail("INVALID_NOTICE_METADATA", "Notice attachment filename is invalid")
    return filename


class _GwangjuNoticeHTMLParser(HTMLParser):
    """Extract only identity-bound text from the bounded official notice HTML."""

    _BOUNDARY_TAGS = frozenset({"div", "li", "p", "td", "tr", "ul"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.notice_ids: list[str] = []
        self.text_nodes: list[str] = []
        self.attachments: list[GwangjuNoticeAttachment] = []
        self._seen_indices: set[str] = set()
        self._active_index: str | None = None
        self._active_text: list[str] = []
        self._pending: tuple[str, str] | None = None
        self._pending_text: list[str] = []

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        output: dict[str, str] = {}
        for key, value in attrs:
            normalized_key = key.casefold()
            if normalized_key in output:
                raise _fail("INVALID_NOTICE_METADATA", "Notice HTML has duplicate attributes")
            output[normalized_key] = value or ""
        return output

    def _finish_pending(self) -> None:
        if self._pending is None:
            return
        file_index, filename = self._pending
        size_match = _NOTICE_SIZE_RE.fullmatch(_collapsed_text("".join(self._pending_text)))
        if size_match is None:
            raise _fail(
                "INVALID_NOTICE_METADATA",
                "Notice attachment filename and size are not matched",
            )
        byte_count = int(size_match.group(1).replace(",", ""))
        if byte_count < 1 or byte_count > MAX_CONFIGURED_BYTES:
            raise _fail("INVALID_NOTICE_METADATA", "Notice attachment size is invalid")
        if file_index in self._seen_indices:
            raise _fail("INVALID_NOTICE_METADATA", "Notice attachment index is ambiguous")
        self._seen_indices.add(file_index)
        self.attachments.append(GwangjuNoticeAttachment(file_index, filename, byte_count))
        self._pending = None
        self._pending_text = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.casefold()
        attributes = self._attributes(attrs)
        if tag == "input" and attributes.get("name", "").casefold() == "b_idx":
            self.notice_ids.append(attributes.get("value", ""))
        if tag == "br":
            self._finish_pending()
            return
        if tag != "a":
            return
        if self._active_index is not None:
            raise _fail("INVALID_NOTICE_METADATA", "Notice attachment link is malformed")
        self._finish_pending()
        indices: set[str] = set()
        for attribute_name in ("href", "onclick"):
            candidate = attributes.get(attribute_name)
            if candidate:
                match = _FILE_DOWN_RE.fullmatch(candidate)
                if match is not None:
                    indices.add(match.group(2))
        if len(indices) > 1:
            raise _fail("INVALID_NOTICE_METADATA", "Notice attachment index is ambiguous")
        if indices:
            self._active_index = indices.pop()
            self._active_text = []

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        collapsed = _collapsed_text(data)
        if collapsed:
            self.text_nodes.append(collapsed)
        if self._active_index is not None:
            self._active_text.append(data)
        elif self._pending is not None:
            self._pending_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self._active_index is not None:
            filename = _gwangju_filename("".join(self._active_text))
            self._pending = (self._active_index, filename)
            self._pending_text = []
            self._active_index = None
            self._active_text = []
            return
        if tag in self._BOUNDARY_TAGS:
            self._finish_pending()

    def finish(self) -> None:
        if self._active_index is not None:
            raise _fail("INVALID_NOTICE_METADATA", "Notice attachment link is malformed")
        self._finish_pending()


def parse_gwangju_notice_html(
    html: str, *, notice_id: str | int
) -> GwangjuNoticeMetadata:
    """Parse bounded notice metadata without following links or parsing attachments."""
    normalized_notice_id = _bounded_numeric_id(
        notice_id, "B_IDX", _NOTICE_ID_RE, maximum=2_147_483_647
    )
    if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_NOTICE_HTML_BYTES:
        raise _fail("INVALID_NOTICE_METADATA", "Notice HTML is invalid or too large")
    if "\x00" in html:
        raise _fail("INVALID_NOTICE_METADATA", "Notice HTML contains invalid data")
    parser = _GwangjuNoticeHTMLParser()
    try:
        parser.feed(html)
        parser.close()
        parser.finish()
    except MunicipalSourceFetchError:
        raise
    except Exception as exc:
        raise _fail("INVALID_NOTICE_METADATA", "Notice HTML could not be parsed") from exc

    returned_notice_ids = set(parser.notice_ids)
    if returned_notice_ids != {normalized_notice_id}:
        raise _fail("NOTICE_ID_MISMATCH", "Notice view changed notice identity")

    title_matches: dict[str, re.Match[str]] = {}
    for text_node in parser.text_nodes:
        match = _EFFECTIVE_DATE_RE.fullmatch(text_node)
        if match is not None:
            title_matches[text_node] = match
    if len(title_matches) != 1:
        raise _fail("INVALID_NOTICE_METADATA", "Notice title or effective date is ambiguous")
    title, title_match = next(iter(title_matches.items()))
    try:
        effective_date = date(
            int(title_match.group("year")),
            int(title_match.group("month")),
            int(title_match.group("day")),
        ).isoformat()
    except ValueError as exc:
        raise _fail("INVALID_NOTICE_METADATA", "Notice effective date is invalid") from exc
    if not parser.attachments:
        raise _fail("INVALID_NOTICE_METADATA", "Notice has no matched attachments")
    return GwangjuNoticeMetadata(
        normalized_notice_id,
        title,
        effective_date,
        GWANGJU_ORIGIN + GWANGJU_NOTICE_VIEW_PATH,
        tuple(parser.attachments),
    )


class MunicipalSourceFetcher:
    """Fetch one registry-selected data.go.kr file into an immutable quarantine."""

    _official_origin = OFFICIAL_ORIGIN
    _official_host = OFFICIAL_HOST

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        max_bytes: int = DEFAULT_MAX_BYTES,
        _connection_factory: Callable[[str, int, float], Any] | None = None,
        _monotonic: Callable[[], float] | None = None,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 1.0 <= float(timeout_seconds) <= 60.0
        ):
            raise ValueError("timeout_seconds must be 1..60")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise ValueError("max_bytes must be an integer")
        if not 1024 <= max_bytes <= MAX_CONFIGURED_BYTES:
            raise ValueError(f"max_bytes must be 1024..{MAX_CONFIGURED_BYTES}")
        self.timeout_seconds = float(timeout_seconds)
        self.max_bytes = max_bytes
        self._connection_factory = _connection_factory or self._default_connection
        self._monotonic = _monotonic or time.monotonic

    @staticmethod
    def _default_connection(host: str, port: int, timeout: float) -> Any:
        # http.client does not consult HTTP(S)_PROXY or other environment proxies.
        return http.client.HTTPSConnection(host, port=port, timeout=timeout)

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise _fail("DEADLINE_EXCEEDED", "Official file fetch exceeded its total deadline")
        return remaining

    def _connect(self, url: str, deadline: float) -> tuple[Any, str]:
        parsed = urlsplit(_fixed_host_url(url, self._official_host))
        connection = self._connection_factory(
            self._official_host, parsed.port or 443, self._remaining(deadline)
        )
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        return connection, target

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        deadline: float,
        allow_download_redirects: bool,
    ) -> _Download:
        current_url = _fixed_host_url(url, self._official_host)
        for redirect_count in range(MAX_REDIRECTS + 1):
            connection, target = self._connect(current_url, deadline)
            try:
                connection.request(method, target, body=body, headers=dict(headers))
                _set_socket_timeout(connection, self._remaining(deadline))
                response = connection.getresponse()
                self._remaining(deadline)
            except (socket.timeout, TimeoutError) as exc:
                connection.close()
                raise _fail(
                    "DEADLINE_EXCEEDED", "Official file fetch exceeded its total deadline"
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                connection.close()
                raise _fail("NETWORK_ERROR", "Official file request failed") from exc

            status_code = int(getattr(response, "status", 0) or 0)
            if status_code not in {301, 302, 303, 307, 308}:
                return _Download(status_code, response, connection)
            try:
                location = _response_header(response, "Location")
            finally:
                response.close()
                connection.close()
            if not allow_download_redirects or location is None:
                raise _fail("UNSAFE_REDIRECT", "Unexpected redirect from official endpoint")
            if redirect_count >= MAX_REDIRECTS:
                raise _fail("TOO_MANY_REDIRECTS", "Official download redirected too many times")
            current_url = _fixed_host_url(
                urljoin(current_url, location), self._official_host
            )
            method, body = "GET", None
        raise AssertionError("unreachable")

    def _read_bounded(
        self, response: Any, connection: Any, *, maximum: int, deadline: float
    ) -> bytes:
        _content_length(response, maximum)
        chunks: list[bytes] = []
        total = 0
        while True:
            _set_socket_timeout(connection, self._remaining(deadline))
            try:
                chunk = response.read(min(READ_CHUNK_BYTES, maximum - total + 1))
            except (socket.timeout, TimeoutError) as exc:
                raise _fail(
                    "DEADLINE_EXCEEDED", "Official file fetch exceeded its total deadline"
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise _fail("NETWORK_ERROR", "Official response read failed") from exc
            self._remaining(deadline)
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray)):
                raise _fail("INVALID_RESPONSE", "Official endpoint returned invalid bytes")
            total += len(chunk)
            if total > maximum:
                raise _fail("RESPONSE_TOO_LARGE", "Official response exceeds the byte limit")
            chunks.append(bytes(chunk))
        return b"".join(chunks)

    def _attachment(
        self, public_data_pk: str, public_data_detail_pk: str, deadline: float
    ) -> _Attachment:
        body = urlencode(
            (
                ("publicDataPk", public_data_pk),
                ("publicDataDetailPk", public_data_detail_pk),
                ("atchFileId", ""),
                ("fileDetailSn", "1"),
                ("publicDataTyCode", "PR0051"),
            )
        ).encode("ascii")
        reply = self._request(
            "POST",
            OFFICIAL_ORIGIN + ATTACHMENT_METADATA_PATH,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "User-Agent": "busro-itda-municipal-quarantine/0.1",
            },
            body=body,
            deadline=deadline,
            allow_download_redirects=False,
        )
        try:
            if reply.status != 200:
                raise _fail("METADATA_HTTP_ERROR", "Attachment metadata request failed")
            raw = self._read_bounded(
                reply.response, reply.connection, maximum=MAX_METADATA_BYTES, deadline=deadline
            )
        finally:
            reply.response.close()
            reply.connection.close()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _fail("INVALID_METADATA", "Attachment metadata is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("status") is not True:
            raise _fail("ATTACHMENT_UNAVAILABLE", "Official attachment is unavailable")
        returned_pk = payload.get("publicDataPk")
        returned_detail = payload.get("publicDataDetailPk")
        if returned_pk is not None and str(returned_pk) != public_data_pk:
            raise _fail("METADATA_MISMATCH", "Attachment metadata changed dataset identity")
        if returned_detail is not None and str(returned_detail) != public_data_detail_pk:
            raise _fail("METADATA_MISMATCH", "Attachment metadata changed detail identity")
        attachment_id = payload.get("atchFileId")
        file_detail_sn = str(payload.get("fileDetailSn", ""))
        if (
            not isinstance(attachment_id, str)
            or _ATTACHMENT_ID_RE.fullmatch(attachment_id) is None
            or _FILE_DETAIL_SN_RE.fullmatch(file_detail_sn) is None
        ):
            raise _fail("INVALID_ATTACHMENT_ID", "Official attachment identifiers are invalid")
        return _Attachment(attachment_id, file_detail_sn)

    def _previous(
        self, previous: PreviousFetch | None, expected_format: str, store: Path
    ) -> tuple[PreviousFetch | None, Path | None]:
        if previous is None:
            return None, None
        digest = _sha256(previous.sha256)
        previous_format = _format(previous.format)
        if previous_format != expected_format:
            raise _fail("INVALID_PREVIOUS_FETCH", "Previous format does not match this fetch")
        normalized = PreviousFetch(
            digest,
            previous_format,
            _conditional_header(previous.etag, "ETag"),
            _conditional_header(previous.last_modified, "Last-Modified"),
        )
        path = store / f"{digest}{_EXTENSION[expected_format]}"
        self._verify_existing(path, digest, expected_format)
        return normalized, path

    def _verify_existing(self, path: Path, digest: str, expected_format: str) -> int:
        try:
            metadata = os.lstat(path)
        except FileNotFoundError as exc:
            raise _fail("PREVIOUS_CONTENT_MISSING", "Previous quarantine content is missing") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise _fail("IMMUTABLE_COLLISION", "Quarantine content path is not a regular file")
        actual, byte_count = _hash_file(path, self.max_bytes)
        if actual != digest or _sniff(path) != expected_format:
            raise _fail("IMMUTABLE_COLLISION", "Quarantine content-address collision detected")
        return byte_count

    def _download(
        self,
        attachment: _Attachment,
        *,
        previous: PreviousFetch | None,
        deadline: float,
    ) -> _Download:
        query = urlencode(
            {
                "atchFileId": attachment.attachment_id,
                "fileDetailSn": attachment.file_detail_sn,
            }
        )
        headers = {
            "Accept": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
                "application/hwp+zip,text/csv,application/octet-stream"
            ),
            "Accept-Encoding": "identity",
            "User-Agent": "busro-itda-municipal-quarantine/0.1",
        }
        if previous is not None and previous.etag is not None:
            headers["If-None-Match"] = previous.etag
        if previous is not None and previous.last_modified is not None:
            headers["If-Modified-Since"] = previous.last_modified
        return self._request(
            "GET",
            f"{OFFICIAL_ORIGIN}{DOWNLOAD_PATH}?{query}",
            headers=headers,
            body=None,
            deadline=deadline,
            allow_download_redirects=True,
        )

    def _stream_to_temp(
        self,
        response: Any,
        connection: Any,
        store: Path,
        deadline: float,
    ) -> tuple[Path, str, int, str, str | None, str | None]:
        media_type = _content_type(response)
        encoding = _response_header(response, "Content-Encoding")
        if encoding is not None and encoding.lower() != "identity":
            raise _fail("UNSUPPORTED_CONTENT_ENCODING", "Compressed HTTP content is not allowed")
        _content_length(response, self.max_bytes)
        etag = _response_header(response, "ETag")
        last_modified = _response_header(response, "Last-Modified")
        file_handle: io.BufferedWriter | None = None
        temp_path: Path | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(prefix=".fetch-", suffix=".part", dir=store)
            temp_path = Path(temp_name)
            file_handle = os.fdopen(descriptor, "wb")
            digest = hashlib.sha256()
            byte_count = 0
            while True:
                _set_socket_timeout(connection, self._remaining(deadline))
                try:
                    chunk = response.read(READ_CHUNK_BYTES)
                except (socket.timeout, TimeoutError) as exc:
                    raise _fail(
                        "DEADLINE_EXCEEDED",
                        "Official file fetch exceeded its total deadline",
                    ) from exc
                except (OSError, http.client.HTTPException) as exc:
                    raise _fail("NETWORK_ERROR", "Official file response read failed") from exc
                self._remaining(deadline)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise _fail("INVALID_RESPONSE", "Official endpoint returned invalid bytes")
                byte_count += len(chunk)
                if byte_count > self.max_bytes:
                    raise _fail("RESPONSE_TOO_LARGE", "Official file exceeds the byte limit")
                digest.update(chunk)
                file_handle.write(chunk)
            if byte_count == 0:
                raise _fail("EMPTY_DOWNLOAD", "Official file download was empty")
            file_handle.flush()
            os.fsync(file_handle.fileno())
            file_handle.close()
            file_handle = None
            return temp_path, digest.hexdigest(), byte_count, media_type, etag, last_modified
        except Exception:
            if file_handle is not None:
                file_handle.close()
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
            raise

    def _install(self, temp_path: Path, final_path: Path, digest: str, fmt: str) -> bool:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        created_identity: tuple[int, int] | None = None

        def remove_created() -> None:
            if created_identity is None:
                return
            try:
                current = os.lstat(final_path)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise _fail(
                    "QUARANTINE_CLEANUP_FAILED",
                    "Could not inspect failed quarantine content",
                ) from exc
            if (current.st_dev, current.st_ino) != created_identity:
                return
            try:
                final_path.unlink()
            except FileNotFoundError:
                return
            except OSError as exc:
                raise _fail(
                    "QUARANTINE_CLEANUP_FAILED",
                    "Could not remove failed quarantine content",
                ) from exc

        try:
            temp_digest, _ = _hash_file(temp_path, self.max_bytes)
            if temp_digest != digest or _sniff(temp_path) != fmt:
                raise _fail(
                    "QUARANTINE_INSTALL_FAILED",
                    "Temporary quarantine content changed before installation",
                )
            try:
                descriptor = os.open(final_path, flags, 0o600)
            except FileExistsError:
                self._verify_existing(final_path, digest, fmt)
                return False
            except OSError as exc:
                if exc.errno == errno.EEXIST:
                    self._verify_existing(final_path, digest, fmt)
                    return False
                raise _fail(
                    "QUARANTINE_INSTALL_FAILED",
                    "Could not exclusively create quarantine content",
                ) from exc

            created = os.fstat(descriptor)
            created_identity = (created.st_dev, created.st_ino)
            destination = os.fdopen(descriptor, "wb")
            descriptor = None
            copied = 0
            with destination:
                with temp_path.open("rb") as source:
                    while True:
                        chunk = source.read(READ_CHUNK_BYTES)
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > self.max_bytes:
                            raise _fail(
                                "RESPONSE_TOO_LARGE",
                                "Quarantine content exceeds the byte limit",
                            )
                        written = destination.write(chunk)
                        if written != len(chunk):
                            raise _fail(
                                "QUARANTINE_INSTALL_FAILED",
                                "Could not copy complete quarantine content",
                            )
                    destination.flush()
                    os.fsync(destination.fileno())
            self._verify_existing(final_path, digest, fmt)
            return True
        except Exception as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                descriptor = None
            remove_created()
            if isinstance(exc, OSError):
                raise _fail(
                    "QUARANTINE_INSTALL_FAILED",
                    "Could not copy quarantine content",
                ) from exc
            raise
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            temp_path.unlink(missing_ok=True)

    def fetch(
        self,
        *,
        public_data_pk: str,
        public_data_detail_pk: str,
        expected_format: str,
        quarantine_root: Path | str,
        previous: PreviousFetch | None = None,
    ) -> FetchResult:
        """Fetch and quarantine raw official bytes; never parse or activate them."""
        public_pk = _public_data_pk(public_data_pk)
        detail_pk = _detail_pk(public_data_detail_pk)
        fmt = _format(expected_format)
        root = Path(quarantine_root).expanduser()
        store = root / "data-go-kr"
        try:
            store.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _fail("QUARANTINE_UNAVAILABLE", "Quarantine directory is unavailable") from exc
        if store.is_symlink() or not store.is_dir():
            raise _fail("QUARANTINE_UNAVAILABLE", "Quarantine directory is unsafe")

        prior, prior_path = self._previous(previous, fmt, store)
        deadline = self._monotonic() + self.timeout_seconds
        attachment = self._attachment(public_pk, detail_pk, deadline)
        reply = self._download(attachment, previous=prior, deadline=deadline)
        if reply.status == 304:
            try:
                if prior is None or prior_path is None:
                    raise _fail(
                        "INVALID_NOT_MODIFIED", "304 response has no verified prior content"
                    )
                etag = _response_header(reply.response, "ETag") or prior.etag
                last_modified = (
                    _response_header(reply.response, "Last-Modified") or prior.last_modified
                )
            finally:
                reply.response.close()
                reply.connection.close()
            byte_count = self._verify_existing(prior_path, prior.sha256, fmt)
            return FetchResult(
                "NOT_MODIFIED",
                public_pk,
                detail_pk,
                attachment.attachment_id,
                attachment.file_detail_sn,
                fmt,
                "application/octet-stream",
                prior.sha256,
                byte_count,
                prior_path,
                etag,
                last_modified,
            )
        try:
            if reply.status != 200:
                raise _fail("DOWNLOAD_HTTP_ERROR", "Official file download failed")
            temp_path, digest, byte_count, media_type, etag, last_modified = self._stream_to_temp(
                reply.response, reply.connection, store, deadline
            )
        finally:
            reply.response.close()
            reply.connection.close()

        try:
            detected = _sniff(temp_path)
            if detected != fmt:
                raise _fail("FORMAT_MISMATCH", "Downloaded file does not match registry format")
            if media_type not in _ALLOWED_MIMES[fmt]:
                raise _fail("MIME_MISMATCH", "Download MIME type does not match file magic")
            final_path = store / f"{digest}{_EXTENSION[fmt]}"
            installed = self._install(temp_path, final_path, digest, fmt)
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        status_value = "DOWNLOADED" if installed else "UNCHANGED"
        return FetchResult(
            status_value,
            public_pk,
            detail_pk,
            attachment.attachment_id,
            attachment.file_detail_sn,
            fmt,
            media_type,
            digest,
            byte_count,
            final_path,
            etag,
            last_modified,
        )


def _decode_notice_html(raw: bytes, content_type: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type not in {"text/html", "application/xhtml+xml"}:
        raise _fail("NOTICE_MIME_MISMATCH", "Notice view did not return HTML")
    charset_match = re.search(
        r"(?:^|;)\s*charset\s*=\s*['\"]?([A-Za-z0-9._-]+)",
        content_type,
        re.IGNORECASE,
    )
    charset = charset_match.group(1).casefold() if charset_match else None
    charset_aliases = {
        "utf-8": "utf-8",
        "utf8": "utf-8",
        "euc-kr": "cp949",
        "euckr": "cp949",
        "cp949": "cp949",
        "ms949": "cp949",
    }
    if charset is not None and charset not in charset_aliases:
        raise _fail("INVALID_NOTICE_ENCODING", "Notice character encoding is unsupported")
    encodings = (charset_aliases[charset],) if charset else ("utf-8", "cp949")
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise _fail("INVALID_NOTICE_ENCODING", "Notice HTML could not be decoded")


class GwangjuNoticeFetcher(MunicipalSourceFetcher):
    """Quarantine one chosen XLSX from the fixed official Gwangju notice board."""

    _official_origin = GWANGJU_ORIGIN
    _official_host = GWANGJU_HOST

    def _notice_metadata(
        self, notice_id: str, *, deadline: float
    ) -> GwangjuNoticeMetadata:
        body = urlencode((("B_IDX", notice_id),)).encode("ascii")
        reply = self._request(
            "POST",
            GWANGJU_ORIGIN + GWANGJU_NOTICE_VIEW_PATH,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "User-Agent": "busro-itda-municipal-quarantine/0.1",
            },
            body=body,
            deadline=deadline,
            allow_download_redirects=True,
        )
        try:
            if reply.status != 200:
                raise _fail("NOTICE_HTTP_ERROR", "Official notice view request failed")
            content_type = _response_header(reply.response, "Content-Type")
            if content_type is None:
                raise _fail("NOTICE_MIME_MISMATCH", "Notice Content-Type is missing")
            raw = self._read_bounded(
                reply.response,
                reply.connection,
                maximum=MAX_NOTICE_HTML_BYTES,
                deadline=deadline,
            )
        finally:
            reply.response.close()
            reply.connection.close()
        return parse_gwangju_notice_html(
            _decode_notice_html(raw, content_type), notice_id=notice_id
        )

    def _notice_download(
        self,
        notice_id: str,
        file_index: str,
        *,
        previous: PreviousFetch | None,
        deadline: float,
    ) -> _Download:
        body = urlencode((("B_IDX", notice_id), ("FILE_IDX", file_index))).encode(
            "ascii"
        )
        headers = {
            "Accept": (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
                "application/octet-stream"
            ),
            "Accept-Encoding": "identity",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "busro-itda-municipal-quarantine/0.1",
        }
        if previous is not None and previous.etag is not None:
            headers["If-None-Match"] = previous.etag
        if previous is not None and previous.last_modified is not None:
            headers["If-Modified-Since"] = previous.last_modified
        return self._request(
            "POST",
            GWANGJU_ORIGIN + GWANGJU_NOTICE_DOWNLOAD_PATH,
            headers=headers,
            body=body,
            deadline=deadline,
            allow_download_redirects=True,
        )

    def fetch(
        self,
        *,
        notice_id: str | int,
        file_index: str | int,
        quarantine_root: Path | str,
        previous: PreviousFetch | None = None,
    ) -> GwangjuFetchResult:
        """Fetch raw XLSX bytes only; never parse or activate timetable content."""
        normalized_notice_id = _bounded_numeric_id(
            notice_id, "B_IDX", _NOTICE_ID_RE, maximum=2_147_483_647
        )
        normalized_file_index = _bounded_numeric_id(
            file_index, "FILE_IDX", _FILE_INDEX_RE, maximum=9999
        )
        root = Path(quarantine_root).expanduser()
        store = root / "gwangju-bus"
        try:
            store.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _fail("QUARANTINE_UNAVAILABLE", "Quarantine directory is unavailable") from exc
        if store.is_symlink() or not store.is_dir():
            raise _fail("QUARANTINE_UNAVAILABLE", "Quarantine directory is unsafe")

        prior, prior_path = self._previous(previous, "XLSX", store)
        deadline = self._monotonic() + self.timeout_seconds
        metadata = self._notice_metadata(normalized_notice_id, deadline=deadline)
        matches = [
            attachment
            for attachment in metadata.attachments
            if attachment.file_index == normalized_file_index
        ]
        if len(matches) != 1:
            raise _fail(
                "ATTACHMENT_UNAVAILABLE",
                "Chosen attachment filename and size are not matched in the notice",
            )
        attachment = matches[0]
        if attachment.byte_count > self.max_bytes:
            raise _fail("RESPONSE_TOO_LARGE", "Notice attachment exceeds the byte limit")

        reply = self._notice_download(
            normalized_notice_id,
            normalized_file_index,
            previous=prior,
            deadline=deadline,
        )
        if reply.status == 304:
            try:
                if prior is None or prior_path is None:
                    raise _fail(
                        "INVALID_NOT_MODIFIED", "304 response has no verified prior content"
                    )
                etag = _response_header(reply.response, "ETag") or prior.etag
                last_modified = (
                    _response_header(reply.response, "Last-Modified")
                    or prior.last_modified
                )
            finally:
                reply.response.close()
                reply.connection.close()
            byte_count = self._verify_existing(prior_path, prior.sha256, "XLSX")
            if byte_count != attachment.byte_count:
                raise _fail(
                    "NOTICE_SIZE_MISMATCH",
                    "Quarantined bytes do not match the current notice size",
                )
            return GwangjuFetchResult(
                "NOT_MODIFIED",
                normalized_notice_id,
                normalized_file_index,
                metadata.title,
                metadata.effective_date,
                metadata.source_url,
                attachment.filename,
                "XLSX",
                "application/octet-stream",
                prior.sha256,
                byte_count,
                prior_path,
                etag,
                last_modified,
            )

        try:
            if reply.status != 200:
                raise _fail("DOWNLOAD_HTTP_ERROR", "Official notice attachment download failed")
            response_length = _content_length(reply.response, self.max_bytes)
            if response_length is not None and response_length != attachment.byte_count:
                raise _fail(
                    "NOTICE_SIZE_MISMATCH",
                    "Download size does not match the official notice",
                )
            temp_path, digest, byte_count, media_type, etag, last_modified = self._stream_to_temp(
                reply.response, reply.connection, store, deadline
            )
        finally:
            reply.response.close()
            reply.connection.close()

        try:
            if byte_count != attachment.byte_count:
                raise _fail(
                    "NOTICE_SIZE_MISMATCH",
                    "Downloaded bytes do not match the official notice size",
                )
            detected = _sniff(temp_path)
            if detected != "XLSX":
                raise _fail("FORMAT_MISMATCH", "Notice attachment is not XLSX")
            if media_type not in _ALLOWED_MIMES["XLSX"]:
                raise _fail("MIME_MISMATCH", "Download MIME type does not match XLSX magic")
            final_path = store / f"{digest}.xlsx"
            installed = self._install(temp_path, final_path, digest, "XLSX")
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise
        return GwangjuFetchResult(
            "DOWNLOADED" if installed else "UNCHANGED",
            normalized_notice_id,
            normalized_file_index,
            metadata.title,
            metadata.effective_date,
            metadata.source_url,
            attachment.filename,
            "XLSX",
            media_type,
            digest,
            byte_count,
            final_path,
            etag,
            last_modified,
        )


__all__ = [
    "ATTACHMENT_METADATA_PATH",
    "DEFAULT_MAX_BYTES",
    "DOWNLOAD_PATH",
    "FetchResult",
    "GWANGJU_HOST",
    "GWANGJU_NOTICE_DOWNLOAD_PATH",
    "GWANGJU_NOTICE_VIEW_PATH",
    "GWANGJU_ORIGIN",
    "GwangjuFetchResult",
    "GwangjuNoticeAttachment",
    "GwangjuNoticeFetcher",
    "GwangjuNoticeMetadata",
    "MAX_NOTICE_HTML_BYTES",
    "MunicipalSourceFetchError",
    "MunicipalSourceFetcher",
    "OFFICIAL_HOST",
    "OFFICIAL_ORIGIN",
    "PreviousFetch",
    "parse_gwangju_notice_html",
]
