"""Validated, read-only registry of official municipal bus data sources."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit


SERVICE_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = SERVICE_DIR / "fixtures"
DEFAULT_REGISTRY_PATH = FIXTURE_DIR / "municipal_sources.json"

MAX_FILE_BYTES = 262_144
MAX_SOURCES = 200
MAX_URLS_PER_SOURCE = 8
MAX_QUERY_LENGTH = 100
MAX_RESULT_LIMIT = 100
MAX_OFFSET = 10_000

VALID_ORIGIN_STATUSES = frozenset(
    {
        "VERIFIED_SCHEDULE_ORIGIN",
        "VERIFIED_PRIOR_ONLY",
        "VERIFIED_ROUTE_ONLY",
        "SOURCE_DOWN",
        "DATA_GAP",
    }
)
VALID_STATUSES = VALID_ORIGIN_STATUSES
VALID_INGESTION_STATUSES = frozenset(
    {
        "DISCOVERED_ONLY",
        "STAGED",
        "ACTIVE",
        "STALE",
        "REJECTED",
    }
)
VALID_SCHEDULE_GRANULARITIES = frozenset(
    {
        "NONE",
        "UNKNOWN",
        "TERMINAL_DEPARTURES",
        "STOP_LEVEL_TIMES",
    }
)
VALID_COLLECTION_POLICIES = frozenset(
    {
        "BOUNDED_API",
        "MANUAL_APPLICATION_ONLY",
        "PERMISSION_REQUIRED",
        "DISCOVERY_ONLY",
        "SCREEN_CHECK_ONLY",
    }
)
ALLOWED_URL_HOSTS = frozenset(
    {
        "www.data.go.kr",
        "www.ktdb.go.kr",
        "www.yd21.go.kr",
        "www.gc.go.kr",
        "bus.gimcheon.go.kr",
        "sj.go.kr",
        "bis.yc.go.kr",
        "its.gyeongju.go.kr",
        "livebus.gyeongju.go.kr",
        "ulsan.go.kr",
        "www.ulsan.go.kr",
        "www.ulsanbus.or.kr",
        "bus.gwangju.go.kr",
    }
)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class RegistryError(ValueError):
    """Raised when a registry file or query violates the bounded contract."""


def _bounded_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RegistryError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise RegistryError(f"{name} must be between {minimum} and {maximum}")
    return value


def _safe_string(value: Any, name: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str):
        raise RegistryError(f"{name} must be a string")
    if not value or len(value) > maximum or _CONTROL_RE.search(value):
        raise RegistryError(f"{name} is empty, too long, or contains control characters")
    return value


def _enum_string(value: Any, name: str, allowed: frozenset[str]) -> str:
    normalized = _safe_string(value, name, maximum=80)
    if normalized not in allowed:
        raise RegistryError(f"{name} is invalid")
    return normalized


def _safe_string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise RegistryError(f"{name} must contain 1 to 20 entries")
    normalized = [
        _safe_string(item, f"{name}[{index}]", maximum=100)
        for index, item in enumerate(value)
    ]
    if len(set(normalized)) != len(normalized):
        raise RegistryError(f"{name} contains duplicate entries")
    return normalized


def _walk_strings(value: Any, path: str = "root") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str) or _CONTROL_RE.search(key):
                raise RegistryError(f"{path} contains an invalid key")
            yield from _walk_strings(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _walk_strings(nested, f"{path}[{index}]")


def _validate_url(raw_url: Any) -> str:
    url = _safe_string(raw_url, "url", maximum=2048)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_URL_HOSTS:
        raise RegistryError("url must use https and a verified allowlisted host")
    if parsed.username is not None or parsed.password is not None:
        raise RegistryError("url userinfo is not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RegistryError("url has an invalid port") from exc
    if port not in (None, 443) or parsed.fragment:
        raise RegistryError("url port or fragment is not allowed")
    decoded_path = parsed.path
    for _ in range(3):
        decoded_path = unquote(decoded_path)
    if "\\" in decoded_path or any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise RegistryError("url path traversal is not allowed")
    return url


def _resolve_registry_path(path: Path | str, allowed_root: Path | str) -> Path:
    root = Path(allowed_root).expanduser().resolve()
    resolved = Path(path).expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RegistryError("registry path must stay inside its allowed root") from exc
    if not resolved.is_file():
        raise RegistryError("registry file does not exist")
    return resolved


def _validate_priority_order(raw: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list) or len(raw) != 5:
        raise RegistryError("priority_order must contain exactly five tiers")
    priorities: list[dict[str, Any]] = []
    seen_tiers: set[int] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RegistryError("priority_order entries must be objects")
        tier = _bounded_int(item.get("tier"), "priority tier", minimum=1, maximum=5)
        if tier in seen_tiers:
            raise RegistryError("priority_order contains a duplicate tier")
        seen_tiers.add(tier)
        priorities.append(
            {
                "tier": tier,
                "id": _safe_string(item.get("id"), f"priority_order[{index}].id", maximum=40),
                "label": _safe_string(item.get("label"), f"priority_order[{index}].label", maximum=40),
            }
        )
    if [item["tier"] for item in priorities] != [1, 2, 3, 4, 5]:
        raise RegistryError("priority_order must preserve tiers 1 through 5")
    if priorities[0]["id"] != "TAGO":
        raise RegistryError("TAGO must remain the first source priority")
    return tuple(priorities)


def _validate_source(raw: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RegistryError(f"sources[{index}] must be an object")
    for string_path, string_value in _walk_strings(raw, f"sources[{index}]"):
        if len(string_value) > 2048 or _CONTROL_RE.search(string_value):
            raise RegistryError(f"{string_path} is too long or contains control characters")

    source_id = _safe_string(raw.get("id"), f"sources[{index}].id", maximum=64)
    if not _ID_RE.fullmatch(source_id):
        raise RegistryError("source id must contain only lowercase letters, digits, and hyphens")
    origin_status = _safe_string(
        raw.get("origin_status"), f"sources[{index}].origin_status", maximum=40
    )
    if origin_status not in VALID_ORIGIN_STATUSES:
        raise RegistryError(f"unsupported source origin_status: {origin_status}")
    ingestion_status = _enum_string(
        raw.get("ingestion_status", "DISCOVERED_ONLY"),
        f"sources[{index}].ingestion_status",
        VALID_INGESTION_STATUSES,
    )
    schedule_granularity = _enum_string(
        raw.get("schedule_granularity"),
        f"sources[{index}].schedule_granularity",
        VALID_SCHEDULE_GRANULARITIES,
    )
    collection_policy = _enum_string(
        raw.get("collection_policy"),
        f"sources[{index}].collection_policy",
        VALID_COLLECTION_POLICIES,
    )
    if origin_status in {"VERIFIED_SCHEDULE_ORIGIN", "VERIFIED_PRIOR_ONLY"}:
        if schedule_granularity == "NONE":
            raise RegistryError("schedule origins must declare schedule granularity")

    projection_allowed = raw.get("projection_allowed")
    if projection_allowed is not None and not isinstance(projection_allowed, bool):
        raise RegistryError(f"sources[{index}].projection_allowed must be a boolean")
    allowed_uses: list[str] | None = None
    prohibited_uses: list[str] | None = None
    if origin_status == "VERIFIED_PRIOR_ONLY":
        if projection_allowed is not False:
            raise RegistryError("prior-only sources must set projection_allowed=false")
        allowed_uses = _safe_string_list(
            raw.get("allowed_uses"), f"sources[{index}].allowed_uses"
        )
        prohibited_uses = _safe_string_list(
            raw.get("prohibited_uses"), f"sources[{index}].prohibited_uses"
        )
        if set(allowed_uses) & set(prohibited_uses):
            raise RegistryError("allowed_uses and prohibited_uses must be disjoint")
    if projection_allowed is True and (
        ingestion_status != "ACTIVE"
        or origin_status != "VERIFIED_SCHEDULE_ORIGIN"
    ):
        raise RegistryError(
            "current projection requires a VERIFIED_SCHEDULE_ORIGIN with "
            "ingestion_status=ACTIVE and projection_allowed=true"
        )
    tier = _bounded_int(raw.get("priority_tier"), "priority_tier", minimum=1, maximum=5)

    urls = raw.get("urls")
    if not isinstance(urls, list) or not 1 <= len(urls) <= MAX_URLS_PER_SOURCE:
        raise RegistryError(f"source urls must contain 1 to {MAX_URLS_PER_SOURCE} entries")
    normalized_urls: list[dict[str, str]] = []
    for url_index, url_item in enumerate(urls):
        if not isinstance(url_item, dict):
            raise RegistryError("source url entries must be objects")
        normalized_urls.append(
            {
                "role": _safe_string(
                    url_item.get("role"),
                    f"sources[{index}].urls[{url_index}].role",
                    maximum=50,
                ),
                "url": _validate_url(url_item.get("url")),
                "robots": _safe_string(
                    url_item.get("robots"),
                    f"sources[{index}].urls[{url_index}].robots",
                    maximum=50,
                ),
            }
        )

    normalized = deepcopy(raw)
    normalized.pop("status", None)
    normalized.update(
        {
            "id": source_id,
            "name": _safe_string(raw.get("name"), f"sources[{index}].name", maximum=120),
            "municipality": _safe_string(
                raw.get("municipality"), f"sources[{index}].municipality", maximum=80
            ),
            "priority_tier": tier,
            "origin_status": origin_status,
            "ingestion_status": ingestion_status,
            "schedule_granularity": schedule_granularity,
            "collection_policy": collection_policy,
            "urls": normalized_urls,
            "license": _safe_string(
                raw.get("license"), f"sources[{index}].license", maximum=60
            ),
        }
    )
    if allowed_uses is not None and prohibited_uses is not None:
        normalized.update(
            {
                "projection_allowed": False,
                "allowed_uses": allowed_uses,
                "prohibited_uses": prohibited_uses,
            }
        )
    refresh = raw.get("refresh")
    if not isinstance(refresh, dict):
        raise RegistryError("source refresh must be an object")
    _safe_string(refresh.get("policy"), f"sources[{index}].refresh.policy", maximum=100)
    return normalized


class SourceRegistry:
    """Immutable-in-practice source index with bounded list and search operations."""

    def __init__(self, document: dict[str, Any]) -> None:
        if not isinstance(document, dict) or document.get("schema_version") != 2:
            raise RegistryError("unsupported municipal source registry schema")
        self._priority_order = _validate_priority_order(document.get("priority_order"))
        raw_sources = document.get("sources")
        if not isinstance(raw_sources, list) or not 1 <= len(raw_sources) <= MAX_SOURCES:
            raise RegistryError(f"sources must contain 1 to {MAX_SOURCES} entries")

        sources: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(raw_sources):
            source = _validate_source(raw, index=index)
            if source["id"] in seen_ids:
                raise RegistryError(f"duplicate source id: {source['id']}")
            seen_ids.add(source["id"])
            sources.append(source)
        self._sources = tuple(sorted(sources, key=lambda source: (source["priority_tier"], source["id"])))

    @classmethod
    def from_file(
        cls,
        path: Path | str = DEFAULT_REGISTRY_PATH,
        *,
        allowed_root: Path | str = FIXTURE_DIR,
    ) -> "SourceRegistry":
        resolved = _resolve_registry_path(path, allowed_root)
        size = resolved.stat().st_size
        if size <= 0 or size > MAX_FILE_BYTES:
            raise RegistryError(f"registry file must be between 1 and {MAX_FILE_BYTES} bytes")
        try:
            document = json.loads(resolved.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryError("registry file must be valid UTF-8 JSON") from exc
        return cls(document)

    @property
    def priority_order(self) -> list[dict[str, Any]]:
        return deepcopy(list(self._priority_order))

    def list_sources(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        status: str | None = None,
        origin_status: str | None = None,
        priority_tier: int | None = None,
    ) -> list[dict[str, Any]]:
        bounded_limit = _bounded_int(limit, "limit", minimum=1, maximum=MAX_RESULT_LIMIT)
        bounded_offset = _bounded_int(offset, "offset", minimum=0, maximum=MAX_OFFSET)
        if status is not None and status not in VALID_ORIGIN_STATUSES:
            raise RegistryError("status filter is invalid")
        if origin_status is not None and origin_status not in VALID_ORIGIN_STATUSES:
            raise RegistryError("origin_status filter is invalid")
        if status is not None and origin_status is not None and status != origin_status:
            raise RegistryError("status and origin_status filters conflict")
        selected_origin_status = origin_status or status
        if priority_tier is not None:
            priority_tier = _bounded_int(
                priority_tier, "priority_tier", minimum=1, maximum=5
            )
        matches = (
            source
            for source in self._sources
            if (
                selected_origin_status is None
                or source["origin_status"] == selected_origin_status
            )
            and (priority_tier is None or source["priority_tier"] == priority_tier)
        )
        selected: list[dict[str, Any]] = []
        for index, source in enumerate(matches):
            if index < bounded_offset:
                continue
            if len(selected) >= bounded_limit:
                break
            selected.append(deepcopy(source))
        return selected

    def search(self, query: str, *, limit: int = 25) -> list[dict[str, Any]]:
        normalized_query = _safe_string(query.strip(), "query", maximum=MAX_QUERY_LENGTH).casefold()
        bounded_limit = _bounded_int(limit, "limit", minimum=1, maximum=MAX_RESULT_LIMIT)
        results: list[dict[str, Any]] = []
        for source in self._sources:
            haystack = " ".join(
                (
                    source["id"],
                    source["name"],
                    source["municipality"],
                    source["origin_status"],
                    source["ingestion_status"],
                    source["schedule_granularity"],
                    source["collection_policy"],
                    *(url["role"] for url in source["urls"]),
                )
            ).casefold()
            if normalized_query in haystack:
                results.append(deepcopy(source))
                if len(results) >= bounded_limit:
                    break
        return results


def load_default_registry() -> SourceRegistry:
    return SourceRegistry.from_file()


__all__ = [
    "ALLOWED_URL_HOSTS",
    "DEFAULT_REGISTRY_PATH",
    "MAX_FILE_BYTES",
    "MAX_RESULT_LIMIT",
    "RegistryError",
    "SourceRegistry",
    "VALID_COLLECTION_POLICIES",
    "VALID_INGESTION_STATUSES",
    "VALID_ORIGIN_STATUSES",
    "VALID_SCHEDULE_GRANULARITIES",
    "VALID_STATUSES",
    "load_default_registry",
]
