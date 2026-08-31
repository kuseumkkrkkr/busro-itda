"""Vercel-safe request dispatcher for the Busro Itda service.

The static nationwide catalog is copied to the function's writable temporary
directory once per warm instance.  Mutable observation data is intentionally
ephemeral until a durable database (for example Supabase Postgres) is linked.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any, Mapping

from app import AppError, BusroService
from config import Settings


SERVICE_DIR = Path(__file__).resolve().parent
PACKAGED_CATALOG = SERVICE_DIR / "data" / "network_catalog.sqlite3"
CATALOG_META = SERVICE_DIR / "data" / "network_catalog.meta.json"
API_PATH_RE = re.compile(r"^/api/[a-z0-9_/-]{1,80}$")
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


_SERVICE: BusroService | None = None
_SERVICE_LOCK = threading.Lock()


def _catalog_manifest() -> dict[str, Any]:
    try:
        manifest = json.loads(CATALOG_META.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("deployment catalog metadata is unavailable") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError("deployment catalog metadata is invalid")
    expected_bytes = manifest.get("bytes")
    if not isinstance(expected_bytes, int) or not 1_000_000 <= expected_bytes <= 150_000_000:
        raise RuntimeError("deployment catalog size metadata is invalid")
    return manifest


def _valid_sqlite(path: Path, expected_bytes: int) -> bool:
    try:
        if path.stat().st_size != expected_bytes:
            return False
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


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
    if not _valid_sqlite(PACKAGED_CATALOG, expected_bytes):
        raise RuntimeError("packaged deployment catalog failed validation")

    target = _runtime_root() / f"network-catalog-{manifest['sha256'][:16]}.sqlite3"
    if _valid_sqlite(target, expected_bytes):
        return target

    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    with PACKAGED_CATALOG.open("rb") as source, temporary.open("xb") as destination:
        shutil.copyfileobj(source, destination, length=1024 * 1024)
        destination.flush()
        os.fsync(destination.fileno())
    if not _valid_sqlite(temporary, expected_bytes):
        temporary.unlink(missing_ok=True)
        raise RuntimeError("runtime deployment catalog copy failed validation")
    os.replace(temporary, target)
    return target


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
        "catalog_storage": "packaged_sqlite_runtime_copy",
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
        return RuntimeResponse(exc.status, exc.payload())
    except Exception:
        return RuntimeResponse(
            500,
            {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
        )
