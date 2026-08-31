"""Vercel-safe request dispatcher for the Busro Itda service.

The static nationwide catalog is copied to the function's writable temporary
directory once per warm instance.  Mutable observation data is intentionally
ephemeral until a durable database (for example Supabase Postgres) is linked.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import threading
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app import AppError, BusroService
from config import Settings


SERVICE_DIR = Path(__file__).resolve().parent
PACKAGED_CATALOG = SERVICE_DIR / "data" / "network_catalog.sqlite3"
CATALOG_META = SERVICE_DIR / "data" / "network_catalog.meta.json"
API_PATH_RE = re.compile(r"^/api/[a-z0-9_/-]{1,80}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SUPABASE_HOST_RE = re.compile(r"^[a-z0-9]{20}\.supabase\.co$")
SUPABASE_PUBLIC_PATH_RE = re.compile(
    r"^/storage/v1/object/public/[A-Za-z0-9._-]{1,63}/[A-Za-z0-9._/-]{1,768}\.tar\.gz$"
)
GITHUB_RELEASE_PATH_RE = re.compile(
    r"^/[A-Za-z0-9][A-Za-z0-9.-]{0,38}/"
    r"[A-Za-z0-9._-]{1,100}/releases/download/"
    r"([A-Za-z0-9][A-Za-z0-9._+-]{0,127})/"
    r"[A-Za-z0-9][A-Za-z0-9._+-]{0,255}\.tar\.gz$"
)
GITHUB_ASSET_PATH_RE = re.compile(
    r"^/github-production-release-asset(?:-[A-Za-z0-9]+)?/"
    r"[0-9]{1,20}/[A-Za-z0-9._-]{16,512}$"
)
GITHUB_ASSET_HOSTS = frozenset(
    {
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
    }
)
CATALOG_ARCHIVE_MEMBER = "network_catalog.sqlite3"
MAX_ARCHIVE_BYTES = 50_000_000
MAX_CATALOG_BYTES = 400 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
ARCHIVE_TIMEOUT_SECONDS = 20
DISABLED_SERVERLESS_MUTATIONS = {
    "/api/collect",
    "/api/positions/collect",
    "/api/network/hydrate",
}


@dataclass(frozen=True, slots=True)
class RuntimeResponse:
    status: int
    payload: dict[str, Any]
    cache_control: str = "private, no-store"
    retry_after_seconds: int | None = None


_SERVICE: BusroService | None = None
_SERVICE_LOCK = threading.Lock()


class _CatalogRedirectHandler(HTTPRedirectHandler):
    """Permit only GitHub Release's single hop to a GitHub asset host."""

    def __init__(self, initial_url: str):
        super().__init__()
        self._initial_url = initial_url
        self._redirect_count = 0

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        if (
            self._redirect_count != 0
            or not _is_github_release_url(self._initial_url)
            or not _is_github_asset_url(new_url)
        ):
            return None
        self._redirect_count += 1
        return super().redirect_request(
            request,
            file_pointer,
            code,
            message,
            headers,
            new_url,
        )


def _catalog_manifest() -> dict[str, Any]:
    try:
        manifest = json.loads(CATALOG_META.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("deployment catalog metadata is unavailable") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("deployment catalog metadata is invalid")
    expected_bytes = manifest.get("uncompressed_bytes", manifest.get("bytes"))
    if not isinstance(expected_bytes, int) or not 1_000_000 <= expected_bytes <= MAX_CATALOG_BYTES:
        raise RuntimeError("deployment catalog size metadata is invalid")
    expected_sha256 = manifest.get("uncompressed_sha256", manifest.get("sha256"))
    if not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(expected_sha256):
        raise RuntimeError("deployment catalog digest metadata is invalid")

    if "bytes" in manifest and manifest["bytes"] != expected_bytes:
        raise RuntimeError("deployment catalog size metadata is inconsistent")
    if "sha256" in manifest and str(manifest["sha256"]).lower() != expected_sha256.lower():
        raise RuntimeError("deployment catalog digest metadata is inconsistent")

    normalized = dict(manifest)
    normalized["bytes"] = expected_bytes
    normalized["sha256"] = expected_sha256.lower()
    archive_sha256 = normalized.get("archive_sha256")
    if archive_sha256 is not None:
        if not isinstance(archive_sha256, str) or not SHA256_RE.fullmatch(archive_sha256):
            raise RuntimeError("deployment catalog archive digest metadata is invalid")
        normalized["archive_sha256"] = archive_sha256.lower()
    archive_bytes = normalized.get("archive_bytes")
    if archive_bytes is not None:
        if (
            isinstance(archive_bytes, bool)
            or not isinstance(archive_bytes, int)
            or not 1 <= archive_bytes <= MAX_ARCHIVE_BYTES
        ):
            raise RuntimeError("deployment catalog archive size metadata is invalid")
    return normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(DOWNLOAD_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sqlite(path: Path, expected_bytes: int, expected_sha256: str) -> bool:
    try:
        if path.stat().st_size != expected_bytes:
            return False
        with path.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                return False
        return _sha256_file(path) == expected_sha256
    except OSError:
        return False


def _archive_url() -> str | None:
    raw = os.getenv("BUSRO_CATALOG_ARCHIVE_URL")
    if raw is None or not raw.strip():
        return None
    return _validated_archive_url(raw.strip())


def _printable_ascii(value: str, *, limit: int) -> bool:
    return len(value) <= limit and all(0x21 <= ord(character) <= 0x7E for character in value)


def _is_github_release_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    match = GITHUB_RELEASE_PATH_RE.fullmatch(parsed.path)
    return bool(
        _printable_ascii(url, limit=2_048)
        and parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "github.com"
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and port is None
        and match is not None
        and match.group(1).lower() != "latest"
    )


def _is_github_asset_url(url: str) -> bool:
    """Validate the signed CDN target without ever surfacing its query string."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        _printable_ascii(url, limit=8_192)
        and parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() in GITHUB_ASSET_HOSTS
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and port is None
        and bool(parsed.query)
        and GITHUB_ASSET_PATH_RE.fullmatch(parsed.path) is not None
    )


def _validated_archive_url(raw_url: str) -> str:
    """Accept Supabase public objects, fixed GitHub releases, or an exact allowlist."""

    if not _printable_ascii(raw_url, limit=2_048):
        raise RuntimeError("catalog archive URL is invalid")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        raise RuntimeError("catalog archive URL is invalid") from None
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or port not in (None, 443)
        or "%" in parsed.path
        or "\\" in parsed.path
        or "//" in parsed.path
        or not parsed.path.endswith(".tar.gz")
    ):
        raise RuntimeError("catalog archive URL is not allowed")
    path_segments = parsed.path.split("/")[1:]
    if not path_segments or any(segment in {"", ".", ".."} for segment in path_segments):
        raise RuntimeError("catalog archive URL is not allowed")

    exact_allowlist = {
        entry.strip()
        for entry in os.getenv("BUSRO_CATALOG_ARCHIVE_ALLOWED_URLS", "").split(",")
        if entry.strip()
    }
    default_supabase_object = (
        port is None
        and SUPABASE_HOST_RE.fullmatch(hostname) is not None
        and SUPABASE_PUBLIC_PATH_RE.fullmatch(parsed.path) is not None
    )
    default_github_release = _is_github_release_url(raw_url)
    if hostname == "github.com" and not default_github_release:
        raise RuntimeError("catalog archive URL is not allowed")
    if not default_supabase_object and not default_github_release and raw_url not in exact_allowlist:
        raise RuntimeError("catalog archive URL is not allowed")
    return raw_url


def _open_archive_response(url: str):
    request = Request(
        url,
        headers={
            "Accept": "application/gzip, application/octet-stream",
            "Accept-Encoding": "identity",
            "User-Agent": "busro-itda-catalog/1",
        },
        method="GET",
    )
    return build_opener(_CatalogRedirectHandler(url)).open(
        request,
        timeout=ARCHIVE_TIMEOUT_SECONDS,
    )


def _content_length(response) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    values = headers.get_all("Content-Length") if hasattr(headers, "get_all") else None
    if values is None:
        value = headers.get("Content-Length") if hasattr(headers, "get") else None
        values = [] if value is None else [value]
    if len(values) > 1:
        raise RuntimeError("catalog archive response length is invalid")
    if not values:
        return None
    raw = str(values[0]).strip()
    if not raw.isascii() or not raw.isdigit():
        raise RuntimeError("catalog archive response length is invalid")
    return int(raw)


def _download_catalog_archive(url: str, destination: Path, manifest: Mapping[str, Any]) -> None:
    archive_digest = hashlib.sha256()
    downloaded = 0
    try:
        with _open_archive_response(url) as response:
            status = getattr(response, "status", None)
            if status is not None and status != 200:
                raise RuntimeError("catalog archive download failed")
            final_url = response.geturl() if hasattr(response, "geturl") else url
            github_asset_redirect = _is_github_release_url(url) and _is_github_asset_url(final_url)
            if final_url != url and not github_asset_redirect:
                raise RuntimeError("catalog archive redirect is not allowed")
            declared_length = _content_length(response)
            if declared_length is not None and declared_length > MAX_ARCHIVE_BYTES:
                raise RuntimeError("catalog archive exceeds the compressed size limit")
            expected_archive_bytes = manifest.get("archive_bytes")
            if expected_archive_bytes is not None and declared_length is not None:
                if declared_length != expected_archive_bytes:
                    raise RuntimeError("catalog archive size validation failed")

            with destination.open("wb") as output:
                while True:
                    chunk = response.read(min(DOWNLOAD_CHUNK_BYTES, MAX_ARCHIVE_BYTES - downloaded + 1))
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_ARCHIVE_BYTES:
                        raise RuntimeError("catalog archive exceeds the compressed size limit")
                    archive_digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
    except RuntimeError:
        raise
    except (HTTPError, URLError, HTTPException, TimeoutError, OSError):
        raise RuntimeError("catalog archive download failed") from None

    if downloaded == 0:
        raise RuntimeError("catalog archive download failed")
    if declared_length is not None and downloaded != declared_length:
        raise RuntimeError("catalog archive size validation failed")
    expected_archive_bytes = manifest.get("archive_bytes")
    if expected_archive_bytes is not None and downloaded != expected_archive_bytes:
        raise RuntimeError("catalog archive size validation failed")
    expected_archive_sha256 = manifest.get("archive_sha256")
    if expected_archive_sha256 is not None and archive_digest.hexdigest() != expected_archive_sha256:
        raise RuntimeError("catalog archive digest validation failed")


def _extract_catalog_archive(archive_path: Path, destination: Path, manifest: Mapping[str, Any]) -> None:
    expected_bytes = int(manifest["bytes"])
    expected_sha256 = str(manifest["sha256"])
    written = 0
    digest = hashlib.sha256()
    try:
        with tarfile.open(archive_path, mode="r|gz") as archive:
            member = archive.next()
            if (
                member is None
                or member.name != CATALOG_ARCHIVE_MEMBER
                or member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}
                or not member.isfile()
                or member.size != expected_bytes
            ):
                raise RuntimeError("catalog archive structure is invalid")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("catalog archive structure is invalid")
            with source, destination.open("wb") as output:
                while True:
                    chunk = source.read(min(DOWNLOAD_CHUNK_BYTES, expected_bytes - written + 1))
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > expected_bytes:
                        raise RuntimeError("catalog archive expanded size is invalid")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if archive.next() is not None:
                raise RuntimeError("catalog archive must contain exactly one member")
    except RuntimeError:
        raise
    except (tarfile.TarError, EOFError, OSError):
        raise RuntimeError("catalog archive extraction failed") from None

    if written != expected_bytes or digest.hexdigest() != expected_sha256:
        raise RuntimeError("catalog archive content validation failed")
    if not _valid_sqlite(destination, expected_bytes, expected_sha256):
        raise RuntimeError("catalog archive SQLite validation failed")


def _runtime_root() -> Path:
    configured = os.getenv("BUSRO_RUNTIME_ROOT")
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        root = Path(tempfile.gettempdir()).resolve() / "busro-itda"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _prepare_catalog_copy() -> Path:
    manifest = _catalog_manifest()
    expected_bytes = int(manifest["bytes"])
    expected_sha256 = str(manifest["sha256"])

    root = _runtime_root()
    target = root / f"network-catalog-{expected_sha256}.sqlite3"
    if _valid_sqlite(target, expected_bytes, expected_sha256):
        return target

    archive_url = _archive_url()
    archive_temporary: Path | None = None
    catalog_temporary: Path | None = None
    try:
        catalog_handle = tempfile.NamedTemporaryFile(
            dir=root,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        )
        catalog_temporary = Path(catalog_handle.name)
        catalog_handle.close()

        if archive_url is not None:
            archive_handle = tempfile.NamedTemporaryFile(
                dir=root,
                prefix=".network-catalog-",
                suffix=".tar.gz.tmp",
                delete=False,
            )
            archive_temporary = Path(archive_handle.name)
            archive_handle.close()
            _download_catalog_archive(archive_url, archive_temporary, manifest)
            _extract_catalog_archive(archive_temporary, catalog_temporary, manifest)
        else:
            if not _valid_sqlite(PACKAGED_CATALOG, expected_bytes, expected_sha256):
                raise RuntimeError("packaged deployment catalog failed validation")
            with PACKAGED_CATALOG.open("rb") as source, catalog_temporary.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=DOWNLOAD_CHUNK_BYTES)
                destination.flush()
                os.fsync(destination.fileno())

        if not _valid_sqlite(catalog_temporary, expected_bytes, expected_sha256):
            raise RuntimeError("runtime deployment catalog copy failed validation")
        os.replace(catalog_temporary, target)
        catalog_temporary = None
        return target
    finally:
        if archive_temporary is not None:
            archive_temporary.unlink(missing_ok=True)
        if catalog_temporary is not None:
            catalog_temporary.unlink(missing_ok=True)


def _settings() -> Settings:
    settings = Settings.from_env()
    if os.getenv("VERCEL") or os.getenv("BUSRO_RUNTIME_COPY_CATALOG") == "1":
        root = _runtime_root()
        settings = replace(
            settings,
            db_path=root / "observations.sqlite3",
            network_catalog_path=_prepare_catalog_copy(),
            operator_token=None,
        )
    return settings


def get_service() -> BusroService:
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = BusroService(_settings())
    return _SERVICE


def reset_runtime_for_tests() -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = None


def _status(service: BusroService) -> dict[str, Any]:
    payload = service.status()
    capabilities = payload.setdefault("capabilities", {})
    capabilities.update(
        {
            "snapshot_collection": False,
            "position_snapshot_collection": False,
            "verified_route_hydration": False,
            "historical_archive": "serverless_ephemeral_until_supabase_linked",
            "serverless_persistent_storage": False,
        }
    )
    payload["deployment"] = {
        "platform": "vercel",
        "runtime": "python_serverless",
        "catalog_storage": (
            "immutable_archive_runtime_extract"
            if os.getenv("BUSRO_CATALOG_ARCHIVE_URL")
            else "packaged_sqlite_runtime_copy"
        ),
        "observation_storage": "ephemeral",
        "secret_scope": "server_environment_only",
    }
    return payload


def _cache_control(path: str) -> str:
    if path in {"/api/network/status", "/api/network/cities", "/api/network/stops", "/api/network/routes", "/api/sources"}:
        return "public, s-maxage=300, stale-while-revalidate=3600"
    if path in {"/api/cities", "/api/routes", "/api/routes/info", "/api/routes/stops", "/api/stops", "/api/stops/nearby", "/api/stops/routes"}:
        return "public, s-maxage=60, stale-while-revalidate=300"
    if path in {"/api/arrivals", "/api/positions"}:
        return "public, s-maxage=10, stale-while-revalidate=20"
    return "private, no-store"


def dispatch_request(
    method: str,
    path: str,
    query: Mapping[str, str] | None = None,
    body: Mapping[str, Any] | None = None,
) -> RuntimeResponse:
    method = str(method or "").upper()
    path = str(path or "")
    if method not in {"GET", "POST"} or not API_PATH_RE.fullmatch(path):
        return RuntimeResponse(404, {"ok": False, "error": {"code": "NOT_FOUND", "message": "API endpoint not found"}})
    if method == "POST" and path in DISABLED_SERVERLESS_MUTATIONS:
        return RuntimeResponse(
            503,
            {
                "ok": False,
                "error": {
                    "code": "PERSISTENT_STORAGE_REQUIRED",
                    "message": "This operation requires durable storage and is disabled on the serverless deployment",
                },
            },
        )

    service = get_service()
    query_data = dict(query or {})
    body_data = dict(body or {})
    try:
        if method == "GET":
            handlers = {
                "/api/status": lambda: _status(service),
                "/api/arrivals": lambda: service.arrivals(query_data),
                "/api/history": lambda: service.history(query_data),
                "/api/positions": lambda: service.positions(query_data),
                "/api/cities": lambda: service.cities(query_data),
                "/api/routes": lambda: service.routes(query_data),
                "/api/routes/info": lambda: service.route_info(query_data),
                "/api/routes/stops": lambda: service.route_stops(query_data),
                "/api/stops": lambda: service.stops(query_data),
                "/api/stops/nearby": lambda: service.nearby_stops(query_data),
                "/api/stops/routes": lambda: service.stop_routes(query_data),
                "/api/network/status": lambda: service.network_status(query_data),
                "/api/network/cities": lambda: service.network_cities(query_data),
                "/api/network/stops": lambda: service.network_stops(query_data),
                "/api/network/routes": lambda: service.network_routes(query_data),
                "/api/sources": lambda: service.sources(query_data),
                "/api/passages": lambda: service.passage_history(query_data),
            }
        else:
            handlers = {
                "/api/simulate": lambda: service.simulate(body_data),
                "/api/replay": lambda: service.replay(body_data),
                "/api/mappings/validate": lambda: service.validate_mapping(body_data),
                "/api/journeys/generate": lambda: service.generate_journeys(body_data),
                "/api/osm/geometry": lambda: service.route_geometry(body_data),
            }
        operation = handlers.get(path)
        if operation is None:
            raise AppError("NOT_FOUND", "API endpoint not found", status=404)
        return RuntimeResponse(200, operation(), _cache_control(path))
    except AppError as exc:
        retry_after = None
        if exc.status in {429, 503} and isinstance(exc.details, dict):
            try:
                retry_after = min(
                    86_400,
                    max(1, int(exc.details.get("retry_after_seconds", 1))),
                )
            except (TypeError, ValueError):
                retry_after = 1
        return RuntimeResponse(
            exc.status,
            exc.payload(),
            retry_after_seconds=retry_after,
        )
    except Exception:
        return RuntimeResponse(
            500,
            {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
        )
