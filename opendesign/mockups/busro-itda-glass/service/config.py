"""Runtime configuration for the local bus data service."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


SERVICE_DIR = Path(__file__).resolve().parent


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


DEFAULT_DEV_ORIGINS = (
    "http://127.0.0.1:8289",
    "http://localhost:8289",
    "http://127.0.0.1:8290",
    "http://localhost:8290",
    "http://127.0.0.1:8791",
    "http://localhost:8791",
)


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8791
    fixture_mode: bool = False
    tago_service_key: str | None = None
    tago_timeout_seconds: float = 6.0
    cache_ttl_seconds: int = 30
    catalog_cache_ttl_seconds: int = 86_400
    max_body_bytes: int = 65_536
    db_path: Path = SERVICE_DIR / "data" / "busro_itda.sqlite3"
    network_catalog_path: Path = SERVICE_DIR / "data" / "network_catalog.sqlite3"
    fixture_path: Path = SERVICE_DIR / "fixtures" / "tago_arrivals.json"
    position_fixture_path: Path = SERVICE_DIR / "fixtures" / "tago_positions.json"
    catalog_fixture_path: Path = SERVICE_DIR / "fixtures" / "tago_catalog.json"
    fixture_delays_path: Path = SERVICE_DIR / "fixtures" / "delay_samples.json"
    position_gap_seconds: int = 900
    allowed_origins: tuple[str, ...] = DEFAULT_DEV_ORIGINS
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "[::1]")

    @classmethod
    def from_env(cls, *, fixture_override: bool | None = None) -> "Settings":
        fixture_mode = _env_bool("BUSRO_FIXTURE_MODE", False)
        if fixture_override is not None:
            fixture_mode = fixture_override

        timeout = min(max(float(os.getenv("TAGO_TIMEOUT_SECONDS", "6")), 1.0), 15.0)
        cache_ttl = min(max(int(os.getenv("BUSRO_CACHE_TTL_SECONDS", "30")), 1), 300)
        catalog_cache_ttl = min(
            max(int(os.getenv("BUSRO_CATALOG_CACHE_TTL_SECONDS", "86400")), 60), 86_400
        )
        max_body = min(max(int(os.getenv("BUSRO_MAX_BODY_BYTES", "65536")), 1024), 262_144)
        position_gap = min(max(int(os.getenv("BUSRO_POSITION_GAP_SECONDS", "900")), 60), 3600)
        port = int(os.getenv("BUSRO_PORT", "8791"))
        if not 1 <= port <= 65_535:
            raise ValueError("BUSRO_PORT must be between 1 and 65535")

        db_path = Path(os.getenv("BUSRO_DB_PATH", str(cls.db_path))).expanduser().resolve()
        network_catalog_path = Path(
            os.getenv("BUSRO_NETWORK_CATALOG_PATH", str(cls.network_catalog_path))
        ).expanduser().resolve()
        return cls(
            host=os.getenv("BUSRO_HOST", "127.0.0.1"),
            port=port,
            fixture_mode=fixture_mode,
            tago_service_key=os.getenv("TAGO_SERVICE_KEY") or None,
            tago_timeout_seconds=timeout,
            cache_ttl_seconds=cache_ttl,
            catalog_cache_ttl_seconds=catalog_cache_ttl,
            max_body_bytes=max_body,
            db_path=db_path,
            network_catalog_path=network_catalog_path,
            position_fixture_path=Path(
                os.getenv("BUSRO_POSITION_FIXTURE_PATH", str(cls.position_fixture_path))
            ).expanduser().resolve(),
            catalog_fixture_path=Path(
                os.getenv("BUSRO_CATALOG_FIXTURE_PATH", str(cls.catalog_fixture_path))
            ).expanduser().resolve(),
            position_gap_seconds=position_gap,
            allowed_origins=_csv_env("BUSRO_ALLOWED_ORIGINS", DEFAULT_DEV_ORIGINS),
            allowed_hosts=_csv_env("BUSRO_ALLOWED_HOSTS", ("127.0.0.1", "localhost", "[::1]")),
        )
